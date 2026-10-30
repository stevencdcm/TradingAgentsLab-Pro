"""Per-run usage ledger and the OFFICIAL-API cost report.

Two different questions are answered by two different pieces of code and must never be mixed:

  * "How much may this run spend?"  -> CostGuard (`cost_guard.py`, `llm_providers._COST_PER_M_TOKENS`): conservative CEILINGS
    that reserve budget. Untouched by this module.
  * "What would OpenAI's published API price say this run used?" -> this module: measured input / cached / cache-write /
    output tokens x the official table in `official_pricing`. An ESTIMATE from a price list, not an invoice.

Billing kinds:
  api_key  OpenAI API key: `api_estimate_usd` is the official-price estimate of what the account was charged.
  oauth    ChatGPT subscription: extra API billing does not apply. If the model has an exact official API row we also list an
           "API-equivalent reference" - what the same tokens would cost on the API - never an invoice and never a % of a plan.
  local    On-device model: external API fee is 0; electricity and hardware are NOT priced and not claimed to be 0.
  other    Vendors whose official prices were not imported: tokens are listed, USD is unknown.

A figure is only called `complete` when every line was priced from reported usage. A missing field is reported as missing
(`missing`), never as 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import official_pricing as op

SCHEMA = "tal.cost-report/1"


@dataclass
class _Line:
    stage: str
    agent: Optional[str]
    provider: Optional[str]
    model_requested: Optional[str]
    model_billed: Optional[str]
    service_tier: Optional[str]
    context: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # calls whose usage did not report the field -> the line is partial for that field
    cached_unreported_calls: int = 0
    cache_write_unreported_calls: int = 0
    reasoning_unreported_calls: int = 0
    tier_unreported_calls: int = 0
    unmeasured: bool = False                     # the stage ran but produced no usage numbers at all


def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


@dataclass
class UsageLedger:
    """Accumulates measured usage per (stage, agent, model, service tier, context bucket)."""

    provider: Optional[str] = None
    _lines: dict[tuple, _Line] = field(default_factory=dict)

    def record(
        self,
        *,
        stage: str = "debate",
        agent: Optional[str] = None,
        provider: Optional[str] = None,
        model_requested: Optional[str] = None,
        model_billed: Optional[str] = None,
        input_tokens: Any = 0,
        output_tokens: Any = 0,
        cached_input_tokens: Any = None,
        cache_write_tokens: Any = None,
        reasoning_tokens: Any = None,
        service_tier: Optional[str] = None,
        tier_reported: bool = False,
    ) -> None:
        """One finished model call. None for cached/cache-write/reasoning means the provider did not report it."""
        i_in, i_out = _int(input_tokens) or 0, _int(output_tokens) or 0
        ctx = op.context_bucket(i_in)
        key = (stage, agent, provider or self.provider, model_requested, model_billed, service_tier, ctx)
        ln = self._lines.get(key)
        if ln is None:
            ln = self._lines[key] = _Line(stage, agent, provider or self.provider, model_requested, model_billed, service_tier, ctx)
        ln.calls += 1
        ln.input_tokens += i_in
        ln.output_tokens += i_out
        for attr, unrep, val in (("cached_input_tokens", "cached_unreported_calls", cached_input_tokens),
                                 ("cache_write_tokens", "cache_write_unreported_calls", cache_write_tokens),
                                 ("reasoning_tokens", "reasoning_unreported_calls", reasoning_tokens)):
            v = _int(val)
            if v is None:
                setattr(ln, unrep, getattr(ln, unrep) + 1)
            else:
                setattr(ln, attr, getattr(ln, attr) + v)
        if not tier_reported:
            ln.tier_unreported_calls += 1

    def record_unmeasured(self, *, stage: str, agent: Optional[str], provider: Optional[str], model: Optional[str]) -> None:
        """A stage that ran but whose usage could not be measured (e.g. Dexter without provider-reported usage)."""
        key = (stage, agent, provider, model, model, None, "short")
        ln = self._lines.setdefault(key, _Line(stage, agent, provider, model, model, None, "short"))
        ln.unmeasured = True

    def __bool__(self) -> bool:
        return bool(self._lines)

    def lines(self) -> list[_Line]:
        return list(self._lines.values())

    def totals(self) -> tuple[int, int]:
        ls = self._lines.values()
        return sum(x.input_tokens for x in ls), sum(x.output_tokens for x in ls)


def usage_from_langchain(response: Any) -> tuple[int, int, dict[str, Any]]:
    """(input, output, detail) from a LangChain LLMResult. detail values are None when the provider did not report them.

    `usage_metadata` is preferred: input_token_details.cache_read / cache_creation and output_token_details.reasoning,
    plus response_metadata.model_name / service_tier. Falls back to llm_output token_usage with NO details (all None).
    """
    detail: dict[str, Any] = {"cached": None, "cache_write": None, "reasoning": None, "model": None, "service_tier": None}
    in_tok = out_tok = 0
    seen = False
    try:
        for gen_list in response.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                um = getattr(msg, "usage_metadata", None) or {}
                if not um:
                    continue
                seen = True
                in_tok += _int(um.get("input_tokens")) or 0
                out_tok += _int(um.get("output_tokens")) or 0
                itd, otd = um.get("input_token_details"), um.get("output_token_details")
                if isinstance(itd, dict):
                    if "cache_read" in itd:
                        detail["cached"] = (detail["cached"] or 0) + (_int(itd.get("cache_read")) or 0)
                    if "cache_creation" in itd:
                        detail["cache_write"] = (detail["cache_write"] or 0) + (_int(itd.get("cache_creation")) or 0)
                if isinstance(otd, dict) and "reasoning" in otd:
                    detail["reasoning"] = (detail["reasoning"] or 0) + (_int(otd.get("reasoning")) or 0)
                rm = getattr(msg, "response_metadata", None) or {}
                detail["model"] = detail["model"] or rm.get("model_name") or rm.get("model")
                detail["service_tier"] = detail["service_tier"] or rm.get("service_tier")
    except Exception:  # noqa: BLE001 - metering must never break a run
        pass
    if seen:
        return in_tok, out_tok, detail
    try:
        lo = getattr(response, "llm_output", None) or {}
        usage = lo.get("token_usage") or lo.get("usage") or {}
        in_tok = _int(usage.get("input_tokens") or usage.get("prompt_tokens")) or 0
        out_tok = _int(usage.get("output_tokens") or usage.get("completion_tokens")) or 0
        detail["model"] = lo.get("model_name") or lo.get("model")
    except Exception:  # noqa: BLE001
        pass
    return in_tok, out_tok, detail


def _line_report(ln: _Line, billing: str) -> dict[str, Any]:
    missing: list[str] = []
    if ln.unmeasured:
        missing.append("usage_not_measured")
    if ln.cached_unreported_calls:
        missing.append("cached_input_tokens")
    if ln.reasoning_unreported_calls:
        missing.append("reasoning_tokens")
    price_model = ln.model_billed or ln.model_requested
    out: dict[str, Any] = {
        "stage": ln.stage, "agent": ln.agent, "provider": ln.provider,
        "model": price_model, "model_requested": ln.model_requested, "calls": ln.calls,
        "input_tokens": ln.input_tokens, "cached_input_tokens": ln.cached_input_tokens if not ln.cached_unreported_calls else None,
        "uncached_input_tokens": max(0, ln.input_tokens - ln.cached_input_tokens - ln.cache_write_tokens) if not ln.cached_unreported_calls else None,
        "cache_write_tokens": ln.cache_write_tokens, "output_tokens": ln.output_tokens,
        "reasoning_tokens": ln.reasoning_tokens if not ln.reasoning_unreported_calls else None,
        "context": ln.context, "usd": None, "usd_basis": None, "price": None, "unpriced_reason": None, "missing": missing,
    }
    if ln.unmeasured:
        out["unpriced_reason"] = "usage_not_measured"
        return out
    if ln.reasoning_tokens > ln.output_tokens:
        out.setdefault("notes", []).append("reasoning_exceeds_output_not_added")
    if billing == "local":
        out.update(usd=0.0, usd_basis="local_no_external_api_fee")
        return out
    if ln.provider != "openai":
        out["unpriced_reason"] = "official_price_not_imported_for_provider"
        return out
    priced = op.price_usage(
        model=price_model, service_tier=ln.service_tier, context=ln.context, input_tokens=ln.input_tokens,
        output_tokens=ln.output_tokens, cached_input_tokens=ln.cached_input_tokens, cache_write_tokens=ln.cache_write_tokens,
    )
    if priced["usd"] is None:
        out["unpriced_reason"] = priced["reason"]
        return out
    if ln.tier_unreported_calls and ln.service_tier is None:
        out.setdefault("notes", []).append("service_tier_not_reported_assumed_standard")
    out["price"] = {"model_id": priced["price_model_id"], "resolved_via": priced["resolved_via"], "tier": priced["tier"],
                    "rates": priced["rates"]}
    if priced["notes"]:
        out.setdefault("notes", []).extend(priced["notes"])
    if billing == "oauth":
        # Subscription route: this is a reference only, never what was billed.
        out.update(usd=round(priced["usd"], 6), usd_basis="api_equivalent_reference")
    else:
        out.update(usd=round(priced["usd"], 6), usd_basis="official_api_estimate")
    return out


def build_report(
    ledger: Optional[UsageLedger],
    *,
    billing: str,
    run_state: str = "completed",
    basis: str = "metered",
    extra_incomplete: Iterable[str] = (),
) -> Optional[dict[str, Any]]:
    """Assemble the report dict that rides on `session.complete` (and is recomputed for old sessions).

    billing: "api_key" | "oauth" | "local" | other. run_state: "completed" or anything else (cancelled / failed /
    interrupted -> the figure is a partial lower bound). Returns None when nothing was metered.
    """
    if ledger is None or not ledger:
        return None
    lines = [_line_report(ln, billing) for ln in ledger.lines()]
    return _assemble(lines, billing=billing, run_state=run_state, basis=basis, extra=list(extra_incomplete))


def _assemble(lines: list[dict[str, Any]], *, billing: str, run_state: str, basis: str, extra: list[str]) -> dict[str, Any]:
    reasons: list[str] = list(extra)
    if run_state != "completed":
        reasons.append(f"run_{run_state}_usage_is_partial")
    for ln in lines:
        for m in ln["missing"]:
            reasons.append(f"{ln['stage']}:{m}")
        if ln["unpriced_reason"]:
            reasons.append(f"{ln['stage']}:{ln['unpriced_reason']}")
    reasons = sorted(set(reasons))
    priced = [ln for ln in lines if ln["usd"] is not None]
    sum_usd = round(sum(ln["usd"] for ln in priced), 6) if priced else None
    t_in = sum(ln["input_tokens"] for ln in lines)
    t_out = sum(ln["output_tokens"] for ln in lines)
    cached_known = all(ln["cached_input_tokens"] is not None for ln in lines)
    totals: dict[str, Any] = {
        "input_tokens": t_in, "output_tokens": t_out,
        "cached_input_tokens": sum(ln["cached_input_tokens"] or 0 for ln in lines) if cached_known else None,
        "cache_write_tokens": sum(ln["cache_write_tokens"] or 0 for ln in lines),
        "complete": not reasons, "incomplete_reasons": reasons,
        "api_estimate_usd": None, "api_equivalent_reference_usd": None, "external_api_usd": None,
    }
    if billing == "api_key":
        totals["api_estimate_usd"] = sum_usd
    elif billing == "oauth":
        totals["api_equivalent_reference_usd"] = sum_usd
    elif billing == "local":
        totals["external_api_usd"] = 0.0
    notes = ["ceiling_prices_for_cost_guard_are_separate",
             "regional_uplift_tools_and_storage_not_included",
             "standard_tier_assumed_unless_response_names_a_tier",
             "reasoning_tokens_are_part_of_output_not_added_twice"]
    if billing == "oauth":
        notes.append("subscription_no_extra_api_billing_reference_is_not_an_invoice")
    if billing == "local":
        notes.append("local_model_external_api_fee_zero_electricity_not_priced")
    if any((ln.get("price") or {}).get("model_id") == "gpt-5.6-sol" for ln in lines):
        notes.append("gpt-5.6-sol_promotional_price_through_2026-11-21")
    if any(ln["stage"] == "dexter" for ln in lines):
        notes.append("dexter_input_tokens_assumed_to_include_cached_tokens")
    return {
        "schema": SCHEMA, "basis": basis, "billing": billing, "run_state": run_state, "extra_incomplete": list(extra),
        "price": {"version": op.PRICE_VERSION, "verified_at": op.VERIFIED_AT, "sources": list(op.SOURCES),
                  "unit": "USD_per_1M_tokens", "service_tier_assumed": "standard", "context_short_max_input_tokens": op.CONTEXT_SHORT_MAX},
        "lines": lines, "totals": totals, "notes": notes,
    }


def add_dexter(report: Optional[dict[str, Any]], *, billing: str, provider: Optional[str], model: Optional[str],
               ran: bool, raw_usage: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Fold the Dexter job into a report as its own line(s). Dexter that did not run adds nothing; Dexter that ran
    without provider-reported usage adds an UNMEASURED line (unknown cost), never a zero one."""
    if not ran:
        return report
    u = raw_usage or {}
    led = UsageLedger(provider=provider)
    if u.get("measured") and _int(u.get("input_tokens")) is not None:
        led.record(stage="dexter", agent="dexter", provider=provider, model_requested=model, model_billed=model,
                   input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                   cached_input_tokens=u.get("cached_tokens"), reasoning_tokens=u.get("reasoning_tokens"))
    else:
        led.record_unmeasured(stage="dexter", agent="dexter", provider=provider, model=model)
    d_lines = [_line_report(ln, billing) for ln in led.lines()]
    if report is None:
        return _assemble(d_lines, billing=billing, run_state="completed", basis="metered", extra=[])
    return _assemble(report["lines"] + d_lines, billing=report["billing"], run_state=report["run_state"],
                     basis=report["basis"], extra=report.get("extra_incomplete", []))


def brief(report: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Small totals-only view for the live `cost.usage` event (the per-agent lines ride only on session.complete)."""
    if not report:
        return None
    t = report["totals"]
    return {"billing": report["billing"], "api_estimate_usd": t["api_estimate_usd"],
            "api_equivalent_reference_usd": t["api_equivalent_reference_usd"], "complete": t["complete"],
            "price_version": report["price"]["version"]}


def recompute_for_session(complete: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Report for a session saved before reports existed, from its stored provider/model/auth/tokens.

    It is labelled basis="recomputed_current_list_price": today's official table applied to the stored totals, with the
    cache split and the deep/quick model split unknown, so it is always incomplete. It never overwrites stored accounting.
    """
    if not isinstance(complete, dict):
        return None
    prov, auth = complete.get("provider"), complete.get("auth_kind")
    in_t, out_t = _int(complete.get("input_tokens")), _int(complete.get("output_tokens"))
    if in_t is None or out_t is None or not prov:
        return None
    billing = auth if auth in ("api_key", "oauth", "local") else ("local" if prov == "local" else "api_key")
    led = UsageLedger(provider=prov)
    br = complete.get("usage_breakdown") or {}
    dex = br.get("dexter") if isinstance(br, dict) else None
    d_in = _int((dex or {}).get("input_tokens")) or 0
    d_out = _int((dex or {}).get("output_tokens")) or 0
    model = complete.get("deep_model") or complete.get("model")
    led.record(stage="debate", agent=None, provider=prov, model_requested=model, model_billed=model,
               input_tokens=max(0, in_t - d_in), output_tokens=max(0, out_t - d_out))
    extra = ["model_split_and_cache_split_unknown_in_saved_session"]
    if complete.get("quick_model") and complete.get("quick_model") != model:
        extra.append("quick_model_tokens_priced_at_deep_model")
    if dex and (d_in or d_out) and (dex.get("ran") is not False):
        led.record(stage="dexter", agent="dexter", provider=prov, model_requested=model, model_billed=model,
                   input_tokens=d_in, output_tokens=d_out)
    return build_report(led, billing=billing, run_state="completed", basis="recomputed_current_list_price", extra_incomplete=extra)
