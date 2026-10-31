"""Ceiling-only models (Claude Haiku 5.5, tiered price): the REPORTED cost is unknown, but the USD caps must keep counting the
run. Releasing the reservation as NULL made daily/weekly/monthly spend stay 0 forever (review D37R B2)."""

from __future__ import annotations

import asyncio

import pytest

from engine import cost_guard
from engine.cost_guard import CostGuardBlocked, finalize_reservation, get_state, initialize, reserve, settle_run_end, update_config
from engine.llm_providers import ProviderConfig, ceiling_cost
from engine.tests.fake_anthropic import FakeAnthropic

M = "claude-haiku-5-5"
KEY = "sk-ant-test-key-1234567890"


def _res(model=M):
    return reserve(model=model, auth_kind="api_key", max_tokens=400)


def test_ceiling_hold_counts_the_measured_bound_in_all_windows(tmp_db):
    initialize()
    r = _res()
    bound = ceiling_cost(M, 100_000, 20_000)
    assert finalize_reservation(r.reservation_id, actual_cost_usd=None, hold_ceiling=True, ceiling_cost_usd=bound)
    spend, _ = get_state()
    assert spend.daily_usd == spend.weekly_usd == spend.monthly_usd == pytest.approx(bound)


def test_without_hold_a_null_settlement_counts_nothing_the_bug_this_prevents(tmp_db):
    initialize()
    r = _res()
    finalize_reservation(r.reservation_id, actual_cost_usd=None)
    assert get_state()[0].daily_usd == 0.0


def test_no_computable_bound_holds_the_reserved_estimate(tmp_db):
    initialize()
    r = _res()
    finalize_reservation(r.reservation_id, actual_cost_usd=None, hold_ceiling=True, ceiling_cost_usd=None)
    assert get_state()[0].daily_usd == pytest.approx(r.est_cost_usd)


def test_a_measured_zero_bound_releases_the_hold(tmp_db):
    initialize()
    r = _res()
    finalize_reservation(r.reservation_id, actual_cost_usd=None, hold_ceiling=True, ceiling_cost_usd=0.0)
    assert get_state()[0].daily_usd == 0.0


def test_interrupted_run_is_held_at_the_reserved_estimate(tmp_db):
    """Engine restart / crash: run_manager settles with billable_started and no finalize ever ran."""
    initialize()
    r = _res()
    settle_run_end(r.reservation_id, billable_started=True)
    assert get_state()[0].daily_usd == pytest.approx(r.est_cost_usd)


def test_cancelled_run_keeps_its_measured_bound_after_run_manager_settles_it(tmp_db):
    initialize()
    r = _res()
    bound = ceiling_cost(M, 5_000, 1_000)
    finalize_reservation(r.reservation_id, actual_cost_usd=None, hold_ceiling=True, ceiling_cost_usd=bound)
    settle_run_end(r.reservation_id, billable_started=True)       # run_manager marks non-completed runs 'incomplete'
    assert get_state()[0].daily_usd == pytest.approx(bound)


def test_exact_priced_models_are_unchanged(tmp_db):
    initialize()
    r = _res("claude-sonnet-5-5")
    finalize_reservation(r.reservation_id, actual_cost_usd=0.01)
    assert get_state()[0].daily_usd == 0.0                         # counted through the sessions table, as before


# ---- end to end through live_debate: completed, cancelled, and the N-th run is refused --------------------------------


@pytest.fixture
def srv(monkeypatch):
    s = FakeAnthropic(KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
    yield s
    s.close()


def _cfg():
    return ProviderConfig.from_dict({"provider": "anthropic", "auth": {"type": "api_key", "api_key": KEY}, "model": M})


async def _run(rid, *, stop_after=None):
    from engine.live_debate import live_debate

    events = []
    gen = live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=_cfg(), reservation_id=rid, report_language="zh-TW")
    try:
        async for e in gen:
            events.append(e)
            if stop_after and len(events) >= stop_after:
                break
    finally:
        await gen.aclose()
    return events


def test_completed_runs_fill_the_cap_and_the_nth_is_refused_while_cost_stays_unknown(tmp_db, srv):
    initialize()
    est = cost_guard.worst_case_reservation(model=M, auth_kind="api_key", max_tokens=400)
    # One fake run = 12 calls x (12 in, 7 out) tokens.
    per_run = ceiling_cost(M, 12 * 12, 12 * 7)
    update_config(enabled=True, cap_daily_usd=est + 2.5 * per_run, cap_weekly_usd=0, cap_monthly_usd=0, cap_sessions_per_day=0)
    allowed = 0
    for _ in range(10):
        try:
            r = _res()
        except CostGuardBlocked as exc:
            assert exc.over_dimension == "daily"
            break
        events = asyncio.run(_run(r.reservation_id))
        done = next(e for e in events if e["type"] == "session.complete")
        assert done["estimated_cost_usd"] is None and done["cost_known"] is False       # still unknown on screen / in History
        allowed += 1
    else:
        pytest.fail("the daily cap never refused a Haiku 5.5 run: spend is being released as zero")
    assert allowed == 3 and get_state()[0].daily_usd == pytest.approx(3 * per_run)


def test_a_cancelled_run_is_held_at_what_it_measured(tmp_db, srv):
    initialize()
    r = _res()
    events = asyncio.run(_run(r.reservation_id, stop_after=4))     # client goes away mid-run
    assert not any(e["type"] == "session.complete" for e in events)
    spend = get_state()[0].daily_usd
    assert 0 < spend < r.est_cost_usd                              # its measured bound, not released and not the whole estimate


# ---- Pro (full graph) finalize ----------------------------------------------------------------------------------------


class _Meter:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


def test_pro_finalize_holds_the_ceiling_for_haiku_and_not_for_unrelated_unknowns(tmp_db):
    from engine import full_debate as fd

    initialize()
    merged = {"deep_think_llm": M, "quick_think_llm": M}
    r = _res()
    fd._finalize_reservation(r.reservation_id, "api_key", merged, _Meter(200_000, 30_000))
    assert get_state()[0].daily_usd == pytest.approx(ceiling_cost(M, 200_000, 30_000))
    # deep Opus 5.5 (exact price) + quick Haiku: bounded by the dearer client's rate
    mixed = {"deep_think_llm": "claude-opus-5-5", "quick_think_llm": M}
    ok, bound = fd._ceiling_hold("api_key", mixed, _Meter(1_000_000, 0))
    assert ok and bound == pytest.approx(4.0)
    # a model with NO row keeps the old unknown path (it needs an explicit override), no ceiling hold
    assert fd._ceiling_hold("api_key", {"deep_think_llm": "claude-opus-9-9", "quick_think_llm": M}, _Meter(1, 1)) == (False, None)
    assert fd._ceiling_hold("oauth", merged, _Meter(1, 1)) == (False, None)
    assert fd._ceiling_hold("api_key", {"deep_think_llm": "claude-opus-5-5", "quick_think_llm": "claude-opus-5-5"}, _Meter(1, 1)) == (False, None)


# ---- missing usage vs a true zero (review: usage gaps must not release the hold) -----------------------------------------


class _ScriptAdapter:
    def __init__(self, script):
        self._script = list(script)

    async def open(self, *, api_key: str) -> None:
        pass

    async def close(self) -> None:
        pass

    async def complete(self, **_kw):
        item = self._script.pop(0) if self._script else ("ok", 10, 5)
        if isinstance(item, BaseException):
            raise item
        return item


async def _drain_live(monkeypatch, rid, script):
    from engine import live_debate

    monkeypatch.setattr(live_debate, "adapter_for", lambda _c: _ScriptAdapter(script))
    return [e async for e in live_debate.live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None,
                                                    config=_cfg(), reservation_id=rid)]


def test_a_call_without_usage_numbers_keeps_the_reserved_estimate_held(tmp_db, monkeypatch):
    initialize()
    r = _res()
    script = [("ok", 10, 5)] * 3 + [("no usage", 0, 0)] + [("ok", 10, 5)] * 8    # one finished call reported nothing
    asyncio.run(_drain_live(monkeypatch, r.reservation_id, script))
    assert get_state()[0].daily_usd == pytest.approx(r.est_cost_usd)               # MISSING data: not the (low) measured bound


def test_complete_usage_is_held_at_the_measured_bound_not_the_estimate(tmp_db, monkeypatch):
    initialize()
    r = _res()
    asyncio.run(_drain_live(monkeypatch, r.reservation_id, [("ok", 10, 5)] * 12))
    assert get_state()[0].daily_usd == pytest.approx(ceiling_cost(M, 12 * 10, 12 * 5))
    assert get_state()[0].daily_usd < r.est_cost_usd


def test_a_true_zero_nothing_ran_releases_the_hold(tmp_db, monkeypatch):
    initialize()
    r = _res()
    asyncio.run(_drain_live(monkeypatch, r.reservation_id, [RuntimeError("provider down")] * 12))
    assert get_state()[0].daily_usd == 0.0


def test_pro_missing_usage_vs_true_zero(tmp_db):
    from engine import full_debate as fd
    from engine.cost_report import UsageLedger

    merged = {"deep_think_llm": M, "quick_think_llm": M}

    class Meter(_Meter):
        def __init__(self, i, o, calls=0, inflight=0, ledger=None):
            super().__init__(i, o)
            self.calls, self.inflight, self.ledger = calls, inflight, ledger

    assert fd._ceiling_hold("api_key", merged, Meter(0, 0, calls=0)) == (True, 0.0)            # no call ever happened: true zero
    assert fd._ceiling_hold("api_key", merged, Meter(0, 0, calls=3)) == (True, None)           # calls finished, no usage: missing
    assert fd._ceiling_hold("api_key", merged, Meter(500, 50, calls=2, inflight=1)) == (True, None)   # unmetered call in flight
    gap = UsageLedger(provider="anthropic")
    gap.record_no_usage(stage="debate", agent="a", provider="anthropic", model=M)
    assert fd._ceiling_hold("api_key", merged, Meter(500, 50, calls=2, ledger=gap)) == (True, None)
    ok = UsageLedger(provider="anthropic")
    ok.record(stage="debate", agent="a", provider="anthropic", model_requested=M, model_billed=M, input_tokens=500, output_tokens=50)
    hold, bound = fd._ceiling_hold("api_key", merged, Meter(500, 50, calls=1, ledger=ok))
    assert hold and bound == pytest.approx(ceiling_cost(M, 500, 50))
