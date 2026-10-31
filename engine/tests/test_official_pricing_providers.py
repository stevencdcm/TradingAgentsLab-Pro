"""Official price tables for Anthropic / xAI / Gemini (+ OpenAI unchanged): traceability to the saved page snapshots, cache categories,
prompt-length tier boundaries (per call), unknown stays unknown, exact-vs-range, and READ-TIME enrichment of saved reports."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from engine import cost_report as cr
from engine import official_pricing as op
from engine.cost_report import UsageLedger, build_report, enrich_report, exact_run_cost, recompute_for_session

ROOT = Path(__file__).resolve().parents[2]


def _gen(module: str, snapshot: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / "tools" / f"{module}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, (ROOT / "docs" / "pricing-snapshots" / snapshot).read_text(encoding="utf-8")


# ---- the committed data modules ARE what the saved official pages say --------------------------------------------------------


def test_anthropic_module_matches_the_saved_pricing_page():
    from engine import anthropic_price_data as d

    g, text = _gen("gen_anthropic_pricing", "anthropic-pricing-2026-10-31.md")
    data, long_from = g.parse(text)
    assert data == d.PRICES and long_from == d.LONG_FROM == {"claude-haiku-5-5": 100001}
    # spot values straight from the page text
    assert d.PRICES["claude-opus-4-8"]["standard"]["short"] == {"input": 5.0, "write_30m": 6.25, "write_12h": 10.0, "cached_input": 0.5, "output": 25.0}
    assert d.PRICES["claude-fable-5-1"]["standard"]["short"]["cached_input"] == 0.25          # 0.025x
    assert d.PRICES["claude-opus-5-5"]["standard"]["short"] == {"input": 4.0, "write_30m": 5.0, "write_12h": 8.0, "cached_input": 0.2, "output": 20.0}
    assert d.PRICES["claude-haiku-5-5"]["standard"]["long"] == {"input": 0.5, "write_30m": 0.625, "write_12h": 1.0, "cached_input": 0.05, "output": 2.5}


def test_xai_module_matches_the_saved_models_page():
    from engine import xai_price_data as d

    g, text = _gen("gen_xai_pricing", "xai-models-2026-10-31.md")
    data, long_from = g.parse(text)
    assert data == d.PRICES and long_from == d.LONG_FROM
    assert d.PRICES["grok-4.3"]["standard"]["short"]["input"] == 1.25 and d.PRICES["grok-4.3"]["standard"]["long"]["output"] == 5.0
    assert "grok-4.20-reasoning" not in d.PRICES                         # the bare alias is not on the page: never priced by inference


def test_gemini_module_matches_the_saved_pricing_page():
    from engine import gemini_price_data as d

    g, text = _gen("gen_gemini_pricing", "gemini-pricing-2026-10-31.html")
    data, long_from = g.parse(text)
    assert data == d.PRICES and long_from == d.LONG_FROM == {"gemini-2.5-pro": 200001}
    assert d.PRICES["gemini-2.5-pro"]["standard"]["long"]["output"] == 15.0
    assert "gemini-2.0-flash" not in d.PRICES                            # not on today's page: unknown, not guessed


# ---- Anthropic pricing incl. cache categories ---------------------------------------------------------------------------------


def _usd(model, **kw):
    kw.setdefault("provider", "anthropic")
    kw.setdefault("service_tier", None)
    kw.setdefault("context", "short")
    kw.setdefault("cached_input_tokens", 0)
    return op.price_usage(model=model, **kw)


def test_anthropic_plain_and_cache_categories():
    r = _usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000)
    assert r["usd"] == pytest.approx(4.0 + 2.0) and r["price_model_id"] == "claude-opus-5-5"
    # 1M prompt tokens = 600K plain + 300K cache read + 100K 5m write; output 0
    r = _usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=0, cached_input_tokens=300_000, cache_write_30m_tokens=100_000)
    assert r["usd"] == pytest.approx(0.6 * 4.0 + 0.3 * 0.20 + 0.1 * 5.0)
    r = _usd("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0, cache_write_12h_tokens=1_000_000)
    assert r["usd"] == pytest.approx(4.0)                                # 1h write = 2x input
    r = _usd("claude-fable-5-1", input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000)
    assert r["usd"] == pytest.approx(0.25)                               # 0.025x read on Fable 5.1
    r = _usd("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000)
    assert r["usd"] == pytest.approx(0.30)                               # the standard 0.1x on older models


def test_anthropic_unsplit_cache_write_is_a_5m_to_1h_range():
    r = _usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=0, cache_write_unsplit_tokens=1_000_000)
    assert r["usd"] == pytest.approx(5.0) and r["usd_max"] == pytest.approx(8.0)


def test_anthropic_only_standard_is_priced_other_tiers_are_unknown_not_standard():
    for tier in ("batch", "priority", "fast"):
        r = _usd("claude-opus-5-5", input_tokens=1000, output_tokens=1000, service_tier=tier)
        assert r["usd"] is None and r["reason"] == "unsupported_service_tier"
    assert _usd("claude-opus-5-5", input_tokens=10, output_tokens=10, service_tier="standard")["usd"] is not None


def test_dated_snapshot_resolves_to_its_alias_and_is_labelled_anything_else_is_unknown():
    r = _usd("claude-sonnet-4-5-20250929", input_tokens=1_000_000, output_tokens=0)
    assert r["usd"] == pytest.approx(3.0) and r["resolved_via"] == "snapshot_of_alias" and "priced_via_undated_alias_of_snapshot" in r["notes"]
    for unknown in ("claude-opus-9-9", "claude-sonnet", ""):
        assert _usd(unknown, input_tokens=1, output_tokens=1)["usd"] is None


# ---- prompt-length tiers, boundaries, per call --------------------------------------------------------------------------------


@pytest.mark.parametrize("provider,model,below,at", [
    ("anthropic", "claude-haiku-5-5", 100_000, 100_001),     # "up to 100,000" short, "over 100,000" long
    ("xai", "grok-4.3", 199_999, 200_000),                   # "< 200k" short, "reaches 200k" long
    ("gemini", "gemini-2.5-pro", 200_000, 200_001),          # "<= 200k" short, "> 200k" long
    ("openai", "gpt-5.5", 272_000, 272_001),                 # unchanged
])
def test_context_boundaries(provider, model, below, at):
    assert op.context_bucket(below, model, provider) == "short"
    assert op.context_bucket(at, model, provider) == "long"


def test_models_without_a_threshold_never_enter_a_long_tier():
    assert op.context_bucket(5_000_000, "claude-opus-5-5", "anthropic") == "short"
    assert op.context_bucket(5_000_000, "gemini-2.5-flash", "gemini") == "short"


def test_haiku_tier_boundary_prices_and_cache_counts_toward_the_threshold():
    short = _usd("claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000_000, context="short")
    long_ = _usd("claude-haiku-5-5", input_tokens=100_001, output_tokens=1_000_000, context="long")
    assert short["usd"] == pytest.approx(100_000 * 0.10 / 1e6 + 0.50)
    assert long_["usd"] == pytest.approx(100_001 * 0.50 / 1e6 + 2.50)
    # the ledger decides the bucket from the WHOLE prompt (input incl. cache reads/writes) of each call
    led = UsageLedger(provider="anthropic")
    led.record(agent="a", provider="anthropic", model_requested="claude-haiku-5-5", model_billed="claude-haiku-5-5", input_tokens=40_000,
               cached_input_tokens=60_000 + 1, cache_write_tokens=0, output_tokens=10)   # prompt 40_000 -> input includes cache: see below
    assert {ln.context for ln in led.lines()} == {"short"}
    led2 = UsageLedger(provider="anthropic")
    led2.record(agent="a", provider="anthropic", model_requested="claude-haiku-5-5", model_billed="claude-haiku-5-5", input_tokens=100_001,
                cached_input_tokens=100_001, cache_write_tokens=0, output_tokens=10)
    assert {ln.context for ln in led2.lines()} == {"long"}                   # a request mostly served from cache still pays the long tier


def test_each_call_is_priced_at_its_own_tier_in_one_report():
    led = UsageLedger(provider="anthropic")
    for tokens in (60_000, 100_001):
        led.record(agent="a", provider="anthropic", model_requested="claude-haiku-5-5", model_billed="claude-haiku-5-5", input_tokens=tokens,
                   cached_input_tokens=0, cache_write_tokens=0, cache_write_30m_tokens=0, cache_write_12h_tokens=0, reasoning_tokens=0,
                   output_tokens=1000, service_tier="standard", tier_reported=True)
    rep = build_report(led, billing="api_key")
    assert sorted(ln["context"] for ln in rep["lines"]) == ["long", "short"]
    want = (60_000 * 0.10 + 1000 * 0.50) / 1e6 + (100_001 * 0.50 + 1000 * 2.50) / 1e6
    assert rep["totals"]["api_estimate_usd"] == pytest.approx(want, abs=2e-6)
    assert rep["totals"]["complete"] is True and exact_run_cost(rep) == pytest.approx(want, abs=2e-6)


# ---- unknown stays unknown / billing bases -------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider,model", [("xai", "grok-4.20-reasoning"), ("gemini", "gemini-2.0-flash"), ("minimax", "MiniMax-M2.7"),
                                           ("anthropic", "claude-opus-9-9"), ("openrouter", "openai/gpt-4o-mini")])
def test_unresolved_models_are_unknown_never_zero(provider, model):
    led = UsageLedger(provider=provider)
    led.record(agent="a", provider=provider, model_requested=model, model_billed=model, input_tokens=1000, output_tokens=100,
               cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0)
    rep = build_report(led, billing="api_key")
    assert rep["totals"]["api_estimate_usd"] is None and rep["lines"][0]["usd"] is None and exact_run_cost(rep) is None


def test_billing_bases_stay_separate_for_anthropic_lines():
    def mk(billing):
        led = UsageLedger(provider="anthropic")
        led.record(agent="a", provider="anthropic", model_requested="claude-opus-5-5", model_billed="claude-opus-5-5", input_tokens=1_000_000,
                   output_tokens=0, cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0)
        return build_report(led, billing=billing)

    api, oauth, local = mk("api_key"), mk("oauth"), mk("local")
    assert api["totals"]["api_estimate_usd"] == pytest.approx(4.0) and api["totals"]["api_equivalent_reference_usd"] is None
    assert oauth["totals"]["api_estimate_usd"] is None and oauth["totals"]["api_equivalent_reference_usd"] == pytest.approx(4.0)
    assert local["totals"]["external_api_usd"] == 0.0 and local["totals"]["api_estimate_usd"] is None
    assert exact_run_cost(oauth) is None and exact_run_cost(local) is None                  # only an API-key bill is an exact cost


def test_a_usage_gap_is_never_exact():
    led = UsageLedger(provider="anthropic")
    led.record(agent="a", provider="anthropic", model_requested="claude-opus-5-5", model_billed="claude-opus-5-5", input_tokens=100, output_tokens=10,
               cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0)
    led.record_no_usage(stage="debate", agent="b", provider="anthropic", model="claude-opus-5-5")
    assert exact_run_cost(build_report(led, billing="api_key")) is None


# ---- read-time enrichment of SAVED reports -------------------------------------------------------------------------------------


def _old_line(model="claude-opus-5-5", provider="anthropic", **kw):
    ln = {"stage": "debate", "agent": "market_analyst", "provider": provider, "model": model, "model_requested": model, "calls": 2,
          "input_tokens": 1_000_000, "cached_input_tokens": 0, "uncached_input_tokens": 1_000_000, "cache_write_tokens": 0,
          "cache_write_30m_tokens": 0, "cache_write_12h_tokens": 0, "cache_write_unsplit_tokens": 0, "output_tokens": 100_000,
          "reasoning_tokens": None, "usage_unreported_calls": 0, "context": "short", "usd": None, "usd_max": None, "usd_basis": None,
          "price": None, "unpriced_reason": "official_price_not_imported_for_provider", "missing": ["reasoning_tokens"]}
    ln.update(kw)
    return ln


def _saved(lines, billing="api_key"):
    return cr._assemble(lines, billing=billing, run_state="completed", basis="metered", extra=[])


def test_enrichment_prices_unpriced_lines_labels_them_and_never_mutates_the_saved_report():
    saved = _saved([_old_line()])
    snapshot = copy.deepcopy(saved)
    new = enrich_report(saved)
    assert saved == snapshot                                                       # immutable original
    assert new is not saved and new["basis"] == "recomputed_current_list_price"
    assert new["lines"][0]["usd"] == pytest.approx(4.0 + 2.0) and new["lines"][0]["historical_recompute"] is True
    assert new["price"]["providers"]["anthropic"]["verified_at"] == "2026-10-31" and "anthropic-api-2026-10-31" in new["price"]["version"]
    assert new["totals"]["api_estimate_usd"] == pytest.approx(6.0)


def test_enrichment_leaves_stored_prices_and_unsupported_providers_alone():
    nothing = _saved([_old_line(provider="minimax", model="MiniMax-M2.7")])
    assert enrich_report(nothing) is nothing                                       # no table for MiniMax: same object back
    priced = _old_line(unpriced_reason=None, usd=1.23, usd_basis="official_api_estimate", price={"model_id": "x"}, missing=[])
    keep = _saved([priced])
    assert enrich_report(keep) is keep
    mixed = enrich_report(_saved([priced, _old_line(agent="trader")]))
    assert mixed["lines"][0]["usd"] == 1.23 and "historical_recompute" not in mixed["lines"][0]       # earlier recorded figure preserved
    assert mixed["lines"][1]["historical_recompute"] is True


def test_enrichment_of_a_tiered_model_is_a_range_flagged_incomplete_not_an_exact_zero_or_ceiling():
    new = enrich_report(_saved([_old_line(model="claude-haiku-5-5", input_tokens=200_000, output_tokens=20_000, calls=4)]))
    ln = new["lines"][0]
    low = (200_000 * 0.10 + 20_000 * 0.50) / 1e6
    high = (200_000 * 0.50 + 20_000 * 2.50) / 1e6
    assert ln["usd"] == pytest.approx(low, abs=1e-6) and ln["usd_max"] == pytest.approx(high, abs=1e-6) and "price_tier" in ln["missing"]
    assert new["totals"]["complete"] is False and exact_run_cost(new) is None
    assert 0 < ln["usd"] < ln["usd_max"]


def test_enrichment_keeps_missing_usage_as_incomplete_not_complete():
    new = enrich_report(_saved([_old_line(), _old_line(agent="b", usd=None, input_tokens=0, output_tokens=0, calls=1, usage_unreported_calls=1,
                                                       unpriced_reason="usage_not_reported", missing=["usage_not_reported"])]))
    assert new["totals"]["complete"] is False and any("usage_not_reported" in r for r in new["totals"]["incomplete_reasons"])


def test_oauth_enrichment_is_an_api_equivalent_reference_only():
    new = enrich_report(_saved([_old_line()], billing="oauth"))
    assert new["totals"]["api_estimate_usd"] is None and new["totals"]["api_equivalent_reference_usd"] == pytest.approx(6.0)
    assert any("subscription_no_extra_api_billing" in n for n in new["notes"])


def test_local_reports_are_never_enriched():
    rep = _saved([_old_line(provider="local", model="qwen")], billing="local")
    assert enrich_report(rep) is rep


def test_recompute_for_a_report_less_session_prices_exact_models_and_ranges_tiered_ones():
    opus = recompute_for_session({"provider": "anthropic", "auth_kind": "api_key", "model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 100_000})
    assert opus["totals"]["api_estimate_usd"] == pytest.approx(6.0) and opus["basis"] == "recomputed_current_list_price"
    assert opus["totals"]["complete"] is False                                     # cache/model split unknown for a saved total
    haiku = recompute_for_session({"provider": "anthropic", "auth_kind": "api_key", "model": "claude-haiku-5-5", "input_tokens": 500_000, "output_tokens": 50_000})
    ln = haiku["lines"][0]
    assert ln["usd"] < ln["usd_max"] and "price_tier" in ln["missing"]


# ---- storage read path (History list + detail) -----------------------------------------------------------------------------------


def test_history_list_and_detail_enrich_at_read_time_without_writing(tmp_db):
    from engine import storage

    rep = _saved([_old_line()])
    complete = {"type": "session.complete", "provider": "anthropic", "model": "claude-opus-5-5", "auth_kind": "api_key", "live": True,
                "input_tokens": 1_000_000, "output_tokens": 100_000, "estimated_cost_usd": 9.99, "cost_report": rep}
    sid = storage.write_session(ticker="AAPL", trade_date="2026-10-01", events=[complete], decision={"action": "neutral", "confidence": 0.5, "reasoning": "x"},
                               live=True, provider="anthropic", model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000,
                               estimated_cost_usd=9.99, auth_kind="api_key", cost_summary=cr.summary(rep))
    row = storage.list_sessions()[0]
    assert row.cost_summary["basis"] == "recomputed_current_list_price" and row.cost_summary["api_estimate_usd"] == pytest.approx(6.0)
    assert row.estimated_cost_usd == 9.99                                           # the recorded accounting figure is untouched
    detail = storage.detail_to_dict(storage.get_session(sid))
    assert detail["cost_report"]["totals"]["api_estimate_usd"] == pytest.approx(6.0)
    assert detail["cost_summary"]["api_estimate_usd"] == pytest.approx(6.0)        # header agrees with the panel
    import sqlite3

    raw = sqlite3.connect(str(tmp_db)).execute("SELECT events_json, cost_summary, estimated_cost_usd FROM sessions WHERE id = ?", (sid,)).fetchone()
    assert json.loads(raw[0])[0]["cost_report"]["lines"][0]["usd"] is None          # stored events not rewritten
    assert json.loads(raw[1])["api_estimate_usd"] is None and raw[2] == 9.99          # stored digest and cost not rewritten


# ---- usage normalisation of the adapters --------------------------------------------------------------------------------------


def test_gemini_adapter_counts_thinking_tokens_as_output_and_reports_cache():
    import asyncio
    from types import SimpleNamespace

    from engine.llm_providers import GeminiAdapter

    usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=30, cached_content_token_count=40)
    resp = SimpleNamespace(text="ok", usage_metadata=usage)
    a = GeminiAdapter()
    a._client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_k: resp))
    text, tin, tout = asyncio.run(a.complete(system="s", user="u", model="gemini-2.5-pro", max_tokens=10))
    assert (text, tin, tout) == ("ok", 100, 50)                                        # 20 visible + 30 thinking, all billed as output
    assert a.last_usage["cached"] == 40 and a.last_usage["reasoning"] == 30


def test_anthropic_usage_detail_reads_cache_fields_and_ttl_split():
    from types import SimpleNamespace

    from engine.llm_providers import usage_detail

    u = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=9,
                        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=4, ephemeral_1h_input_tokens=5), service_tier="standard")
    d = usage_detail(u)
    assert (d["cached"], d["cache_write"], d["cache_write_30m"], d["cache_write_12h"], d["service_tier"]) == (7, 9, 4, 5, "standard")
