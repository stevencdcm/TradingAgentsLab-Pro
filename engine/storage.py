"""SQLite-backed session persistence.

Writes a single row per completed debate (stub or live), keyed by a
generated ULID-style id. The full event array is stored as a JSON blob in
one column rather than normalized into rows — list views show the headline
columns, detail views inflate the JSON. If a future feature needs to query
inside events (e.g. "find all sessions where bull_researcher mentioned X"),
that's a worth-it migration then; not preemptively now.

Storage lives at `<repo>/data/sessions.db` (next to the engine venv). Path
can be overridden with the `TAL_SESSIONS_DB` env var when needed.

Failure mode: every public function returns gracefully on errors — the
caller (WS stream handler) treats persistence as best-effort and never
fails the stream because the write failed.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA_VERSION = 2


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_confidence REAL NOT NULL,
    decision_reasoning TEXT NOT NULL,
    live INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,
    auth_kind TEXT,
    created_at TEXT NOT NULL,
    events_json TEXT NOT NULL
);

-- `provider` and `auth_kind` were added in v2. Older databases get
-- in-place ALTER TABLE adds below — both columns are nullable and additive
-- so older readers/writers stay forward-compatible.


CREATE INDEX IF NOT EXISTS sessions_ticker_idx ON sessions(ticker);
CREATE INDEX IF NOT EXISTS sessions_created_at_idx ON sessions(created_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    note TEXT
);
"""


def _default_db_path() -> Path:
    override = os.environ.get("TAL_SESSIONS_DB")
    if override:
        return Path(override).expanduser().resolve()
    # When the engine is started with cwd=<repo> (Electron does this; see
    # engine-runner.ts), `Path.cwd()` is the repo root.
    repo = Path.cwd()
    return (repo / "data" / "sessions.db").resolve()


# Cached at import; tests can override by setting TAL_SESSIONS_DB and
# calling `_reset_for_tests()`.
_db_path: Path = _default_db_path()
_initialized = False
# Serializes first-time schema init. Two threads first-initializing a fresh DB
# concurrently (e.g. a debate write + cost finalize + Telegram trigger landing
# together) would otherwise both run the DDL + WAL-mode switch at once and one
# would get "database is locked" — busy_timeout does not reliably cover the
# journal_mode change. RLock (not Lock) because cost_guard.initialize() can
# re-enter _ensure_initialized() on the same thread.
_init_lock = threading.RLock()


def _reset_for_tests() -> None:
    """Test helper — clears the init flag and re-resolves the path."""
    global _db_path, _initialized
    _db_path = _default_db_path()
    _initialized = False


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        # Double-checked: another thread may have completed init while we
        # waited for the lock.
        if _initialized:
            return
        _do_initialize()


def _do_initialize() -> None:
    global _initialized
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA_DDL)
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        else:
            stored = int(row[0])
            if stored > SCHEMA_VERSION:
                raise RuntimeError(
                    f"sessions.db schema version {stored} newer than this "
                    f"engine supports ({SCHEMA_VERSION}); refusing to write."
                )
        # In-place additive migrations for older databases. Both columns are
        # nullable — older readers ignore them, older writers leave NULL.
        cur = conn.execute("PRAGMA table_info(sessions)")
        cols = {row["name"] for row in cur.fetchall()}
        if "provider" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN provider TEXT")
        if "auth_kind" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN auth_kind TEXT")
        wcols = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
        scols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "elapsed_s" not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN elapsed_s REAL")
        if "market" not in wcols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN market TEXT")
        if "name" not in wcols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN name TEXT")
        # Resumable runs (D7): additive tables, ignored by older engines. They hold run state and sanitized events only —
        # never provider_config, secrets, or request headers.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                ticker TEXT, trade_date TEXT, provider TEXT, model TEXT, auth_kind TEXT, engine TEXT, market TEXT,
                created_at REAL NOT NULL, ended_at REAL, elapsed_s REAL,
                input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL, cost_known INTEGER,
                session_id TEXT, terminal_code TEXT, error TEXT, last_seq INTEGER NOT NULL DEFAULT 0,
                last_event_type TEXT, last_event_at REAL, steps_done INTEGER NOT NULL DEFAULT 0,
                reservation_id TEXT, engine_started INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_runs_owner_state ON runs (owner, state);
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL, etype TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            );
            """
        )
        # D17: the latest official-price usage report of a run (JSON), so a run that ends without a result still has one.
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "cost_report" not in rcols:
            conn.execute("ALTER TABLE runs ADD COLUMN cost_report TEXT")
        # Bump the stored version forward if we just migrated up. Never
        # downgrade — a v2 db running on a v1 engine is rejected above.
        conn.execute(
            "UPDATE schema_version SET version = ? WHERE version < ?",
            (SCHEMA_VERSION, SCHEMA_VERSION),
        )
        conn.commit()
    # Mark initialized BEFORE the cost_guard call so that if cost_guard
    # re-enters storage._ensure_initialized() (it does — it calls back to
    # ensure the sessions table exists for its aggregator) we don't recurse.
    _initialized = True
    # Initialize CostGuard tables + seed config row. Imported lazily to
    # avoid the cost_guard → live_debate → llm_providers chain at import
    # time (storage.py is imported by server.py very early).
    from . import cost_guard as _cost_guard

    _cost_guard.initialize()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    try:
        # busy_timeout MUST be the first pragma. Switching journal_mode to WAL
        # itself takes a write lock, and until busy_timeout is set the default
        # is 0 (raise "database is locked" immediately). Setting it first means
        # even the WAL-mode switch waits for a competing first-connection
        # instead of erroring — this was the source of the intermittent
        # test_concurrent_writes flakiness. A session write and a cost finalize
        # can also land together at runtime.
        conn.execute("PRAGMA busy_timeout=5000")
        # Sensible defaults for a single-process embedded DB.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def _new_id() -> str:
    """ULID-like id: time + random suffix, lexicographic-sortable."""
    # Use millisecond epoch + 8 random bytes hex. Compact, sortable, safe
    # in URLs.
    ms = int(time.time() * 1000)
    rand = _secrets.token_hex(4)
    return f"{ms:012x}-{rand}"


@dataclass
class SessionSummary:
    """List-view payload — no event blob."""

    id: str
    ticker: str
    trade_date: str
    decision_action: str
    decision_confidence: float
    decision_reasoning: str
    live: bool
    provider: Optional[str]
    model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    estimated_cost_usd: Optional[float]
    created_at: str
    # Wall-clock seconds of the WHOLE run (data fetch, Dexter, debate), measured by the engine and stored
    # once; None for sessions saved before this was recorded (shown as "unknown", never guessed).
    elapsed_s: Optional[float] = None


@dataclass
class SessionDetail(SessionSummary):
    """Detail-view payload — events list inflated from JSON."""

    events: list[dict] = field(default_factory=list)


def db_path() -> str:
    """Public accessor — for /health surfaces."""
    return str(_db_path)


def write_session(
    *,
    ticker: str,
    trade_date: str,
    events: list[dict],
    decision: dict,
    live: bool,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    estimated_cost_usd: Optional[float] = None,
    auth_kind: Optional[str] = None,
    elapsed_s: Optional[float] = None,
) -> Optional[str]:
    """Persist a completed session. Returns the new id, or None on failure.

    Best-effort: never raises. Failures are logged to stderr and ignored.
    """
    try:
        _ensure_initialized()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] init failed: {type(exc).__name__}: {exc}\n")
        return None

    sid = _new_id()
    created_at = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    )
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, ticker, trade_date,
                    decision_action, decision_confidence, decision_reasoning,
                    live, provider, model, input_tokens, output_tokens,
                    estimated_cost_usd, auth_kind, created_at, events_json, elapsed_s
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    sid,
                    ticker,
                    trade_date,
                    # Column names predate the stance model (kept to avoid a
                    # SQLite migration): decision_action now stores the stance
                    # string, decision_confidence the conviction. Legacy rows
                    # holding BUY/SELL/HOLD are display-mapped in the UI.
                    str(decision.get("stance", decision.get("action", "neutral"))),
                    float(decision.get("conviction", decision.get("confidence", 0.0))),
                    str(decision.get("reasoning", "")),
                    1 if live else 0,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    estimated_cost_usd,
                    auth_kind,
                    created_at,
                    json.dumps(events, separators=(",", ":")),
                    elapsed_s,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] write failed: {type(exc).__name__}: {exc}\n")
        return None
    return sid


def write_session_from_events(
    *, ticker: str, trade_date: str, events: list[dict]
) -> Optional[str]:
    """Persist a session by extracting metadata from its captured event stream.

    Shared by the WS handler (`server.py`) and the Telegram bot path
    (`telegram_bot.py`) so both surfaces produce identical History rows and
    feed the same global spend ledger. If the stream was aborted (no
    `session.complete` event), nothing is written — partial transcripts
    don't belong in History or in the cost ledger.
    """
    if not events:
        return None
    complete: Optional[dict] = None
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "session.complete":
            complete = ev
            break
    if complete is None:
        return None
    raw_decision = complete.get("decision")
    decision = raw_decision if isinstance(raw_decision, dict) else {
        "stance": "neutral",
        "conviction": 0.0,
        "reasoning": "Session ended without a well-formed assessment payload.",
    }
    return write_session(
        ticker=ticker,
        trade_date=trade_date,
        events=events,
        decision=decision,
        live=bool(complete.get("live", False)),
        provider=complete.get("provider"),
        model=complete.get("model"),
        input_tokens=complete.get("input_tokens"),
        output_tokens=complete.get("output_tokens"),
        estimated_cost_usd=complete.get("estimated_cost_usd"),
        auth_kind=complete.get("auth_kind"),
        elapsed_s=complete.get("elapsed_s") if isinstance(complete.get("elapsed_s"), (int, float)) else None,
    )


def list_sessions(*, limit: int = 50, ticker: Optional[str] = None) -> list[SessionSummary]:
    """List recent completed sessions, newest first. Best-effort."""
    try:
        _ensure_initialized()
        with _connect() as conn:
            if ticker:
                cur = conn.execute(
                    """
                    SELECT id, ticker, trade_date, decision_action,
                           decision_confidence, decision_reasoning,
                           live, provider, model, input_tokens, output_tokens,
                           estimated_cost_usd, created_at, elapsed_s
                    FROM sessions
                    WHERE ticker = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (ticker.upper(), max(1, min(limit, 500))),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, ticker, trade_date, decision_action,
                           decision_confidence, decision_reasoning,
                           live, provider, model, input_tokens, output_tokens,
                           estimated_cost_usd, created_at, elapsed_s
                    FROM sessions
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 500)),),
                )
            rows = cur.fetchall()
        return [
            SessionSummary(
                id=row["id"],
                ticker=row["ticker"],
                trade_date=row["trade_date"],
                decision_action=row["decision_action"],
                decision_confidence=float(row["decision_confidence"]),
                decision_reasoning=row["decision_reasoning"],
                live=bool(row["live"]),
                provider=row["provider"],
                model=row["model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                estimated_cost_usd=row["estimated_cost_usd"],
                created_at=row["created_at"],
                elapsed_s=row["elapsed_s"],
            )
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] list failed: {type(exc).__name__}: {exc}\n")
        return []


def get_session(session_id: str) -> Optional[SessionDetail]:
    """Single-session detail with full event log. Returns None when absent."""
    try:
        _ensure_initialized()
        with _connect() as conn:
            cur = conn.execute(
                """
                SELECT id, ticker, trade_date, decision_action,
                       decision_confidence, decision_reasoning,
                       live, provider, model, input_tokens, output_tokens,
                       estimated_cost_usd, created_at, events_json, elapsed_s
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        try:
            events = json.loads(row["events_json"])
            if not isinstance(events, list):
                events = []
        except Exception:  # noqa: BLE001
            events = []
        return SessionDetail(
            id=row["id"],
            ticker=row["ticker"],
            trade_date=row["trade_date"],
            decision_action=row["decision_action"],
            decision_confidence=float(row["decision_confidence"]),
            decision_reasoning=row["decision_reasoning"],
            live=bool(row["live"]),
            provider=row["provider"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
            created_at=row["created_at"],
            elapsed_s=row["elapsed_s"],
            events=events,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] get failed: {type(exc).__name__}: {exc}\n")
        return None


def delete_session(session_id: str) -> bool:
    """Delete a single session. Returns True on actual deletion, False otherwise."""
    try:
        _ensure_initialized()
        with _connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] delete failed: {type(exc).__name__}: {exc}\n")
        return False


def summary_to_dict(s: SessionSummary) -> dict[str, Any]:
    return asdict(s)


def detail_to_dict(d: SessionDetail) -> dict[str, Any]:
    out = asdict(d)
    out["cost_report"] = session_cost_report(d.events)
    return out


def session_cost_report(events: list[dict]) -> Optional[dict[str, Any]]:
    """The official-price report saved with the run, else (older sessions) a clearly labelled recomputation from the stored
    totals at TODAY's list price. Never written back: saved accounting (estimated_cost_usd etc.) is not touched."""
    complete = next((e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "session.complete"), None)
    if complete is None:
        return None
    stored = complete.get("cost_report")
    if isinstance(stored, dict):
        return stored
    try:
        from .cost_report import recompute_for_session

        return recompute_for_session(complete)
    except Exception:  # noqa: BLE001 - the detail view must still open
        return None


# ---- Watchlist -------------------------------------------------------------


@dataclass
class WatchlistEntry:
    ticker: str
    added_at: str
    note: Optional[str]
    # Taiwan entries carry their official identity; legacy / US rows leave both None.
    market: Optional[str] = None      # "TWSE" (上市) | "TPEx" (上櫃)
    name: Optional[str] = None


class WatchlistConflict(RuntimeError):
    """Ticker already exists on the watchlist."""


def list_watchlist() -> list[WatchlistEntry]:
    """Return watchlist entries, newest first. Best-effort."""
    try:
        _ensure_initialized()
        with _connect() as conn:
            cur = conn.execute(
                "SELECT ticker, added_at, note, market, name FROM watchlist ORDER BY added_at DESC"
            )
            rows = cur.fetchall()
        return [
            WatchlistEntry(
                ticker=row["ticker"],
                added_at=row["added_at"],
                note=row["note"],
                market=row["market"],
                name=row["name"],
            )
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] watchlist list failed: {type(exc).__name__}: {exc}\n")
        return []


def add_watchlist(
    *, ticker: str, note: Optional[str] = None, market: Optional[str] = None, name: Optional[str] = None
) -> WatchlistEntry:
    """Add a ticker to the watchlist. Raises WatchlistConflict if already present."""
    _ensure_initialized()
    cleaned = (ticker or "").strip().upper()
    if not cleaned:
        raise ValueError("ticker required")
    if not (1 <= len(cleaned) <= 8):
        raise ValueError("ticker must be 1-8 characters")
    cleaned_note = (note or "").strip() or None
    added_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO watchlist (ticker, added_at, note, market, name) VALUES (?, ?, ?, ?, ?)",
                (cleaned, added_at, cleaned_note, market, (name or "").strip() or None),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise WatchlistConflict(f"{cleaned} is already on the watchlist") from exc
    return WatchlistEntry(ticker=cleaned, added_at=added_at, note=cleaned_note, market=market, name=(name or '').strip() or None)


def remove_watchlist(ticker: str) -> bool:
    """Remove a ticker. Returns True on actual deletion."""
    try:
        _ensure_initialized()
        cleaned = (ticker or "").strip().upper()
        if not cleaned:
            return False
        with _connect() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE ticker = ?", (cleaned,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[storage] watchlist remove failed: {type(exc).__name__}: {exc}\n")
        return False


def watchlist_to_dict(entry: WatchlistEntry) -> dict[str, Any]:
    return asdict(entry)
