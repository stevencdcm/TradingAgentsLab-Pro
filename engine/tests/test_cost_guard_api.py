"""Cost Guard — HTTP endpoint integration tests.

Drives the FastAPI app via TestClient (no real socket; in-process). Covers
the four endpoints (GET /cost-guard/state, PUT /cost-guard/config,
POST /cost-guard/check, POST /cost-guard/reserve) plus the WS handler's
auto-reserve fall-through behavior.

The WS-side reservation finalization is exercised indirectly here: we
verify that a stub debate run does NOT touch reservations (no provider_config
means the live path is skipped entirely).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from engine import cost_guard
from engine.server import build_app


TOKEN = "test-token-1234"


@pytest.fixture
def client(tmp_db):
    """Authenticated TestClient bound to a fresh per-test DB."""
    app = build_app(token=TOKEN)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


# ---- /cost-guard/state ------------------------------------------------------


def test_state_returns_defaults_on_fresh_db(client):
    r = client.get("/cost-guard/state")
    assert r.status_code == 200
    data = r.json()
    assert data["spend"]["daily_usd"] == 0
    assert data["spend"]["weekly_usd"] == 0
    assert data["spend"]["monthly_usd"] == 0
    assert data["spend"]["sessions_today"] == 0
    assert data["config"]["enabled"] is True
    assert data["config"]["cap_daily_usd"] == 1.00
    assert data["config"]["cap_weekly_usd"] == 5.00
    assert data["config"]["cap_monthly_usd"] == 15.00


def test_state_requires_bearer_token(tmp_db):
    app = build_app(token=TOKEN)
    with TestClient(app) as c:
        r = c.get("/cost-guard/state")  # no Authorization header
        assert r.status_code == 401


# ---- /cost-guard/config -----------------------------------------------------


def test_config_update_partial(client):
    r = client.put("/cost-guard/config", json={"cap_daily_usd": 2.50})
    assert r.status_code == 200
    data = r.json()
    assert data["cap_daily_usd"] == 2.50
    assert data["cap_weekly_usd"] == 5.00  # unchanged

    r2 = client.get("/cost-guard/state")
    assert r2.json()["config"]["cap_daily_usd"] == 2.50


def test_config_update_disable(client):
    r = client.put("/cost-guard/config", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_config_update_rejects_negative_daily(client):
    # Pydantic ge=0 should bounce this with a 422
    r = client.put("/cost-guard/config", json={"cap_daily_usd": -1})
    assert r.status_code == 422


# ---- /cost-guard/check ------------------------------------------------------


def test_check_allows_on_fresh_db(client):
    r = client.post(
        "/cost-guard/check",
        json={"model": "gpt-4o-mini", "auth_kind": "api_key", "max_tokens": 400},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["allow"] is True
    assert data["over_dimension"] is None
    assert data["est_reservation_usd"] > 0


def test_check_oauth_returns_zero_estimate(client):
    r = client.post(
        "/cost-guard/check",
        json={"model": "gpt-5.4", "auth_kind": "oauth", "max_tokens": 400},
    )
    assert r.status_code == 200
    assert r.json()["est_reservation_usd"] == 0.0


def test_check_blocks_after_seeding_session_near_cap(client):
    """Seed the sessions ledger with a near-cap row, then verify a check
    against gpt-4o-mini (~$0.008 worst case) tips us over a $0.01 cap."""
    from engine import storage

    client.put("/cost-guard/config", json={"cap_daily_usd": 0.01})
    # Write a real session row representing prior spend.
    storage.write_session(
        ticker="TEST",
        trade_date="2026-05-09",
        events=[],
        decision={"action": "HOLD", "confidence": 0.5, "reasoning": "seed"},
        live=True,
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=100,
        estimated_cost_usd=0.005,
        auth_kind="api_key",
    )
    # daily cap = $0.01, prior spend = $0.005, est for next = $0.008
    # → 0.005 + 0.008 = $0.013 > $0.01 → block
    r = client.post(
        "/cost-guard/check",
        json={"model": "gpt-4o-mini", "auth_kind": "api_key", "max_tokens": 400},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["allow"] is False
    assert data["over_dimension"] == "daily"


def test_check_invalid_auth_kind_rejected(client):
    r = client.post(
        "/cost-guard/check",
        json={"model": "gpt-4o-mini", "auth_kind": "magic", "max_tokens": 400},
    )
    assert r.status_code == 422  # pydantic regex


# ---- /cost-guard/reserve ----------------------------------------------------


def test_reserve_returns_id_and_creates_row(client):
    r = client.post(
        "/cost-guard/reserve",
        json={"model": "gpt-4o-mini", "auth_kind": "api_key", "max_tokens": 400},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["reservation_id"]
    assert data["est_cost_usd"] > 0
    assert data["override"] is False
    # State should reflect the new reservation as in-flight cost
    state = client.get("/cost-guard/state").json()
    assert state["spend"]["daily_usd"] == pytest.approx(data["est_cost_usd"], rel=1e-6)


def test_reserve_blocks_with_402_when_over_cap(client):
    client.put("/cost-guard/config", json={"cap_daily_usd": 0.001})
    r = client.post(
        "/cost-guard/reserve",
        json={"model": "gpt-4o-mini", "auth_kind": "api_key", "max_tokens": 400},
    )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "cost_guard_blocked"
    assert detail["over_dimension"] == "daily"
    assert "spend" in detail
    assert "config" in detail
    assert detail["est_cost_usd"] > 0


def test_reserve_with_override_succeeds_over_cap(client):
    client.put("/cost-guard/config", json={"cap_daily_usd": 0.001})
    r = client.post(
        "/cost-guard/reserve",
        json={
            "model": "gpt-4o-mini",
            "auth_kind": "api_key",
            "max_tokens": 400,
            "override": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["override"] is True


def test_oauth_reserve_does_not_count_against_cost(client):
    """OAuth reservations have est_cost_usd=0 and don't inflate cost spend."""
    r = client.post(
        "/cost-guard/reserve",
        json={"model": "gpt-5.4", "auth_kind": "oauth", "max_tokens": 400},
    )
    assert r.status_code == 200
    assert r.json()["est_cost_usd"] == 0.0
    state = client.get("/cost-guard/state").json()
    assert state["spend"]["daily_usd"] == 0.0
    assert state["spend"]["sessions_today"] == 1


# ---- WS handler auto-reserve fallthrough -----------------------------------


def test_ws_stub_debate_does_not_touch_cost_guard(client):
    """No provider_config means stub mode → no reservation is created."""
    with client.websocket_connect(f"/stream?token={TOKEN}") as ws:
        ws.send_json({"ticker": "AAPL", "trade_date": "2026-05-09"})
        events = []
        try:
            while True:
                event = ws.receive_json()
                events.append(event)
                if event.get("type") == "session.complete":
                    break
        except Exception:
            pass
    # No reservation was created — daily spend stays 0
    state = client.get("/cost-guard/state").json()
    assert state["spend"]["sessions_today"] == 0
    assert state["spend"]["daily_usd"] == 0


def test_ws_live_debate_blocked_by_cost_guard(client):
    """When a live debate request would exceed caps, the WS sends a
    cost.blocked event and closes with 1008."""
    # Tighten cap so any live debate is blocked
    client.put("/cost-guard/config", json={"cap_daily_usd": 0.001})
    with client.websocket_connect(f"/stream?token={TOKEN}") as ws:
        ws.send_json(
            {
                "ticker": "NVDA",
                "trade_date": "2026-05-09",
                "provider_config": {
                    "provider": "openai",
                    "auth": {"type": "api_key", "api_key": "sk-fake"},
                    "model": "gpt-4o-mini",
                    "max_tokens": 400,
                },
            }
        )
        # The first non-data event should be cost.blocked. Skip data events.
        blocked_event = None
        try:
            for _ in range(5):
                event = ws.receive_json()
                if event.get("type") == "cost.blocked":
                    blocked_event = event
                    break
        except Exception:
            pass
    assert blocked_event is not None
    assert blocked_event["over_dimension"] == "daily"
    assert "message" in blocked_event


# ---- S8: a paid model with no verified price is UNKNOWN cost, never free ---------------------------------


def test_unpriced_paid_model_is_refused_while_caps_are_enabled(client):
    r = client.post("/cost-guard/check", json={"model": "claude-unpriced-test-9", "auth_kind": "api_key", "max_tokens": 400}).json()
    assert r["allow"] is False and r["over_dimension"] == "unknown_price"
    res = client.post("/cost-guard/reserve", json={"model": "claude-unpriced-test-9", "auth_kind": "api_key", "max_tokens": 400})
    assert res.status_code == 402 and res.json()["detail"]["over_dimension"] == "unknown_price"


def test_unpriced_paid_model_may_proceed_only_by_explicit_override_or_when_guard_is_off(client):
    ok = client.post("/cost-guard/reserve", json={"model": "claude-unpriced-test-9", "auth_kind": "api_key", "max_tokens": 400, "override": True})
    assert ok.status_code == 200
    client.put("/cost-guard/config", json={"enabled": False})
    assert client.post("/cost-guard/check", json={"model": "claude-unpriced-test-9", "auth_kind": "api_key", "max_tokens": 400}).json()["allow"] is True


def test_free_and_priced_paths_are_unaffected(client):
    for model, kind in (("gpt-4o-mini", "api_key"), ("Q3.8 Flash Next 原版", "local"), ("anything", "oauth")):
        assert client.post("/cost-guard/check", json={"model": model, "auth_kind": kind, "max_tokens": 400}).json()["allow"] is True


def test_session_complete_stores_null_cost_for_unpriced_models(monkeypatch, tmp_db):
    import asyncio

    from engine.llm_providers import ProviderConfig
    from engine.live_debate import live_debate

    class _A:
        async def open(self, **_): ...
        async def close(self): ...
        async def complete(self, **_):
            return ("ok", 100, 10)

    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: _A())

    async def run(model):
        cfg = ProviderConfig(provider="anthropic", auth={"type": "api_key", "api_key": "k"}, model=model)
        return [e async for e in live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg)]

    unknown = [e for e in asyncio.run(run("claude-unpriced-test-9")) if e["type"] == "session.complete"][0]
    assert unknown["estimated_cost_usd"] is None and unknown["cost_known"] is False
    known = [e for e in asyncio.run(run("gpt-4o-mini")) if e["type"] == "session.complete"][0]
    assert known["estimated_cost_usd"] > 0 and known["cost_known"] is True


# ---- E10: unknown cost is settled as unknown (NULL) and EVERY billable model is checked -----------------------------


def _fin(rid):
    with cost_guard._connect() as conn:
        return conn.execute("SELECT finalized, finalized_cost_usd, override FROM cost_reservations WHERE id = ?", (rid,)).fetchone()


def test_unknown_price_reservation_settles_as_null_not_zero(client):
    rid = client.post("/cost-guard/reserve", json={"model": "claude-unpriced-test-9", "auth_kind": "api_key", "max_tokens": 400, "override": True}).json()["reservation_id"]
    assert cost_guard.reservation_override(rid) is True
    assert cost_guard.finalize_reservation(rid, actual_cost_usd=None) is True
    row = _fin(rid)
    assert row[0] == 1 and row[1] is None                                    # unknown, NOT 0.0
    rid2 = client.post("/cost-guard/reserve", json={"model": "gpt-4o-mini", "auth_kind": "api_key", "max_tokens": 400}).json()["reservation_id"]
    cost_guard.finalize_reservation(rid2, actual_cost_usd=0.0123)
    assert _fin(rid2)[1] == 0.0123                                           # priced runs still settle numerically


def test_full_debate_and_live_debate_settle_unknown_models_as_null(tmp_db):
    from engine import full_debate as fd

    cg = cost_guard
    for deep, quick, known in (("gpt-4o", "claude-unpriced-test-9", False), ("claude-unpriced-test-9", "gpt-4o", False), ("gpt-4o", "gpt-4o-mini", True)):
        rid = cg.reserve(model="gpt-4o", auth_kind="api_key", max_tokens=400, override=True).reservation_id
        meter = fd._TokenMeter(None)
        meter.input_tokens, meter.output_tokens = 1000, 100
        fd._finalize_reservation(rid, "api_key", {"deep_think_llm": deep, "quick_think_llm": quick}, meter)
        cost = _fin(rid)[1]
        assert (cost is not None) is known, (deep, quick, cost)


def test_pro_quick_model_is_checked_not_only_deep(client):
    body = {"model": "gpt-4o", "auth_kind": "api_key", "max_tokens": 400}
    assert client.post("/cost-guard/check", json=body).json()["allow"] is True
    r = client.post("/cost-guard/check", json={**body, "extra_models": ["claude-unpriced-test-9"]}).json()
    assert r["allow"] is False and r["over_dimension"] == "unknown_price"
    assert client.post("/cost-guard/reserve", json={**body, "extra_models": ["claude-unpriced-test-9"]}).status_code == 402
    assert client.post("/cost-guard/reserve", json={**body, "extra_models": ["claude-unpriced-test-9"], "override": True}).status_code == 200
    assert client.post("/cost-guard/check", json={**body, "auth_kind": "local", "extra_models": ["claude-unpriced-test-9"]}).json()["allow"] is True


async def _async_empty():
    return []


def test_ws_pro_run_with_unpriced_quick_model_is_blocked_without_override(client, monkeypatch):
    from engine import full_debate as fd

    async def must_not_run(*a, **k):
        raise AssertionError("graph must not start")
        yield

    async def no_net(**_):
        return None

    from engine import server as srv

    monkeypatch.setattr(fd, "full_debate", must_not_run)
    monkeypatch.setattr(srv, "_fetch_summary_safe", no_net)       # isolated: no yfinance / network
    monkeypatch.setattr(srv, "_fetch_news_safe", lambda **_: _async_empty())
    pre = client.post("/cost-guard/reserve", json={"model": "gpt-4o", "auth_kind": "api_key", "max_tokens": 400}).json()["reservation_id"]
    frame = {"ticker": "AAPL", "trade_date": "2026-10-29", "engine": "pro", "reservation_id": pre,
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}, "model": "gpt-4o"},
             "pro_config": {"deep_think_llm": "gpt-4o", "quick_think_llm": "claude-unpriced-test-9"}}
    with client.websocket_connect(f"/stream?token={TOKEN}") as ws:
        ws.send_json(frame)
        ev = ws.receive_json()
        while ev["type"] != "cost.blocked":
            ev = ws.receive_json()
    assert ev["type"] == "cost.blocked" and ev["over_dimension"] == "unknown_price"
    assert _fin(pre)[0] == 1                                                  # the held estimate is released, not left to expire


def test_dexter_never_starts_for_an_unpriced_paid_model_and_shares_the_run_budget(monkeypatch, tmp_db):
    import asyncio

    from engine import tw_research as twr
    from engine.llm_providers import ProviderConfig

    monkeypatch.setattr(twr, "dexter_model_and_env", lambda c: ({"id": "m"}, {}, None))
    ran = []
    monkeypatch.setattr(twr, "run_dexter_job", lambda **k: ran.append(1) or {"status": "succeeded", "findings": [], "usage": {"measured": True}})
    pack = {"snapshot_id": "a" * 64, "security_id": "TW:2330"}
    cfg = ProviderConfig(provider="anthropic", auth={"type": "api_key", "api_key": "k"}, model="claude-unpriced-test-9")
    st = asyncio.run(twr.dexter_stage(pack=pack, path=tmp_db.parent / "p.json", out_dir=tmp_db.parent, config=cfg, lang="en"))
    assert not ran and st["ran"] is False and st["dexter"]["reason"] == "cost_guard_blocked:unknown_price"
    # Same budget: the graph's own reservation is already counted, so Dexter's worst case must fit in what is LEFT.
    cfg2 = ProviderConfig(provider="anthropic", auth={"type": "api_key", "api_key": "k"}, model="claude-haiku-4-5")
    res = cost_guard.reserve(model="claude-haiku-4-5", auth_kind="api_key", max_tokens=400)
    cost_guard.update_config(enabled=True, cap_daily_usd=res.est_cost_usd + 0.0001)
    st2 = asyncio.run(twr.dexter_stage(pack=pack, path=tmp_db.parent / "p.json", out_dir=tmp_db.parent, config=cfg2, lang="en"))
    assert not ran and st2["ran"] is False and st2["dexter"]["reason"].startswith("cost_guard_blocked:")
