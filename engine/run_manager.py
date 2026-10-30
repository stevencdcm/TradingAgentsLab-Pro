"""Resumable analysis runs (D7).

A run is owned by the engine process, not by a WebSocket. The WebSocket (or an HTTP poll) only SUBSCRIBES to it, so a
refresh, a dropped tunnel or a second tab never cancels the work, never re-reserves cost and never calls the model again.

  * `RunManager.start_or_attach()` creates the run (one background task) or, for a repeated start with the same
    idempotency key (`client_run_key`, else `reservation_id`), returns the existing run.
  * Every event gets a monotonically increasing `seq`. `Run.subscribe(after)` takes the replay snapshot AND registers the
    live queue in one synchronous step (no `await` in between), so a reconnecting client sees every event exactly once.
  * Subscribers read from bounded queues filled with `put_nowait`: a slow client is dropped (`run.resync`) and
    re-subscribes with `after=<last seq>`; generation is never blocked by a consumer.
  * Run metadata and sanitised events are stored in the local SQLite database (tables `runs`, `run_events`). Never the
    start frame: no provider_config, no secret references, no headers. A server restart marks unfinished runs
    `interrupted`; nothing is ever re-run automatically.
  * Runs are owned by the authenticated owner (`local` for the loopback entry, the verified owner login behind the
    workbench proxy). Any lookup by another owner behaves as "not found".
"""

from __future__ import annotations

import asyncio
import collections
import json
import secrets as _secrets
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from . import cost_guard, secret_refs, storage

MAX_EVENTS = 5000               # stored/replayable events per run (terminal events are always kept)
MAX_EVENT_BYTES = 262_144
MAX_REPLAY_BYTES = 8 * 1024 * 1024   # in-memory replay budget per run (oldest events are dropped first)
KEEP_EVENTS_AFTER_END_S = 600        # a finished run keeps its replay buffer in memory this long, then reloads from the database
EXTEND_EVERY_S = 60
SUB_QUEUE_MAX = 2000
MAX_ACTIVE_PER_OWNER = 4
KEEP_IN_MEMORY = 50
KEEP_DB_RUNS = 200
KEEP_DB_SECONDS = 7 * 86400
ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
_PHASE_BY_TYPE = {
    "session.start": "data", "data.summary": "data", "news.headlines": "data", "run.notice": "data",
    "evidence.pack": "evidence", "dexter.result": "evidence", "graph.plan": "debate", "cost.usage": "debate",
    "session.complete": "done",
}


def _now() -> float:
    return time.time()


def start_meta(raw: Any) -> dict[str, Any]:
    """Non-secret descriptors of a start frame (what the status panel needs). Nothing else of the frame is kept."""
    raw = raw if isinstance(raw, dict) else {}
    pc = raw.get("provider_config") if isinstance(raw.get("provider_config"), dict) else {}
    auth = pc.get("auth") if isinstance(pc.get("auth"), dict) else {}
    eng = str(raw.get("engine") or "").lower()
    return {
        "ticker": str(raw.get("ticker") or "")[:24].upper(),
        "trade_date": str(raw.get("trade_date") or "")[:10],
        "provider": (str(pc.get("provider") or "")[:40] or None),
        "model": (str(pc.get("model") or "")[:120] or None),
        "auth_kind": (str(auth.get("type") or "")[:20] or None),
        "engine": "pro" if eng in ("full", "pro") else "free",
    }


def idempotency_key(raw: Any) -> Optional[str]:
    raw = raw if isinstance(raw, dict) else {}
    for k in ("client_run_key", "reservation_id"):
        v = raw.get(k)
        if isinstance(v, str) and 4 <= len(v) <= 80:
            return f"{k}:{v}"
    return None


def _scrub(text: str) -> str:
    return secret_refs.scrub_key_shapes(secret_refs.redact(text))


def _clean_event(ev: dict[str, Any]) -> str:
    """JSON for storage: key shapes / resolved secrets scrubbed, oversized payloads replaced by a marker."""
    txt = _scrub(json.dumps(ev, ensure_ascii=False, separators=(",", ":"), default=str))
    if len(txt.encode("utf-8")) > MAX_EVENT_BYTES:
        txt = json.dumps({"type": ev.get("type"), "seq": ev.get("seq"), "truncated": True}, separators=(",", ":"))
    return txt


class _Sub:
    __slots__ = ("q", "lagged")

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        self.lagged = False


class RunSink:
    """The object the (unchanged) run body talks to instead of a WebSocket."""

    def __init__(self, run: "Run", start_raw: dict[str, Any]) -> None:
        self.run = run
        self._start = start_raw
        self.query_params: dict[str, str] = {}
        self.scope: dict[str, Any] = {}

    async def receive_json(self) -> dict[str, Any]:
        return self._start

    async def send_json(self, event: dict[str, Any]) -> None:
        self.run.emit(event)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.run.close_code = (code, reason)


class Run:
    def __init__(self, mgr: "RunManager", run_id: str, owner: str, meta: dict[str, Any], key: Optional[str]) -> None:
        self.mgr, self.id, self.owner, self.key = mgr, run_id, owner, key
        self.meta = meta
        self.state = "queued"
        self.created_at = _now()
        self.ended_at: Optional[float] = None
        self.events: collections.deque = collections.deque()
        self._sizes: collections.deque = collections.deque()
        self.event_bytes = 0
        self.first_kept_seq = 1
        self.seq = 0
        self.truncated = False
        self.subs: set[_Sub] = set()
        self.task: Optional[asyncio.Task] = None
        self.cancel_requested = False
        self.close_code: Optional[tuple[int, str]] = None
        self.heartbeat: Optional[dict[str, Any]] = None
        self.last_event_at: Optional[float] = None
        self.last_event_type: Optional[str] = None
        self.phase = "starting"
        self.steps_done = 0
        self.input_tokens: Optional[int] = None
        self.output_tokens: Optional[int] = None
        self.cost: Optional[float] = None
        self.cost_known = False
        self.session_id: Optional[str] = None
        self.error: Optional[str] = None
        self.terminal_code: Optional[str] = None
        self.elapsed_override: Optional[float] = None
        self.completed_seen = False
        self.reservation_id: Optional[str] = None
        self.engine_started = False
        self.last_extend = 0.0

    # ---- derived -------------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def elapsed_s(self) -> float:
        if self.elapsed_override is not None:
            return self.elapsed_override
        return round((self.ended_at or _now()) - self.created_at, 1)

    def status(self) -> dict[str, Any]:
        """Safe snapshot for the page: no events, no provider configuration, no secrets."""
        hb = self.heartbeat
        return {
            "run_id": self.id, "state": self.state, "active": self.active,
            "ticker": self.meta.get("ticker"), "trade_date": self.meta.get("trade_date"),
            "provider": self.meta.get("provider"), "model": self.meta.get("model"), "auth_kind": self.meta.get("auth_kind"),
            "engine": self.meta.get("engine"), "phase": self.phase, "steps_done": self.steps_done,
            "started_at": self.created_at, "ended_at": self.ended_at, "elapsed_s": self.elapsed_s(),
            "last_seq": self.seq, "last_event_type": self.last_event_type,
            "since_last_event_s": None if self.last_event_at is None else round(_now() - self.last_event_at, 1),
            "heartbeat_age_s": None if not hb else round(_now() - hb["at"], 1),
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.cost, "cost_known": self.cost_known,
            "session_id": self.session_id, "terminal_code": self.terminal_code, "error": self.error,
            "replay_truncated": self.truncated, "replay_earliest_seq": self.first_kept_seq, "subscribers": len(self.subs),
            # A user cancel is asked for but the run has not actually ended yet (a model call already in flight is awaited).
            "cancelling": self.cancel_requested and self.active,
            "usage_partial": (not self.active) and self.state != "completed",
            # Usage is complete only for a finished analysis; a stopped one is measured through the last completed model call.
            "usage_complete": self.state == "completed",
        }

    # ---- events --------------------------------------------------------------------------------------------------
    def emit(self, event: dict[str, Any]) -> None:
        """Record + fan out one event. Synchronous on purpose: it is atomic with respect to `subscribe()`."""
        if not isinstance(event, dict):
            return
        now = _now()
        if self.reservation_id and self.active and now - self.last_extend >= EXTEND_EVERY_S:
            # The run is alive (events / heartbeats keep coming): keep its budget hold from expiring mid-run.
            self.last_extend = now
            cost_guard.extend_reservation(self.reservation_id)
        if event.get("type") == "run.heartbeat":
            self.heartbeat = {**event, "at": now}
            self._fan(event)                                   # liveness only: not sequenced, not stored
            return
        self.seq += 1
        ev = {**event, "seq": self.seq}
        self._track(ev)
        size = len(json.dumps(ev, ensure_ascii=False, default=str))
        self.events.append(ev)
        self._sizes.append(size)
        self.event_bytes += size
        # Bounded replay: events AND bytes. The OLDEST are dropped first; a client that needs them is told (run.replay_truncated).
        while len(self.events) > 1 and (len(self.events) > MAX_EVENTS or self.event_bytes > MAX_REPLAY_BYTES):
            self.events.popleft()
            self.event_bytes -= self._sizes.popleft()
            self.truncated = True
        self.first_kept_seq = self.events[0]["seq"]
        self.mgr._persist_event(self, ev)
        self._fan(ev)

    def _track(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        self.last_event_type, self.last_event_at = t, _now()
        self.phase = (ev.get("phase") if t == "agent.message" and ev.get("phase") else _PHASE_BY_TYPE.get(t, self.phase))
        if t == "agent.message":
            self.steps_done += 1
        elif t in ("cost.usage", "session.complete"):
            if isinstance(ev.get("input_tokens"), int):
                self.input_tokens = ev["input_tokens"]
            if isinstance(ev.get("output_tokens"), int):
                self.output_tokens = ev["output_tokens"]
            c = ev.get("estimated_cost_usd", ev.get("est_cost_usd"))
            if isinstance(c, (int, float)):
                self.cost, self.cost_known = float(c), True
            elif t == "session.complete":
                self.cost, self.cost_known = None, False
            if t == "session.complete":
                self.completed_seen = True
        elif t == "run.error":
            self.error = _scrub(str(ev.get("error") or ""))[:300]

    def _fan(self, ev: dict[str, Any]) -> None:
        for sub in list(self.subs):
            try:
                sub.q.put_nowait(ev)
            except asyncio.QueueFull:                          # slow consumer: drop it, never block the run
                sub.lagged = True
                self.subs.discard(sub)
                while not sub.q.empty():
                    sub.q.get_nowait()
                sub.q.put_nowait(None)

    def subscribe(self, after: int = 0) -> tuple[list[dict[str, Any]], _Sub]:
        """Replay snapshot (seq > after) and live registration in ONE synchronous step: no gap, no duplicate."""
        self.mgr._ensure_events(self)
        snap = [e for e in self.events if e["seq"] > after]
        sub = _Sub()
        if self.active:
            self.subs.add(sub)
        else:
            sub.q.put_nowait(None)
        return snap, sub

    def drop_events(self) -> None:
        """Finished run: release the in-memory replay buffer (the database still has it)."""
        if not self.active:
            self.events.clear()
            self._sizes.clear()
            self.event_bytes = 0

    def unsubscribe(self, sub: _Sub) -> None:
        self.subs.discard(sub)

    def finish_subs(self) -> None:
        for sub in list(self.subs):
            try:
                sub.q.put_nowait(None)
            except asyncio.QueueFull:
                sub.lagged = True
        self.subs.clear()


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---- persistence (best effort: a storage problem never fails a run) --------------------------------------------
    def _db(self, fn: Callable[[Any], Any]) -> Any:
        try:
            storage._ensure_initialized()
            with storage._connect() as conn:
                out = fn(conn)
                conn.commit()
                return out
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[runs] storage error: {type(exc).__name__}: {exc}\n")
            return None

    def _save_meta(self, run: Run) -> None:
        s = run.status()
        self._db(lambda c: c.execute(
            """INSERT INTO runs (run_id, owner, state, ticker, trade_date, provider, model, auth_kind, engine, created_at, ended_at, elapsed_s,
                   input_tokens, output_tokens, estimated_cost_usd, cost_known, session_id, terminal_code, error, last_seq, last_event_type,
                   last_event_at, steps_done, reservation_id, engine_started)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, ended_at=excluded.ended_at, elapsed_s=excluded.elapsed_s,
                   input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, estimated_cost_usd=excluded.estimated_cost_usd,
                   cost_known=excluded.cost_known, session_id=excluded.session_id, terminal_code=excluded.terminal_code, error=excluded.error,
                   last_seq=excluded.last_seq, last_event_type=excluded.last_event_type, last_event_at=excluded.last_event_at,
                   steps_done=excluded.steps_done, reservation_id=excluded.reservation_id, engine_started=excluded.engine_started""",
            (run.id, run.owner, run.state, s["ticker"], s["trade_date"], s["provider"], s["model"], s["auth_kind"], s["engine"],
             run.created_at, run.ended_at, run.elapsed_s() if not run.active else None, run.input_tokens, run.output_tokens, run.cost,
             1 if run.cost_known else 0, run.session_id, run.terminal_code, run.error, run.seq, run.last_event_type, run.last_event_at,
             run.steps_done, run.reservation_id, 1 if run.engine_started else 0)))

    def _persist_event(self, run: Run, ev: dict[str, Any]) -> None:
        def write(c):
            c.execute("INSERT OR IGNORE INTO run_events (run_id, seq, ts, etype, payload) VALUES (?,?,?,?,?)",
                      (run.id, ev["seq"], _now(), str(ev.get("type")), _clean_event(ev)))
            if ev["seq"] > MAX_EVENTS and ev["seq"] % 100 == 0:          # rolling window in the database too
                c.execute("DELETE FROM run_events WHERE run_id=? AND seq<=?", (run.id, ev["seq"] - MAX_EVENTS))

        self._db(write)
        if ev.get("type") in ("session.complete", "run.error", "run.finished", "agent.message", "session.start", "cost.usage") or ev["seq"] % 20 == 0:
            self._save_meta(run)

    def _ensure_events(self, run: Run) -> None:
        """A run reloaded from the database has no in-memory events yet."""
        if run.events or run.seq == 0 or run.active:
            return
        rows = self._db(lambda c: c.execute("SELECT payload FROM run_events WHERE run_id=? ORDER BY seq LIMIT ?", (run.id, MAX_EVENTS + 5)).fetchall()) or []
        for r in rows:
            try:
                ev = json.loads(r["payload"])
            except ValueError:
                continue
            run.events.append(ev)
            run._sizes.append(len(r["payload"]))
            run.event_bytes += len(r["payload"])
        if run.events:
            run.first_kept_seq = run.events[0]["seq"]
            run.truncated = run.truncated or run.first_kept_seq > 1

    def _load(self, run_id: str) -> Optional[Run]:
        row = self._db(lambda c: c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
        if row is None:
            return None
        run = Run(self, row["run_id"], row["owner"], {k: row[k] for k in ("ticker", "trade_date", "provider", "model", "auth_kind", "engine")}, None)
        run.state, run.created_at, run.ended_at, run.elapsed_override = row["state"], row["created_at"], row["ended_at"], row["elapsed_s"]
        run.seq, run.input_tokens, run.output_tokens, run.cost = row["last_seq"], row["input_tokens"], row["output_tokens"], row["estimated_cost_usd"]
        run.cost_known, run.session_id, run.terminal_code, run.error = bool(row["cost_known"]), row["session_id"], row["terminal_code"], row["error"]
        run.last_event_type, run.last_event_at, run.steps_done = row["last_event_type"], row["last_event_at"], row["steps_done"]
        run.reservation_id, run.engine_started = row["reservation_id"], bool(row["engine_started"])
        if run.elapsed_override is None and run.ended_at:
            run.elapsed_override = round(run.ended_at - run.created_at, 1)
        run.phase = "done" if run.state == "completed" else run.state
        return run

    def recover(self) -> int:
        """Server start: unfinished runs of a previous process become `interrupted` (never re-run), old runs are purged."""
        now = _now()
        rows = self._db(lambda c: c.execute(
            "SELECT r.run_id AS run_id, r.reservation_id AS reservation_id, r.engine_started AS engine_started, "
            "COALESCE((SELECT MAX(seq) FROM run_events e WHERE e.run_id = r.run_id), r.last_seq) AS last_seq "
            "FROM runs r WHERE r.state IN ('queued','running')").fetchall()) or []
        for r in rows:
            # Settle the budget hold FIRST (before any expiry sweep can turn it into an uncounted NULL): nothing billable
            # started -> released at 0; otherwise the measured cost, or the reserved estimate as a conservative hold.
            cost_guard.settle_run_end(r["reservation_id"], billable_started=bool(r["engine_started"]))
            payload = json.dumps({"type": "run.finished", "seq": r["last_seq"] + 1, "state": "interrupted", "code": "server_restart",
                                  "message": "The engine restarted while this analysis was running. It was NOT re-run automatically."}, separators=(",", ":"))
            self._db(lambda c, r=r, payload=payload: (
                c.execute("INSERT OR IGNORE INTO run_events (run_id, seq, ts, etype, payload) VALUES (?,?,?,?,?)", (r["run_id"], r["last_seq"] + 1, now, "run.finished", payload)),
                c.execute("UPDATE runs SET state='interrupted', ended_at=?, terminal_code='server_restart', last_seq=?, last_event_type='run.finished' WHERE run_id=?",
                          (now, r["last_seq"] + 1, r["run_id"]))))
        self._db(lambda c: (
            c.execute("DELETE FROM run_events WHERE run_id IN (SELECT run_id FROM runs WHERE state NOT IN ('queued','running') AND COALESCE(ended_at, created_at) < ?)", (now - KEEP_DB_SECONDS,)),
            c.execute("DELETE FROM runs WHERE state NOT IN ('queued','running') AND COALESCE(ended_at, created_at) < ?", (now - KEEP_DB_SECONDS,)),
            c.execute("DELETE FROM run_events WHERE run_id IN (SELECT run_id FROM runs ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (KEEP_DB_RUNS,)),
            c.execute("DELETE FROM runs WHERE run_id IN (SELECT run_id FROM runs ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (KEEP_DB_RUNS,))))
        return len(rows)

    # ---- lookup (owner-scoped: another owner's run is "not found") --------------------------------------------------
    def get(self, owner: str, run_id: str) -> Optional[Run]:
        run = self.runs.get(run_id) or self._load(run_id)
        if run is None or run.owner != owner:
            return None
        return run

    def list_for(self, owner: str, limit: int = 10) -> list[Run]:
        mem = [r for r in self.runs.values() if r.owner == owner]
        seen = {r.id for r in mem}
        rows = self._db(lambda c: c.execute("SELECT run_id FROM runs WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, limit)).fetchall()) or []
        for r in rows:
            if r["run_id"] not in seen:
                loaded = self._load(r["run_id"])
                if loaded:
                    mem.append(loaded)
        return sorted(mem, key=lambda r: r.created_at, reverse=True)[:limit]

    # ---- start / attach / cancel -----------------------------------------------------------------------------------
    async def start_or_attach(self, owner: str, raw: dict[str, Any], executor: Callable[[RunSink], Awaitable[None]]) -> tuple[Run, bool]:
        key = idempotency_key(raw)
        if key and (owner, key) in self._keys:
            existing = self.runs.get(self._keys[(owner, key)])
            if existing is not None:
                return existing, False                           # repeated start: same run, no second reservation / model call
        run = Run(self, "run_" + _secrets.token_hex(8), owner, start_meta(raw), key)
        rid = raw.get("reservation_id") if isinstance(raw, dict) else None
        run.reservation_id = rid if isinstance(rid, str) and 4 <= len(rid) <= 80 else None
        self.runs[run.id] = run
        if key:
            self._keys[(owner, key)] = run.id
        self._save_meta(run)
        if sum(1 for r in self.runs.values() if r.owner == owner and r.active) > MAX_ACTIVE_PER_OWNER:
            run.emit({"type": "run.error", "code": "too_many_runs", "error": f"At most {MAX_ACTIVE_PER_OWNER} analyses can run at once."})
            run.close_code = (1008, "too_many_runs")
            self._finish(run, "failed", "too_many_runs")
            return run, True
        run.task = asyncio.create_task(self._drive(run, raw, executor))
        self._tasks.add(run.task)
        run.task.add_done_callback(self._tasks.discard)
        run.task.add_done_callback(lambda _t, run=run: self._settle_if_unfinished(run))
        return run, True

    async def _drive(self, run: Run, raw: dict[str, Any], executor: Callable[[RunSink], Awaitable[None]]) -> None:
        run.state = "running"
        self._save_meta(run)
        sink = RunSink(run, raw)
        state, code = "completed", None
        try:
            await executor(sink)
            if not run.completed_seen:
                state = "failed"
                code = (run.close_code[1] if run.close_code and run.close_code[1] else None) or ("run_error" if run.error else "ended_without_result")
        except asyncio.CancelledError:
            if run.session_id:                                  # the result was already saved: this run completed
                state, code = "completed", None
            elif run.cancel_requested:
                state, code = "cancelled", "user_cancelled"
                run.emit({"type": "run.cancelled", "message": "Stopped by the user."})
            else:                                               # process shutdown
                state, code = "interrupted", "server_shutdown"
        except Exception as exc:  # noqa: BLE001 — safety net: the run body normally reports its own errors
            state, code = "failed", "engine_exception"
            msg = _scrub(f"{type(exc).__name__}: {exc}")
            run.emit({"type": "run.error", "code": "engine_exception", "error": f"Analysis stopped (internal error): {msg}"})
            run.close_code = (1011, "engine_exception")
        finally:
            self._finish(run, state, code)

    def _settle_if_unfinished(self, run: Run) -> None:
        """A task cancelled before its first step never enters `_drive`'s try block: still give the run a terminal state."""
        if run.active:
            if run.cancel_requested:
                run.emit({"type": "run.cancelled", "message": "Stopped by the user."})
                self._finish(run, "cancelled", "user_cancelled")
            else:
                self._finish(run, "interrupted", "server_shutdown")

    def _finish(self, run: Run, state: str, code: Optional[str]) -> None:
        if not run.active and run.ended_at is not None:
            return
        run.state, run.terminal_code, run.ended_at = state, code, _now()
        run.emit({"type": "run.finished", "state": state, "code": code, "elapsed_s": run.elapsed_s(), "input_tokens": run.input_tokens,
                  "output_tokens": run.output_tokens, "estimated_cost_usd": run.cost, "cost_known": run.cost_known, "session_id": run.session_id})
        if state != "completed" or run.session_id is None:
            # Not completed, OR completed but its history row was never written (storage failure): there is no session whose
            # cost the caps would count, so settle the hold as incomplete spend instead of letting the cost vanish.
            cost_guard.settle_run_end(run.reservation_id, billable_started=run.engine_started)
        self._save_meta(run)
        run.finish_subs()
        try:
            asyncio.get_running_loop().call_later(KEEP_EVENTS_AFTER_END_S, run.drop_events)
        except RuntimeError:
            pass
        done = sorted((r for r in self.runs.values() if not r.active), key=lambda r: r.created_at)
        for r in done[:max(0, len(done) - KEEP_IN_MEMORY)]:
            self.runs.pop(r.id, None)
            if r.key:
                self._keys.pop((r.owner, r.key), None)

    async def cancel(self, run: Run, timeout: float = 5.0) -> Run:
        """Explicit stop, idempotent: the run's task is cancelled exactly ONCE however many callers ask (double click, two tabs,
        menu + button). Later callers only wait. The run body's own `finally` blocks settle the cost reservation once the model
        call already in flight has really landed; `timeout` only bounds how long THIS request waits, not the cancellation."""
        if not run.active or run.task is None or run.task.done():
            return run
        if run.completed_seen or run.session_id:
            return run                       # the result already exists (webhooks are finishing): nothing left to stop, never double-count
        if not run.cancel_requested:
            run.cancel_requested = True
            run.emit({"type": "run.cancelling", "message": "Stop requested: waiting for the model call already in flight to finish."})
            run.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(run.task), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        return run

    # ---- subscription loop shared by /stream and /runs/{id}/stream -------------------------------------------------
    async def serve(self, ws: Any, run: Run, after: int = 0, *, resumed: bool = False, announce: bool = True) -> None:
        snap, sub = run.subscribe(after)
        try:
            if announce:
                keep = ("state", "ticker", "trade_date", "provider", "model", "engine", "elapsed_s", "started_at", "last_seq", "replay_truncated")
                await ws.send_json({"type": "run.attached", "run_id": run.id, "resumed": resumed, "after": after,
                                    **{k: v for k, v in run.status().items() if k in keep}})
            if announce and after + 1 < run.first_kept_seq:
                await ws.send_json({"type": "run.replay_truncated", "earliest_seq": run.first_kept_seq, "after": after})
            for ev in snap:
                if announce or ev.get("type") != "run.finished":      # legacy /stream clients never saw run.finished
                    await ws.send_json(ev)
            if announce and run.heartbeat and run.active:
                await ws.send_json({k: v for k, v in run.heartbeat.items() if k != "at"})
            while True:
                item = await sub.q.get()
                if item is None:
                    if sub.lagged:
                        await ws.send_json({"type": "run.resync", "reason": "slow_subscriber", "last_seq": run.seq})
                        await _safe_close(ws, 1013, "slow_subscriber")
                        return
                    break
                if announce or item.get("type") != "run.finished":
                    await ws.send_json(item)
        except Exception:  # noqa: BLE001 — client went away (refresh / network): the run keeps going
            return
        finally:
            run.unsubscribe(sub)
        code, reason = run.close_code or (1000, "")
        await _safe_close(ws, code, reason)


async def _safe_close(ws: Any, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001
        pass
