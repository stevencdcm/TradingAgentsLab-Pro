"""Official-API price report: price table provenance, unit rules, ledger, billing kinds, Dexter, partial runs.

Everything here is fake usage and in-memory objects: no network, no real model, no Keychain, no real database.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import cost_report as cr
from engine import official_pricing as op
from engine import openai_price_data as data
from engine import storage
from engine.cost_report import UsageLedger, build_report

ROOT = Path(__file__).resolve().parents[2]


def _one(model="gpt-6.1-sol", **kw):
    led = UsageLedger(provider="openai")
    args = dict(stage="debate", agent="market", provider="openai", model_requested=model, model_billed=model,
                input_tokens=100_000, output_tokens=10_000, cached_input_tokens=0, cache_write_tokens=0,
                reasoning_tokens=0, service_tier="default", tier_reported=True)
    args.update(kw)
    led.record(**args)
    return led


def test_catalog_provenance_and_official_numbers():
    snap = ROOT / "docs" / "pricing-snapshots" / "openai-pricing-2026-10-31.md"
    assert hashlib.sha256(snap.read_bytes()).hexdigest() in data.__doc__           # data module names the exact page text it came from
    assert data.SOURCES == ("https://developers.openai.com/api/docs/pricing", "https://openai.com/api/pricing")
    assert data.VERIFIED_AT == "2026-10-31" and data.PRICE_VERSION == "openai-api-2026-10-31"
    sol = data.PRICES["gpt-6.1-sol"]["standard"]
    assert sol["short"] == {"input": 2.0, "output": 10.0, "cached_input": 0.1, "write_30m": 2.5, "write_12h": 5.0}
    assert sol["long"]["input"] == 4.0 and sol["long"]["output"] == 15.0
    assert data.PRICES["gpt-5.5"]["standard"]["short"]["output"] == 30.0
    assert data.PRICES["gpt-4o"]["standard"]["short"]["cached_input"] == 1.25
    assert data.PRICES["gpt-6.1-sol"]["fast"]["short"]["input"] == 4.0 and "ultrafast" in data.PRICES["gpt-6-astra"]


def test_official_table_is_not_the_budget_ceiling_table():
    from engine.llm_providers import _COST_PER_M_TOKENS

    assert _COST_PER_M_TOKENS["gpt-5.5"] == {"input": 10.00, "output": 40.00}          # CostGuard ceiling untouched
    assert data.PRICES["gpt-5.5"]["standard"]["short"]["input"] == 5.0                  # official price is lower


def test_cache_read_billed_at_cached_rate_and_input_includes_cached():
    r = build_report(_one(cached_input_tokens=60_000), billing="api_key")
    ln = r["lines"][0]
    assert ln["uncached_input_tokens"] == 40_000 and ln["cached_input_tokens"] == 60_000
    assert ln["usd"] == pytest.approx(40_000 * 2.0 / 1e6 + 60_000 * 0.1 / 1e6 + 10_000 * 10.0 / 1e6)   # 0.186
    assert ln["usd_basis"] == "official_api_estimate" and r["totals"]["api_estimate_usd"] == pytest.approx(0.186)
    assert r["totals"]["complete"] is True


def test_reasoning_tokens_are_not_billed_twice():
    base = build_report(_one(reasoning_tokens=0), billing="api_key")["totals"]["api_estimate_usd"]
    with_reasoning = build_report(_one(reasoning_tokens=7_000), billing="api_key")
    assert with_reasoning["totals"]["api_estimate_usd"] == base
    assert with_reasoning["lines"][0]["reasoning_tokens"] == 7_000 and with_reasoning["lines"][0]["output_tokens"] == 10_000
    odd = build_report(_one(reasoning_tokens=50_000), billing="api_key")                 # more reasoning than output: anomaly, not added
    assert odd["totals"]["api_estimate_usd"] == base and "reasoning_exceeds_output_not_added" in odd["lines"][0]["notes"]


def test_cache_write_is_priced_per_ttl_and_a_total_without_a_split_is_a_range_and_incomplete():
    split = build_report(_one(cached_input_tokens=0, cache_write_30m_tokens=10_000, cache_write_12h_tokens=10_000, cache_write_tokens=20_000), billing="api_key")
    ln = split["lines"][0]
    assert ln["usd"] == pytest.approx(80_000 * 2.0 / 1e6 + 10_000 * 2.5 / 1e6 + 10_000 * 5.0 / 1e6 + 10_000 * 10.0 / 1e6)
    assert ln["usd_max"] is None and split["totals"]["complete"] is True and ln["cache_write_12h_tokens"] == 10_000
    unsplit = build_report(_one(cached_input_tokens=0, cache_write_tokens=20_000), billing="api_key")
    ln = unsplit["lines"][0]
    low = 80_000 * 2.0 / 1e6 + 20_000 * 2.5 / 1e6 + 10_000 * 10.0 / 1e6
    high = 80_000 * 2.0 / 1e6 + 20_000 * 5.0 / 1e6 + 10_000 * 10.0 / 1e6
    assert ln["usd"] == pytest.approx(low) and ln["usd_max"] == pytest.approx(high) and "cache_write_ttl" in ln["missing"]
    t = unsplit["totals"]
    assert t["complete"] is False and t["api_estimate_usd"] == pytest.approx(low) and t["api_estimate_usd_max"] == pytest.approx(high)
    assert "debate:cache_write_ttl" in t["incomplete_reasons"] and "cache_write_ttl_unsplit_priced_as_range_30m_to_12h" in unsplit["notes"]


def test_write_priced_model_whose_usage_says_nothing_about_writes_is_not_complete():
    r = build_report(_one(cache_write_tokens=None), billing="api_key")
    assert "cache_write_tokens" in r["lines"][0]["missing"] and r["totals"]["complete"] is False
    older = build_report(_one(model="gpt-5.5", cache_write_tokens=None), billing="api_key")      # no published write price: nothing to miss
    assert older["totals"]["complete"] is True


def test_long_context_request_uses_long_rates():
    led = _one(input_tokens=300_000, output_tokens=1_000)
    ln = build_report(led, billing="api_key")["lines"][0]
    assert ln["context"] == "long" and ln["usd"] == pytest.approx(300_000 * 4.0 / 1e6 + 1_000 * 15.0 / 1e6)


def test_long_context_without_a_listed_long_price_is_unknown_not_short_price():
    ln = build_report(_one(model="gpt-5.5-pro", input_tokens=300_000, output_tokens=10, service_tier="flex"), billing="api_key")
    assert ln["lines"][0]["usd"] is None and ln["lines"][0]["unpriced_reason"] == "long_context_price_not_listed"
    assert ln["totals"]["api_estimate_usd"] is None and ln["totals"]["complete"] is False


def test_multi_model_lines_sum_and_snapshot_alias_is_labelled():
    led = UsageLedger(provider="openai")
    led.record(stage="debate", agent="analyst", provider="openai", model_requested="gpt-4o", model_billed="gpt-4o-2024-08-06",
               input_tokens=1000, output_tokens=500, cached_input_tokens=0, reasoning_tokens=0, cache_write_tokens=0, service_tier="default", tier_reported=True)
    led.record(stage="debate", agent="pm", provider="openai", model_requested="gpt-6-astra", model_billed="gpt-6-astra",
               input_tokens=2000, output_tokens=100, cached_input_tokens=0, reasoning_tokens=0, cache_write_tokens=0, service_tier="default", tier_reported=True)
    r = build_report(led, billing="api_key")
    by = {ln["agent"]: ln for ln in r["lines"]}
    assert by["analyst"]["price"]["resolved_via"] == "snapshot_of_alias" and by["analyst"]["price"]["model_id"] == "gpt-4o"
    assert by["pm"]["price"]["resolved_via"] == "exact"
    assert r["totals"]["api_estimate_usd"] == pytest.approx(by["analyst"]["usd"] + by["pm"]["usd"])


def test_unknown_model_is_unknown_never_zero():
    r = build_report(_one(model="gpt-6.1-sol-codex-preview"), billing="api_key")
    assert r["lines"][0]["usd"] is None and r["lines"][0]["unpriced_reason"] == "no_official_price"
    assert r["totals"]["api_estimate_usd"] is None and r["totals"]["complete"] is False
    assert any("no_official_price" in x for x in r["totals"]["incomplete_reasons"])


def test_unreported_cache_field_is_partial_and_not_a_fake_zero():
    r = build_report(_one(cached_input_tokens=None, reasoning_tokens=None), billing="api_key")
    ln = r["lines"][0]
    assert ln["cached_input_tokens"] is None and ln["uncached_input_tokens"] is None and ln["reasoning_tokens"] is None
    assert set(ln["missing"]) == {"cached_input_tokens", "reasoning_tokens"}
    assert r["totals"]["complete"] is False and r["totals"]["cached_input_tokens"] is None
    assert ln["usd"] == pytest.approx(100_000 * 2.0 / 1e6 + 10_000 * 10.0 / 1e6)     # all input at the plain rate: an upper bound


def test_oauth_is_subscription_with_an_api_equivalent_reference_only():
    r = build_report(_one(), billing="oauth")
    assert r["totals"]["api_estimate_usd"] is None                                   # no extra API billing on a subscription
    assert r["totals"]["api_equivalent_reference_usd"] == pytest.approx(0.3)
    assert r["lines"][0]["usd_basis"] == "api_equivalent_reference"
    assert "subscription_no_extra_api_billing_reference_is_not_an_invoice" in r["notes"]
    unknown = build_report(_one(model="gpt-6.1-sol-pro-oauth"), billing="oauth")      # no exact official row: no reference either
    assert unknown["totals"]["api_equivalent_reference_usd"] is None and unknown["totals"]["api_estimate_usd"] is None


def test_local_model_external_fee_is_zero_and_power_is_not_claimed():
    led = UsageLedger(provider="local")
    led.record(stage="debate", agent="a", provider="local", model_requested="qwen", model_billed="qwen", input_tokens=10, output_tokens=5)
    r = build_report(led, billing="local")
    assert r["totals"]["external_api_usd"] == 0.0 and r["totals"]["api_estimate_usd"] is None
    assert "local_model_external_api_fee_zero_electricity_not_priced" in r["notes"]


def test_other_vendors_are_listed_but_unpriced():
    led = UsageLedger(provider="minimax")    # no official price table imported for MiniMax (its pricing page lists other models)
    led.record(stage="debate", agent="a", provider="minimax", model_requested="MiniMax-M2.7", model_billed="MiniMax-M2.7", input_tokens=10, output_tokens=5)
    r = build_report(led, billing="api_key")
    assert r["lines"][0]["unpriced_reason"] == "official_price_not_imported_for_provider" and r["totals"]["api_estimate_usd"] is None


def test_dexter_is_its_own_line_and_added_to_the_total():
    base = build_report(_one(), billing="api_key")
    r = cr.add_dexter(base, billing="api_key", provider="openai", model="gpt-6.1-sol", ran=True,
                      raw_usage={"measured": True, "input_tokens": 100_000, "output_tokens": 10_000, "cached_tokens": 50_000, "reasoning_tokens": 4_000})
    stages = [ln["stage"] for ln in r["lines"]]
    assert stages == ["debate", "dexter"]
    dx = r["lines"][1]
    assert "cache_write_tokens" in dx["missing"] and r["totals"]["complete"] is False            # Dexter never reports cache writes
    assert dx["usd"] == pytest.approx(50_000 * 2.0 / 1e6 + 50_000 * 0.1 / 1e6 + 10_000 * 10.0 / 1e6)
    assert r["totals"]["api_estimate_usd"] == pytest.approx(base["totals"]["api_estimate_usd"] + dx["usd"])
    assert r["totals"]["input_tokens"] == 200_000


def test_dexter_without_measured_usage_makes_the_total_incomplete_not_zero():
    r = cr.add_dexter(build_report(_one(), billing="api_key"), billing="api_key", provider="openai", model="gpt-6.1-sol",
                      ran=True, raw_usage={"measured": False})
    dx = r["lines"][1]
    assert dx["usd"] is None and dx["unpriced_reason"] == "usage_not_measured" and "usage_not_measured" in dx["missing"]
    assert r["totals"]["complete"] is False


def test_dexter_that_did_not_run_adds_nothing():
    base = build_report(_one(), billing="api_key")
    assert cr.add_dexter(base, billing="api_key", provider="openai", model="gpt-6.1-sol", ran=False, raw_usage=None) is base


def test_cancelled_or_interrupted_runs_are_flagged_partial_lower_bound():
    for state in ("cancelled", "failed", "interrupted", "running"):
        r = build_report(_one(), billing="api_key", run_state=state)
        assert r["totals"]["complete"] is False and f"run_{state}_usage_is_partial" in r["totals"]["incomplete_reasons"]
        assert r["totals"]["api_estimate_usd"] == pytest.approx(0.3)               # what was metered is still shown


def test_empty_ledger_has_no_report():
    assert build_report(UsageLedger(provider="openai"), billing="api_key") is None and cr.brief(None) is None


# ---- usage extraction from the two real shapes -------------------------------------------------------------------------

def _lc_response(model, in_t, out_t, cache_read=None, reasoning=None, tier="default"):
    um = {"input_tokens": in_t, "output_tokens": out_t, "total_tokens": in_t + out_t}
    if cache_read is not None:
        um["input_token_details"] = {"cache_read": cache_read, "cache_creation": 0}
    if reasoning is not None:
        um["output_token_details"] = {"reasoning": reasoning}
    msg = SimpleNamespace(usage_metadata=um, response_metadata={"model_name": model, "service_tier": tier})
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]], llm_output={})


def test_token_meter_records_agent_model_and_cache_split_per_call():
    from engine.full_debate import _TokenMeter

    m = _TokenMeter(None)
    m.ledger = UsageLedger(provider="openai")
    m.on_chat_model_start({}, [], run_id="r1", metadata={"langgraph_node": "market_analyst"}, invocation_params={"model": "gpt-6-luna"})
    m.on_chat_model_start({}, [], run_id="r2", metadata={"langgraph_node": "portfolio_manager"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_llm_end(_lc_response("gpt-6-luna", 10_000, 500, cache_read=4_000, reasoning=100), run_id="r1")
    m.on_llm_end(_lc_response("gpt-6.1-sol", 20_000, 800, cache_read=0, reasoning=300), run_id="r2")
    assert (m.input_tokens, m.output_tokens) == (30_000, 1_300)                    # the cap's own counting is unchanged
    r = build_report(m.ledger, billing="api_key")
    by = {ln["agent"]: ln for ln in r["lines"]}
    assert by["market_analyst"]["model"] == "gpt-6-luna" and by["market_analyst"]["cached_input_tokens"] == 4_000
    assert by["market_analyst"]["usd"] == pytest.approx(6_000 * 0.10 / 1e6 + 4_000 * 0.01 / 1e6 + 500 * 0.50 / 1e6)
    assert by["portfolio_manager"]["reasoning_tokens"] == 300 and by["portfolio_manager"]["usd"] == pytest.approx(20_000 * 2.0 / 1e6 + 800 * 10.0 / 1e6)
    assert r["totals"]["complete"] is True


def test_token_meter_without_usage_details_is_partial():
    from engine.full_debate import _TokenMeter

    m = _TokenMeter(None)
    m.ledger = UsageLedger(provider="openai")
    m.on_chat_model_start({}, [], run_id="r", metadata={"langgraph_node": "x"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_llm_end(_lc_response("gpt-6.1-sol", 1000, 100), run_id="r")                # no cache / reasoning details reported
    r = build_report(m.ledger, billing="api_key")
    assert r["totals"]["complete"] is False and "cached_input_tokens" in r["lines"][0]["missing"]


def test_usage_detail_reads_chat_and_responses_shapes_and_leaves_unreported_as_none():
    from engine.llm_providers import usage_detail

    chat = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=7), completion_tokens_details=SimpleNamespace(reasoning_tokens=3))
    assert usage_detail(chat, model="m", service_tier="default") == {"cached": 7, "cache_write": None, "cache_write_30m": None, "cache_write_12h": None,
                                                                    "reasoning": 3, "model": "m", "service_tier": "default"}
    resp = {"input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 2}}
    assert usage_detail(resp)["cached"] == 5 and usage_detail(resp)["reasoning"] == 2
    assert set(usage_detail(SimpleNamespace(prompt_tokens_details=None, completion_tokens_details=None)).values()) == {None}


# ---- live_debate (simplified path) end to end with a fake adapter -------------------------------------------------------

class _Adapter:
    name = "stub"
    last_usage = None

    def __init__(self, detail):
        self.n = 0
        self.detail = detail

    async def open(self, *, api_key):
        return None

    async def close(self):
        return None

    async def complete(self, *, system, user, model, max_tokens):
        from engine.live_debate import MAX_AGENTS_PER_SESSION

        self.n += 1
        self.last_usage = dict(self.detail) if self.detail is not None else None
        text = "STANCE=NEUTRAL\nCONVICTION=0.5\nBULL_STRENGTH=50\nBEAR_STRENGTH=50\nRISK=MODERATE\nr" if self.n == MAX_AGENTS_PER_SESSION else f"o{self.n}"
        return text, 1000, 200


async def _run(monkeypatch, auth_kind, detail, model="gpt-6.1-sol", provider="openai"):
    from engine.live_debate import ProviderConfig, SentimentBlock, live_debate

    auth = {"type": "api_key", "api_key": "sk-fake"} if auth_kind == "api_key" else (
        {"type": "oauth", "access": "t", "refresh": "r", "expires": "2099-01-01T00:00:00Z"} if auth_kind == "oauth"
        else {"type": "local", "base_url": "http://localhost:1/v1"})
    monkeypatch.setattr("engine.live_debate.adapter_for", lambda _c: _Adapter(detail))
    out = []
    async for ev in live_debate(ticker="NVDA", trade_date="2026-05-15", summary=None, headlines=None,
                                config=ProviderConfig(provider=provider, auth=auth, model=model, max_tokens=400), sentiment=SentimentBlock()):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_live_debate_complete_carries_per_agent_official_report_and_the_exact_figure_replaces_a_missing_ceiling(monkeypatch):
    from engine.live_debate import MAX_AGENTS_PER_SESSION
    from engine.llm_providers import estimate_cost_or_none

    evs = await _run(monkeypatch, "api_key", {"cached": 400, "cache_write": 0, "reasoning": 50, "model": "gpt-6.1-sol", "service_tier": "default"})
    comp = next(e for e in evs if e["type"] == "session.complete")
    rep = comp["cost_report"]
    assert len(rep["lines"]) == MAX_AGENTS_PER_SESSION and {ln["agent"] for ln in rep["lines"]} >= {"portfolio_manager"}
    assert rep["totals"]["input_tokens"] == 1000 * MAX_AGENTS_PER_SESSION and rep["totals"]["complete"] is True
    per = 600 * 2.0 / 1e6 + 400 * 0.1 / 1e6 + 200 * 10.0 / 1e6
    assert rep["totals"]["api_estimate_usd"] == pytest.approx(per * MAX_AGENTS_PER_SESSION)
    # gpt-6.1-sol has no budget-ceiling row, but its usage is complete and the official table prices it: the SAME exact figure is the
    # session's cost (History, report and the CostGuard settlement agree) instead of "unknown".
    assert comp["estimated_cost_usd"] == pytest.approx(per * MAX_AGENTS_PER_SESSION, abs=1e-4) and comp["cost_known"] is True
    assert estimate_cost_or_none("gpt-6.1-sol", 1, 1) is None
    last_usage = [e for e in evs if e["type"] == "cost.usage"][-1]
    assert last_usage["official_cost"]["complete"] is False                            # running total is never called complete
    assert "lines" not in last_usage["official_cost"]


@pytest.mark.asyncio
async def test_live_debate_oauth_shows_reference_not_a_bill(monkeypatch):
    evs = await _run(monkeypatch, "oauth", {"cached": 0, "cache_write": 0, "reasoning": 0, "model": "gpt-6.1-sol", "service_tier": None})
    comp = next(e for e in evs if e["type"] == "session.complete")
    t = comp["cost_report"]["totals"]
    assert comp["estimated_cost_usd"] == 0.0 and t["api_estimate_usd"] is None and t["api_equivalent_reference_usd"] > 0


@pytest.mark.asyncio
async def test_live_debate_adapter_without_detail_yields_a_partial_report(monkeypatch):
    evs = await _run(monkeypatch, "api_key", None)
    t = next(e for e in evs if e["type"] == "session.complete")["cost_report"]["totals"]
    assert t["complete"] is False and t["cached_input_tokens"] is None


# ---- history: stored report, legacy recompute, saved accounting untouched ----------------------------------------------

def test_history_returns_stored_report_and_recomputes_legacy_sessions_as_current_list_price():
    stored = build_report(_one(), billing="api_key")
    ev_new = [{"type": "session.complete", "provider": "openai", "model": "gpt-6.1-sol", "cost_report": stored}]
    assert storage.session_cost_report(ev_new) is stored
    legacy = [{"type": "session.start"}, {"type": "session.complete", "provider": "openai", "auth_kind": "api_key", "model": "gpt-6.1-sol",
                                         "input_tokens": 100_000, "output_tokens": 10_000, "estimated_cost_usd": 0.5, "cost_known": True}]
    rep = storage.session_cost_report(legacy)
    assert rep["basis"] == "recomputed_current_list_price" and rep["totals"]["complete"] is False
    assert rep["totals"]["api_estimate_usd"] == pytest.approx(0.3) and rep["price"]["verified_at"] == "2026-10-31"
    assert legacy[1]["estimated_cost_usd"] == 0.5                                        # the saved figure is never rewritten
    assert storage.session_cost_report([{"type": "session.start"}]) is None
    assert storage.session_cost_report([{"type": "session.complete", "provider": "openai"}]) is None   # nothing to recompute from


def test_legacy_oauth_recompute_is_a_reference_and_legacy_dexter_tokens_are_split_out():
    leg = {"type": "session.complete", "provider": "openai", "auth_kind": "oauth", "model": "gpt-6.1-sol", "input_tokens": 5000, "output_tokens": 500}
    t = cr.recompute_for_session(leg)["totals"]
    assert t["api_estimate_usd"] is None and t["api_equivalent_reference_usd"] > 0
    with_dx = {"type": "session.complete", "provider": "openai", "auth_kind": "api_key", "model": "gpt-6.1-sol", "input_tokens": 6000, "output_tokens": 600,
               "usage_breakdown": {"dexter": {"input_tokens": 1000, "output_tokens": 100, "ran": True}}}
    rep = cr.recompute_for_session(with_dx)
    assert [ln["stage"] for ln in rep["lines"]] == ["debate", "dexter"] and rep["totals"]["input_tokens"] == 6000


def test_detail_to_dict_includes_cost_report(tmp_path, monkeypatch):
    monkeypatch.setenv("TAL_DB_PATH", str(tmp_path / "s.db")) if False else None
    d = storage.SessionDetail(id="x", ticker="T", trade_date="2026-01-01", decision_action="HOLD", decision_confidence=0.5, decision_reasoning="r",
                              live=True, provider="openai", model="gpt-6.1-sol", input_tokens=1, output_tokens=1, estimated_cost_usd=None, created_at="t",
                              events=[{"type": "session.complete", "provider": "openai", "model": "gpt-6.1-sol", "input_tokens": 10, "output_tokens": 5}])
    assert storage.detail_to_dict(d)["cost_report"]["basis"] == "recomputed_current_list_price"


def test_report_has_no_secret_shapes_and_is_json_serialisable():
    import json

    s = json.dumps(build_report(_one(), billing="api_key"))
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", s)


# ---- D17: cache-write TTL, through the REAL langchain-openai mapping -----------------------------------------------------

# OpenAI's prompt-caching guide example: gpt-6.1-sol, 12,000 input (4,000 cached, 7,000 written: 3,000 at 30m + 4,000 at 12h), 1,000 output.
OFFICIAL_USAGE = {
    "prompt_tokens": 12000, "completion_tokens": 1000, "total_tokens": 13000,
    "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000,
                              "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000}},
    "completion_tokens_details": {"reasoning_tokens": 200},
}
OFFICIAL_RESPONSES_USAGE = {
    "input_tokens": 12000, "output_tokens": 1000, "total_tokens": 13000,
    "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000,
                             "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000}},
    "output_tokens_details": {"reasoning_tokens": 200},
}


def _real_chat_llm_result(usage, service_tier="default", model="gpt-6.1-sol-2026-10-01"):
    """What the callback really receives: ChatOpenAI._create_chat_result on a raw Chat Completions body, wrapped as an LLMResult."""
    from langchain_core.outputs import LLMResult
    from langchain_openai import ChatOpenAI

    body = {"id": "x", "model": model, "service_tier": service_tier,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}], "usage": usage}
    res = ChatOpenAI(model="gpt-6.1-sol", api_key="not-a-real-key")._create_chat_result(body)
    return LLMResult(generations=[res.generations], llm_output=res.llm_output)


def _meter_report(response, model="gpt-6.1-sol"):
    from engine.full_debate import _TokenMeter

    m = _TokenMeter(None)
    m.ledger = UsageLedger(provider="openai")
    m.on_chat_model_start({}, [], run_id="r", metadata={"langgraph_node": "pm"}, invocation_params={"model": model})
    m.on_llm_end(response, run_id="r")
    return build_report(m.ledger, billing="api_key")


def test_official_guide_example_through_real_langchain_openai_is_0_0399_not_0_0299():
    r = _meter_report(_real_chat_llm_result(OFFICIAL_USAGE))
    ln = r["lines"][0]
    assert (ln["cache_write_30m_tokens"], ln["cache_write_12h_tokens"], ln["cache_write_unsplit_tokens"]) == (3000, 4000, 0)
    assert ln["cached_input_tokens"] == 4000 and ln["uncached_input_tokens"] == 1000 and ln["reasoning_tokens"] == 200
    assert ln["model"] == "gpt-6.1-sol-2026-10-01" and ln["price"]["resolved_via"] == "snapshot_of_alias"
    assert ln["usd"] == pytest.approx(0.0399) and ln["usd_max"] is None
    assert r["totals"]["api_estimate_usd"] == pytest.approx(0.0399) and r["totals"]["complete"] is True
    assert r["totals"]["api_estimate_usd"] != pytest.approx(0.0299)                       # the old "everything is 30m" answer


def test_langchain_mapping_alone_loses_the_ttl_split_which_is_why_raw_usage_is_read():
    from langchain_openai.chat_models.base import _create_usage_metadata

    assert _create_usage_metadata(OFFICIAL_USAGE)["input_token_details"] == {"cache_read": 4000, "cache_creation": 7000}   # no 30m / 12h


def test_responses_api_shape_has_only_the_write_total_so_it_is_a_range_and_incomplete():
    from langchain_core.outputs import ChatGeneration, LLMResult
    from langchain_core.messages import AIMessage
    from langchain_openai.chat_models.base import _create_usage_metadata_responses

    um = _create_usage_metadata_responses(OFFICIAL_RESPONSES_USAGE)
    msg = AIMessage(content="hi", usage_metadata=um, response_metadata={"model_name": "gpt-6.1-sol", "service_tier": "default"})
    r = _meter_report(LLMResult(generations=[[ChatGeneration(message=msg)]]))
    ln = r["lines"][0]
    assert ln["cache_write_unsplit_tokens"] == 7000 and "cache_write_ttl" in ln["missing"]
    assert ln["usd"] == pytest.approx(0.0299) and ln["usd_max"] == pytest.approx(1000 * 2 / 1e6 + 4000 * 0.1 / 1e6 + 7000 * 5.0 / 1e6 + 1000 * 10 / 1e6)
    assert r["totals"]["complete"] is False and r["totals"]["api_estimate_usd_max"] > r["totals"]["api_estimate_usd"]
    assert "cache_write_ttl_unsplit_priced_as_range_30m_to_12h" in r["notes"]


def test_flex_prefixed_detail_keys_are_still_read():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult
    from langchain_openai.chat_models.base import _create_usage_metadata_responses

    raw = {**OFFICIAL_RESPONSES_USAGE, "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 0}}
    um = _create_usage_metadata_responses(raw, "flex")
    assert "flex_cache_read" in um["input_token_details"]                                  # langchain renamed the key
    msg = AIMessage(content="hi", usage_metadata=um, response_metadata={"model_name": "gpt-6.1-sol", "service_tier": "flex"})
    ln = _meter_report(LLMResult(generations=[[ChatGeneration(message=msg)]]))["lines"][0]
    assert ln["cached_input_tokens"] == 4000 and ln["reasoning_tokens"] == 200 and ln["price"]["tier"] == "flex"
    assert ln["usd"] == pytest.approx(8000 * 1.0 / 1e6 + 4000 * 0.05 / 1e6 + 1000 * 5.0 / 1e6)


def test_full_graph_call_without_usage_is_a_visible_gap_not_a_complete_report():
    from engine.full_debate import _TokenMeter

    m = _TokenMeter(None)
    m.ledger = UsageLedger(provider="openai")
    m.on_chat_model_start({}, [], run_id="a", metadata={"langgraph_node": "analyst"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_chat_model_start({}, [], run_id="b", metadata={"langgraph_node": "analyst"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_llm_end(_real_chat_llm_result(OFFICIAL_USAGE), run_id="a")
    m.on_llm_end(SimpleNamespace(generations=[[]], llm_output={}), run_id="b")               # finished, but no usage at all
    r = build_report(m.ledger, billing="api_key")
    assert sum(ln["calls"] for ln in r["lines"]) == 2                                        # not silently dropped
    assert any("usage_not_reported" in ln["missing"] for ln in r["lines"])
    assert r["totals"]["complete"] is False and "debate:usage_not_reported" in r["totals"]["incomplete_reasons"]


@pytest.mark.asyncio
async def test_live_debate_adapter_reporting_the_ttl_split_prices_each_ttl(monkeypatch):
    from engine.live_debate import MAX_AGENTS_PER_SESSION

    detail = {"cached": 0, "cache_write": 600, "cache_write_30m": 200, "cache_write_12h": 400, "reasoning": 0, "model": "gpt-6.1-sol", "service_tier": "default"}
    evs = await _run(monkeypatch, "api_key", detail)
    rep = next(e for e in evs if e["type"] == "session.complete")["cost_report"]
    per = 400 * 2.0 / 1e6 + 200 * 2.5 / 1e6 + 400 * 5.0 / 1e6 + 200 * 10.0 / 1e6
    assert rep["totals"]["api_estimate_usd"] == pytest.approx(per * MAX_AGENTS_PER_SESSION) and rep["totals"]["complete"] is True


def test_models_without_a_long_row_on_the_page_use_the_single_published_price():
    ln = build_report(_one(model="gpt-4.1", input_tokens=500_000, output_tokens=1_000, service_tier="default"), billing="api_key")["lines"][0]
    assert ln["context"] == "long" and ln["usd"] == pytest.approx(500_000 * 2.0 / 1e6 + 1_000 * 8.0 / 1e6)
    assert data.PRICES["gpt-4.1"]["standard"]["long"] == data.PRICES["gpt-4.1"]["standard"]["short"]
    assert data.PRICES["gpt-5.5-pro"]["flex"]["long"] is None                                  # a real "-" on the page stays unknown


def test_alias_priced_oauth_reference_is_labelled():
    r = build_report(_alias_oauth(), billing="oauth")
    assert "priced_via_undated_alias_of_snapshot" in r["notes"] and r["lines"][0]["price"]["resolved_via"] == "snapshot_of_alias"


def _alias_oauth():
    led = UsageLedger(provider="openai")
    led.record(stage="debate", agent="a", provider="openai", model_requested="gpt-6.1-sol", model_billed="gpt-6.1-sol-2026-10-01",
               input_tokens=100, output_tokens=10, cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0, service_tier="default", tier_reported=True)
    return led


def test_notes_for_every_assumption_reach_the_report():
    r = build_report(_one(model="gpt-5.5-pro", cached_input_tokens=100, service_tier=None, tier_reported=False), billing="api_key")
    assert "no_cached_input_price_billed_at_input_rate" in r["notes"] and "service_tier_not_reported_assumed_standard" in r["notes"]


def test_with_run_state_marks_an_existing_report_partial():
    from engine.cost_report import with_run_state

    done = build_report(_one(), billing="api_key")
    cut = with_run_state(done, "cancelled")
    assert done["totals"]["complete"] is True and cut["totals"]["complete"] is False
    assert cut["totals"]["api_estimate_usd"] == done["totals"]["api_estimate_usd"] and cut["run_state"] == "cancelled"
    assert with_run_state(None, "failed") is None


# ---- D19: a call with no usage is UNKNOWN, never $0 --------------------------------------------------------------------

def _no_usage_ledger(n=2, provider="openai", model="gpt-6.1-sol"):
    led = UsageLedger(provider=provider)
    for _ in range(n):
        led.record_no_usage(stage="debate", agent="analyst", provider=provider, model=model)
    return led


def test_all_calls_without_usage_is_unknown_not_zero_dollars():
    r = build_report(_no_usage_ledger(), billing="api_key")
    ln = r["lines"][0]
    assert ln["calls"] == 2 and ln["usd"] is None and ln["usd_basis"] is None and ln["unpriced_reason"] == "usage_not_reported"
    assert r["totals"]["api_estimate_usd"] is None and r["totals"]["complete"] is False                 # no measurable paid line: the total is unknown too
    assert "debate:usage_not_reported" in r["totals"]["incomplete_reasons"]


def test_oauth_with_no_usage_has_no_reference_and_local_keeps_its_zero_external_fee():
    o = build_report(_no_usage_ledger(), billing="oauth")
    assert o["totals"]["api_equivalent_reference_usd"] is None and o["totals"]["api_estimate_usd"] is None and o["lines"][0]["usd"] is None
    loc = build_report(_no_usage_ledger(provider="local", model="qwen"), billing="local")
    assert loc["totals"]["external_api_usd"] == 0.0 and loc["lines"][0]["usd"] == 0.0               # a local model has no external fee whatever it reports
    assert loc["totals"]["complete"] is False                                                        # but the missing usage is still flagged


def test_mixed_measured_and_unmeasured_lists_a_known_subtotal_and_says_it_is_a_lower_bound():
    led = _one()                                                   # one measured line: 0.3 USD
    led.record_no_usage(stage="debate", agent="other", provider="openai", model="gpt-6-luna")
    r = build_report(led, billing="api_key")
    by = {ln["agent"]: ln for ln in r["lines"]}
    assert by["other"]["usd"] is None and by["market"]["usd"] == pytest.approx(0.3)
    assert r["totals"]["api_estimate_usd"] == pytest.approx(0.3) and r["totals"]["complete"] is False
    same_line = _one(model_billed="gpt-6.1-sol", service_tier=None, tier_reported=False)           # same key: measured + unmeasured on ONE line
    same_line.record_no_usage(stage="debate", agent="market", provider="openai", model="gpt-6.1-sol")
    m = build_report(same_line, billing="api_key")["lines"][0]
    assert m["calls"] == 2 and m["usage_unreported_calls"] == 1 and m["usd"] is not None
    assert "usage_gap_subtotal_is_a_lower_bound" in m["notes"]


def test_raw_model_events_with_and_without_usage_reach_the_report():
    from engine.full_debate import _TokenMeter

    m = _TokenMeter(None)
    m.ledger = UsageLedger(provider="openai")
    m.on_chat_model_start({}, [], run_id="a", metadata={"langgraph_node": "analyst"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_chat_model_start({}, [], run_id="b", metadata={"langgraph_node": "trader"}, invocation_params={"model": "gpt-6.1-sol"})
    m.on_llm_end(SimpleNamespace(generations=[[]], llm_output={}), run_id="a")                    # only this one: no usage at all
    r = build_report(m.ledger, billing="api_key")
    assert r["totals"]["api_estimate_usd"] is None and r["lines"][0]["usd"] is None
    m.on_llm_end(_real_chat_llm_result(OFFICIAL_USAGE), run_id="b")
    r2 = build_report(m.ledger, billing="api_key")
    assert r2["totals"]["api_estimate_usd"] == pytest.approx(0.0399) and r2["totals"]["complete"] is False   # known part + visible gap


@pytest.mark.asyncio
async def test_live_debate_zero_token_calls_are_unknown_in_the_completed_report(monkeypatch):
    class Zero(_Adapter):
        async def complete(self, **kw):
            text, _i, _o = await super().complete(**kw)
            return text, 0, 0

    from engine.live_debate import ProviderConfig, SentimentBlock, live_debate

    monkeypatch.setattr("engine.live_debate.adapter_for", lambda _c: Zero(None))
    cfg = ProviderConfig(provider="openai", auth={"type": "api_key", "api_key": "sk-fake"}, model="gpt-6.1-sol", max_tokens=400)
    evs = [e async for e in live_debate(ticker="NVDA", trade_date="2026-05-15", summary=None, headlines=None, config=cfg, sentiment=SentimentBlock())]
    t = next(e for e in evs if e["type"] == "session.complete")["cost_report"]["totals"]
    assert t["api_estimate_usd"] is None and t["complete"] is False


# ---- D23: the History LIST knows the official-price digest (a subscription is not "$0.0000") --------------------------------

def _save(tmp_db, **o):
    d = dict(ticker="QCOM", trade_date="2026-10-30", events=[{"type": "session.complete"}], decision={"stance": "neutral", "conviction": 0.5, "reasoning": "r"},
             live=True, provider="openai", model="gpt-6.1-sol", input_tokens=165248, output_tokens=17087, estimated_cost_usd=0.0, auth_kind="oauth")
    d.update(o)
    return storage.write_session(**d)


def test_qcom_shaped_oauth_row_lists_an_api_equivalent_reference_not_zero(tmp_db):
    _save(tmp_db)
    row = storage.list_sessions()[0]
    assert row.estimated_cost_usd == 0.0 and row.auth_kind == "oauth"                       # budget accounting untouched
    cs = row.cost_summary
    assert cs["billing"] == "oauth" and cs["basis"] == "recomputed_current_list_price" and cs["complete"] is False
    assert cs["api_equivalent_reference_usd"] == pytest.approx((165248 * 2.0 + 17087 * 10.0) / 1e6) and cs["api_estimate_usd"] is None
    assert cs["verified_at"] == "2026-10-31" and cs["price_version"] == "openai-api-2026-10-31"


def test_oauth_row_without_an_exact_official_model_is_unknown_not_invented(tmp_db):
    _save(tmp_db, model="gpt-6.1-sol-codex-preview")                                         # no such alias on the price page: no price is made up
    cs = storage.list_sessions()[0].cost_summary
    assert cs["api_equivalent_reference_usd"] is None and cs["api_estimate_usd"] is None and cs["complete"] is False


def test_api_key_and_local_and_demo_rows(tmp_db):
    _save(tmp_db, auth_kind="api_key", model="gpt-5.5", ticker="AAPL", estimated_cost_usd=0.9)
    _save(tmp_db, auth_kind="local", provider="local", model="qwen", ticker="LOC", estimated_cost_usd=0.0)
    _save(tmp_db, live=False, provider=None, model=None, input_tokens=None, output_tokens=None, ticker="DEMO", auth_kind=None)
    by = {r.ticker: r for r in storage.list_sessions()}
    assert by["AAPL"].cost_summary["api_estimate_usd"] == pytest.approx((165248 * 5.0 + 17087 * 30.0) / 1e6) and by["AAPL"].estimated_cost_usd == 0.9
    assert by["LOC"].cost_summary["external_api_usd"] == 0.0 and by["LOC"].cost_summary["api_estimate_usd"] is None
    assert by["DEMO"].cost_summary is None


def test_a_stored_digest_wins_and_is_saved_from_session_complete(tmp_db):
    rep = build_report(_one(), billing="oauth")
    complete = {"type": "session.complete", "live": True, "provider": "openai", "model": "gpt-6.1-sol", "auth_kind": "oauth", "input_tokens": 100_000,
                "output_tokens": 10_000, "estimated_cost_usd": 0.0, "cost_report": rep,
                "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "r"}}
    saved = storage.write_session_from_events(ticker="QCOM", trade_date="2026-10-30", events=[complete])
    cs = next(r for r in storage.list_sessions() if r.id == saved).cost_summary
    assert cs["basis"] == "metered" and cs["complete"] is True and cs["api_equivalent_reference_usd"] == pytest.approx(0.3)
    assert storage.get_session(saved).cost_summary == cs


def test_existing_database_gets_the_additive_column_and_old_rows_still_list(tmp_db):
    import sqlite3

    with storage._connect() as conn:
        storage._ensure_initialized()
    _save(tmp_db)
    con = sqlite3.connect(storage.db_path())
    con.execute("ALTER TABLE sessions DROP COLUMN cost_summary")                              # an engine from before D23
    con.commit(); con.close()
    storage._reset_for_tests() if hasattr(storage, "_reset_for_tests") else None
    rows = storage.list_sessions()
    assert rows and rows[0].cost_summary["api_equivalent_reference_usd"] > 0                  # recomputed from columns; the list still works


# ---- D25 (Opus D24 B1): sessions saved by the PR21 engine (report, no digest column) show the MEASURED figure everywhere ---------

def _pr21_era_session(tmp_db):
    rep = build_report(_one(cached_input_tokens=60_000), billing="oauth")                    # measured, with a cache read: cheaper than all-uncached
    complete = {"type": "session.complete", "live": True, "provider": "openai", "model": "gpt-6.1-sol", "auth_kind": "oauth", "input_tokens": 100_000,
                "output_tokens": 10_000, "estimated_cost_usd": 0.0, "cost_report": rep, "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "r"}}
    sid = storage.write_session(ticker="QCOM", trade_date="2026-10-30", events=[{"type": "session.start"}, complete], decision=complete["decision"], live=True,
                                provider="openai", model="gpt-6.1-sol", input_tokens=100_000, output_tokens=10_000, estimated_cost_usd=0.0,
                                auth_kind="oauth")                                          # no cost_summary: what the merged PR21 engine wrote
    return sid, rep


def test_pr21_era_session_detail_header_matches_its_report_panel_even_before_any_backfill(tmp_db):
    sid, rep = _pr21_era_session(tmp_db)
    d = storage.detail_to_dict(storage.get_session(sid))
    assert d["cost_summary"]["basis"] == "metered" and d["cost_summary"]["complete"] is True
    assert d["cost_summary"]["api_equivalent_reference_usd"] == d["cost_report"]["totals"]["api_equivalent_reference_usd"] == rep["totals"]["api_equivalent_reference_usd"]
    assert d["cost_summary"]["api_equivalent_reference_usd"] < (100_000 * 2.0 + 10_000 * 10.0) / 1e6          # not the all-uncached recomputation


def test_migration_backfills_the_digest_from_the_saved_report_and_never_touches_accounting(tmp_db):
    import sqlite3

    sid, rep = _pr21_era_session(tmp_db)
    other = _save(tmp_db, ticker="OLD")                                                      # an older row with no report: stays NULL, recomputed on read
    storage._reset_for_tests()                                                               # "restart" on the same database: init runs again
    storage._ensure_initialized()
    con = sqlite3.connect(storage.db_path())
    stored = dict(con.execute("SELECT id, cost_summary FROM sessions").fetchall())
    acct = con.execute("SELECT estimated_cost_usd, input_tokens, output_tokens FROM sessions WHERE id=?", (sid,)).fetchone()
    con.close()
    assert stored[sid] is not None and stored[other] is None
    assert acct == (0.0, 100_000, 10_000)                                                    # accounting columns untouched
    by = {r.id: r for r in storage.list_sessions()}
    assert by[sid].cost_summary["basis"] == "metered" and by[sid].cost_summary["api_equivalent_reference_usd"] == pytest.approx(rep["totals"]["api_equivalent_reference_usd"])
    assert by[other].cost_summary["basis"] == "recomputed_current_list_price"
    storage._reset_for_tests()
    storage.list_sessions()                                                                  # running the backfill twice changes nothing
    assert {r.id: r.cost_summary for r in storage.list_sessions()}[sid]["basis"] == "metered"


def test_session_with_unknown_auth_kind_gets_no_dollar_figure():
    cs = cr.summary(cr.recompute_for_session({"type": "session.complete", "provider": "openai", "model": "gpt-5.5", "input_tokens": 1000, "output_tokens": 100}))
    assert cs["billing"] == "unknown" and cs["api_estimate_usd"] is None and cs["api_equivalent_reference_usd"] is None
