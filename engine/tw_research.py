"""Taiwan research flow (S3 + S5 wiring).

For a Taiwan code the US-oriented tools of the full graph cannot be used, and nothing
may silently fall back to US data. This flow instead:

 1. builds the Taiwan evidence pack (FinMind + official TWSE/TPEx cross-check),
 2. runs a sandboxed Dexter job over ONLY that pack (read_file; see integrations/dexter-runner),
 3. runs the multi-agent debate (free engine) grounded on the same pack and on the
    VERIFIED Dexter findings, so the report can cite evidence items,
 4. always reports what is missing (`unknowns`) and whether Dexter completed.

Yields the same event shapes the UI already renders, plus `evidence.pack` and `dexter.result`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import cost_guard, secret_refs, storage, tw_evidence
from .data_providers import QuoteSummary
from .dexter_job import run_dexter_job
from .live_debate import live_debate
from .dexter_job import HARD_LIMITS
from .llm_providers import ProviderConfig, estimate_cost_or_none, is_priced, worst_case_job_cost
from .report_language import normalize_report_language

DEXTER_ROOT = Path(os.environ.get("TAL_DEXTER_ROOT", Path.home() / ".local" / "share" / "her30" / "dexter"))


def dexter_model_and_env(config: ProviderConfig) -> tuple[Optional[dict[str, str]], dict[str, str], Optional[str]]:
    """Map the Lab Pro provider onto what the Dexter runner can drive.

    Returns (model, env, unsupported_reason). Keys go into the CHILD's environment only.
    """
    p, a = config.provider, config.auth
    if a.get("type") == "oauth":
        return None, {}, "provider_not_supported: Dexter has no ChatGPT-OAuth support"
    if p == "anthropic" and a.get("api_key"):
        return {"provider": "anthropic", "name": config.model, "route": "cloud"}, {"ANTHROPIC_API_KEY": a["api_key"]}, None
    if p == "openai" and a.get("api_key"):
        return {"provider": "openai", "name": config.model, "route": "cloud"}, {"OPENAI_API_KEY": a["api_key"]}, None
    if p == "local":
        env = {"OPENAI_BASE_URL": a["base_url"], "OPENAI_API_KEY": a.get("api_key") or "local"}
        return {"provider": "openai", "name": config.model, "route": "local"}, env, None
    return None, {}, f"provider_not_supported: {p}"


def _summary(pack: dict, trade_date: str) -> Optional[QuoteSummary]:
    prices = sorted((i for i in pack["items"] if i["kind"] == "price"), key=lambda i: i["period"])
    if not prices:
        return None
    first, last = prices[0]["values"], prices[-1]["values"]
    closes = [p["values"]["close"] for p in prices if p["values"].get("close") is not None]
    vols = [p["values"].get("volume_shares") or 0 for p in prices]
    if not closes or first.get("close") in (None, 0):
        return None
    return QuoteSummary(
        ticker=pack["security_id"].split(":", 1)[1], trade_date=trade_date, as_of=prices[-1]["period"],
        last_close=float(last["close"]), period_open=float(first.get("open") or first["close"]),
        period_high=max(float(p["values"]["high"]) for p in prices if p["values"].get("high") is not None),
        period_low=min(float(p["values"]["low"]) for p in prices if p["values"].get("low") is not None),
        period_change_pct=(float(last["close"]) - float(first["close"])) / float(first["close"]) * 100,
        avg_volume=sum(vols) / max(len(vols), 1), sessions=len(prices), source="FinMind (raw, unadjusted) + TWSE/TPEx check",
        asset_class="equity",
    )


def evidence_context(pack: dict, dexter: dict, report_language: str) -> str:
    """Text appended to every debate agent's context. Facts only; each line is citable."""
    zh = report_language == "zh-TW"
    lines = [("證據包（只可使用以下事實，引用時寫 [種類 期間]；缺的就說未知）" if zh else
              "Evidence pack (use ONLY these facts; cite as [kind period]; say unknown otherwise):"),
             f"snapshot {pack['snapshot_id'][:12]} · as_of {pack['as_of']}"]
    for kind in ("price", "revenue", "fin_is_bs_cf", "chips"):
        rows = [i for i in pack["items"] if i["kind"] == kind][-6:]
        for it in rows:
            vals = ", ".join(f"{k}={v}" for k, v in it["values"].items())
            lines.append(f"- [{kind} {it['period']}] {vals}")
    cc = [i for i in pack["items"] if i["kind"] == "price_crosscheck"]
    if cc:
        lines.append(f"- 官方交叉核對 {cc[0]['source'].split(':')[0]} {cc[0]['period']}: all_match={cc[0].get('all_match')}")
    lines.append(("已知缺口：" if zh else "Known gaps: ") + " | ".join(pack["unknowns"]))
    ok = [f for f in dexter.get("findings", []) if f.get("verification") == "verified"]
    if dexter.get("status") == "succeeded" and ok:
        lines.append("Dexter 已驗證發現：" if zh else "Dexter verified findings:")
        for f in ok:
            cites = ", ".join(f"[{c['kind']} {c['period']}]" for c in f["citations"])
            lines.append(f"- ({f['id']}) {f['claim']} {cites}")
    else:
        lines.append((f"Dexter 未完成（{dexter.get('status')}：{dexter.get('reason')}）；以下分析不含 Dexter 發現。" if zh
                      else f"Dexter did not complete ({dexter.get('status')}: {dexter.get('reason')}); the analysis below has no Dexter findings."))
    return "\n".join(lines)


async def dexter_stage(*, pack: dict, path: Path, out_dir: Path, config: ProviderConfig, lang: str, run_dexter: bool = True) -> dict[str, Any]:
    """Run (or visibly skip) the sandboxed Dexter job over a built evidence pack. Shared by the simplified flow and the
    Pro full graph. Never silent: `event` always says whether Dexter ran, why not, and what it used."""
    model, env, unsupported = dexter_model_and_env(config)
    billable = config.auth_kind not in ("oauth", "local")
    worst = worst_case_job_cost(config.model, HARD_LIMITS["max_total_tokens"]) if billable else 0.0
    blocked = cost_guard.check_extra_usd(est_cost_usd=worst or 0.0, auth_kind=config.auth_kind) if model is not None else None
    if model is not None and billable and not is_priced(config.model):
        blocked = "unknown_price"          # never start an extra paid job whose cost cannot be bounded
    if not run_dexter or model is None or blocked:
        reason = unsupported or ("cost_guard_blocked:" + blocked if blocked else "disabled")
        dexter = {"schema": "her30.dexter.result/1", "status": "unsupported", "reason": reason, "findings": [],
                  "unknowns": [], "tool_calls": [], "usage": {"measured": False}}
    else:
        question = ("請針對最新價量與營收趨勢，列出有證據支持的重點與反證。" if lang == "zh-TW"
                    else "List the evidence-backed points on the latest price action and revenue trend, plus counter-evidence.")
        dexter = await asyncio.to_thread(
            run_dexter_job, evidence_pack_path=path, question=question, model=model,
            jobs_root=out_dir.parent / "dexter-jobs", dexter_root=DEXTER_ROOT, provider_env=env, market="TW", report_language=lang,
        )
        dexter["unknowns"] = [secret_refs.redact(str(u)) for u in dexter.get("unknowns", [])]
    dexter_ran = dexter.get("status") != "unsupported"
    du = dexter.get("usage") or {}
    # Cost of the Dexter job: measured tokens x price; never 0 for "unknown". Unmeasured usage of a
    # job that ran is billed at the job's hard ceiling (conservative) when the model has a price.
    d_in, d_out = int(du.get("input_tokens") or 0), int(du.get("output_tokens") or 0)
    if not dexter_ran or not billable:
        d_cost: Optional[float] = 0.0
    elif du.get("measured"):
        d_cost = estimate_cost_or_none(config.model, d_in, d_out)
    else:
        d_cost = worst
    usage = {"input_tokens": d_in, "output_tokens": d_out, "measured": bool(du.get("measured")), "est_cost_usd": d_cost, "ran": dexter_ran}
    event = {"type": "dexter.result", "ran": dexter_ran,
             **{k: dexter.get(k) for k in ("status", "reason", "findings", "unknowns", "answer_markdown", "tool_calls", "usage", "elapsed_ms", "job_id")}}
    return {"dexter": dexter, "ran": dexter_ran, "raw_usage": du, "billable": billable, "worst": worst,
            "d_in": d_in, "d_out": d_out, "d_cost": d_cost, "usage": usage, "event": event}


async def tw_research(
    *, code: str, trade_date: str, config: ProviderConfig, report_language: str, reservation_id: Optional[str] = None,
    run_dexter: bool = True, report_sink: Optional[Any] = None,
) -> AsyncIterator[dict[str, Any]]:
    lang = normalize_report_language(report_language)
    ticker = code
    yield {"type": "session.start", "ticker": ticker, "trade_date": trade_date, "report_language": lang, "market": "TW"}
    as_of = f"{trade_date}T16:30:00+08:00"
    try:
        pack = await asyncio.to_thread(tw_evidence.build_tw_evidence, code, as_of)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "run.error", "error": secret_refs.redact(f"evidence pack failed: {type(exc).__name__}: {exc}")}
        return
    out_dir = Path(os.environ.get("TAL_EVIDENCE_DIR") or storage._default_db_path().parent / "evidence")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{pack['security_id'].replace(':', '_')}-{pack['snapshot_id'][:16]}.json"
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    yield {"type": "evidence.pack", "security_id": pack["security_id"], "snapshot_id": pack["snapshot_id"],
           "coverage": pack["coverage"], "unknowns": pack["unknowns"], "as_of": pack["as_of"]}
    summary = _summary(pack, trade_date)
    if summary is None:
        yield {"type": "run.error", "error": "no price rows for this code in the requested window (not a listed security, or FinMind is unavailable); nothing is invented"}
        return
    yield {"type": "data.summary", "ticker": summary.ticker, "trade_date": summary.trade_date, "as_of": summary.as_of,
           "last_close": summary.last_close, "period_open": summary.period_open, "period_high": summary.period_high,
           "period_low": summary.period_low, "period_change_pct": summary.period_change_pct, "avg_volume": summary.avg_volume,
           "sessions": summary.sessions, "source": summary.source, "asset_class": "equity"}

    # --- Dexter (read_file over the pack only) -----------------------------------------
    st = await dexter_stage(pack=pack, path=path, out_dir=out_dir, config=config, lang=lang, run_dexter=run_dexter)
    dexter, dexter_ran, du, billable, worst = st["dexter"], st["ran"], st["raw_usage"], st["billable"], st["worst"]
    d_in, d_out, d_cost, dexter_usage = st["d_in"], st["d_out"], st["d_cost"], st["usage"]
    yield st["event"]

    # --- Debate grounded on the pack ----------------------------------------------------
    ctx = evidence_context(pack, dexter, lang)
    def _sink(rep: Optional[dict[str, Any]]) -> None:
        if report_sink is not None:
            report_sink(_report_with_dexter(rep, config, dexter_ran, du))

    _sink(None)                                  # Dexter's own usage is visible even if the run ends before the debate starts
    async for ev in live_debate(
        ticker=ticker, trade_date=trade_date, summary=summary, headlines=None, config=config,
        reservation_id=reservation_id, report_language=lang, extra_context=ctx, report_sink=_sink,
    ):
        if ev["type"] == "session.start":
            continue
        if ev["type"] == "session.complete":
            debate_cost = ev.get("estimated_cost_usd")
            cost_known = d_cost is not None and debate_cost is not None
            ev = {**ev, "market": "TW", "engine": "simplified",
                  "evidence": {"snapshot_id": pack["snapshot_id"], "path_name": path.name},
                  "dexter": {"status": dexter.get("status"), "reason": dexter.get("reason")},
                  "usage_breakdown": {"debate": {"input_tokens": ev.get("input_tokens"), "output_tokens": ev.get("output_tokens"),
                                                 "est_cost_usd": debate_cost},
                                      "dexter": dexter_usage},
                  # Totals include the Dexter job. Unmeasured Dexter usage stays flagged, never silently 0.
                  "input_tokens": (ev.get("input_tokens") or 0) + d_in,
                  "output_tokens": (ev.get("output_tokens") or 0) + d_out,
                  # null (unknown) when any billable component has no verified price; never a fake 0.0.
                  "estimated_cost_usd": round(debate_cost + d_cost, 4) if (debate_cost is not None and d_cost is not None) else None,
                  "cost_known": debate_cost is not None and d_cost is not None,
                  "cost_complete": bool(cost_known and (not billable or is_priced(config.model)) and (du.get("measured") or not dexter_ran or not billable)),
                  "usage_complete": bool(du.get("measured") or not dexter_ran),
                  "cost_report": _report_with_dexter(ev.get("cost_report"), config, dexter_ran, du)}
        yield ev


async def full_graph_dexter(*, code: str, trade_date: str, config: ProviderConfig, lang: str, enabled: bool = True) -> dict[str, Any]:
    """Dexter pre-step of the Taiwan Pro full graph: build the evidence pack for the SAME trade date, run Dexter over it
    (or visibly skip), and return a tool-visible summary plus the event/usage the server folds into the run.
    Never raises and never silently omits Dexter: the returned `event` always states ran / not-ran and why."""
    as_of = f"{trade_date}T16:30:00+08:00"
    out_dir = Path(os.environ.get("TAL_EVIDENCE_DIR") or storage._default_db_path().parent / "evidence")
    try:
        pack = await asyncio.to_thread(tw_evidence.build_tw_evidence, code, as_of)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{pack['security_id'].replace(':', '_')}-{pack['snapshot_id'][:16]}.json"
        path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        st = await dexter_stage(pack=pack, path=path, out_dir=out_dir, config=config, lang=lang, run_dexter=enabled)
    except Exception as exc:  # noqa: BLE001 — the graph still runs; Dexter's absence is stated, not hidden
        reason = secret_refs.redact(f"{type(exc).__name__}: {exc}")
        ev = {"type": "dexter.result", "ran": False, "status": "unsupported", "reason": f"stage_error: {reason}", "findings": [], "unknowns": []}
        return {"event": ev, "block": {"ran": False, "status": "unsupported", "reason": ev["reason"], "text": "", "verified": 0},
                "d_in": 0, "d_out": 0, "d_cost": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0, "measured": False, "est_cost_usd": 0.0, "ran": False},
                "ran": False, "measured": False, "billable": False, "raw_usage": {}}
    dexter = st["dexter"]
    text = evidence_context(pack, dexter, lang) if st["ran"] else ""
    verified = len([f for f in dexter.get("findings", []) if f.get("verification") == "verified"])
    st["event"] = {**st["event"], "snapshot_id": pack["snapshot_id"]}
    block = {"ran": st["ran"], "status": dexter.get("status"), "reason": dexter.get("reason"), "text": text if st["ran"] else "", "verified": verified}
    return {"event": st["event"], "block": block, "d_in": st["d_in"], "d_out": st["d_out"], "d_cost": st["d_cost"], "usage": st["usage"],
            "ran": st["ran"], "measured": bool(st["raw_usage"].get("measured")), "billable": st["billable"],
            "raw_usage": st["raw_usage"]}


def _report_with_dexter(report: Optional[dict[str, Any]], config: ProviderConfig, ran: bool, raw_usage: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Official-price report with the Dexter job added as its own line(s); never raises (the report is secondary)."""
    try:
        from .cost_report import add_dexter

        return add_dexter(report, billing=config.auth_kind or "api_key", provider=config.provider, model=config.model,
                          ran=ran, raw_usage=raw_usage)
    except Exception:  # noqa: BLE001
        return report


def fold_dexter_into_complete(ev: dict[str, Any], res: Optional[dict[str, Any]], config: ProviderConfig) -> dict[str, Any]:
    """Add Dexter's measured usage/cost to a full-graph `session.complete`; unknown stays unknown (never a fake 0)."""
    if res is None:
        return {**ev, "dexter": {"ran": False, "status": "not_requested", "reason": "disabled"}}
    d_cost = res["d_cost"]
    debate_cost = ev.get("estimated_cost_usd")
    known = debate_cost is not None and d_cost is not None
    blk = res["block"]
    return {
        **ev,
        "dexter": {"ran": blk["ran"], "status": blk["status"], "reason": blk["reason"], "verified": blk["verified"]},
        "usage_breakdown": {"debate": {"input_tokens": ev.get("input_tokens"), "output_tokens": ev.get("output_tokens"), "est_cost_usd": debate_cost},
                            "dexter": res["usage"]},
        "input_tokens": (ev.get("input_tokens") or 0) + res["d_in"],
        "output_tokens": (ev.get("output_tokens") or 0) + res["d_out"],
        "estimated_cost_usd": round(debate_cost + d_cost, 4) if known else None,
        "cost_known": bool(known and ev.get("cost_known", True)),
        "usage_complete": bool(res["measured"] or not res["ran"]),
        "cost_report": _report_with_dexter(ev.get("cost_report"), config, res.get("ran", False), res.get("raw_usage")),
    }
