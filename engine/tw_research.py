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

from . import secret_refs, storage, tw_evidence
from .data_providers import QuoteSummary
from .dexter_job import run_dexter_job
from .live_debate import live_debate
from .llm_providers import ProviderConfig
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


async def tw_research(
    *, code: str, trade_date: str, config: ProviderConfig, report_language: str, reservation_id: Optional[str] = None,
    run_dexter: bool = True,
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
    model, env, unsupported = dexter_model_and_env(config)
    if not run_dexter or model is None:
        dexter = {"schema": "her30.dexter.result/1", "status": "unsupported", "reason": unsupported or "disabled", "findings": [],
                  "unknowns": [], "tool_calls": [], "usage": {"measured": False}}
    else:
        question = ("請針對最新價量與營收趨勢，列出有證據支持的重點與反證。" if lang == "zh-TW"
                    else "List the evidence-backed points on the latest price action and revenue trend, plus counter-evidence.")
        dexter = await asyncio.to_thread(
            run_dexter_job, evidence_pack_path=path, question=question, model=model,
            jobs_root=out_dir.parent / "dexter-jobs", dexter_root=DEXTER_ROOT, provider_env=env, market="TW", report_language=lang,
        )
        dexter["unknowns"] = [secret_refs.redact(str(u)) for u in dexter.get("unknowns", [])]
    yield {"type": "dexter.result", **{k: dexter.get(k) for k in ("status", "reason", "findings", "unknowns", "answer_markdown", "tool_calls", "usage", "elapsed_ms", "job_id")}}

    # --- Debate grounded on the pack ----------------------------------------------------
    ctx = evidence_context(pack, dexter, lang)
    async for ev in live_debate(
        ticker=ticker, trade_date=trade_date, summary=summary, headlines=None, config=config,
        reservation_id=reservation_id, report_language=lang, extra_context=ctx,
    ):
        if ev["type"] == "session.start":
            continue
        if ev["type"] == "session.complete":
            ev = {**ev, "market": "TW", "evidence": {"snapshot_id": pack["snapshot_id"], "path_name": path.name},
                  "dexter": {"status": dexter.get("status"), "reason": dexter.get("reason")}}
        yield ev
