"""Engine-owned batch API and recovery tests, with isolated storage and model calls."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import quote

from fastapi.testclient import TestClient

from engine import server, storage
from engine.batch_manager import BatchManager
from engine.run_manager import Run, RunManager, RunSink
from engine.run_slot import SlotGate
from engine import secret_refs
from engine import cost_guard


def _body(key="batch-key-123", **start_changes):
    start = {"trade_date": "2026-11-09", "engine": "free", "report_language": "en",
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "fake-model-key-123456"},
                                 "model": "gpt-4o-mini", "max_tokens": 50}}
    start.update(start_changes)
    return {"tickers": [" AAPL ", "msft", "NVDA"], "client_batch_key": key, "start": start}


def _all_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, str):
        yield value


def _assert_no_secret(value, secrets):
    assert all(secret not in text for text in _all_strings(value) for secret in secrets)


def _db_values(conn, table):
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    out = []
    for row in rows:
        values = dict(zip(columns, row))
        for column in ("payload", "events_json", "cost_report", "cost_summary"):
            if column in values and values[column]:
                try:
                    values[column] = json.loads(values[column])
                except ValueError:
                    pass
        out.append(values)
    return out


def test_rest_batch_runs_without_websocket_or_polling(tmp_db, monkeypatch):
    calls = []

    class FakeAdapter:
        async def open(self, **kwargs):
            pass

        async def close(self):
            pass

        async def complete(self, **kwargs):
            calls.append(1)
            return ("ok fake-model-key-123456", 10, 2)

    async def empty(*args, **kwargs):
        return []

    async def no_summary(*args, **kwargs):
        return None

    async def no_text(*args, **kwargs):
        return ""

    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: FakeAdapter())
    monkeypatch.setattr(server, "_fetch_summary_safe", no_summary)
    monkeypatch.setattr(server, "_fetch_news_safe", empty)
    monkeypatch.setattr(server.sentiment_sources, "fetch_stocktwits_messages_async", no_text)
    monkeypatch.setattr(server.sentiment_sources, "fetch_reddit_posts_async", no_text)
    app = server.build_app(token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        first = client.post("/batches", json=_body(), headers=headers)
        assert first.status_code == 200, first.text
        bid = first.json()["batch"]["batch_id"]
        assert client.post("/batches", json=_body(), headers=headers).json()["batch"]["batch_id"] == bid
        assert client.post("/batches", json=_body(engine="pro"), headers=headers).status_code == 409
        # No WebSocket and no GET while the worker runs.
        time.sleep(15)
        result = client.get(f"/batches/{bid}", headers=headers)
        assert result.status_code == 200, result.text
        batch = result.json()
        assert batch["state"] == "completed", batch
        assert batch["counts"]["completed"] == 3
        _assert_no_secret(result.json(), ("fake-model-key-123456",))
        assert [x["ticker"] for x in batch["items"]] == ["AAPL", "MSFT", "NVDA"]
        assert len({x["run_id"] for x in batch["items"]}) == 3
        assert len(calls) >= 3
        for item in batch["items"]:
            assert client.get(f'/runs/{item["run_id"]}', headers=headers).json()["batch_id"] == bid
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cost_reservations WHERE finalized=0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM run_events WHERE etype='session.start'").fetchone()[0] == 3
        for table in ("batches", "batch_items", "runs", "run_events", "sessions", "cost_reservations"):
            _assert_no_secret(_db_values(conn, table), ("fake-model-key-123456",))
    with TestClient(server.build_app(token="test-token")) as client:
        assert client.get(f"/batches/{bid}", headers=headers).json()["counts"]["completed"] == 3


def test_slot_gate_cancel_and_release():
    async def scenario():
        gate = SlotGate(lambda: False)
        assert gate.try_claim("a")
        assert not gate.try_claim("b")
        task = asyncio.create_task(gate.wait_claim("b", lambda: False, timeout_s=10))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            assert False, "cancellation was swallowed"
        gate.release("b")
        assert gate.holder == "a"
        gate.release("a")
        assert gate.try_claim("b")
    asyncio.run(scenario())


def test_run_sink_scope_comes_from_trusted_owner():
    run = Run(RunManager(), "r", "alice", {}, None)
    sink = RunSink(run, {"tal_owner": "bob"})
    assert sink.scope == {"tal_owner": "alice"}


def test_cancel_before_drive_clears_sensitive_references(tmp_db):
    async def executor(sink):
        assert False, "cancelled task must not start executor"
    async def scenario():
        mgr = RunManager()
        run, _ = await mgr.start_or_attach("owner", {"ticker": "AAPL", "provider_config": {
            "provider": "openai", "auth": {"type": "api_key", "api_key": "early-secret-123456"}}}, executor,
            batch=("batch", 0, 1), prepare=lambda run: True)
        run.task.cancel()
        try: await run.task
        except asyncio.CancelledError: pass
        await asyncio.sleep(0)
        assert run.scrubber.values == set() and run.batch_prepare is None
        assert run.state == "interrupted"
    asyncio.run(scenario())


def test_slot_gate_single_claim_lingering_and_timeout():
    async def scenario():
        zombie = True
        gate = SlotGate(lambda: zombie)
        assert not gate.try_claim("interactive")
        assert not await gate.wait_claim("batch", lambda: False, timeout_s=0)
        try:
            await gate.wait_claim("batch", lambda: True)
        except asyncio.CancelledError:
            pass
        else:
            assert False, "cancelled condition was ignored"
        zombie = False
        winners = await asyncio.gather(asyncio.to_thread(gate.try_claim, "interactive"),
                                       asyncio.to_thread(gate.try_claim, "batch"))
        assert winners.count(True) == 1
        gate.release("wrong-owner")
        assert gate.holder is not None
        gate.release(gate.holder)
        assert gate.holder is None
    asyncio.run(scenario())


def test_schema_v2_upgrade_and_recover(tmp_db):
    storage._ensure_initialized()
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO sessions(id,ticker,trade_date,decision_action,decision_confidence,decision_reasoning,created_at,events_json) VALUES('old','AAPL','2026-01-01','neutral',0.5,'old','2026-01-01','[]')")
        conn.execute("DROP INDEX idx_runs_batch")
        for column in ("batch_id", "batch_index", "batch_total"):
            conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        conn.execute("DROP INDEX idx_cost_reservations_run")
        conn.execute("ALTER TABLE cost_reservations DROP COLUMN run_id")
        conn.execute("DROP TABLE batch_items")
        conn.execute("DROP TABLE batches")
        conn.execute("UPDATE schema_version SET version=2")
    storage._reset_for_tests()
    storage._ensure_initialized()
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT ticker FROM sessions WHERE id='old'").fetchone()[0] == "AAPL"
        conn.execute("INSERT INTO batches(batch_id,owner,client_key,fingerprint,state,created_at) VALUES('b','local','k','f','running',1)")
        conn.execute("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES('b',0,'AAPL','running')")
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,batch_id,batch_index) VALUES('r','local','running',1,'b',0)")
    rm = RunManager()
    rm.recover()
    BatchManager(rm).recover()
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert conn.execute("SELECT state FROM batches WHERE batch_id='b'").fetchone()[0] == "interrupted"


def test_run_scope_scrubber_cleans_events_replay_and_database(tmp_db):
    values = ('plainRandomToken98765', 'local-key-123456', 'https://hook.test/bot-token-987654',
              'quoted"\\secret98765', 'plainRandomToken98765-more')
    frame = {"ticker": "AAPL", "provider_config": {"auth": {"api_key": values[0], "access": values[1]}},
             "webhooks": [{"url": values[2], "token": values[3]}]}

    async def executor(sink):
        for value in values:
            await sink.send_json({"type": "agent.message", "content": value, "echo-" + value: "key echo"})
        raise RuntimeError(" ".join(values))

    async def scenario():
        mgr = RunManager()
        run, _ = await mgr.start_or_attach("owner", frame, executor)
        await run.task
        replay, _ = run.subscribe()
        _assert_no_secret(replay, values)
        _assert_no_secret(run.status(), values)
    asyncio.run(scenario())
    with sqlite3.connect(tmp_db) as conn:
        for table in ("runs", "run_events", "sessions", "batches", "batch_items"):
            _assert_no_secret(_db_values(conn, table), values)


def test_batch_key_rotates_plain_secret_but_rejects_setting_change(tmp_db):
    from engine.batch_manager import BatchError

    async def executor(sink):
        await sink.send_json({"type": "session.complete", "decision": {}})
        sink.run.session_id = "fake-session"

    async def scenario():
        mgr = BatchManager(RunManager(), executor)
        body = _body()
        first = await mgr.create("owner", body)
        rotated = _body(provider_config={"provider": "openai", "auth": {"type": "api_key", "api_key": "rotated-key-98765"},
                                         "model": "gpt-4o-mini", "max_tokens": 50})
        assert (await mgr.create("owner", rotated))["batch_id"] == first["batch_id"]
        changed = _body(tw_dexter=True)
        for changed in (changed, _body(pro_config={"quick_think_llm": "other"}),
                        _body(webhooks=[{"url": "https://first.test/hook"}]),
                        _body(provider_config={"provider": "openai", "auth": {"type": "api_key", "api_key": "rotated-key-98765"},
                                               "model": "gpt-4o", "max_tokens": 50})):
            try:
                await mgr.create("owner", changed)
            except BatchError as e:
                assert e.code == "idempotency_conflict"
            else:
                assert False
        for task in list(mgr._tasks):
            await task
    asyncio.run(scenario())


def test_webhook_url_and_tokens_rotate_but_structure_conflicts(tmp_db):
    from engine.batch_manager import BatchError

    async def executor(sink):
        await sink.send_json({"type": "run.error", "error": "synthetic"})

    async def scenario():
        mgr = BatchManager(RunManager(), executor)
        first = _body(webhooks=[{"id": "one", "kind": "slack", "enabled": True,
                                  "url": "https://hooks.slack.test/services/first-secret-12345",
                                  "custom_token": "first-token-12345"}])
        bid = (await mgr.create("owner", first))["batch_id"]
        rotated = _body(webhooks=[{"id": "one", "kind": "slack", "enabled": True,
                                    "url": "https://hooks.slack.test/services/second-secret-67890",
                                    "custom_token": "second-token-67890"}])
        assert (await mgr.create("owner", rotated))["batch_id"] == bid
        for hooks in ([{**rotated["start"]["webhooks"][0], "enabled": False}],
                      [{**rotated["start"]["webhooks"][0], "kind": "discord"}],
                      rotated["start"]["webhooks"] + [{"id": "two", "url": "https://example.test/hook"}]):
            try:
                await mgr.create("owner", _body(webhooks=hooks))
            except BatchError as exc:
                assert exc.code == "idempotency_conflict"
            else:
                assert False
        for task in list(mgr._tasks): await task
    asyncio.run(scenario())


def test_scrubber_embedded_keys_short_boundary_and_local_url():
    long = 'abc"\\secret-123456789'
    short = "x"
    encoded = quote("plain+Password123", safe="")
    url = f"http://alice:{encoded}@localhost:18000/v1"
    scrubber = secret_refs.Scrubber({"provider_config": {"auth": {"api_key": long, "refresh": short,
                                                                   "base_url": url}}})
    cleaned = scrubber.clean({"pre" + long + "post": "pre" + long + "post", "short": "prefixpost x"})
    _assert_no_secret(cleaned, (long,))
    assert cleaned["short"] == "prefixpost [REDACTED]"
    echoed = " ".join(("plain+Password123", encoded, url, "alice:plain+Password123"))
    _assert_no_secret(scrubber.clean_error(echoed), ("plain+Password123", encoded, url, "alice:plain+Password123"))
    assert scrubber.clean("source https://news.test/article") == "source https://news.test/article"


def test_batch_unique_ticker_limit_after_dedup(tmp_db):
    from engine.batch_manager import BatchError
    body = _body()
    body["tickers"] = [" aapl " for _ in range(51)]
    tickers, *_ = BatchManager._validate(body)
    assert tickers == ["AAPL"]
    body["tickers"] = ["AAPL"] * 501
    try:
        BatchManager._validate(body)
    except BatchError as exc:
        assert exc.status == 400
    else:
        assert False
    body["tickers"] = [f"T{i}" for i in range(51)]
    try:
        BatchManager._validate(body)
    except BatchError as exc:
        assert exc.status == 400
    else:
        assert False


def test_resolved_short_secret_stops_batch_before_model(tmp_db, monkeypatch):
    class Store:
        def get(self, name): return "short" if name == "llm:openai" else None
    secret_refs.configure(Store())
    try:
        app = server.build_app(token="t")
        mgr = app.state.batch_manager
        body = _body(provider_config={"provider": "openai", "auth": {"type": "api_key",
                                                               "api_key": "@@secret:llm:openai@@"},
                                      "model": "gpt-4o-mini", "max_tokens": 50})
        async def scenario():
            bid = (await mgr.create("owner", body))["batch_id"]
            for task in list(mgr._tasks): await task
            batch = mgr.get("owner", bid)
            assert batch["state"] == "stopped" and batch["terminal_code"] == "invalid_credential"
            assert batch["items"][0]["error_code"] == "invalid_credential"
            assert batch["counts"]["skipped"] == 2
            run = mgr.runs.get("owner", batch["items"][0]["run_id"])
            assert run.close_code == (1008, "invalid_credential")
        asyncio.run(scenario())
    finally:
        secret_refs.configure(None)


def test_batch_owner_scope_and_invalid_fields(tmp_db):
    from engine.batch_manager import BatchError

    async def executor(sink):
        await sink.send_json({"type": "run.error", "error": "synthetic failure"})
        await sink.close(1008, "run_error")

    async def scenario():
        mgr = BatchManager(RunManager(), executor)
        batch = await mgr.create("alice", _body())
        bid = batch["batch_id"]
        assert mgr.list("bob", 5) == []
        for action in (lambda: mgr.get("bob", bid), lambda: mgr.get("alice", "missing")):
            try:
                action()
            except BatchError as e:
                assert e.status == 404 and e.code == "batch_not_found"
            else:
                assert False
        try:
            await mgr.cancel("bob", bid)
        except BatchError as e:
            assert e.status == 404
        else:
            assert False
        for bad in (_body(reservation_id="secret-value-123"),
                    _body(provider_config={"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}})):
            try:
                await mgr.create("alice", bad)
            except BatchError as e:
                assert e.status == 400 and "secret-value-123" not in str(e.detail())
            else:
                assert False
        for task in list(mgr._tasks):
            await task
    asyncio.run(scenario())


def test_new_key_rejected_while_batch_active_and_single_failure_continues(tmp_db):
    from engine.batch_manager import BatchError

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def executor(sink):
            ticker = (await sink.receive_json())["ticker"]
            calls.append(ticker)
            if ticker == "AAPL":
                started.set()
                await release.wait()
                await sink.send_json({"type": "run.error", "error": "synthetic"})
                await sink.close(1008, "run_error")
            else:
                await sink.send_json({"type": "session.complete", "decision": {"stance": "neutral"}})
                sink.run.session_id = "session-" + ticker

        mgr = BatchManager(RunManager(), executor)
        bid = (await mgr.create("owner", _body()))["batch_id"]
        await started.wait()
        try:
            await mgr.create("owner", _body("another-key-123"))
        except BatchError as e:
            assert e.status == 409 and e.code == "batch_active" and e.batch_id == bid
        else:
            assert False
        release.set()
        for task in list(mgr._tasks):
            await task
        result = mgr.get("owner", bid)
        assert result["state"] == "completed"
        assert result["counts"]["failed"] == 1 and result["counts"]["completed"] == 2
        assert calls == ["AAPL", "MSFT", "NVDA"]
    asyncio.run(scenario())


def test_oauth_refresh_checks_returned_expiry_after_slot(tmp_db):
    async def scenario():
        manager = BatchManager(RunManager())
        bid = "batch-id"
        manager._templates[bid] = {"provider_config": {"auth": {"type": "oauth", "access": "old-token-12345",
                                                           "expires": time.time() - 1}}}
        run = __import__("engine.run_manager", fromlist=["Run"]).Run(manager.runs, "r", "o", {}, None)
        run.batch_id = bid
        manager.oauth_refresh = lambda: {"access": "new-token-12345", "expires": (time.time() + 700) * 1000}
        assert await manager._prepare(run)
        assert manager._templates[bid]["provider_config"]["auth"]["expires"] > 1e12
        manager.oauth_refresh = lambda: {"access": "old-token-12345", "expires": time.time() - 1}
        assert not await manager._prepare(run)
        assert run.terminal_code == "oauth_refresh_failed"
        manager.oauth_refresh = None
        manager._templates[bid]["provider_config"]["auth"]["expires"] = time.time() - 1
        assert not await manager._prepare(run)
        assert run.terminal_code == "oauth_expired"
    asyncio.run(scenario())


def test_oauth_expires_while_waiting_for_real_slot(tmp_db, monkeypatch):
    from engine import batch_manager as batch_mod

    clock = [1_800_000_000.0]
    monkeypatch.setattr(batch_mod, "time", SimpleNamespace(time=lambda: clock[0]))
    original = SlotGate.wait_claim
    claims = []

    async def delayed_claim(self, run_id, cancelled, **kwargs):
        assert self.try_claim("blocker")
        claims.append(run_id)
        await asyncio.sleep(0)
        clock[0] += 100
        self.release("blocker")
        return await original(self, run_id, cancelled, **kwargs)

    monkeypatch.setattr(SlotGate, "wait_claim", delayed_claim)
    app = server.build_app(token="t")
    mgr = app.state.batch_manager
    mgr.oauth_refresh = lambda: {"access": "fresh-access-token-12345", "expires": clock[0] + 900}
    body = _body(provider_config={"provider": "openai", "auth": {"type": "oauth", "access": "old-access-token-12345",
                                                               "expires": clock[0] + 650}, "model": "gpt-4o-mini", "max_tokens": 50})
    async def scenario():
        bid = (await mgr.create("owner", body))["batch_id"]
        for task in list(mgr._tasks):
            await task
        assert len(claims) == 3
        assert mgr.get("owner", bid)["state"] == "completed"
    # The actual executor is used; only external model/data calls are replaced.
    async def empty(*args, **kwargs): return []
    async def no_text(*args, **kwargs): return ""
    class Adapter:
        async def open(self, **kwargs): pass
        async def close(self): pass
        async def complete(self, **kwargs): return ("neutral", 1, 1)
    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: Adapter())
    monkeypatch.setattr(server, "_fetch_summary_safe", empty)
    monkeypatch.setattr(server, "_fetch_news_safe", empty)
    monkeypatch.setattr(server.sentiment_sources, "fetch_stocktwits_messages_async", no_text)
    monkeypatch.setattr(server.sentiment_sources, "fetch_reddit_posts_async", no_text)
    asyncio.run(scenario())


def test_oauth_refresh_error_timeout_and_cancel(tmp_db, monkeypatch):
    from engine import batch_manager as mod
    from engine.run_manager import Run

    async def scenario():
        manager = BatchManager(RunManager())
        manager._templates["b"] = {"provider_config": {"auth": {"type": "oauth", "access": "old-token-12345",
                                                            "expires": time.time() - 1}}}
        run = Run(manager.runs, "r", "o", {}, None)
        run.batch_id = "b"
        manager.oauth_refresh = lambda: (_ for _ in ()).throw(RuntimeError("refresh failed"))
        assert not await manager._prepare(run) and run.terminal_code == "oauth_refresh_failed"

        async def timeout(*args):
            raise asyncio.TimeoutError()
        monkeypatch.setattr(mod, "run_in_threadpool", timeout)
        manager.oauth_refresh = lambda: None
        assert not await manager._prepare(run) and run.terminal_code == "oauth_refresh_failed"

        async def cancellation(*args):
            run.cancel_requested = True
            return {"access": "new-token-12345", "expires": time.time() + 800}
        monkeypatch.setattr(mod, "run_in_threadpool", cancellation)
        try:
            await manager._prepare(run)
        except asyncio.CancelledError:
            pass
        else:
            assert False, "cancelled OAuth hook started the run"
    asyncio.run(scenario())


def test_recover_settles_reservation_linked_by_run_id(tmp_db):
    storage._ensure_initialized()
    reservation = cost_guard.reserve(model="gpt-4o-mini", auth_kind="api_key", max_tokens=50, run_id="crashed-run")
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,engine_started) VALUES('crashed-run','owner','running',1,1)")
    assert RunManager().recover() == 1
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT finalized FROM cost_reservations WHERE id=?", (reservation.reservation_id,)).fetchone()[0] == 1


def test_recover_settles_orphan_reservation_without_run_row(tmp_db):
    storage._ensure_initialized()
    reservation = cost_guard.reserve(model="gpt-4o-mini", auth_kind="api_key", max_tokens=50, run_id="missing-run")
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='missing-run'").fetchone()[0] == 0
    RunManager().recover()
    with sqlite3.connect(tmp_db) as conn:
        row = conn.execute("SELECT finalized,finalized_cost_usd FROM cost_reservations WHERE id=?", (reservation.reservation_id,)).fetchone()
        assert row == (1, 0.0)


def test_batch_first_run_metadata_failure_stops_before_executor(tmp_db, monkeypatch):
    async def scenario():
        calls = []
        mgr = RunManager()
        original = mgr._save_meta
        def fail_initial(run, *, strict=False):
            if strict and run.batch_id and run.state == "queued":
                raise OSError("disk offline")
            return original(run, strict=strict)
        monkeypatch.setattr(mgr, "_save_meta", fail_initial)
        batch_mgr = BatchManager(mgr, lambda sink: calls.append(sink.run.id))
        bid = (await batch_mgr.create("owner", _body()))["batch_id"]
        for task in list(batch_mgr._tasks): await task
        batch = batch_mgr.get("owner", bid)
        assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
        assert batch["counts"]["failed"] == 1 and batch["counts"]["skipped"] == 2
        assert calls == [] and mgr.runs == {}
    asyncio.run(scenario())


def test_batch_db_stale_terminal_row_stops_before_next_item(tmp_db, monkeypatch):
    async def executor(sink):
        calls.append(sink.run.id)
        sink.run.session_id = "persisted-session"
        await sink.send_json({"type": "session.complete", "decision": {"stance": "neutral"}})

    async def scenario():
        mgr = RunManager()
        original = mgr._save_meta
        def stale_final(run, *, strict=False):
            if run.batch_id and run.state == "completed": return
            return original(run, strict=strict)
        monkeypatch.setattr(mgr, "_save_meta", stale_final)
        batch_mgr = BatchManager(mgr, executor)
        bid = (await batch_mgr.create("owner", _body()))["batch_id"]
        for task in list(batch_mgr._tasks): await task
        batch = batch_mgr.get("owner", bid)
        assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
        assert batch["items"][0]["error_code"] == "session_persist_failed"
        assert batch["counts"]["skipped"] == 2 and len(calls) == 1
        with sqlite3.connect(tmp_db) as conn:
            assert conn.execute("SELECT state FROM runs WHERE run_id=?", (calls[0],)).fetchone()[0] == "running"
    calls = []
    asyncio.run(scenario())


def test_batch_terminal_metadata_retries_twice_and_fails_closed(tmp_db, monkeypatch):
    attempts = []
    calls = []
    async def executor(sink):
        calls.append(1)
        sink.run.session_id = "s"
        await sink.send_json({"type": "session.complete", "decision": {}})

    async def scenario():
        runs = RunManager()
        original = runs._save_meta
        def fail_terminal(run, *, strict=False):
            if run.batch_id and run.state == "completed":
                if strict:
                    attempts.append(1)
                    raise OSError("write failed")
                return
            return original(run, strict=strict)
        monkeypatch.setattr(runs, "_save_meta", fail_terminal)
        mgr = BatchManager(runs, executor)
        bid = (await mgr.create("owner", _body()))["batch_id"]
        for task in list(mgr._tasks): await task
        batch = mgr.get("owner", bid)
        assert len(attempts) == 2 and len(calls) == 1
        assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
        assert batch["items"][0]["error_code"] == "session_persist_failed"
    asyncio.run(scenario())


def test_batch_recover_restores_decision_and_cost_from_session(tmp_db):
    storage._ensure_initialized()
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO batches(batch_id,owner,client_key,fingerprint,state,created_at) VALUES('b','owner','key','fingerprint','running',1)")
        conn.execute("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES('b',0,'AAPL','running')")
        conn.execute("INSERT INTO sessions(id,ticker,trade_date,decision_action,decision_confidence,decision_reasoning,created_at,events_json,estimated_cost_usd,elapsed_s) VALUES('s','AAPL','2026-01-01','buy',0.81,'reason','2026-01-01','[]',1.25,12.5)")
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,batch_id,batch_index,session_id) VALUES('r','owner','completed',1,'b',0,'s')")
    mgr = BatchManager(RunManager())
    mgr.recover()
    item = mgr.get("owner", "b")["items"][0]
    assert item["state"] == "completed" and item["stance"] == "buy" and item["conviction"] == 0.81
    assert item["estimated_cost_usd"] == 1.25 and item["elapsed_s"] == 12.5


def test_batch_recover_restores_result_from_completion_event(tmp_db):
    storage._ensure_initialized()
    event = {"type": "session.complete", "decision": {"stance": "sell", "conviction": 0.77},
             "estimated_cost_usd": 2.5, "elapsed_s": 17.0}
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO batches(batch_id,owner,client_key,fingerprint,state,created_at) VALUES('b','owner','key','fingerprint','running',1)")
        conn.execute("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES('b',0,'AAPL','running')")
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,batch_id,batch_index,session_id) VALUES('r','owner','completed',1,'b',0,'s')")
        conn.execute("INSERT INTO run_events(run_id,seq,ts,etype,payload) VALUES('r',1,1,'session.complete',?)", (json.dumps(event),))
    mgr = BatchManager(RunManager())
    mgr.recover()
    item = mgr.get("owner", "b")["items"][0]
    assert item["state"] == "completed" and item["stance"] == "sell" and item["conviction"] == 0.77
    assert item["estimated_cost_usd"] == 2.5 and item["elapsed_s"] == 17.0


def test_batch_recover_uses_run_link_and_requires_session(tmp_db):
    storage._ensure_initialized()
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO batches(batch_id,owner,client_key,fingerprint,state,created_at) VALUES('b','owner','key','fingerprint','running',1)")
        conn.execute("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES('b',0,'AAPL','running')")
        conn.execute("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES('b',1,'MSFT','pending')")
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,batch_id,batch_index) VALUES('r','owner','completed',1,'b',0)")
    mgr = BatchManager(RunManager())
    mgr.recover()
    snapshot = mgr.get("owner", "b")
    assert snapshot["state"] == "stopped" and snapshot["terminal_code"] == "storage_unavailable"
    assert snapshot["items"][0]["state"] == "failed"
    assert snapshot["items"][0]["run_id"] == "r"
    assert snapshot["items"][0]["error_code"] == "session_persist_failed"
    assert snapshot["items"][1]["state"] == "skipped"


def test_cancel_active_batch_is_idempotent_and_skips_remaining(tmp_db):
    async def scenario():
        started = asyncio.Event()
        calls = []

        async def executor(sink):
            calls.append(sink.run.id)
            started.set()
            await asyncio.Event().wait()

        mgr = BatchManager(RunManager(), executor)
        batch = await mgr.create("owner", _body())
        bid = batch["batch_id"]
        await started.wait()
        first = await mgr.cancel("owner", bid)
        second = await mgr.cancel("owner", bid)
        for task in list(mgr._tasks):
            await task
        final = mgr.get("owner", bid)
        assert first["cancel_requested"] and second["cancel_requested"]
        assert final["state"] == "cancelled" and final["terminal_code"] == "user_cancelled"
        assert final["counts"]["skipped"] == 2
        assert len(calls) == 1
    asyncio.run(scenario())


def test_cost_block_stops_batch_and_skips_remaining(tmp_db):
    async def scenario():
        calls = []

        async def executor(sink):
            calls.append(sink.run.id)
            await sink.send_json({"type": "cost.blocked", "over_dimension": "daily"})
            await sink.close(1008, "cost_guard_blocked")

        mgr = BatchManager(RunManager(), executor)
        bid = (await mgr.create("owner", _body()))["batch_id"]
        for task in list(mgr._tasks):
            await task
        batch = mgr.get("owner", bid)
        assert batch["state"] == "stopped" and batch["terminal_code"] == "cost_guard_blocked"
        assert batch["counts"]["failed"] == 1 and batch["counts"]["skipped"] == 2
        assert len(calls) == 1
    asyncio.run(scenario())


def test_item_write_failure_finishes_current_run_without_starting_next(tmp_db, monkeypatch):
    async def scenario():
        calls = []

        async def executor(sink):
            calls.append(sink.run.id)
            await sink.send_json({"type": "session.complete", "decision": {}})
            sink.run.session_id = "saved"

        mgr = BatchManager(RunManager(), executor)
        original = mgr._update
        failures = 0

        def fail_twice(bid, table, fields, index=None):
            nonlocal failures
            if table == "batch_items" and fields.get("state") == "waiting_slot" and failures < 1:
                failures += 1
                raise OSError("storage offline")
            return original(bid, table, fields, index)

        monkeypatch.setattr(mgr, "_update", fail_twice)
        bid = (await mgr.create("owner", _body()))["batch_id"]
        for task in list(mgr._tasks):
            await task
        batch = mgr.get("owner", bid)
        assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
        assert len(calls) == 1
    asyncio.run(scenario())


def test_real_executor_running_write_fails_twice_then_storage_recovers(tmp_db, monkeypatch):
    app = server.build_app(token="t")
    mgr = app.state.batch_manager
    original_db = mgr._db
    failures = []
    model_calls = []

    def fail_running_db(fn):
        captures = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in (fn.__closure__ or ()))))
        sql = captures.get("sql", "")
        params = captures.get("params", [])
        if sql.startswith("UPDATE batches SET") and params and params[0] == "running" and len(failures) < 2:
            failures.append(1)
            raise OSError("transient DB failure")
        return original_db(fn)

    monkeypatch.setattr(mgr, "_db", fail_running_db)

    class Adapter:
        async def open(self, **kwargs): pass
        async def close(self): pass
        async def complete(self, **kwargs):
            model_calls.append(1)
            return ("neutral", 1, 1)
    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: Adapter())

    async def scenario():
        bid = (await mgr.create("owner", _body()))["batch_id"]
        for task in list(mgr._tasks): await task
        batch = mgr.get("owner", bid)
        assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
        assert batch["counts"]["skipped"] == 2 and len(failures) == 2 and model_calls == []
        run = mgr.runs.get("owner", batch["items"][0]["run_id"])
        assert run.terminal_code == "storage_unavailable" and run.close_code == (1008, "storage_unavailable")
        assert any(e.get("type") == "run.error" and e.get("code") == "storage_unavailable" for e in run.events)
    asyncio.run(scenario())


def test_batch_secret_is_absent_from_rest_database_and_log(tmp_db, monkeypatch, capsys):
    secret = 'random"\\secret-123456789'

    class BadAdapter:
        async def open(self, **kwargs):
            pass

        async def close(self):
            pass

        async def complete(self, **kwargs):
            raise RuntimeError(secret)

    async def no_summary(*args, **kwargs):
        return None

    async def empty(*args, **kwargs):
        return []

    async def no_text(*args, **kwargs):
        return ""

    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: BadAdapter())
    monkeypatch.setattr(server, "_fetch_summary_safe", no_summary)
    monkeypatch.setattr(server, "_fetch_news_safe", empty)
    monkeypatch.setattr(server.sentiment_sources, "fetch_stocktwits_messages_async", no_text)
    monkeypatch.setattr(server.sentiment_sources, "fetch_reddit_posts_async", no_text)
    body = _body("secret-key-123", provider_config={"provider": "openai", "auth": {"type": "api_key", "api_key": secret},
                                                     "model": "gpt-4o-mini", "max_tokens": 50})
    body["tickers"] = ["AAPL"]
    headers = {"Authorization": "Bearer t"}
    with TestClient(server.build_app(token="t")) as client:
        post = client.post("/batches", json=body, headers=headers)
        assert post.status_code == 200, post.text
        bid = post.json()["batch"]["batch_id"]
        time.sleep(1)
        responses = [client.get(f"/batches/{bid}", headers=headers).text,
                     client.get("/batches", headers=headers).text]
        run_id = client.get(f"/batches/{bid}", headers=headers).json()["items"][0]["run_id"]
        if run_id:
            responses.extend([client.get(f"/runs/{run_id}", headers=headers).text,
                              client.get(f"/runs/{run_id}/events", headers=headers).text])
        for response in responses:
            _assert_no_secret(json.loads(response), (secret,))
    with sqlite3.connect(tmp_db) as conn:
        for table in ("batches", "batch_items", "runs", "run_events", "sessions", "cost_reservations"):
            _assert_no_secret(_db_values(conn, table), (secret,))
    log = capsys.readouterr().err
    assert secret not in log and json.dumps(secret, ensure_ascii=False)[1:-1] not in log


def test_taiwan_snapshot_scope_for_single_and_batch_owner(tmp_db, tmp_path, monkeypatch):
    from engine import full_debate, tw_graph_tools, tw_snapshot, tw_research
    from engine.secret_store import SecretStore
    from engine.tests.test_web_app import COOKIE, LAUNCH, MemKeyring, PORT
    from engine.web_app import ProxyConfig, build_web_app, COOKIE_NAME
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("TAL_TW_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(tw_graph_tools, "install", lambda: None)
    monkeypatch.setattr(tw_graph_tools, "quote_summary_event", lambda *a: None)
    monkeypatch.setattr(tw_graph_tools, "library_config", lambda: {"data_vendors": {}})
    monkeypatch.setattr(tw_graph_tools, "source_health", lambda: {})
    monkeypatch.setattr(tw_graph_tools, "limitations", lambda lang: [])

    async def fake_dexter(**kwargs):
        return {"block": {"ran": False, "status": "skipped", "reason": "disabled", "verified": 0},
                "event": {"type": "dexter.result", "status": "skipped"}, "ran": False,
                "d_in": 0, "d_out": 0, "d_cost": 0.0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0},
                "measured": False, "raw_usage": {}}

    async def fake_graph(*args, **kwargs):
        yield {"type": "session.complete", "ticker": "2330", "decision": {"stance": "neutral", "conviction": 0.5},
               "provider": "openai", "model": "gpt-4o-mini", "estimated_cost_usd": 0.0}

    monkeypatch.setattr(tw_research, "full_graph_dexter", fake_dexter)
    monkeypatch.setattr(full_debate, "full_debate", fake_graph)
    static = tmp_path / "dist"
    static.mkdir()
    (static / "assets").mkdir()
    (static / "index.html").write_text("<html></html>")
    origin = "https://m3.tailnet.example:47900"
    proxy = ProxyConfig(public_origin=origin, base_path="/lab-pro", secret="p" * 40,
                        owners=frozenset({"alice@example.com", "bob@example.com"}))
    app = build_web_app(token="t", port=PORT, data_dir=tmp_path / "web-data", static_dir=static,
                        launch_secret=LAUNCH, cookie_value=COOKIE, store=SecretStore(MemKeyring()), proxy=proxy)

    def make_client(owner):
        c = TestClient(app, base_url=origin, client=("127.0.0.1", 50000))
        c.cookies.set(COOKIE_NAME, COOKIE)
        headers = {"host": "m3.tailnet.example:47900", "x-tal-proxy-auth": "p" * 40,
                   "x-tal-owner": owner, "origin": origin, "cookie": f"{COOKIE_NAME}={COOKIE}"}
        return c, headers

    start = {"ticker": "2330", "trade_date": "2026-11-09", "engine": "pro", "tw_flow": "full", "tw_dexter": False,
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "fake-model-key-123456"},
                                 "model": "gpt-4o-mini", "max_tokens": 50}}
    alice, ah = make_client("alice@example.com")
    bob, bh = make_client("bob@example.com")
    with alice, bob:
        with alice.websocket_connect("/stream", headers=ah) as ws:
            ws.send_json(start)
            sid_a = None
            try:
                while True:
                    event = ws.receive_json()
                    if event.get("type") == "session.complete":
                        sid_a = event["evidence_snapshot"]["snapshot_id"]
            except WebSocketDisconnect:
                pass
            assert sid_a
        body = {"tickers": ["2330"], "client_batch_key": "owner-test-key-123", "start": {k: v for k, v in start.items() if k != "ticker"}}
        post = bob.post("/batches", json=body, headers=bh)
        assert post.status_code == 200, post.text
        bid = post.json()["batch"]["batch_id"]
        time.sleep(1)
        item = bob.get(f"/batches/{bid}", headers=bh).json()["items"][0]
        events = bob.get(f'/runs/{item["run_id"]}/events', headers=bh).json()
        sid_b = next(e["evidence_snapshot"]["snapshot_id"] for e in events["events"] if e["type"] == "session.complete")
        assert alice.get(f"/tw/run-snapshots/{sid_a}", headers=ah).status_code == 200
        assert bob.get(f"/tw/run-snapshots/{sid_b}", headers=bh).status_code == 200
        assert alice.get(f"/tw/run-snapshots/{sid_b}", headers=ah).status_code == 404
        assert bob.get(f"/tw/run-snapshots/{sid_a}", headers=bh).status_code == 404
