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
    cache_write_30m_tokens: int = 0
    cache_write_12h_tokens: int = 0
    cache_write_unsplit_tokens: int = 0       # a write total was reported but not its 30m/12h split
    reasoning_tokens: int = 0
    # calls whose usage did not report the field -> the line is partial for that field
    cached_unreported_calls: int = 0
    cache_write_unreported_calls: int = 0     # calls that said nothing at all about cache writes
    nousage_calls: int = 0                    # calls that finished but returned no usage numbers at all
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
        cache_write_30m_tokens: Any = None,
        cache_write_12h_tokens: Any = None,
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
        s30, s12, total = _int(cache_write_30m_tokens), _int(cache_write_12h_tokens), _int(cache_write_tokens)
        if s30 is not None or s12 is not None:
            ln.cache_write_30m_tokens += s30 or 0
            ln.cache_write_12h_tokens += s12 or 0
            if total is not None and total > (s30 or 0) + (s12 or 0):
                ln.cache_write_unsplit_tokens += total - (s30 or 0) - (s12 or 0)
        elif total is not None:
            ln.cache_write_unsplit_tokens += total
        else:
            ln.cache_write_unreported_calls += 1
        for attr, unrep, val in (("cached_input_tokens", "cached_unreported_calls", cached_input_tokens),
                                 ("reasoning_tokens", "reasoning_unreported_calls", reasoning_tokens)):
            v = _int(val)
            if v is None:
                setattr(ln, unrep, getattr(ln, unrep) + 1)
            else:
                setattr(ln, attr, getattr(ln, attr) + v)
        if not tier_reported:
            ln.tier_unreported_calls += 1

    def record_no_usage(self, *, stage: str, agent: Optional[str], provider: Optional[str], model: Optional[str]) -> None:
        """A model call finished but its response carried no usage numbers: kept as a visible gap, not dropped."""
        key = (stage, agent, provider or self.provider, model, model, None, "short")
        ln = self._lines.get(key)
        if ln is None:
            ln = self._lines[key] = _Line(stage, agent, provider or self.provider, model, model, None, "short")
        ln.calls += 1
        ln.nousage_calls += 1

    def record_unmeasured(self, *, stage: str, agent: Optional[str], provider: Optional[str], model: Optional[str]) -> None:
        """A stage that ran but whose usage could not be measured (e.g. Dexter without provider-reported usage)."""
        key = (stage, agent, provider, model, model, None, "short")
        ln = self._lines.setdefault(key, _Line(stage, agent, provider, model, model, None, "short"))
        ln.unmeasured = True

    def has_usage_gap(self) -> bool:
        """True when some finished call reported no usage numbers (or a stage could not be measured): measured totals are then a
        LOWER bound, so a budget hold must not be derived from them alone."""
        return any(ln.nousage_calls > 0 or ln.unmeasured for ln in self._lines.values())

    def __bool__(self) -> bool:
        return bool(self._lines)

    def lines(self) -> list[_Line]:
        return list(self._lines.values())

    def totals(self) -> tuple[int, int]:
        ls = self._lines.values()
        return sum(x.input_tokens for x in ls), sum(x.output_tokens for x in ls)


_TIER_PREFIXES = ("", "flex_", "priority_")


def _prefixed(d: dict, name: str) -> Any:
    """langchain-openai renames detail keys for flex / priority calls (`flex_cache_read`); read either form."""
    for p in _TIER_PREFIXES:
        if f"{p}{name}" in d:
            return d[f"{p}{name}"]
    return None


def usage_from_langchain(response: Any) -> tuple[int, int, dict[str, Any]]:
    """(input, output, detail) from a LangChain LLMResult. detail values are None when the provider did not report them.

    Totals come from `usage_metadata`. The cache / reasoning split is read from the provider's RAW usage when LangChain kept
    it (`llm_output["token_usage"]`, non-streaming Chat Completions) because LangChain's own mapping drops the cache-write TTL
    split (`cache_write_tokens_details.ttl_30m_tokens / ttl_12h_tokens`); otherwise from `usage_metadata`'s detail dicts
    (cache_creation is then a TOTAL with no TTL split, which the report prices as a 30m-12h range and flags incomplete).
    """
    from .llm_providers import usage_detail

    detail: dict[str, Any] = {"cached": None, "cache_write": None, "cache_write_30m": None, "cache_write_12h": None,
                              "reasoning": None, "model": None, "service_tier": None}
    in_tok = out_tok = 0
    seen = False
    lo = getattr(response, "llm_output", None)
    lo = lo if isinstance(lo, dict) else {}
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
                    for key, name in (("cached", "cache_read"), ("cache_write", "cache_creation")):
                        v = _int(_prefixed(itd, name))
                        if v is not None:
                            detail[key] = (detail[key] or 0) + v
                if isinstance(otd, dict):
                    v = _int(_prefixed(otd, "reasoning"))
                    if v is not None:
                        detail["reasoning"] = (detail["reasoning"] or 0) + v
                rm = getattr(msg, "response_metadata", None) or {}
                gi = getattr(gen, "generation_info", None) or {}
                for src in (rm, gi, lo):
                    detail["model"] = detail["model"] or src.get("model_name") or src.get("model")
                    detail["service_tier"] = detail["service_tier"] or src.get("service_tier")
    except Exception:  # noqa: BLE001 - metering must never break a run
        pass
    raw = lo.get("token_usage") or lo.get("usage")
    if isinstance(raw, dict) and raw:
        # The raw provider usage is the more complete record: take the split (incl. the TTL split) from it.
        rd = usage_detail(raw)
        for k in ("cached", "cache_write", "cache_write_30m", "cache_write_12h", "reasoning"):
            if rd[k] is not None:
                detail[k] = rd[k]
        detail["model"] = detail["model"] or lo.get("model_name") or lo.get("model")
        detail["service_tier"] = detail["service_tier"] or lo.get("service_tier")
        if not seen:
            in_tok = _int(raw.get("input_tokens") or raw.get("prompt_tokens")) or 0
            out_tok = _int(raw.get("output_tokens") or raw.get("completion_tokens")) or 0
    return in_tok, out_tok, detail


def _line_report(ln: _Line, billing: str) -> dict[str, Any]:
    missing: list[str] = []
    if ln.unmeasured:
        missing.append("usage_not_measured")
    if ln.nousage_calls:
        missing.append("usage_not_reported")
    if ln.cached_unreported_calls:
        missing.append("cached_input_tokens")
    if ln.reasoning_unreported_calls:
        missing.append("reasoning_tokens")
    if ln.cache_write_unsplit_tokens:
        missing.append("cache_write_ttl")
    writes = ln.cache_write_30m_tokens + ln.cache_write_12h_tokens + ln.cache_write_unsplit_tokens
    price_model = ln.model_billed or ln.model_requested
    out: dict[str, Any] = {
        "stage": ln.stage, "agent": ln.agent, "provider": ln.provider,
        "model": price_model, "model_requested": ln.model_requested, "calls": ln.calls,
        "input_tokens": ln.input_tokens, "cached_input_tokens": ln.cached_input_tokens if not ln.cached_unreported_calls else None,
        "uncached_input_tokens": max(0, ln.input_tokens - ln.cached_input_tokens - writes) if not ln.cached_unreported_calls else None,
        "cache_write_tokens": writes, "cache_write_30m_tokens": ln.cache_write_30m_tokens,
        "cache_write_12h_tokens": ln.cache_write_12h_tokens, "cache_write_unsplit_tokens": ln.cache_write_unsplit_tokens,
        "output_tokens": ln.output_tokens,
        "reasoning_tokens": ln.reasoning_tokens if not ln.reasoning_unreported_calls else None,
        "usage_unreported_calls": ln.nousage_calls,
        "context": ln.context, "usd": None, "usd_max": None, "usd_basis": None, "price": None, "unpriced_reason": None,
        "missing": missing,
    }
    if ln.unmeasured:
        out["unpriced_reason"] = "usage_not_measured"
        return out
    if ln.reasoning_tokens > ln.output_tokens:
        out.setdefault("notes", []).append("reasoning_exceeds_output_not_added")
    all_unreported = ln.nousage_calls > 0 and ln.nousage_calls >= ln.calls
    if all_unreported and billing != "local":
        # Every call on this line finished without usage: its cost is UNKNOWN. Pricing zero tokens would show a fake $0.
        out["unpriced_reason"] = "usage_not_reported"
        return out
    if ln.nousage_calls:
        out.setdefault("notes", []).append("usage_gap_subtotal_is_a_lower_bound")
    if billing == "local":
        out.update(usd=0.0, usd_basis="local_no_external_api_fee")
        return out
    if ln.provider != "openai":
        out["unpriced_reason"] = "official_price_not_imported_for_provider"
        return out
    priced = op.price_usage(
        model=price_model, service_tier=ln.service_tier, context=ln.context, input_tokens=ln.input_tokens,
        output_tokens=ln.output_tokens, cached_input_tokens=ln.cached_input_tokens,
        cache_write_30m_tokens=ln.cache_write_30m_tokens, cache_write_12h_tokens=ln.cache_write_12h_tokens,
        cache_write_unsplit_tokens=ln.cache_write_unsplit_tokens,
    )
    if priced["usd"] is None:
        out["unpriced_reason"] = priced["reason"]
        return out
    # A model with a published write surcharge whose calls said nothing about writes: they may have been billed as writes.
    if ln.cache_write_unreported_calls and (priced["rates"] or {}).get("write_30m") is not None:
        missing.append("cache_write_tokens")
    if ln.tier_unreported_calls and ln.service_tier is None:
        out.setdefault("notes", []).append("service_tier_not_reported_assumed_standard")
    out["price"] = {"model_id": priced["price_model_id"], "resolved_via": priced["resolved_via"], "tier": priced["tier"],
                    "rates": priced["rates"]}
    if priced["notes"]:
        out.setdefault("notes", []).extend(priced["notes"])
    basis = "api_equivalent_reference" if billing == "oauth" else "official_api_estimate"   # oauth: a reference, never a bill
    out.update(usd=round(priced["usd"], 6), usd_basis=basis,
               usd_max=None if priced["usd_max"] is None else round(priced["usd_max"], 6))
    return out


def with_run_state(report: Optional[dict[str, Any]], state: str) -> Optional[dict[str, Any]]:
    """The same report re-assembled for a different terminal state (cancelled / failed / interrupted -> partial)."""
    if not report:
        return report
    return _assemble(report["lines"], billing=report["billing"], run_state=state, basis=report["basis"],
                     extra=report.get("extra_incomplete", []))


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
        "cache_write_30m_tokens": sum(ln["cache_write_30m_tokens"] for ln in lines),
        "cache_write_12h_tokens": sum(ln["cache_write_12h_tokens"] for ln in lines),
        "cache_write_unsplit_tokens": sum(ln["cache_write_unsplit_tokens"] for ln in lines),
        "complete": not reasons, "incomplete_reasons": reasons,
        "api_estimate_usd": None, "api_equivalent_reference_usd": None, "external_api_usd": None,
    }
    max_usd = (round(sum((ln["usd_max"] if ln["usd_max"] is not None else ln["usd"]) for ln in priced), 6)
               if any(ln["usd_max"] is not None for ln in priced) else None)
    totals["api_estimate_usd_max"] = None
    totals["api_equivalent_reference_usd_max"] = None
    if billing == "api_key":
        totals["api_estimate_usd"] = sum_usd
        totals["api_estimate_usd_max"] = max_usd
    elif billing == "oauth":
        totals["api_equivalent_reference_usd"] = sum_usd
        totals["api_equivalent_reference_usd_max"] = max_usd
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
    notes.extend(sorted({n for ln in lines for n in ln.get("notes", [])}))
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


def summary(report: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Compact list-view digest of a report (what a History card needs), stored with the session so the list never parses events."""
    if not report:
        return None
    t = report["totals"]
    return {"billing": report["billing"], "basis": report["basis"], "run_state": report.get("run_state"), "complete": t["complete"],
            "api_estimate_usd": t["api_estimate_usd"], "api_estimate_usd_max": t.get("api_estimate_usd_max"),
            "api_equivalent_reference_usd": t["api_equivalent_reference_usd"],
            "api_equivalent_reference_usd_max": t.get("api_equivalent_reference_usd_max"),
            "external_api_usd": t["external_api_usd"], "price_version": report["price"]["version"],
            "verified_at": report["price"]["verified_at"]}


def brief(report: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Small totals-only view for the live `cost.usage` event (the per-agent lines ride only on session.complete)."""
    if not report:
        return None
    t = report["totals"]
    return {"billing": report["billing"], "api_estimate_usd": t["api_estimate_usd"],
            "api_equivalent_reference_usd": t["api_equivalent_reference_usd"],
            "api_estimate_usd_max": t.get("api_estimate_usd_max"), "complete": t["complete"],
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
    billing = auth if auth in ("api_key", "oauth", "local") else ("local" if prov == "local" else "unknown")   # unknown auth: no figure, never a guess
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
