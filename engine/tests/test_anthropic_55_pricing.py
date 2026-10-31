"""Claude 5.5 price rows (platform pricing page, verified 2026-10-31) and the 'unknown stays unknown' rule for models
the catalog discovers but the price table does not carry."""

from engine import llm_providers as lp


def test_55_rows_match_the_published_rates_or_a_higher_ceiling():  # Haiku row = ceiling only, see below
    assert lp._COST_PER_M_TOKENS["claude-opus-5-5"] == {"input": 4.0, "output": 20.0}
    assert lp._COST_PER_M_TOKENS["claude-sonnet-5-5"] == {"input": 2.0, "output": 10.0}
    assert lp._COST_PER_M_TOKENS["claude-fable-5-1"] == {"input": 10.0, "output": 50.0}
    # Haiku 5.5 is tiered; the table must carry the dearer (>100K prompt) tier so a reservation never under-counts.
    assert lp._COST_PER_M_TOKENS["claude-haiku-5-5"] == {"input": 0.5, "output": 2.5}


def test_estimate_uses_the_row_and_worst_case_takes_the_dearer_rate():
    assert lp.estimate_cost_or_none("claude-opus-5-5", 1_000_000, 100_000) == 4.0 + 2.0
    assert lp.worst_case_job_cost("claude-opus-5-5", 1_000_000) == 20.0


def test_a_discovered_model_without_an_exact_price_is_unknown_not_zero():
    for unknown in ("claude-opus-5-6", "claude-future-9", ""):
        assert lp.is_priced(unknown) is False
        assert lp.estimate_cost_or_none(unknown, 1000, 1000) is None
        assert lp.worst_case_job_cost(unknown, 1000) is None


def test_haiku_55_is_a_ceiling_for_costguard_but_never_a_reported_price():
    m = "claude-haiku-5-5"
    assert lp.has_cost_ceiling(m) is True                    # CostGuard can still bound the spend
    assert lp.worst_case_job_cost(m, 1_000_000) == 2.5       # reservation uses the dearer (>100K tier) rate
    assert lp.is_priced(m) is False                          # ...but it is not an exact price
    assert lp.estimate_cost_or_none(m, 1_000_000, 1_000_000) is None
    assert lp.estimate_cost(m, 1_000_000, 1_000_000) == 0.0  # the "no exact figure" path, never the 0.50/2.50 ceiling


def test_haiku_55_cost_guard_still_treats_it_as_bounded_not_unpriced():
    from engine import cost_guard

    assert cost_guard.unpriced_paid("claude-haiku-5-5", "api_key") is False
    assert cost_guard.unpriced_paid("claude-opus-9-9", "api_key") is True


def test_exact_models_stay_priced():
    for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-sonnet-4-6", "gpt-5.5"):
        assert lp.is_priced(m) and lp.has_cost_ceiling(m)


def test_running_usage_ticks_are_null_for_unpriced_models_and_numbers_for_priced_ones(monkeypatch):
    """P2: the in-run cost.usage event must not show $0.0000 for a model whose price is unknown."""
    import asyncio

    from engine.live_debate import live_debate
    from engine.llm_providers import ProviderConfig
    from engine.tests.fake_anthropic import FakeAnthropic

    key = "sk-ant-test-key-1234567890"
    srv = FakeAnthropic(key)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", srv.url)
    try:
        def ticks(model):
            cfg = ProviderConfig.from_dict({"provider": "anthropic", "auth": {"type": "api_key", "api_key": key}, "model": model})

            async def go():
                return [e async for e in live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg)]

            return [e["est_cost_usd"] for e in asyncio.run(go()) if e["type"] == "cost.usage"]

        assert ticks("claude-opus-9-9") and all(t is None for t in ticks("claude-opus-9-9"))      # no price row at all
        assert ticks("claude-haiku-5-5") and all(t is None for t in ticks("claude-haiku-5-5"))    # ceiling-only
        assert all(isinstance(t, float) and t > 0 for t in ticks("claude-sonnet-5-5"))            # exact price: a real number
    finally:
        srv.close()
