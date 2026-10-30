"""run.heartbeat: liveness while the engine is silent. It must not be mistaken for model output,
must not be persisted, and must not leak the wrapped generator on early exit."""

from __future__ import annotations

import asyncio

import pytest


def test_heartbeats_fill_silence_and_events_pass_through_unchanged():
    from engine.server import with_heartbeat

    async def slow():
        yield {"type": "a"}
        await asyncio.sleep(0.35)
        yield {"type": "b"}

    async def go():
        return [e async for e in with_heartbeat(slow(), meta={"provider": "p", "model": "m", "route": "full"}, interval=0.1)]

    evs = asyncio.run(go())
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "a" and kinds[-1] == "b" and kinds.count("run.heartbeat") >= 2
    hb = [e for e in evs if e["type"] == "run.heartbeat"]
    assert all(h["provider"] == "p" and h["route"] == "full" and h["since_event_s"] >= 0 for h in hb)
    assert hb[-1]["since_event_s"] >= hb[0]["since_event_s"]            # it measures time since the last REAL event
    assert all(set(h) <= {"type", "elapsed_s", "since_event_s", "provider", "model", "route", "auth_kind"} for h in hb)  # no content


def test_early_exit_closes_the_wrapped_generator():
    from engine.server import with_heartbeat

    closed = {"v": False}

    async def inner():
        try:
            yield {"type": "a"}
            await asyncio.sleep(60)
            yield {"type": "never"}
        finally:
            closed["v"] = True

    async def go():
        w = with_heartbeat(inner(), meta={}, interval=0.05)
        got = await w.__anext__()
        assert got["type"] == "a"
        await w.__anext__()                      # a heartbeat while the inner one sleeps
        await w.aclose()

    asyncio.run(go())
    assert closed["v"] is True


def test_heartbeats_reach_the_socket_but_not_the_saved_session(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient

    from engine import server as srv, storage

    monkeypatch.setattr(srv, "HEARTBEAT_INTERVAL_S", 0.05)
    pytest.importorskip("tradingagents")
    from engine import full_debate as fd

    async def fake(*_a, **_k):
        yield {"type": "session.start", "ticker": "AAPL", "trade_date": "2026-10-30"}
        await asyncio.sleep(0.4)
        yield {"type": "session.complete", "ticker": "AAPL", "trade_date": "2026-10-30", "live": True, "provider": "openai",
               "auth_kind": "api_key", "model": "gpt-4o-mini", "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "x"},
               "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.0}

    async def none(**_):
        return None

    async def nonews(**_):
        return []

    monkeypatch.setattr(fd, "full_debate", fake)
    monkeypatch.setattr(srv, "_fetch_summary_safe", none)
    monkeypatch.setattr(srv, "_fetch_news_safe", nonews)
    frame = {"ticker": "AAPL", "trade_date": "2026-10-30", "engine": "pro", "pro_config": {"selected_analysts": ["market"]},
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}, "model": "gpt-4o-mini"}}
    seen = []
    with TestClient(srv.build_app(token="t")) as c:
        with c.websocket_connect("/stream?token=t") as ws:
            ws.send_json(frame)
            while True:
                ev = ws.receive_json()
                seen.append(ev)
                if ev["type"] == "session.complete":
                    break
            with pytest.raises(Exception):      # let the server finish persisting and close (1000)
                ws.receive_json()
    beats = [e for e in seen if e["type"] == "run.heartbeat"]
    assert beats and beats[0]["route"] == "full" and beats[0]["model"] == "gpt-4o-mini"
    import time

    for _ in range(50):                       # persistence runs just after the final event is sent
        rows = storage.list_sessions()
        if rows:
            break
        time.sleep(0.1)
    assert rows
    detail = storage.get_session(rows[0].id)
    assert all(e.get("type") != "run.heartbeat" for e in detail.events)


def test_whole_run_elapsed_is_persisted_once_and_old_rows_stay_unknown(monkeypatch, tmp_db):
    import sqlite3
    import time

    from fastapi.testclient import TestClient

    from engine import server as srv, storage

    pytest.importorskip("tradingagents")
    from engine import full_debate as fd

    async def fake(*_a, **_k):
        yield {"type": "session.start", "ticker": "AAPL", "trade_date": "2026-10-30"}
        await asyncio.sleep(0.6)
        yield {"type": "session.complete", "ticker": "AAPL", "trade_date": "2026-10-30", "live": True, "provider": "openai",
               "auth_kind": "api_key", "model": "gpt-4o-mini", "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "x"},
               "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.0}

    async def none(**_):
        return None

    async def nonews(**_):
        return []

    monkeypatch.setattr(fd, "full_debate", fake)
    monkeypatch.setattr(srv, "_fetch_summary_safe", none)
    monkeypatch.setattr(srv, "_fetch_news_safe", nonews)
    frame = {"ticker": "AAPL", "trade_date": "2026-10-30", "engine": "pro", "pro_config": {"selected_analysts": ["market"]},
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}, "model": "gpt-4o-mini"}}
    with TestClient(srv.build_app(token="t")) as c:
        with c.websocket_connect("/stream?token=t") as ws:
            ws.send_json(frame)
            done = None
            while True:
                ev = ws.receive_json()
                if ev["type"] == "session.complete":
                    done = ev
                    break
            with pytest.raises(Exception):
                ws.receive_json()
    assert 0.5 <= done["elapsed_s"] < 30                       # whole run incl. data fetch, not a model latency
    for _ in range(50):
        rows = storage.list_sessions()
        if rows:
            break
        time.sleep(0.1)
    assert rows[0].elapsed_s == done["elapsed_s"]
    assert storage.get_session(rows[0].id).elapsed_s == done["elapsed_s"]
    # a session saved before the column existed has no trustworthy duration
    con = sqlite3.connect(storage.db_path())
    con.execute("UPDATE sessions SET elapsed_s = NULL")
    con.commit(); con.close()
    assert storage.list_sessions()[0].elapsed_s is None
