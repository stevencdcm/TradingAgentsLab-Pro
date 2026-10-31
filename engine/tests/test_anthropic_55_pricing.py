"""Claude 5.5 price rows (platform pricing page, verified 2026-10-31) and the 'unknown stays unknown' rule for models
the catalog discovers but the price table does not carry."""

from engine import llm_providers as lp


def test_55_rows_match_the_published_rates_or_a_higher_ceiling():
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
