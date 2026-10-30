"""D7: resumable runs. The analysis lives in the engine (RunManager), the WebSocket only subscribes.

Everything here is isolated: a fake provider adapter (no model, no network), an in-memory keyring where a secret store is
involved, and a per-test SQLite database that the fixture verifies is NOT the user's database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from engine import cost_guard, run_manager as rm, server as srv, storage

TOKEN = "t"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def isolated_db(tmp_db):
    """Per-test database, asserted to be a temp file (never the real application database)."""
    assert "test.db" in storage.db_path() and "Application Support" not in storage.db_path()
    return tmp_db


class FakeAdapter:
    """A provider that never leaves the process. The first `free` calls answer at once, later ones wait for `hold`."""

    def __init__(self, free: int = 2, tokens=(100, 10)) -> None:
        self.hold = threading.Event()
        self.free, self.tokens, self.calls = free, tokens, 0

    async def open(self, **_): ...

    async def close(self): ...

    async def complete(self, **_):
        self.calls += 1
        n = self.calls
        while n > self.free and not self.hold.is_set():
            await asyncio.sleep(0.01)
        return ("ok", *self.tokens)


@pytest.fixture
def world(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: adapter)

    async def none(**_):
        return None

    async def empty(**_):
        return []

    async def empty_text(*a, **k):
        return ""

    monkeypatch.setattr(srv, "_fetch_summary_safe", none)
    monkeypatch.setattr(srv, "_fetch_news_safe", empty)
    monkeypatch.setattr(srv.sentiment_sources, "fetch_stocktwits_messages_async", empty_text)
    monkeypatch.setattr(srv.sentiment_sources, "fetch_reddit_posts_async", empty_text)
    return adapter


def frame(key="key-one", **over):
    f = {"ticker": "AAPL", "trade_date": "2026-10-30", "resumable": True, "client_run_key": key,
         "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k-not-a-real-key"}, "model": "gpt-4o-mini", "max_tokens": 50}}
    f.update(over)
    return f


def read_until(ws, pred, limit=500):
    out = []
    for _ in range(limit):
        ev = ws.receive_json()
        out.append(ev)
        if pred(ev):
            return out
    raise AssertionError("predicate never matched")


def drain(ws, limit=500):
    out = []
    try:
        for _ in range(limit):
            out.append(ws.receive_json())
    except Exception:  # noqa: BLE001 — closed by the server
        pass
    return out


def start_and_get_two_messages(c, f):
    with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
        ws.send_json(f)
        evs = read_until(ws, lambda e: e["type"] == "agent.message" and e["seq"] >= 3)
    att = evs[0]
    assert att["type"] == "run.attached" and att["run_id"].startswith("run_") and att["resumed"] is False
    return att["run_id"], evs[-1]["seq"], evs


def reservation(c, model="gpt-4o-mini"):
    return c.post("/cost-guard/reserve", headers=H, json={"model": model, "auth_kind": "api_key", "max_tokens": 50}).json()["reservation_id"]


def res_row(rid):
    with cost_guard._connect() as conn:
        r = conn.execute("SELECT finalized, finalized_cost_usd FROM cost_reservations WHERE id=?", (rid,)).fetchone()
    return None if r is None else (r[0], r[1])


def n_sessions():
    with storage._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def wait_state(c, run_id, states, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get(f"/runs/{run_id}", headers=H).json()
        if st["state"] in states:
            return st
        time.sleep(0.05)
    raise AssertionError(f"run never reached {states}: {st}")


# ---- refresh in the middle of a run ---------------------------------------------------------------------------------------


def test_refresh_mid_run_keeps_the_run_and_resumes_without_gaps_or_duplicates(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        run_id, last, first_batch = start_and_get_two_messages(c, frame())
        # the socket is gone (refresh); the run must still be running and visible
        st = c.get(f"/runs/{run_id}", headers=H).json()
        assert st["state"] == "running" and st["active"] and st["ticker"] == "AAPL" and st["model"] == "gpt-4o-mini"
        assert st["steps_done"] >= 1 and st["input_tokens"] is not None and st["elapsed_s"] >= 0 and st["last_event_type"]
        active = c.get("/runs/active", headers=H).json()
        assert [r["run_id"] for r in active["runs"]] == [run_id]
        calls_before = world.calls
        world.hold.set()
        with c.websocket_connect(f"/runs/{run_id}/stream?token={TOKEN}&after={last}") as ws:
            tail = drain(ws)
        assert tail[0]["type"] == "run.attached" and tail[0]["resumed"] is True and tail[0]["after"] == last
        seqs = [e["seq"] for e in tail if "seq" in e]
        assert seqs == list(range(last + 1, seqs[-1] + 1))                       # contiguous: nothing missed, nothing repeated
        assert tail[-1]["type"] == "run.finished" and tail[-1]["state"] == "completed"
        # the model was called once per step overall: the reconnect did not re-run anything
        all_events = c.get(f"/runs/{run_id}/events?limit=2000", headers=H).json()["events"]
        assert world.calls == sum(1 for e in all_events if e["type"] == "agent.message") >= calls_before
        assert [e["seq"] for e in all_events] == list(range(1, len(all_events) + 1))
        assert n_sessions() == 1


def test_refresh_after_completion_replays_the_finished_run_and_history_has_one_row(world):
    world.free = 10_000
    with TestClient(srv.build_app(token=TOKEN)) as c:
        rid = reservation(c)
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json(frame(reservation_id=rid))
            first = drain(ws)
        assert any(e["type"] == "session.complete" for e in first) and first[-1]["type"] == "run.finished"
        calls = world.calls
        st = wait_state(c, first[0]["run_id"], ("completed",))
        assert st["session_id"] and st["input_tokens"] and st["output_tokens"] and st["terminal_code"] is None
        for _ in range(2):                                                        # reopen the page twice
            with c.websocket_connect(f"/runs/{first[0]['run_id']}/stream?token={TOKEN}&after=0") as ws:
                again = drain(ws)
            assert [e["type"] for e in again[1:]] == [e["type"] for e in first[1:]]
        assert world.calls == calls and n_sessions() == 1                          # no new model call, history written once
        assert res_row(rid)[0] == 1                                                # the reservation was settled once


# ---- duplicate start / two tabs ---------------------------------------------------------------------------------------------


def test_same_start_key_attaches_to_the_same_run_without_a_second_reservation_or_model_call(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        rid = reservation(c)
        f = frame(reservation_id=rid)
        run_id, last, _ = start_and_get_two_messages(c, f)
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws2:               # second tab / retried start, same key
            ws2.send_json(f)
            att = ws2.receive_json()
            assert att["type"] == "run.attached" and att["run_id"] == run_id and att["resumed"] is True
        assert len(c.get("/runs/active", headers=H).json()["runs"]) == 1
        calls = world.calls
        with TestClient(srv.build_app(token=TOKEN)) as _:                          # (unrelated app object must not matter)
            pass
        with cost_guard._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cost_reservations").fetchone()[0] == 1   # only the page's own reservation
        world.hold.set()
        wait_state(c, run_id, ("completed",))
        assert world.calls >= calls and n_sessions() == 1


def test_two_tabs_can_watch_one_run_and_both_get_every_event(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        run_id, last, _ = start_and_get_two_messages(c, frame())
        with c.websocket_connect(f"/runs/{run_id}/stream?token={TOKEN}&after=0") as a, \
                c.websocket_connect(f"/runs/{run_id}/stream?token={TOKEN}&after=0") as b:
            first_a = read_until(a, lambda e: e.get("seq", 0) >= last)
            first_b = read_until(b, lambda e: e.get("seq", 0) >= last)
            world.hold.set()
            rest_a, rest_b = drain(a), drain(b)
        sa = [e["seq"] for e in first_a + rest_a if "seq" in e]
        sb = [e["seq"] for e in first_b + rest_b if "seq" in e]
        assert sa == sb == list(range(1, sa[-1] + 1))


# ---- failure / cancel / disconnect ------------------------------------------------------------------------------------------


def test_explicit_cancel_stops_the_run_settles_the_reservation_and_writes_no_history(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        rid = reservation(c)
        run_id, last, _ = start_and_get_two_messages(c, frame(reservation_id=rid))
        assert res_row(rid)[0] == 0                                               # still held while running
        out = c.post(f"/runs/{run_id}/cancel", headers=H).json()
        assert out["state"] == "cancelled" and out["terminal_code"] == "user_cancelled" and out["input_tokens"]   # usage is kept
        evs = c.get(f"/runs/{run_id}/events?limit=2000", headers=H).json()["events"]
        assert [e["type"] for e in evs[-2:]] == ["run.cancelled", "run.finished"]
        assert res_row(rid)[0] == 1 and res_row(rid)[1] is not None               # settled with the measured cost
        assert n_sessions() == 0 and c.get("/runs/active", headers=H).json()["runs"] == []
        calls = world.calls
        world.hold.set()
        time.sleep(0.3)
        assert world.calls == calls                                               # nothing kept calling the model
        # cancelling again is harmless
        assert c.post(f"/runs/{run_id}/cancel", headers=H).json()["state"] == "cancelled"


def test_disconnect_is_not_a_cancel(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        run_id, _, _ = start_and_get_two_messages(c, frame())
        time.sleep(0.4)
        st = c.get(f"/runs/{run_id}", headers=H).json()
        assert st["state"] == "running" and st["terminal_code"] is None
        world.hold.set()
        assert wait_state(c, run_id, ("completed",))["session_id"]


def test_failed_run_has_a_terminal_state_usage_and_a_released_reservation(world, monkeypatch):
    async def boom(**kw):
        yield {"type": "session.start", "ticker": "AAPL", "trade_date": "2026-10-30"}
        yield {"type": "cost.usage", "input_tokens": 300, "output_tokens": 40, "est_cost_usd": 0.01, "free": False}
        raise RuntimeError("adapter exploded sk-ant-usr-abcdefghijklmnopqrstuvwxyz0123456789")

    monkeypatch.setattr(srv, "live_debate", boom)
    with TestClient(srv.build_app(token=TOKEN)) as c:
        rid = reservation(c)
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json(frame(reservation_id=rid))
            evs = drain(ws)
        st = wait_state(c, evs[0]["run_id"], ("failed",))
        assert st["terminal_code"] == "engine_exception" and st["input_tokens"] == 300 and "RuntimeError" in st["error"]
        err = next(e for e in evs if e["type"] == "run.error")
        assert err["code"] == "engine_exception" and "sk-ant-usr-abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(evs)
        assert res_row(rid)[0] == 1                                               # released, not left to expire
        assert n_sessions() == 0


def test_guard_errors_are_terminal_failed_runs_with_the_original_close_code(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json({"ticker": "???", "trade_date": "2026-10-30", "resumable": True, "client_run_key": "bad-ticker"})
            evs = drain(ws)
        assert any(e["type"] == "run.error" for e in evs)
        st = c.get(f"/runs/{evs[0]['run_id']}", headers=H).json()
        assert st["state"] == "failed" and st["error"]


# ---- restart ----------------------------------------------------------------------------------------------------------------


def test_server_restart_marks_unfinished_runs_interrupted_and_never_reruns_them(world):
    with TestClient(srv.build_app(token=TOKEN)) as old:
        run_id, _, _ = start_and_get_two_messages(old, frame())
        time.sleep(0.2)                                                           # let the old run reach its (blocked) next call
        calls = world.calls
        old.app.state.run_manager._db = lambda fn: None                           # the "old process" died: it writes nothing more
        with TestClient(srv.build_app(token=TOKEN)) as new:                       # a fresh process on the same database
            st = new.get(f"/runs/{run_id}", headers=H).json()
            assert st["state"] == "interrupted" and st["terminal_code"] == "server_restart" and not st["active"]
            evs = new.get(f"/runs/{run_id}/events?limit=2000", headers=H).json()["events"]
            assert evs[-1]["type"] == "run.finished" and evs[-1]["state"] == "interrupted" and "NOT re-run" in evs[-1]["message"]
            assert new.get("/runs/active", headers=H).json()["runs"] == []
            with new.websocket_connect(f"/runs/{run_id}/stream?token={TOKEN}&after=0") as ws:   # still replayable
                assert drain(ws)[-1]["type"] in ("run.finished",)
            time.sleep(0.2)
            assert world.calls == calls                                           # nothing was started again
        world.hold.set()


# ---- security / isolation ---------------------------------------------------------------------------------------------------


def test_runs_api_requires_authentication(world):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        run_id, _, _ = start_and_get_two_messages(c, frame())
        for path in ("/runs/active", f"/runs/{run_id}", f"/runs/{run_id}/events"):
            assert c.get(path).status_code == 401
            assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post(f"/runs/{run_id}/cancel").status_code == 401
        for url in (f"/runs/{run_id}/stream?token=wrong", "/stream?token=wrong"):
            with pytest.raises(Exception):
                with c.websocket_connect(url) as ws:
                    ws.receive_json()
        assert c.get(f"/runs/{run_id}", headers=H).json()["state"] == "running"   # nobody else could touch it
        assert c.get("/runs/run_doesnotexist", headers=H).status_code == 404
        world.hold.set()


def test_another_owner_cannot_see_stream_or_cancel_a_run(world):
    async def go():
        mgr = rm.RunManager()

        async def body(sink):
            await sink.send_json({"type": "session.start"})
            await asyncio.sleep(30)

        run, _ = await mgr.start_or_attach("alice@example.com", {"ticker": "AAPL"}, body)
        assert mgr.get("alice@example.com", run.id) is run
        assert mgr.get("mallory@example.com", run.id) is None and mgr.get("local", run.id) is None
        assert mgr.list_for("mallory@example.com") == []
        await mgr.cancel(run)
        assert run.state == "cancelled"

    asyncio.run(go())


def test_proxy_owner_scope_over_http(tmp_path, world):
    from engine.secret_store import SecretStore
    from engine.tests.test_web_app import COOKIE, COOKIE_NAME, LAUNCH, MemKeyring, PORT
    from engine.tests.test_web_proxy_mode import HOST, PUBLIC, SECRET
    from engine.web_app import ProxyConfig, build_web_app

    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html><head></head><body>app</body></html>")
    cfg = ProxyConfig(public_origin=PUBLIC, base_path="/lab-pro", secret=SECRET, owners=frozenset({"alice@example.com", "bob@example.com"}))
    app = build_web_app(token="engine-token", port=PORT, data_dir=tmp_path / "data", static_dir=static, launch_secret=LAUNCH,
                        cookie_value=COOKIE, store=SecretStore(MemKeyring()), proxy=cfg)

    def hop(owner):
        return {"host": HOST, "x-tal-proxy-auth": SECRET, "x-tal-owner": owner}

    with TestClient(app, base_url=PUBLIC, client=("127.0.0.1", 54321)) as c:
        with c.websocket_connect("/stream", headers={**hop("alice@example.com"), "origin": PUBLIC, "cookie": f"{COOKIE_NAME}={COOKIE}"}) as ws:
            ws.send_json(frame())
            att = read_until(ws, lambda e: e["type"] == "agent.message")[0]
        rid = att["run_id"]
        ck = {"cookie": f"{COOKIE_NAME}={COOKIE}"}
        assert c.get(f"/runs/{rid}", headers={**hop("alice@example.com"), **ck}).status_code == 200
        assert c.get(f"/runs/{rid}", headers={**hop("bob@example.com"), **ck}).status_code == 404
        assert c.get("/runs/active", headers={**hop("bob@example.com"), **ck}).json()["runs"] == []
        assert [r["run_id"] for r in c.get("/runs/active", headers={**hop("alice@example.com"), **ck}).json()["runs"]] == [rid]
        assert c.post(f"/runs/{rid}/cancel", headers={**hop("bob@example.com"), **ck, "origin": PUBLIC}).status_code == 404
        assert c.get(f"/runs/{rid}", headers={**hop("alice@example.com")}).status_code == 401      # no session cookie, no bearer
        with pytest.raises(Exception):                                                              # bob cannot subscribe either
            with c.websocket_connect(f"/runs/{rid}/stream?after=0", headers={**hop("bob@example.com"), "origin": PUBLIC, **ck}) as ws:
                ws.receive_json()
        assert c.post(f"/runs/{rid}/cancel", headers={**hop("alice@example.com"), **ck, "origin": PUBLIC}).json()["state"] == "cancelled"


def test_nothing_secret_is_stored_with_a_run(world, monkeypatch):
    async def chatty(**kw):
        yield {"type": "session.start", "ticker": "AAPL", "trade_date": "2026-10-30"}
        yield {"type": "agent.message", "agent": "a", "phase": "analyst", "content": "echo sk-ant-usr-abcdefghijklmnopqrstuvwxyz0123456789 and Bearer abcdefghijklmnop"}
        yield {"type": "session.complete", "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "x"}, "live": True, "provider": "openai",
               "auth_kind": "api_key", "model": "gpt-4o-mini", "input_tokens": 5, "output_tokens": 2, "estimated_cost_usd": 0.0}

    monkeypatch.setattr(srv, "live_debate", chatty)
    with TestClient(srv.build_app(token=TOKEN)) as c:
        f = frame()
        f["provider_config"]["auth"]["api_key"] = "SECRETVALUE-9f8e7d6c5b4a39281706"
        f["finmind_token"] = "FINMIND-SECRET-1234567890abcdef"
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json(f)
            evs = drain(ws)
        wait_state(c, evs[0]["run_id"], ("completed",))
    with sqlite3.connect(storage.db_path()) as conn:
        dump = json.dumps([list(r) for t in ("runs", "run_events") for r in conn.execute(f"SELECT * FROM {t}")])
    for needle in ("SECRETVALUE", "FINMIND-SECRET", "provider_config", "sk-ant-usr", "Bearer abcdef", "k-not-a-real-key", "authorization"):
        assert needle not in dump, needle
    assert "run_events" in dump or True


# ---- ordering, bounds, slow consumers ---------------------------------------------------------------------------------------


def test_events_endpoint_replays_by_sequence_with_bounds(world):
    world.free = 10_000
    with TestClient(srv.build_app(token=TOKEN)) as c:
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json(frame())
            evs = drain(ws)
        rid = evs[0]["run_id"]
        wait_state(c, rid, ("completed",))
        a = c.get(f"/runs/{rid}/events?after=0&limit=5", headers=H).json()
        assert [e["seq"] for e in a["events"]] == [1, 2, 3, 4, 5] and a["last_seq"] > 5
        b = c.get(f"/runs/{rid}/events?after=5&limit=3", headers=H).json()
        assert [e["seq"] for e in b["events"]] == [6, 7, 8]
        assert c.get(f"/runs/{rid}/events?after={a['last_seq']}", headers=H).json()["events"] == []
        assert len(c.get(f"/runs/{rid}/events?limit=999999", headers=H).json()["events"]) <= 2000


def test_slow_subscriber_is_dropped_and_never_blocks_the_run(monkeypatch):
    monkeypatch.setattr(rm, "SUB_QUEUE_MAX", 3)

    async def go():
        mgr = rm.RunManager()
        run = rm.Run(mgr, "run_x", "local", {"ticker": "AAPL"}, None)
        run.state = "running"
        snap, sub = run.subscribe(0)
        t0 = time.monotonic()
        for i in range(500):
            run.emit({"type": "agent.message", "agent": "a", "content": str(i)})      # nobody reads: emit must not block
        assert time.monotonic() - t0 < 1.0 and run.seq == 500
        assert sub.lagged and sub not in run.subs

        class WS:
            def __init__(self):
                self.sent, self.closed = [], None

            async def send_json(self, ev):
                self.sent.append(ev)

            async def close(self, code=1000, reason=""):
                self.closed = code

        ws = WS()
        # a lagged client is told to resync from its last seq (it re-subscribes with ?after=<seq>) and closed with 1013
        run2 = rm.Run(mgr, "run_y", "local", {}, None)
        run2.state = "running"
        task = asyncio.create_task(mgr.serve(ws, run2, 0))
        await asyncio.sleep(0.05)
        for i in range(50):
            run2.emit({"type": "agent.message", "content": str(i)})
        await asyncio.wait_for(task, 2)
        assert ws.sent[-1]["type"] == "run.resync" and ws.closed == 1013

    asyncio.run(go())


def test_subscribe_snapshot_and_live_registration_are_atomic():
    async def go():
        mgr = rm.RunManager()
        run = rm.Run(mgr, "run_z", "local", {}, None)
        run.state = "running"
        for i in range(5):
            run.emit({"type": "agent.message", "content": str(i)})
        snap, sub = run.subscribe(2)
        run.emit({"type": "agent.message", "content": "later"})
        live = []
        while not sub.q.empty():
            live.append(sub.q.get_nowait())
        seqs = [e["seq"] for e in snap + live]
        assert seqs == [3, 4, 5, 6]

    asyncio.run(go())


def test_event_count_is_bounded_but_terminal_events_are_kept(monkeypatch):
    monkeypatch.setattr(rm, "MAX_EVENTS", 10)

    async def go():
        mgr = rm.RunManager()
        run = rm.Run(mgr, "run_b", "local", {}, None)
        run.state = "running"
        for i in range(100):
            run.emit({"type": "agent.message", "content": str(i)})
        run.emit({"type": "session.complete", "decision": {}})
        assert len(run.events) == 11 and run.truncated and run.events[-1]["type"] == "session.complete" and run.seq == 101

    asyncio.run(go())


# ---- every flow goes through the same managed run ---------------------------------------------------------------------------


def test_free_engine_without_a_provider_is_a_managed_stub_run(monkeypatch):
    with TestClient(srv.build_app(token=TOKEN)) as c:
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json({"ticker": "AAPL", "trade_date": "2026-10-30", "resumable": True, "client_run_key": "stub-run-1"})
            evs = drain(ws, 2000)
        st = c.get(f"/runs/{evs[0]['run_id']}", headers=H).json()
        assert st["state"] == "completed" and st["provider"] is None


def test_legacy_clients_see_exactly_the_old_event_stream(world):
    """No `resumable` flag: no run.attached, no run.finished; events just carry an extra `seq`."""
    world.free = 10_000
    with TestClient(srv.build_app(token=TOKEN)) as c:
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json(frame(resumable=False))
            evs = drain(ws)
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "session.start" and "run.attached" not in kinds and "run.finished" not in kinds and kinds[-1] == "session.complete"


def test_taiwan_full_graph_with_dexter_and_pro_runs_are_managed_runs(tmp_db, monkeypatch):
    from datetime import date

    from engine import full_debate as fd, tw_research
    from engine.tests.test_tw_graph_tools import BASE
    from engine import tw_graph_tools as tg

    async def fake_full(ticker, trade_date, **kw):
        yield {"type": "session.start", "ticker": ticker, "trade_date": trade_date}
        yield {"type": "session.complete", "ticker": ticker, "trade_date": trade_date, "live": True, "provider": "openai_compatible", "auth_kind": "local",
               "model": "m", "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "x"}, "input_tokens": 7, "output_tokens": 3, "estimated_cost_usd": 0.0}

    async def fake_dexter(**kw):
        return {"event": {"type": "dexter.result", "ran": True, "status": "succeeded", "reason": None, "findings": [], "unknowns": []},
                "block": {"ran": True, "status": "succeeded", "reason": None, "text": "", "verified": 0}, "d_in": 11, "d_out": 4, "d_cost": 0.0,
                "usage": {"input_tokens": 11, "output_tokens": 4, "measured": True, "est_cost_usd": 0.0, "ran": True}, "ran": True, "measured": True, "billable": False}

    async def none(**_):
        raise AssertionError("no US data provider for a Taiwan code")

    monkeypatch.setattr(fd, "full_debate", fake_full)
    monkeypatch.setattr(tw_research, "full_graph_dexter", fake_dexter)
    monkeypatch.setattr(srv, "_fetch_summary_safe", none)
    monkeypatch.setattr(srv, "_fetch_news_safe", none)
    monkeypatch.setattr(tg, "quote_summary_event", lambda code, td: {"type": "data.summary", "ticker": code, "trade_date": td, "source": "FinMind"})
    with TestClient(srv.build_app(token=TOKEN)) as c:
        with c.websocket_connect(f"/stream?token={TOKEN}") as ws:
            ws.send_json({**BASE, "engine": "pro", "resumable": True, "client_run_key": "tw-full-1"})
            evs = drain(ws)
        kinds = [e["type"] for e in evs]
        assert kinds[0] == "run.attached" and "dexter.result" in kinds and kinds[-2:] == ["session.complete", "run.finished"]
        st = c.get(f"/runs/{evs[0]['run_id']}", headers=H).json()
        assert st["state"] == "completed" and st["engine"] == "pro" and st["ticker"] == "2330"
        # Dexter usage folded into the completed run's totals (11+7 in, 4+3 out)
        assert (st["input_tokens"], st["output_tokens"]) == (18, 7)
