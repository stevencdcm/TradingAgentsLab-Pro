"""Immutable per-run Taiwan evidence snapshot: exactly what the analysts' tools handed to the model.

A report, or a comparison between two models on the same code and day, must be able to say what input each run actually saw - not what the
sources serve now. Every Taiwan tool call made during a run is recorded here as it answers:

* the tool, its arguments, the EXACT text returned to the model (bounded) and that text's sha256 (the hash is over the full text, so it
  stays comparable even when the stored copy is truncated);
* for each source behind it: source id, URL, request criteria, as_of, data period / data date, `published_at` (null when the source gives
  none - never invented), fetched_at, status and a content hash of the normalized rows used;
* gaps (typed: unavailable / denied / rate limited / empty / not provable as-of / unsupported sector) so coverage is explicit.

Never stored: credentials, the FinMind token, request headers, raw web pages or scripts, full upstream payloads. Only the bounded normalized
text the model read. The snapshot is written once (O_EXCL, read-only file) under the app data directory in a per-owner folder named by a
hash of the owner (no path component comes from the request), with a count/size retention bound. It is served only to its owner by an
authenticated endpoint and is not part of the repository or any public route. Older sessions have no snapshot and say so; nothing is
reconstructed afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = "her30.tw_run_snapshot/1"
MAX_OUTPUT_CHARS = 24_000          # stored copy of one tool answer (hash covers the whole answer)
MAX_ITEMS = 200
MAX_SNAPSHOT_BYTES = 1_500_000
KEEP_PER_OWNER = 200
KEEP_BYTES_PER_OWNER = 200_000_000
ID_RE = re.compile(r"^tws_[0-9a-f]{32}$")
_SECRETISH = re.compile(r"(?i)(token|api[_-]?key|authorization|secret|password)")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _clean_args(args: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments are model-supplied; keep short scalars only and drop anything credential-looking."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if _SECRETISH.search(str(k)):
            continue
        out[str(k)[:40]] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:200]
    return out


def evidence_entry(*, source_id: str, source_url: Optional[str], request: Optional[dict], status: str, data_date: Optional[str] = None,
                   as_of: Optional[str] = None, published_at: Optional[str] = None, fetched_at: Optional[str] = None,
                   rows: Optional[Any] = None, reason: Optional[str] = None, availability: Optional[str] = None) -> dict[str, Any]:
    """One source observation. `rows` (the normalized rows actually used) is hashed, never stored here."""
    req = {k: v for k, v in (request or {}).items() if not _SECRETISH.search(str(k))}
    return {"source_id": source_id, "source_url": source_url, "request": req, "status": status, "reason": reason, "data_date": data_date, "as_of": as_of,
            "published_at": published_at, "fetched_at": fetched_at, "availability": availability,
            "content_sha256": sha256_text(canonical(rows)) if rows is not None else None}


class Recorder:
    """Collects one run's tool calls. Thread-safe (graph tools run on worker threads)."""

    def __init__(self, *, code: Optional[str], trade_date: str) -> None:
        self.code, self.trade_date = code, trade_date
        self.items: list[dict[str, Any]] = []
        self.dropped = 0
        self._lock = threading.Lock()
        self.started_at = _now()
        self.run_nonce = uuid.uuid4().hex                  # makes the snapshot id unique PER RUN (see finalize)

    def add(self, tool: str, args: dict[str, Any], output: str, evidence: Optional[list[dict[str, Any]]] = None, gaps: Optional[list[str]] = None) -> None:
        text = output if isinstance(output, str) else str(output)
        item = {"tool": tool, "args": _clean_args(args), "called_at": _now(), "output_sha256": sha256_text(text), "output_chars": len(text),
                "output": text[:MAX_OUTPUT_CHARS], "output_truncated": len(text) > MAX_OUTPUT_CHARS,
                "evidence": list(evidence or []), "gaps": list(gaps or [])}
        with self._lock:
            if len(self.items) >= MAX_ITEMS:
                self.dropped += 1
                return
            item["seq"] = len(self.items) + 1
            self.items.append(item)

    def finalize(self, *, mode: str, analysts: Optional[list[str]] = None, dropped_analysts: Optional[list[str]] = None,
                 model: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._lock:
            items = [dict(i) for i in self.items]
            dropped = self.dropped
        covered: dict[str, dict[str, int]] = {}
        gaps: list[str] = []
        for it in items:
            for ev in it["evidence"]:
                c = covered.setdefault(ev["source_id"], {"ok": 0, "not_ok": 0})
                c["ok" if ev.get("status") == "ok" else "not_ok"] += 1
            gaps += it["gaps"]
        body = {"schema": SCHEMA, "stock_id": self.code, "trade_date": self.trade_date, "data_mode": mode, "created_at": _now(), "run_started_at": self.started_at,
                "analysts": analysts, "dropped_analysts": dropped_analysts or [], "model": model,
                "items": items, "items_dropped_over_limit": dropped,
                "coverage": {"sources": covered, "gaps": sorted(set(gaps))[:60]}}
        # `content_sha256` covers only what the analysts were handed (calls, exact-output hashes, evidence) - no model, analysts, clock or run key -
        # so two runs that saw identical inputs share it: that is the "same input" test for comparing models. The snapshot ID, by contrast, is
        # unique per run (content + run nonce): each run owns its own file, so a run's model, analysts and times can never be read from
        # another run that happened to see the same text.
        stable = {"stock_id": self.code, "trade_date": self.trade_date, "data_mode": mode,
                  "calls": [{"tool": i["tool"], "args": i["args"], "output_sha256": i["output_sha256"],
                             "evidence": [{k: e[k] for k in ("source_id", "request", "status", "data_date", "content_sha256")} for e in i["evidence"]]} for i in items]}
        digest = sha256_text(canonical(stable))
        body["content_sha256"] = digest
        body["snapshot_id"] = "tws_" + sha256_text(digest + ":" + self.run_nonce)[:32]
        return body


# ---- storage -------------------------------------------------------------------------------------------------------------


def base_dir() -> Path:
    from . import storage

    return Path(os.environ.get("TAL_TW_SNAPSHOT_DIR") or storage._default_db_path().parent / "tw-run-snapshots")


def owner_dir(owner: str, base: Optional[Path] = None) -> Path:
    return (base or base_dir()) / hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:16]


def persist(owner: str, snap: dict[str, Any], base: Optional[Path] = None) -> dict[str, Any]:
    """Write this run's snapshot once (its own file); return the manifest that goes into the session (id, hash, counts, coverage - no content)."""
    d = owner_dir(owner, base)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(snap, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:                       # shrink the stored copies, keep every hash
        slim = dict(snap)
        slim["items"] = [{**i, "output": i["output"][:2000], "output_truncated": True} for i in snap["items"]]
        slim["truncated_to_fit"] = True
        raw = json.dumps(slim, ensure_ascii=False)
    path = d / f"{snap['snapshot_id']}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                                                  # ids are per run: a collision is never silently shared
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    os.chmod(path, 0o400)
    _prune(d, keep=path)
    return manifest_of(snap)


def manifest_of(snap: dict[str, Any]) -> dict[str, Any]:
    return {"schema": snap["schema"], "snapshot_id": snap["snapshot_id"], "content_sha256": snap["content_sha256"], "stock_id": snap.get("stock_id"),
            "trade_date": snap.get("trade_date"), "data_mode": snap.get("data_mode"), "created_at": snap.get("created_at"), "calls": len(snap["items"]),
            "analysts": snap.get("analysts"), "dropped_analysts": snap.get("dropped_analysts"), "coverage": snap.get("coverage")}


def _prune(d: Path, keep: Path) -> None:
    try:
        files = sorted((p for p in d.glob("tws_*.json")), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for i, p in enumerate(files):
            size = p.stat().st_size
            total += size
            if p != keep and (i >= KEEP_PER_OWNER or total > KEEP_BYTES_PER_OWNER):
                p.chmod(0o600)
                p.unlink()
    except OSError:
        pass


def load(owner: str, snapshot_id: str, base: Optional[Path] = None) -> Optional[dict[str, Any]]:
    if not ID_RE.fullmatch(snapshot_id or ""):                               # nothing from the request becomes a path component unchecked
        return None
    p = owner_dir(owner, base) / f"{snapshot_id}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
