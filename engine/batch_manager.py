"""Durable, owner-scoped sequential analysis batches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool

from . import secret_refs, storage
from .llm_providers import ProviderConfig
from .run_manager import Run, RunManager, RunSink, RunStartStorageError, TERMINAL

ACTIVE = ("queued", "running")
ITEM_STATES = ("pending", "waiting_slot", "running", "completed", "failed", "cancelled", "skipped", "interrupted")
START_FIELDS = frozenset(("trade_date", "provider_config", "data_config", "webhooks", "telegram_chat_ids", "engine",
                          "pro_config", "alpha_vantage_key", "tw_flow", "tw_dexter", "tw_data_mode", "finmind_token",
                          "report_language"))


class BatchError(Exception):
    def __init__(self, status: int, code: str, error: str, batch_id: str | None = None):
        self.status, self.code, self.error, self.batch_id = status, code, error, batch_id

    def detail(self) -> dict:
        return {"code": self.code, "error": self.error, **({"batch_id": self.batch_id} if self.batch_id else {})}


def _fingerprint(tickers: list[str], start: dict) -> str:
    """Plaintext credential rotation does not change idempotency; settings do."""
    def mask(obj: Any, key: str = "", webhook: bool = False) -> Any:
        if isinstance(obj, dict):
            return {k: mask(v, k, webhook or key == "webhooks") for k, v in obj.items()}
        if isinstance(obj, list):
            return [mask(v, key, webhook or key == "webhooks") for v in obj]
        if isinstance(obj, str) and secret_refs.is_secret_field(key, webhook=webhook):
            return obj if secret_refs._ref_name(obj) else "<secret>"
        return obj
    blob = json.dumps({"tickers": tickers, "start": mask(start)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class BatchManager:
    def __init__(self, runs: RunManager, executor: Callable[[RunSink], Awaitable[None]] | None = None):
        self.runs, self.executor = runs, executor
        self._lock = asyncio.Lock()
        self._templates: dict[str, dict] = {}
        self._scrubbers: dict[str, secret_refs.Scrubber] = {}
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _db(fn):
        storage._ensure_initialized()
        with storage._connect() as conn:
            out = fn(conn)
            conn.commit()
            return out

    @staticmethod
    def _valid_terminal(row: Any, *, state: str | None = None, session_id: str | None = None) -> bool:
        """One success rule for live completion and restart reconciliation."""
        if row is None or row["state"] not in TERMINAL or (state is not None and row["state"] != state):
            return False
        if row["state"] == "completed" and not row["session_id"]:
            return False
        return state is None or row["session_id"] == session_id

    def _recovered_details(self, run: Any) -> dict[str, Any]:
        def read(conn):
            session = conn.execute("SELECT decision_action,decision_confidence,estimated_cost_usd,elapsed_s "
                                   "FROM sessions WHERE id=?", (run["session_id"],)).fetchone() if run["session_id"] else None
            event = conn.execute("SELECT payload FROM run_events WHERE run_id=? AND etype='session.complete' ORDER BY seq DESC LIMIT 1",
                                 (run["run_id"],)).fetchone()
            return session, event
        session, event = self._db(read)
        ev = json.loads(event["payload"]) if event else {}
        decision = ev.get("decision") if isinstance(ev.get("decision"), dict) else {}
        return {"stance": session["decision_action"] if session else decision.get("stance"),
                "conviction": session["decision_confidence"] if session else decision.get("conviction"),
                "estimated_cost_usd": run["estimated_cost_usd"] if run["estimated_cost_usd"] is not None else
                                      session["estimated_cost_usd"] if session else ev.get("estimated_cost_usd"),
                "elapsed_s": run["elapsed_s"] if run["elapsed_s"] is not None else
                             session["elapsed_s"] if session else ev.get("elapsed_s")}

    @staticmethod
    def _validate(body: Any) -> tuple[list[str], str, dict, str, secret_refs.Scrubber]:
        if not isinstance(body, dict) or set(body) != {"tickers", "client_batch_key", "start"}:
            raise BatchError(400, "invalid_batch", "Allowed fields: tickers, client_batch_key, start.")
        key, start, raw = body["client_batch_key"], body["start"], body["tickers"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise BatchError(400, "invalid_batch", "client_batch_key must be 8 to 80 characters.")
        if not isinstance(start, dict) or not set(start).issubset(START_FIELDS):
            raise BatchError(400, "invalid_batch", "Allowed start fields: " + ", ".join(sorted(START_FIELDS)))
        if not isinstance(raw, list) or len(raw) > 500 or any(not isinstance(t, str) for t in raw):
            raise BatchError(400, "invalid_batch", "tickers must contain 1 to 50 unique strings (500 input maximum).")
        tickers = list(dict.fromkeys(t.strip().upper() for t in raw))
        if not 1 <= len(tickers) <= 50 or any(not 1 <= len(t) <= 32 for t in tickers):
            raise BatchError(400, "invalid_batch", "Ticker length must be 1 to 32.")
        pc = start.get("provider_config")
        try:
            provider = ProviderConfig.from_dict(pc) if isinstance(pc, dict) else None
        except (ValueError, TypeError, AttributeError):
            provider = None
        if not isinstance(pc, dict) or not pc.get("provider") or provider is None:
            raise BatchError(400, "no_provider", "Valid provider_config is required.")
        if not isinstance(start.get("webhooks", []), list) or not isinstance(start.get("data_config", {}), dict):
            raise BatchError(400, "invalid_batch", "start fields have invalid format.")
        try:
            secret_refs.validate_start_refs(start)
        except secret_refs.SecretRefError:
            raise BatchError(400, "secret_ref_rejected", "Secret reference is not allowed in this field.") from None
        scrubber = secret_refs.Scrubber(start)
        if any(len(s) < 8 for s in scrubber.values):
            raise BatchError(400, "invalid_batch", "A credential field has invalid format.")
        return tickers, key, start, _fingerprint(tickers, start), scrubber

    async def create(self, owner: str, body: Any) -> dict:
        tickers, key, start, fingerprint, scrubber = self._validate(body)
        async with self._lock:
            def insert(conn):
                old = conn.execute("SELECT batch_id,fingerprint FROM batches WHERE owner=? AND client_key=?", (owner, key)).fetchone()
                if old:
                    return ("old", old["batch_id"], old["fingerprint"])
                active = conn.execute("SELECT batch_id FROM batches WHERE owner=? AND state IN ('queued','running') LIMIT 1", (owner,)).fetchone()
                if active:
                    return ("active", active["batch_id"], None)
                batch_id = "batch_" + secrets.token_hex(12)
                pc = start["provider_config"]
                auth = pc.get("auth") or {}
                conn.execute("""INSERT INTO batches(batch_id,owner,client_key,fingerprint,state,provider,model,auth_kind,engine,
                              trade_date,report_language,created_at) VALUES(?,?,?,?,'queued',?,?,?,?,?,?,?)""",
                             (batch_id, owner, key, fingerprint, scrubber.clean(pc.get("provider")), scrubber.clean(pc.get("model")),
                              scrubber.clean(auth.get("type")), scrubber.clean(start.get("engine")),
                              scrubber.clean(start.get("trade_date")), scrubber.clean(start.get("report_language")), time.time()))
                conn.executemany("INSERT INTO batch_items(batch_id,item_index,ticker,state) VALUES(?,?,?,'pending')",
                                 [(batch_id, i, t) for i, t in enumerate(tickers)])
                return ("new", batch_id, None)
            try:
                kind, batch_id, old_fingerprint = self._db(insert)
            except Exception:
                raise BatchError(503, "storage_unavailable", "Could not save batch.") from None
            if kind == "old":
                if old_fingerprint != fingerprint:
                    raise BatchError(409, "idempotency_conflict", "Batch key already has different settings.")
            elif kind == "active":
                raise BatchError(409, "batch_active", "Another batch is active.", batch_id)
            else:
                self._templates[batch_id] = start
                self._scrubbers[batch_id] = scrubber
                task = asyncio.create_task(self._run(owner, batch_id, tickers))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return self.get(owner, batch_id)

    def _update(self, batch_id: str, table: str, fields: dict, index: int | None = None) -> None:
        fields = dict(fields)
        scrubber = self._scrubbers.get(batch_id)
        for k in ("error",):
            if k in fields and scrubber:
                fields[k] = scrubber.clean_error(fields[k]) if isinstance(fields[k], str) else fields[k]
        sql = f"UPDATE {table} SET " + ",".join(f"{k}=?" for k in fields)
        sql += " WHERE batch_id=?" + (" AND item_index=?" if index is not None else "")
        params = [*fields.values(), batch_id, *([index] if index is not None else [])]
        for attempt in range(2):
            try:
                self._db(lambda c: c.execute(sql, params))
                return
            except Exception:
                if attempt:
                    raise

    async def _prepare(self, run: Run) -> bool:
        start = self._templates[run.batch_id]
        auth = start["provider_config"].get("auth") or {}
        if auth.get("type") != "oauth":
            return True
        hook = getattr(self, "oauth_refresh", None)
        code = "oauth_refresh_failed" if hook else "oauth_expired"
        try:
            result = await asyncio.wait_for(run_in_threadpool(hook), timeout=30) if hook else auth
            if run.cancel_requested:
                raise asyncio.CancelledError()
            expires = float(result.get("expires") or 0)
            if expires > 1e12:
                expires /= 1000
            if not result.get("access") or expires <= time.time() + 600:
                run.terminal_code = code
                return False
            auth["expires"] = result.get("expires")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            run.terminal_code = code
            return False

    async def _run(self, owner: str, batch_id: str, tickers: list[str]) -> None:
        try:
            for i, ticker in enumerate(tickers):
                row = self._db(lambda c: c.execute("SELECT cancel_requested FROM batches WHERE batch_id=?", (batch_id,)).fetchone())
                if row["cancel_requested"]:
                    self._finish(batch_id, "cancelled", "user_cancelled", i)
                    return
                start = {**self._templates[batch_id], "ticker": ticker, "client_run_key": f"batch:{batch_id}:{i}"}
                try:
                    run, _ = await self.runs.start_or_attach(owner, start, self.executor, batch=(batch_id, i, len(tickers)), prepare=self._prepare)
                except RunStartStorageError:
                    self._update(batch_id, "batch_items", {"state": "failed", "error_code": "storage_unavailable",
                                                          "error": "Could not save run metadata.", "ended_at": time.time()}, i)
                    self._finish(batch_id, "stopped", "storage_unavailable", i + 1)
                    return
                storage_lost = False
                try:
                    self._update(batch_id, "batch_items", {"state": "waiting_slot", "run_id": run.id, "started_at": time.time()}, i)
                except Exception:
                    storage_lost = True
                if run.task is not None:
                    try:
                        await run.task
                    except asyncio.CancelledError:
                        pass
                if storage_lost:
                    try:
                        self._update(batch_id, "batch_items", {"state": "completed" if run.state == "completed" and run.session_id else "failed",
                                                             "run_id": run.id, "session_id": run.session_id,
                                                             "error_code": run.terminal_code, "ended_at": time.time()}, i)
                    except Exception:
                        pass
                    self._finish(batch_id, "stopped", "storage_unavailable", i + 1)
                    return
                status = run.status()
                try:
                    persisted = self._db(lambda c: c.execute("SELECT state,session_id FROM runs WHERE run_id=?", (run.id,)).fetchone())
                    missing = run.meta_persist_failed or not self._valid_terminal(
                        persisted, state=run.state, session_id=run.session_id)
                except Exception:
                    missing = True
                item_state = ("failed" if missing else "completed" if run.state == "completed" and run.session_id else
                              run.state if run.state in ("failed", "cancelled", "interrupted") else "failed")
                decision = next((e.get("decision") for e in reversed(run.events) if e.get("type") == "session.complete"), {}) or {}
                self._update(batch_id, "batch_items", {"state": item_state, "session_id": run.session_id,
                            "stance": decision.get("stance"), "conviction": decision.get("conviction"),
                            "estimated_cost_usd": run.cost, "cost_known": int(run.cost_known) if run.engine_started else None,
                            "cost_partial": int(run.engine_started and item_state != "completed"),
                            "elapsed_s": status["elapsed_s"], "ended_at": time.time(),
                            "error_code": "session_persist_failed" if missing else run.terminal_code,
                            "error": "Session could not be saved." if missing else run.error}, i)
                batch = self.get(owner, batch_id)
                if batch["cancel_requested"]:
                    self._finish(batch_id, "cancelled", "user_cancelled", i + 1)
                    return
                if run.state == "interrupted":
                    self._finish(batch_id, "interrupted", "server_restart", i + 1)
                    return
                stop = "storage_unavailable" if missing else run.terminal_code if run.terminal_code in (
                    "cost_guard_blocked", "oauth_expired", "oauth_refresh_failed", "invalid_credential", "storage_unavailable") else None
                if stop:
                    self._finish(batch_id, "stopped", stop, i + 1)
                    return
            self._finish(batch_id, "completed", None, len(tickers))
        except Exception:
            try:
                self._finish(batch_id, "stopped", "storage_unavailable", 0)
            except Exception:
                pass
        finally:
            self._templates.pop(batch_id, None)
            scrubber = self._scrubbers.pop(batch_id, None)
            if scrubber:
                scrubber.clear()

    def _finish(self, batch_id: str, state: str, code: str | None, remaining: int) -> None:
        self._db(lambda c: c.execute("UPDATE batch_items SET state='skipped', ended_at=? WHERE batch_id=? AND item_index>=? AND state='pending'",
                                     (time.time(), batch_id, remaining)))
        self._update(batch_id, "batches", {"state": state, "terminal_code": code, "ended_at": time.time(),
                                           "error": "Please sign in again before running remaining items." if code in ("oauth_expired", "oauth_refresh_failed") else None})

    def get(self, owner: str, batch_id: str) -> dict:
        row, items = self._db(lambda c: (c.execute("SELECT * FROM batches WHERE batch_id=? AND owner=?", (batch_id, owner)).fetchone(),
                                         c.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY item_index", (batch_id,)).fetchall()))
        if row is None:
            raise BatchError(404, "batch_not_found", "Batch was not found.")
        items = [dict(x) for x in items]
        counts = {s: sum(x["state"] == s for x in items) for s in ITEM_STATES}
        current = next((x["item_index"] for x in items if x["state"] in ("running", "waiting_slot")), None)
        current_run = None
        if current is not None:
            run = self.runs.get(owner, items[current]["run_id"]) if items[current]["run_id"] else None
            if run:
                s = run.status()
                current_run = {"run_id": run.id, "phase": s["phase"], "steps_done": s["steps_done"],
                               "elapsed_s": s["elapsed_s"], "since_last_event_s": s["since_last_event_s"],
                               "last_event_type": s["last_event_type"], "cancelling": s["cancelling"]}
        known = sum(x["estimated_cost_usd"] or 0 for x in items)
        unknown = sum(x["cost_known"] == 0 and x["run_id"] is not None for x in items)
        out = {k: row[k] for k in ("batch_id", "state", "terminal_code", "error", "provider", "model", "auth_kind", "engine",
                                    "trade_date", "report_language", "created_at", "started_at", "ended_at")}
        out.update(active=row["state"] in ACTIVE, cancel_requested=bool(row["cancel_requested"]), total=len(items), counts=counts,
                   current_index=current, cost={"known_usd": known, "unknown_items": unknown,
                                                "partial": bool(unknown or any(x["cost_partial"] for x in items))},
                   items=[{"index": x.pop("item_index"), **{k: (bool(v) if k in ("cost_known", "cost_partial") and v is not None else v)
                                                             for k, v in x.items() if k != "batch_id"}} for x in items], current_run=current_run)
        return out

    def list(self, owner: str, limit: int) -> list[dict]:
        ids = self._db(lambda c: [r[0] for r in c.execute("SELECT batch_id FROM batches WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, limit))])
        return [self.get(owner, x) for x in ids]

    async def cancel(self, owner: str, batch_id: str) -> dict:
        batch = self.get(owner, batch_id)
        if batch["active"]:
            self._update(batch_id, "batches", {"cancel_requested": 1})
            if batch["current_run"]:
                run = self.runs.get(owner, batch["current_run"]["run_id"])
                if run:
                    await self.runs.cancel(run, timeout=0.1)
        return self.get(owner, batch_id)

    def recover(self) -> None:
        ids = self._db(lambda c: [r[0] for r in c.execute("SELECT batch_id FROM batches WHERE state IN ('queued','running')")])
        for bid in ids:
            missing_session = False
            rows = self._db(lambda c: c.execute("SELECT item_index,state FROM batch_items WHERE batch_id=?", (bid,)).fetchall())
            for item in rows:
                run = self._db(lambda c, i=item["item_index"]: c.execute(
                    "SELECT * FROM runs WHERE batch_id=? AND batch_index=?", (bid, i)).fetchone())
                if run:
                    valid = self._valid_terminal(run)
                    missing_session |= not valid
                    state = run["state"] if valid else "failed"
                    detail = self._recovered_details(run) if valid else {}
                    self._update(bid, "batch_items", {"state": state, "run_id": run["run_id"], "session_id": run["session_id"],
                                 "stance": detail.get("stance"), "conviction": detail.get("conviction"),
                                 "estimated_cost_usd": detail.get("estimated_cost_usd", run["estimated_cost_usd"]),
                                 "cost_known": run["cost_known"] if run["engine_started"] else None,
                                 "cost_partial": int(bool(run["engine_started"]) and state != "completed"),
                                 "elapsed_s": detail.get("elapsed_s", run["elapsed_s"]),
                                 "error_code": "session_persist_failed" if not valid else run["terminal_code"],
                                 "error": "Session could not be saved." if not valid else run["error"],
                                 "ended_at": run["ended_at"]}, item["item_index"])
                else:
                    self._update(bid, "batch_items", {"state": "skipped" if item["state"] == "pending" else "interrupted",
                                                       "ended_at": time.time()}, item["item_index"])
            cancelled = self._db(lambda c: c.execute("SELECT cancel_requested FROM batches WHERE batch_id=?", (bid,)).fetchone())[0]
            self._update(bid, "batches", {"state": "cancelled" if cancelled else "stopped" if missing_session else "interrupted",
                                           "terminal_code": "user_cancelled" if cancelled else "storage_unavailable" if missing_session else "server_restart",
                                           "ended_at": time.time()})
