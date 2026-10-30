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


def test_cache_write_uses_write_rate_not_input_rate():
    r = build_report(_one(cached_input_tokens=0, cache_write_tokens=20_000), billing="api_key")["lines"][0]
    assert r["usd"] == pytest.approx(80_000 * 2.0 / 1e6 + 20_000 * 2.5 / 1e6 + 10_000 * 10.0 / 1e6)
    assert "cache_write_ttl_unreported_priced_at_30m_rate" in r["notes"]


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
    led = UsageLedger(provider="anthropic")
    led.record(stage="debate", agent="a", provider="anthropic", model_requested="claude-sonnet-5-5", model_billed="claude-sonnet-5-5", input_tokens=10, output_tokens=5)
    r = build_report(led, billing="api_key")
    assert r["lines"][0]["unpriced_reason"] == "official_price_not_imported_for_provider" and r["totals"]["api_estimate_usd"] is None


def test_dexter_is_its_own_line_and_added_to_the_total():
    base = build_report(_one(), billing="api_key")
    r = cr.add_dexter(base, billing="api_key", provider="openai", model="gpt-6.1-sol", ran=True,
                      raw_usage={"measured": True, "input_tokens": 100_000, "output_tokens": 10_000, "cached_tokens": 50_000, "reasoning_tokens": 4_000})
    stages = [ln["stage"] for ln in r["lines"]]
    assert stages == ["debate", "dexter"]
    dx = r["lines"][1]
    assert dx["usd"] == pytest.approx(50_000 * 2.0 / 1e6 + 50_000 * 0.1 / 1e6 + 10_000 * 10.0 / 1e6)
    assert r["totals"]["api_estimate_usd"] == pytest.approx(base["totals"]["api_estimate_usd"] + dx["usd"])
    assert r["totals"]["input_tokens"] == 200_000 and r["totals"]["complete"] is True


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
        um["input_token_details"] = {"cache_read": cache_read}
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
    assert usage_detail(chat, model="m", service_tier="default") == {"cached": 7, "cache_write": None, "reasoning": 3, "model": "m", "service_tier": "default"}
    resp = {"input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 2}}
    assert usage_detail(resp)["cached"] == 5 and usage_detail(resp)["reasoning"] == 2
    assert usage_detail(SimpleNamespace(prompt_tokens_details=None, completion_tokens_details=None)) == {
        "cached": None, "cache_write": None, "reasoning": None, "model": None, "service_tier": None}


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
async def test_live_debate_complete_carries_per_agent_official_report_and_keeps_ceiling_figures(monkeypatch):
    from engine.live_debate import MAX_AGENTS_PER_SESSION
    from engine.llm_providers import estimate_cost_or_none

    evs = await _run(monkeypatch, "api_key", {"cached": 400, "cache_write": None, "reasoning": 50, "model": "gpt-6.1-sol", "service_tier": "default"})
    comp = next(e for e in evs if e["type"] == "session.complete")
    rep = comp["cost_report"]
    assert len(rep["lines"]) == MAX_AGENTS_PER_SESSION and {ln["agent"] for ln in rep["lines"]} >= {"portfolio_manager"}
    assert rep["totals"]["input_tokens"] == 1000 * MAX_AGENTS_PER_SESSION and rep["totals"]["complete"] is True
    per = 600 * 2.0 / 1e6 + 400 * 0.1 / 1e6 + 200 * 10.0 / 1e6
    assert rep["totals"]["api_estimate_usd"] == pytest.approx(per * MAX_AGENTS_PER_SESSION)
    assert comp["estimated_cost_usd"] is None and comp["cost_known"] is False        # gpt-6.1-sol has no budget-ceiling row: untouched
    assert estimate_cost_or_none("gpt-6.1-sol", 1, 1) is None
    last_usage = [e for e in evs if e["type"] == "cost.usage"][-1]
    assert last_usage["official_cost"]["complete"] is False                            # running total is never called complete
    assert "lines" not in last_usage["official_cost"]


@pytest.mark.asyncio
async def test_live_debate_oauth_shows_reference_not_a_bill(monkeypatch):
    evs = await _run(monkeypatch, "oauth", {"cached": 0, "cache_write": None, "reasoning": 0, "model": "gpt-6.1-sol", "service_tier": None})
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
