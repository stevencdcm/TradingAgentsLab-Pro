"""D64 protocol-safe scrubbing and terminal reservation reconciliation."""

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine import cost_guard, secret_refs, server, storage
from engine.run_manager import RunManager


def _strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_protocol_named_secrets_single_and_batch_complete(tmp_db, monkeypatch):
    class Store:
        def get(self, name): return "session.complete" if name == "llm:openai" else None
    monkeypatch.setattr(secret_refs, "_store", Store())
    class Adapter:
        async def open(self, **kwargs): pass
        async def close(self): pass
        async def complete(self, **kwargs): return ("free text session.complete decision", 1, 1)

    async def none(*args, **kwargs): return None
    async def empty(*args, **kwargs): return []
    async def blank(*args, **kwargs): return ""
    monkeypatch.setattr("engine.live_debate.adapter_for", lambda c: Adapter())
    monkeypatch.setattr(server, "_fetch_summary_safe", none)
    monkeypatch.setattr(server, "_fetch_news_safe", empty)
    monkeypatch.setattr(server.sentiment_sources, "fetch_stocktwits_messages_async", blank)
    monkeypatch.setattr(server.sentiment_sources, "fetch_reddit_posts_async", blank)
    app = server.build_app(token="d64")
    headers = {"Authorization": "Bearer d64"}

    def start(secret):
        return {"ticker": "AAPL", "trade_date": "2026-11-09", "engine": "free",
                "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key":
                                    "@@secret:llm:openai@@" if secret == "session.complete" else secret},
                                    "model": "gpt-4o-mini", "max_tokens": 50}, "resumable": True}

    with TestClient(app) as client:
        for index, secret in enumerate(("session.complete", "decision")):
            frame = start(secret)
            with client.websocket_connect("/stream?token=d64") as ws:
                ws.send_json(frame)
                events = []
                try:
                    while True:
                        events.append(ws.receive_json())
                except WebSocketDisconnect:
                    pass
            assert any(e.get("type") == "session.complete" for e in events)
            assert any(e.get("type") == "run.finished" and e.get("state") == "completed" for e in events), events[-4:]
            run_id = next(e["run_id"] for e in events if e.get("type") == "run.attached")
            status = client.get(f"/runs/{run_id}", headers=headers).json()
            assert status["state"] == "completed" and status["session_id"]
            replay = client.get(f"/runs/{run_id}/events", headers=headers).json()
            assert any(e.get("type") == "session.complete" for e in replay["events"])
            assert all(secret not in text for text in _strings(replay) if text != "session.complete" and text != "decision")

            batch_start = {k: v for k, v in frame.items() if k not in ("ticker", "resumable")}
            body = {"tickers": ["MSFT"], "client_batch_key": f"d64-batch-{index}", "start": batch_start}
            post = client.post("/batches", headers=headers, json=body)
            assert post.status_code == 200, post.text
            bid = post.json()["batch"]["batch_id"]
            for _ in range(100):
                batch = client.get(f"/batches/{bid}", headers=headers).json()
                if not batch["active"]: break
                time.sleep(0.05)
            assert batch["state"] == "completed", batch
            run_id = batch["items"][0]["run_id"]
            status = client.get(f"/runs/{run_id}", headers=headers).json()
            assert status["state"] == "completed" and status["session_id"]
            replay = client.get(f"/runs/{run_id}/events", headers=headers).json()
            assert any(e.get("type") == "session.complete" for e in replay["events"])
    with sqlite3.connect(tmp_db) as conn:
        rows = conn.execute("SELECT decision_action,events_json FROM sessions").fetchall()
        assert len(rows) == 4
        for _, payload in rows:
            events = json.loads(payload)
            assert any(e.get("type") == "session.complete" and "decision" in e for e in events)
            assert "[REDACTED]" in payload and "free text session.complete decision" not in payload


@pytest.mark.parametrize("batched", [False, True])
def test_terminal_hold_recovers_after_two_settle_db_failures(tmp_db, monkeypatch, batched):
    async def none(*args, **kwargs): return None
    async def empty(*args, **kwargs): return []
    async def blank(*args, **kwargs): return ""
    monkeypatch.setattr(server, "_fetch_summary_safe", none)
    monkeypatch.setattr(server, "_fetch_news_safe", empty)
    monkeypatch.setattr(server.sentiment_sources, "fetch_stocktwits_messages_async", blank)
    monkeypatch.setattr(server.sentiment_sources, "fetch_reddit_posts_async", blank)
    app = server.build_app(token="d64")
    runs = app.state.run_manager
    original_save = runs._save_meta
    failed_strict = []

    def fail_before_billing(run, *, strict=False):
        if strict and run.engine_started and not failed_strict:
            failed_strict.append(1)
            raise OSError("metadata disk fault")
        return original_save(run, strict=strict)

    monkeypatch.setattr(runs, "_save_meta", fail_before_billing)
    original_settle = cost_guard.settle_run_end
    original_connect = cost_guard._connect
    failed_settles = []

    def flaky_settle(reservation_id, *, billable_started):
        if len(failed_settles) < 2:
            failed_settles.append(1)
            def broken_connect(): raise OSError("settlement DB fault")
            monkeypatch.setattr(cost_guard, "_connect", broken_connect)
            try: return original_settle(reservation_id, billable_started=billable_started)
            finally: monkeypatch.setattr(cost_guard, "_connect", original_connect)
        return original_settle(reservation_id, billable_started=billable_started)

    monkeypatch.setattr(cost_guard, "settle_run_end", flaky_settle)
    body = {"tickers": ["AAPL", "MSFT"], "client_batch_key": "d64-storage-fail",
            "start": {"trade_date": "2026-11-09", "engine": "free", "provider_config": {
                "provider": "openai", "auth": {"type": "api_key", "api_key": "fake-key-d64-123456"},
                "model": "gpt-4o-mini", "max_tokens": 50}}}
    async def scenario():
        mgr = app.state.batch_manager
        if batched:
            bid = (await mgr.create("owner", body))["batch_id"]
            for task in list(mgr._tasks): await task
            batch = mgr.get("owner", bid)
            assert batch["state"] == "stopped" and batch["terminal_code"] == "storage_unavailable"
            assert batch["counts"]["skipped"] == 1
            return batch["items"][0]["run_id"]
        frame = {**body["start"], "ticker": "AAPL"}
        run, _ = await runs.start_or_attach("owner", frame, mgr.executor)
        await run.task
        assert run.state == "failed" and run.terminal_code == "storage_unavailable"
        return run.id
    run_id = asyncio.run(scenario())
    assert len(failed_strict) == 1 and len(failed_settles) == 2
    with sqlite3.connect(tmp_db) as conn:
        row = conn.execute("SELECT state,engine_started,reservation_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert row[0] == "failed" and row[1] == 0 and row[2]
        assert conn.execute("SELECT finalized FROM cost_reservations WHERE id=?", (row[2],)).fetchone()[0] == 0
        reservation_id = row[2]
    RunManager().recover()
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT finalized,finalized_cost_usd FROM cost_reservations WHERE id=?", (reservation_id,)).fetchone() == (1, 0.0)
    RunManager().recover()
    with sqlite3.connect(tmp_db) as conn:
        assert conn.execute("SELECT finalized,finalized_cost_usd FROM cost_reservations WHERE id=?", (reservation_id,)).fetchone() == (1, 0.0)


def test_recover_terminal_holds_billable_and_completed_session(tmp_db):
    storage._ensure_initialized()
    ids = []
    for suffix in ("incomplete", "completed"):
        hold = cost_guard.reserve(model="gpt-4o-mini", auth_kind="api_key", max_tokens=50, run_id=suffix)
        ids.append(hold.reservation_id)
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,engine_started) VALUES('incomplete','owner','failed',?,1)", (time.time(),))
        conn.execute("INSERT INTO sessions(id,ticker,trade_date,decision_action,decision_confidence,decision_reasoning,created_at,events_json,live,estimated_cost_usd,auth_kind) VALUES('saved','AAPL','2026-11-09','buy',0.8,'ok','2026-11-09T00:00:00Z','[]',1,0.5,'api_key')")
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,engine_started,session_id) VALUES('completed','owner','completed',?,1,'saved')", (time.time(),))
    RunManager().recover()
    with sqlite3.connect(tmp_db) as conn:
        rows = [conn.execute("SELECT finalized,settled_as,finalized_cost_usd FROM cost_reservations WHERE id=?", (rid,)).fetchone() for rid in ids]
        assert rows[0][0] == 1 and rows[0][1] == "incomplete"
        assert rows[1] == (1, None, 0.0)


def test_report_secret_stays_clean_after_finished_run_and_batch(tmp_db):
    from engine.cost_report import UsageLedger, build_report
    from engine.batch_manager import BatchManager

    secret = "fake-report-secret-123456"
    ledger = UsageLedger(provider="openai")
    ledger.record(stage="debate", agent=secret, provider="openai", model_requested="gpt-4o-mini",
                  model_billed="gpt-4o-mini", input_tokens=10, output_tokens=2)
    report = build_report(ledger, billing="api_key")
    assert report and secret in json.dumps(report)

    async def executor(sink):
        await sink.send_json({"type": "session.complete", "decision": {"stance": "neutral"},
                              "cost_report": report})
        raise RuntimeError("after completion")

    frame = {"ticker": "AAPL", "trade_date": "2026-11-09", "engine": "free",
             "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": secret},
                                 "model": "gpt-4o-mini", "max_tokens": 50}}
    app = server.build_app(token="d66")
    runs = app.state.run_manager
    app.state.batch_manager.executor = executor

    async def scenario():
        single, _ = await runs.start_or_attach("local", frame, executor)
        await single.task
        assert single.state == "failed" and single.scrubber.values == set()
        batch_body = {"tickers": ["MSFT"], "client_batch_key": "d66-report-secret",
                      "start": {k: v for k, v in frame.items() if k != "ticker"}}
        batch_id = (await app.state.batch_manager.create("local", batch_body))["batch_id"]
        for task in list(app.state.batch_manager._tasks): await task
        return single.id, batch_id

    single_id, batch_id = asyncio.run(scenario())
    headers = {"Authorization": "Bearer d66"}
    with TestClient(app) as client:
        batch = client.get(f"/batches/{batch_id}", headers=headers).json()
        assert batch["state"] == "completed" and batch["counts"]["failed"] == 1
        batch_run_id = batch["items"][0]["run_id"]
        responses = [client.get(f"/runs/{rid}", headers=headers).json() for rid in (single_id, batch_run_id)]
        responses.append(client.get("/runs/active", headers=headers).json())
        responses.append(batch)
        for response in responses:
            assert secret not in json.dumps(response, ensure_ascii=False)
        assert all(r["cost_report"]["run_state"] == "failed" for r in responses[:2])
        recent = responses[2]["recent"]
        assert {single_id, batch_run_id}.issubset({r["run_id"] for r in recent})
    with sqlite3.connect(tmp_db) as conn:
        for (blob,) in conn.execute("SELECT cost_report FROM runs WHERE run_id IN (?,?)", (single_id, batch_run_id)):
            assert secret not in json.dumps(json.loads(blob))


@pytest.mark.parametrize("case,auth_kind,session_cost,expected_before,expected_after,settled_as,finalized_cost", [
    ("exact", "api_key", 0.25, 1.75, 0.25, None, 0.0),
    ("ceiling", "api_key", None, 1.5, 1.5, "ceiling", 1.5),
    ("gap", "api_key", None, 1.5, 1.5, "ceiling", None),
    ("free", "oauth", None, 0.0, 0.0, None, 0.0),
    ("missing", "api_key", None, 1.5, 1.5, "incomplete", None),
])
def test_recover_completed_hold_preserves_spend(tmp_db, case, auth_kind, session_cost,
                                                expected_before, expected_after, settled_as, finalized_cost):
    storage._ensure_initialized()
    hold = cost_guard.reserve(model="claude-haiku-5-5", auth_kind=auth_kind, max_tokens=50, run_id=case)
    report = {"run_state": "completed", "lines": [{"model": "claude-haiku-5-5", "input_tokens": 1_000_000,
                          "output_tokens": 400_000, "usage_unreported_calls": 1 if case == "gap" else 0,
                          "missing": ["usage_not_reported"] if case == "gap" else []}]}
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE cost_reservations SET est_cost_usd=1.5 WHERE id=?", (hold.reservation_id,))
        if case != "missing":
            conn.execute("INSERT INTO sessions(id,ticker,trade_date,decision_action,decision_confidence,decision_reasoning,created_at,events_json,live,estimated_cost_usd,auth_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("session-" + case, "AAPL", "2026-11-09", "neutral", 0.5, "ok", stamp, "[]", 1,
                          session_cost, auth_kind))
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,engine_started,session_id,cost_report) VALUES(?, 'local','completed',?,1,?,?)",
                     (case, now, "session-" + case, json.dumps(report)))
        before = cost_guard._compute_spend(conn)
        assert before.daily_usd == expected_before
    for _ in range(2):
        RunManager().recover()
        with sqlite3.connect(tmp_db) as conn:
            after = cost_guard._compute_spend(conn)
            row = conn.execute("SELECT finalized,settled_as,finalized_cost_usd FROM cost_reservations WHERE id=?", (hold.reservation_id,)).fetchone()
            assert after.daily_usd == expected_after
            assert row == (1, settled_as, finalized_cost)


@pytest.mark.parametrize("case", ["session_gap_older_running", "only_partial_running"])
def test_recover_ceiling_rejects_stale_running_report(tmp_db, case):
    from engine.cost_report import UsageLedger, build_report

    storage._ensure_initialized()
    hold = cost_guard.reserve(model="claude-haiku-5-5", auth_kind="api_key", max_tokens=50, run_id=case)
    ledger = UsageLedger(provider="anthropic")
    ledger.record(stage="debate", agent="analyst", provider="anthropic",
                  model_requested="claude-haiku-5-5", model_billed="claude-haiku-5-5",
                  input_tokens=1000, output_tokens=100)
    older = build_report(ledger, billing="api_key", run_state="running")
    assert older and older["lines"][0]["input_tokens"] == 1000
    ledger.record_no_usage(stage="debate", agent="analyst", provider="anthropic", model="claude-haiku-5-5")
    completed = build_report(ledger, billing="api_key", run_state="completed")
    assert completed and any(line["usage_unreported_calls"] for line in completed["lines"])
    events = ([{"type": "session.complete", "cost_report": completed}]
              if case == "session_gap_older_running" else [{"type": "session.complete"}])
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE cost_reservations SET est_cost_usd=1.5 WHERE id=?", (hold.reservation_id,))
        conn.execute("INSERT INTO sessions(id,ticker,trade_date,decision_action,decision_confidence,decision_reasoning,created_at,events_json,live,estimated_cost_usd,auth_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("session-" + case, "AAPL", "2026-11-09", "neutral", 0.5, "ok", stamp,
                      json.dumps(events), 1, None, "api_key"))
        conn.execute("INSERT INTO runs(run_id,owner,state,created_at,engine_started,session_id,cost_report) VALUES(?, 'local','completed',?,1,?,?)",
                     (case, now, "session-" + case, json.dumps(older)))
        assert cost_guard._compute_spend(conn).daily_usd == 1.5
    for _ in range(2):
        RunManager().recover()
        with sqlite3.connect(tmp_db) as conn:
            assert cost_guard._compute_spend(conn).daily_usd == 1.5
            assert conn.execute("SELECT finalized,settled_as,finalized_cost_usd FROM cost_reservations WHERE id=?",
                                (hold.reservation_id,)).fetchone() == (1, "ceiling", None)


def test_previous_run_resolved_secret_scrubbed_without_tail_damage(tmp_db, monkeypatch):
    from engine import secret_refs

    old = "other-run-secret-123456"
    class Store:
        def get(self, name): return old if name == "llm:openai" else None
    monkeypatch.setattr(secret_refs, "_store", Store())
    monkeypatch.setattr(secret_refs, "_seen", [])
    mgr = RunManager()

    async def first(sink):
        secret_refs.resolve_start_frame(await sink.receive_json())
        await sink.send_json({"type": "session.complete", "decision": {}})

    async def second(sink):
        await sink.send_json({"type": "agent.message", "content": f"prefix{old}post completed"})
        await sink.send_json({"type": "session.complete", "decision": {}})

    async def scenario():
        frame = {"ticker": "AAPL", "provider_config": {"provider": "openai", "auth": {
            "type": "api_key", "api_key": "@@secret:llm:openai@@"}, "model": "gpt-4o-mini"}}
        run1, _ = await mgr.start_or_attach("local", frame, first)
        await run1.task
        assert old in secret_refs._seen
        run2, _ = await mgr.start_or_attach("local", {"ticker": "MSFT"}, second)
        await run2.task
        content = next(e["content"] for e in run2.events if e.get("type") == "agent.message")
        assert content == "prefix[REDACTED]post completed"
        assert run2.state == "completed" and run2.events[-1]["state"] == "completed"
    asyncio.run(scenario())
