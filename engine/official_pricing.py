"""Official OpenAI API price lookup and the unit rules for turning metered usage into USD.

This module is the ONLY place that reads `openai_price_data` (generated from OpenAI's published pricing page). It is kept
apart from `llm_providers._COST_PER_M_TOKENS`, which are deliberately conservative CEILINGS used to reserve budget in
CostGuard. Nothing here changes what CostGuard reserves or settles; these numbers are an after-the-fact report.

Unit rules (OpenAI, as published on the pricing page):
  * prices are USD per 1,000,000 tokens;
  * `input_tokens` INCLUDES the cached part. Billable = (input - cached) at the input rate + cached at the cached-input rate;
  * a prompt-cache WRITE is billed at the write rate for its TTL (30m / 12h) instead of the plain input rate;
  * reasoning tokens are part of `output_tokens` (already billed at the output rate) - they are shown, never added again;
  * a request whose input exceeds 272K tokens is billed entirely at the long-context rate;
  * Standard processing is assumed unless the response names another service tier. Regional-processing (+10%) and
    FedRAMP uplifts, tool/web-search fees and storage are NOT included.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import openai_price_data as _data

PRICE_VERSION = _data.PRICE_VERSION
VERIFIED_AT = _data.VERIFIED_AT
SOURCES = _data.SOURCES
CONTEXT_SHORT_MAX = 272_000

# response.service_tier -> pricing-page column. None / "default" / "auto" = the normal (Standard) tier.
_TIER_BY_SERVICE_TIER = {
    None: "standard", "default": "standard", "auto": "standard", "standard": "standard",
    "flex": "flex", "batch": "batch", "priority": "fast", "fast": "fast", "ultrafast": "ultrafast",
}
_SNAPSHOT_RE = re.compile(r"^(?P<base>.+)-\d{4}-\d{2}-\d{2}$")


def resolve_model(model_id: Optional[str]) -> Optional[tuple[str, str]]:
    """Map a model id from the provider/response to a row of the official table.

    Returns (price_row_id, how) with how = "exact" | "snapshot_of_alias", or None when the table has no such model.
    A dated snapshot id that is itself listed matches exactly; an unlisted dated snapshot falls back to its undated
    alias and is labelled so. Anything else (OAuth-only aliases, other vendors, typos) is unknown, never guessed.
    """
    if not model_id:
        return None
    mid = str(model_id).strip()
    if mid in _data.PRICES:
        return mid, "exact"
    m = _SNAPSHOT_RE.match(mid)
    if m and m.group("base") in _data.PRICES:
        return m.group("base"), "snapshot_of_alias"
    return None


def tier_for_service_tier(service_tier: Optional[str]) -> Optional[str]:
    return _TIER_BY_SERVICE_TIER.get(service_tier.lower() if isinstance(service_tier, str) else service_tier)


def context_bucket(input_tokens: int) -> str:
    return "short" if input_tokens <= CONTEXT_SHORT_MAX else "long"


def price_usage(
    *,
    model: Optional[str],
    service_tier: Optional[str],
    context: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cache_write_30m_tokens: int = 0,
    cache_write_12h_tokens: int = 0,
    cache_write_unsplit_tokens: int = 0,
) -> dict[str, Any]:
    """USD for one aggregated bucket of calls (same model, tier and context bucket).

    Cache writes are billed per TTL (30m / 12h). Tokens whose TTL split was not reported (`cache_write_unsplit_tokens`) are
    priced at BOTH rates: `usd` takes the 30m rate (the lower bound) and `usd_max` the 12h rate, so the caller can show a range
    and must not call the result complete. Input whose cache status was not reported is simply not in the cached/write
    counts, so it is billed at the plain input rate; the caller marks such a line partial.
    Returns {"usd": float|None, "usd_max": float|None (only when it differs), "price_model_id", "resolved_via", "tier", "rates",
    "reason", "notes": [...]}. `usd` is None (unknown) whenever no official price applies - never 0.
    """
    res = resolve_model(model)
    if res is None:
        return {"usd": None, "usd_max": None, "price_model_id": None, "resolved_via": None, "tier": None, "rates": None,
                "reason": "no_official_price", "notes": []}
    row_id, how = res
    tier = tier_for_service_tier(service_tier)
    base = {"usd": None, "usd_max": None, "price_model_id": row_id, "resolved_via": how, "tier": tier, "rates": None, "notes": []}
    if tier is None:
        return {**base, "reason": "unsupported_service_tier"}
    by_ctx = _data.PRICES[row_id].get(tier)
    if by_ctx is None:
        return {**base, "reason": "tier_not_listed"}
    rates = by_ctx.get(context)
    if rates is None:
        return {**base, "reason": "long_context_price_not_listed"}
    notes: list[str] = []
    if how == "snapshot_of_alias":
        notes.append("priced_via_undated_alias_of_snapshot")
    in_rate, out_rate = rates["input"], rates["output"]
    cached_rate = rates["cached_input"]
    if cached_rate is None:
        cached_rate = in_rate
        if cached_input_tokens:
            notes.append("no_cached_input_price_billed_at_input_rate")
    r30, r12 = rates["write_30m"], rates["write_12h"]
    if r30 is None or r12 is None:                  # no published write surcharge for this model: a write is plain input
        r30 = r12 = in_rate
        if cache_write_30m_tokens or cache_write_12h_tokens or cache_write_unsplit_tokens:
            notes.append("no_cache_write_price_billed_at_input_rate")
    writes = cache_write_30m_tokens + cache_write_12h_tokens + cache_write_unsplit_tokens
    uncached = max(0, input_tokens - cached_input_tokens - writes)
    fixed = (uncached * in_rate + cached_input_tokens * cached_rate + cache_write_30m_tokens * r30
             + cache_write_12h_tokens * r12 + output_tokens * out_rate)
    usd = (fixed + cache_write_unsplit_tokens * r30) / 1_000_000
    usd_max = (fixed + cache_write_unsplit_tokens * r12) / 1_000_000
    if cache_write_unsplit_tokens and r12 != r30:
        notes.append("cache_write_ttl_unsplit_priced_as_range_30m_to_12h")
    else:
        usd_max = None
    return {"usd": usd, "usd_max": usd_max, "price_model_id": row_id, "resolved_via": how, "tier": tier,
            "rates": {**rates, "unit": "USD_per_1M_tokens"}, "reason": None, "notes": notes}
