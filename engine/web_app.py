"""Local web version of Trading Agents Lab Pro.

One process, bound to 127.0.0.1: the existing engine API (`build_app`) plus the
built renderer and a small real bridge (`/web/*`) that replaces what the
Electron preload/IPC provided. Not a mock: consent, secret storage, handshake
and shutdown are real server-side implementations.

Security model
--------------
* Loopback only. Every request must carry a `Host` of 127.0.0.1:<port> or
  localhost:<port> (blocks DNS rebinding) and, when an `Origin` is sent, the same
  origin (blocks cross-site requests and cross-site WebSockets).
* Entry is by a one-time launch URL (`/?k=<random>`) that sets an HttpOnly,
  SameSite=Strict cookie; without that cookie nothing is served (not even the
  page) and the engine bearer token is never handed out.
* Provider secrets live only in the macOS Keychain (fail closed, see
  `secret_store.py`). The page sees opaque `@@secret:name@@` references.
"""

from __future__ import annotations

import hmac
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, secret_refs
from .secret_store import PLAIN_CONFIG, SecretStore, SecretStoreError, ref_for
from .web_oauth import OpenAIOAuth
from .server import build_app

COOKIE_NAME = "tal_web"
CONSENT_VERSION = 1
HERE = Path(__file__).resolve().parent


class LaunchNonce:
    """Single-use launch secret. Consumed on first successful use; the 0600 file that
    tools/open-web.sh reads is removed with it. `rotate()` issues a fresh one (SIGUSR1)
    without ever printing it."""

    def __init__(self, value: str, port: int = 0, file: Path | None = None):
        self.value = value
        self.port = port
        self.file = file
        self._lock = threading.Lock()
        self._write()

    def _write(self) -> None:
        if self.file is None or not self.value:
            return
        self.file.write_text(f"http://127.0.0.1:{self.port}/?k={self.value}\n")
        os.chmod(self.file, 0o600)

    def consume(self, candidate: str) -> bool:
        with self._lock:
            if not self.value or not hmac.compare_digest(candidate, self.value):
                return False
            self.value = ""
            if self.file is not None:
                try:
                    self.file.unlink()
                except FileNotFoundError:
                    pass
            return True

    def rotate(self) -> None:
        import secrets

        with self._lock:
            self.value = secrets.token_urlsafe(24)
            self._write()


class LocalOnly:
    """Pure-ASGI guard (covers HTTP and WebSocket)."""

    def __init__(self, app, *, port: int, launch_secret: "str | LaunchNonce", cookie_value: str):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.launch = launch_secret if isinstance(launch_secret, LaunchNonce) else LaunchNonce(launch_secret, port)
        self.cookie_value = cookie_value

    async def _deny(self, scope, receive, send, status: int, text: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse(text, status_code=status)(scope, receive, send)

    def _cookie_ok(self, headers: dict[bytes, bytes]) -> bool:
        raw = headers.get(b"cookie", b"").decode("latin-1")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and hmac.compare_digest(v, self.cookie_value):
                return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        if headers.get(b"host", b"").decode("latin-1") not in self.hosts:
            return await self._deny(scope, receive, send, 403, "bad host")
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin-1") not in self.origins:
            return await self._deny(scope, receive, send, 403, "bad origin")

        # One-time entry: ?k=<launch secret> -> cookie, then a clean URL.
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/":
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            k = (qs.get("k") or [""])[0]
            if k and self.launch.consume(k):
                resp = PlainTextResponse("", status_code=302, headers={"location": "/"})
                resp.set_cookie(COOKIE_NAME, self.cookie_value, httponly=True, samesite="strict", path="/")
                return await resp(scope, receive, send)

        if not self._cookie_ok(headers):
            return await self._deny(
                scope, receive, send, 403, "Open the launch URL printed by the server (it sets a session cookie)."
            )
        return await self.app(scope, receive, send)


def _json_error(exc: SecretStoreError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)


def build_web_app(
    *,
    token: str,
    port: int,
    data_dir: Path,
    static_dir: Path,
    launch_secret: str,
    cookie_value: str,
    store: SecretStore | None = None,
    oauth: OpenAIOAuth | None = None,
) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)
    store = store or SecretStore()
    secret_refs.configure(store)

    app = build_app(token=token)

    # ---- /web/* : the real bridge -------------------------------------------

    @app.get("/web/handshake")
    async def handshake() -> dict[str, Any]:
        return {"port": port, "token": token}

    consent_file = data_dir / "consent.json"

    def _accepted() -> int | None:
        try:
            return int(json.loads(consent_file.read_text()).get("version"))
        except (OSError, ValueError, TypeError):
            return None

    @app.get("/web/consent")
    async def consent_get() -> dict[str, Any]:
        return {"acceptedVersion": _accepted(), "requiredVersion": CONSENT_VERSION}

    @app.post("/web/consent/accept")
    async def consent_accept() -> bool:
        tmp = consent_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CONSENT_VERSION, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
        os.replace(tmp, consent_file)
        return True

    @app.get("/web/secrets/availability")
    async def secrets_availability() -> dict[str, Any]:
        return {
            "available": store.available(),
            "filePath": "macOS Keychain (service TradingAgentsLab-Web)",
            "corruptionRecovery": None,
        }

    @app.get("/web/secrets/list")
    async def secrets_list():
        try:
            # Fixed mask: no part of a secret (not even the last 4 characters) leaves the server.
            return [{"key": e["key"], "hint": "••••", "updatedAt": e["updatedAt"]} for e in store.list()]
        except SecretStoreError as exc:
            return _json_error(exc)

    @app.post("/web/secrets/set")
    async def secrets_set(body: dict[str, Any] = Body(...)):
        try:
            entry = store.set(str(body.get("key", "")), str(body.get("value", "")))
            return {"hint": "••••", "updatedAt": entry["updatedAt"], "cipher": ""}
        except SecretStoreError as exc:
            return _json_error(exc)

    @app.post("/web/secrets/get")
    async def secrets_get(body: dict[str, Any] = Body(...)):
        name = str(body.get("key", ""))
        try:
            if name in PLAIN_CONFIG:
                return store.get(name)  # configuration, not a secret
            return ref_for(name) if store.has(name) else None
        except SecretStoreError as exc:
            return _json_error(exc)

    @app.post("/web/secrets/delete")
    async def secrets_delete(body: dict[str, Any] = Body(...)):
        try:
            return store.delete(str(body.get("key", "")))
        except SecretStoreError as exc:
            return _json_error(exc)

    oauth = oauth or OpenAIOAuth(store)

    @app.get("/web/oauth/status")
    async def oauth_status() -> dict[str, Any]:
        return oauth.status()

    @app.post("/web/oauth/start")
    async def oauth_start() -> dict[str, Any]:
        return oauth.start_login()

    @app.get("/web/oauth/events")
    async def oauth_events() -> dict[str, Any]:
        return oauth.drain_events()

    @app.post("/web/oauth/prompt")
    async def oauth_prompt(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
        oauth.prompt_response(str(body.get("value", "")))
        return {"ok": True}

    @app.post("/web/oauth/disconnect")
    async def oauth_disconnect() -> bool:
        return oauth.disconnect()

    @app.get("/web/oauth/credentials")
    async def oauth_credentials():
        """References only — the tokens stay on the server; the engine resolves them."""
        d = await run_in_threadpool(oauth.ensure_fresh)
        if not d:
            return None
        return {
            "access": ref_for("oauth:openai:access"),
            "refresh": ref_for("oauth:openai:refresh"),
            "expires": d.get("expires"),
            "email": d.get("email"),
            "accountId": d.get("accountId"),
        }

    @app.post("/web/shutdown")
    async def shutdown() -> dict[str, bool]:
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"ok": True}

    @app.get("/web/info")
    async def info() -> dict[str, Any]:
        return {"version": __version__, "mode": "web", "dataDir": str(data_dir)}

    # ---- static renderer + bridge script ---------------------------------------

    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    index_html = index_html.replace("</head>", '    <script src="/web-bridge.js"></script>\n  </head>', 1)
    bridge_js = (HERE / "web" / "web-bridge.js").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return index_html

    @app.get("/web-bridge.js")
    async def bridge() -> PlainTextResponse:
        return PlainTextResponse(bridge_js, media_type="text/javascript")

    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    # Outermost: runs before everything else.
    nonce = LaunchNonce(launch_secret, port, data_dir / "launch-url.txt")
    app.state.launch = nonce
    app.add_middleware(LocalOnly, port=port, launch_secret=nonce, cookie_value=cookie_value)
    return app
