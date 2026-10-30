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
    cache_write_tokens: int,
) -> dict[str, Any]:
    """USD for one aggregated bucket of calls (same model, tier and context bucket).

    Input whose cache status was not reported is simply not in `cached_input_tokens`, so it is billed at the plain input
    rate (an upper bound for that part); the caller marks such a line partial.
    Returns {"usd": float|None, "price_model_id", "resolved_via", "tier", "rates", "reason", "notes": [...]}.
    `usd` is None (unknown) whenever no official price applies - never 0.
    """
    res = resolve_model(model)
    if res is None:
        return {"usd": None, "price_model_id": None, "resolved_via": None, "tier": None, "rates": None,
                "reason": "no_official_price", "notes": []}
    row_id, how = res
    tier = tier_for_service_tier(service_tier)
    if tier is None:
        return {"usd": None, "price_model_id": row_id, "resolved_via": how, "tier": None, "rates": None,
                "reason": "unsupported_service_tier", "notes": []}
    by_ctx = _data.PRICES[row_id].get(tier)
    if by_ctx is None:
        return {"usd": None, "price_model_id": row_id, "resolved_via": how, "tier": tier, "rates": None,
                "reason": "tier_not_listed", "notes": []}
    rates = by_ctx.get(context)
    if rates is None:
        return {"usd": None, "price_model_id": row_id, "resolved_via": how, "tier": tier, "rates": None,
                "reason": "long_context_price_not_listed", "notes": []}
    notes: list[str] = []
    in_rate, out_rate = rates["input"], rates["output"]
    cached_rate = rates["cached_input"]
    if cached_rate is None:
        cached_rate = in_rate
        if cached_input_tokens:
            notes.append("no_cached_input_price_billed_at_input_rate")
    write_rate = rates["write_30m"]
    if write_rate is None:
        write_rate = in_rate
    elif cache_write_tokens:
        notes.append("cache_write_ttl_unreported_priced_at_30m_rate")
    uncached = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    usd = (uncached * in_rate + cached_input_tokens * cached_rate + cache_write_tokens * write_rate
           + output_tokens * out_rate) / 1_000_000
    return {"usd": usd, "price_model_id": row_id, "resolved_via": how, "tier": tier,
            "rates": {**rates, "unit": "USD_per_1M_tokens"}, "reason": None, "notes": notes}
