"""OpenAI OAuth for the web version (server side).

The browser page never sees tokens. Login runs `oauth-helper.mjs` (pi-ai, the
same flow as the desktop app) as a child process; the server opens the sign-in
URL in the user's default browser, relays progress / the manual-paste prompt to
the page, and stores the resulting credentials ONLY in the Keychain
(`oauth:openai`). The engine resolves `@@secret:oauth:openai:access@@` /
`...:refresh@@` when it builds the provider request.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

from .secret_store import SecretStore

KEY = "oauth:openai"
REFRESH_LEAD_S = 60
_DEFAULT_HELPER = Path(__file__).resolve().parent / "web" / "oauth-helper.mjs"


def _helper() -> Path:
    return Path(os.environ.get("TAL_OAUTH_HELPER") or _DEFAULT_HELPER)


def _node() -> Optional[str]:
    return os.environ.get("TAL_NODE") or shutil.which("node")


def _expires_ms(raw: float) -> float:
    return raw if raw >= 1e12 else raw * 1000


class OpenAIOAuth:
    def __init__(self, store: SecretStore, *, open_url: Callable[[str], Any] = webbrowser.open):
        self.store = store
        self.open_url = open_url
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self.events: list[dict[str, Any]] = []   # progress / prompt events for the page to poll
        self.state = "idle"                       # idle | running | done | error
        self.error: Optional[str] = None
        self._refresh_lock = threading.Lock()

    # ---- stored credentials (server only) -----------------------------------------------

    def _load(self) -> Optional[dict[str, Any]]:
        raw = self.store.get(KEY)
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return d if d.get("access") and d.get("refresh") else None
        except ValueError:
            return None

    def status(self) -> dict[str, Any]:
        d = self._load() if self.store.available() else None
        if not d:
            return {"connected": False}
        exp = _expires_ms(float(d.get("expires", 0)))
        return {
            "connected": True,
            "email": d.get("email"),
            "expiresAt": exp,
            "needsRefresh": time.time() * 1000 + REFRESH_LEAD_S * 1000 >= exp,
            "planType": d.get("planType"),
            "isFreeTier": d.get("planType") == "free",
        }

    def disconnect(self) -> bool:
        return self.store.delete(KEY, _internal=True)

    # ---- login --------------------------------------------------------------------------

    def start_login(self) -> dict[str, Any]:
        node = _node()
        if not node or not _helper().exists():
            return {"success": False, "error": "Node.js was not found (needed for the OpenAI sign-in helper)."}
        with self._lock:
            if self.state == "running":
                return {"success": False, "error": "Another OpenAI login is already in progress."}
            self.state, self.error, self.events = "running", None, []
        threading.Thread(target=self._run_login, args=(node,), daemon=True).start()
        return {"success": True, "started": True}

    def _run_login(self, node: str) -> None:
        try:
            proc = subprocess.Popen(
                [node, str(_helper()), "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "auth":
                    self.events.append({"type": "progress", "message": "Opening browser to complete sign-in…"})
                    self.open_url(ev["url"])
                elif t in ("progress", "prompt"):
                    self.events.append(ev)
                elif t == "done":
                    c = ev["credentials"]
                    self.store.set(KEY, json.dumps(c), _internal=True)
                    self.events.append({"type": "progress", "message": "Connected."})
                    self.state = "done"
                elif t == "error":
                    self.state, self.error = "error", ev.get("message", "login failed")
            proc.wait(timeout=5)
            if self.state == "running":
                self.state, self.error = "error", "Login ended without credentials."
        except Exception as exc:  # noqa: BLE001
            self.state, self.error = "error", f"{type(exc).__name__}"
        finally:
            self._proc = None

    def prompt_response(self, value: str) -> None:
        p = self._proc
        if p and p.stdin and not p.stdin.closed:
            p.stdin.write(json.dumps({"type": "prompt-response", "value": value}) + "\n")
            p.stdin.flush()

    def drain_events(self) -> dict[str, Any]:
        evs, self.events = self.events, []
        return {"events": evs, "state": self.state, "error": self.error}

    # ---- refresh -----------------------------------------------------------------------

    def ensure_fresh(self) -> Optional[dict[str, Any]]:
        d = self._load()
        if not d:
            return None
        if time.time() * 1000 + REFRESH_LEAD_S * 1000 < _expires_ms(float(d.get("expires", 0))):
            return d
        node = _node()
        if not node:
            return d
        with self._refresh_lock:   # refresh tokens may be single-use: never refresh concurrently
            d = self._load() or d
            if time.time() * 1000 + REFRESH_LEAD_S * 1000 < _expires_ms(float(d.get("expires", 0))):
                return d
            try:
                r = subprocess.run(
                    [node, str(_helper()), "refresh"], input=json.dumps({"refresh": d["refresh"], "email": d.get("email")}) + "\n",
                    capture_output=True, text=True, timeout=30,
                )
                for line in r.stdout.splitlines():
                    ev = json.loads(line)
                    if ev.get("type") == "done":
                        self.store.set(KEY, json.dumps(ev["credentials"]), _internal=True)
                        return ev["credentials"]
            except Exception:  # noqa: BLE001 — keep the stored credentials; the call will 401 visibly
                pass
            return d
