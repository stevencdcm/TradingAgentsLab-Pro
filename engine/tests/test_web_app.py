"""Local web version: loopback/Host/Origin/cookie guards, real bridge endpoints,
secret handling (never returned to the page; resolved server-side; fail closed)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine import secret_refs
from engine.secret_store import SecretStore
from engine.web_app import COOKIE_NAME, build_web_app

PORT = 8765
LAUNCH = "launch-secret-xyz"
COOKIE = "cookie-value-abc"
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "sk-test-secret-value-1234567890"


class MemKeyring:
    """In-memory stand-in for the keychain backend (a real, working store)."""

    def __init__(self):
        self.d: dict[tuple[str, str], str] = {}

    def get_keyring(self):  # makes SecretStore.available() see a non-fail backend
        return self

    def set_password(self, s, n, v):
        self.d[(s, n)] = v

    def get_password(self, s, n):
        return self.d.get((s, n))

    def delete_password(self, s, n):
        self.d.pop((s, n), None)


class _Fail(MemKeyring):
    pass


_Fail.__module__ = "keyring.backends.fail"


@pytest.fixture
def web(tmp_path, tmp_db):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html><head><title>x</title></head><body>app</body></html>")
    kr = MemKeyring()
    app = build_web_app(
        token="engine-token", port=PORT, data_dir=tmp_path / "data", static_dir=static,
        launch_secret=LAUNCH, cookie_value=COOKIE, store=SecretStore(kr),
    )
    with TestClient(app, base_url=BASE) as c:
        c.cookies.set(COOKIE_NAME, COOKIE)
        c.kr = kr  # type: ignore[attr-defined]
        yield c
    secret_refs.configure(None)


def anon(web_client: TestClient) -> TestClient:
    return TestClient(web_client.app, base_url=BASE)


# ---- guards ---------------------------------------------------------------------


def test_bad_host_is_rejected(web):
    assert web.get("/web/handshake", headers={"host": "evil.example:8765"}).status_code == 403
    assert web.get("/web/handshake", headers={"host": f"127.0.0.1:{PORT + 1}"}).status_code == 403


def test_cross_origin_is_rejected(web):
    assert web.get("/web/handshake", headers={"origin": "http://evil.example"}).status_code == 403
    assert web.post("/web/consent/accept", headers={"origin": "http://evil.example"}).status_code == 403
    assert web.get("/web/handshake", headers={"origin": BASE}).status_code == 200


def test_no_cookie_no_service_and_no_engine_token(web):
    c = anon(web)
    for path in ("/", "/web/handshake", "/web/secrets/list", "/health", "/web-bridge.js"):
        assert c.get(path).status_code == 403, path


def test_launch_url_sets_httponly_samesite_strict_cookie(web):
    c = anon(web)
    r = c.get(f"/?k={LAUNCH}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    assert anon(web).get("/?k=wrong", follow_redirects=False).status_code == 403


def test_websocket_origin_and_cookie_enforced(web):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with web.websocket_connect("/stream?token=engine-token", headers={"origin": "http://evil.example"}):
            pass
    c = anon(web)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/stream?token=engine-token"):
            pass


# ---- real bridge -------------------------------------------------------------------


def test_handshake_page_and_bridge_script(web):
    h = web.get("/web/handshake").json()
    assert h == {"port": PORT, "token": "engine-token"}
    page = web.get("/").text
    assert '<script src="/web-bridge.js"></script>' in page
    js = web.get("/web-bridge.js").text
    assert "window.tradingAgentsLab" in js and "localStorage" not in js


def test_consent_round_trip_persists(web, tmp_path):
    assert web.get("/web/consent").json() == {"acceptedVersion": None, "requiredVersion": 1}
    assert web.post("/web/consent/accept").json() is True
    assert web.get("/web/consent").json()["acceptedVersion"] == 1
    assert (tmp_path / "data" / "consent.json").exists()


def test_engine_still_requires_its_bearer_token(web):
    assert web.get("/health").status_code == 401
    assert web.get("/health", headers={"Authorization": "Bearer engine-token"}).status_code == 200


# ---- secrets ------------------------------------------------------------------------


def test_secret_never_comes_back_to_the_page(web):
    r = web.post("/web/secrets/set", json={"key": "llm:anthropic", "value": SECRET})
    assert r.status_code == 200 and SECRET not in r.text and SECRET[-4:] not in r.text
    got = web.post("/web/secrets/get", json={"key": "llm:anthropic"}).json()
    assert got == "@@secret:llm:anthropic@@"
    listing = web.get("/web/secrets/list")
    assert listing.json()[0]["key"] == "llm:anthropic" and listing.json()[0]["hint"] == "••••"
    for resp in (listing, web.get("/web/secrets/availability"), web.get("/web/info")):
        assert SECRET not in resp.text and SECRET[-4:] not in resp.text
    assert web.post("/web/secrets/get", json={"key": "llm:openai"}).json() is None
    assert web.post("/web/secrets/delete", json={"key": "llm:anthropic"}).json() is True
    assert web.post("/web/secrets/get", json={"key": "llm:anthropic"}).json() is None


def test_plain_config_is_readable_but_credentials_and_webhooks_are_not(web):
    web.post("/web/secrets/set", json={"key": "local:base-url", "value": "http://127.0.0.1:8000/v1"})
    assert web.post("/web/secrets/get", json={"key": "local:base-url"}).json() == "http://127.0.0.1:8000/v1"
    web.post("/web/secrets/set", json={"key": "local:api-key", "value": SECRET})
    assert web.post("/web/secrets/get", json={"key": "local:api-key"}).json() == "@@secret:local:api-key@@"
    for k in ("webhooks:configs", "telegram:bot-token"):
        r = web.post("/web/secrets/set", json={"key": k, "value": "x"})
        assert r.status_code == 403


def test_fail_closed_without_a_real_keychain(tmp_path, tmp_db):
    static = tmp_path / "d"; (static / "assets").mkdir(parents=True); (static / "index.html").write_text("<head></head>")
    app = build_web_app(token="t", port=PORT, data_dir=tmp_path / "x", static_dir=static,
                        launch_secret=LAUNCH, cookie_value=COOKIE, store=SecretStore(_Fail()))
    with TestClient(app, base_url=BASE) as c:
        c.cookies.set(COOKIE_NAME, COOKIE)
        assert c.get("/web/secrets/availability").json()["available"] is False
        r = c.post("/web/secrets/set", json={"key": "llm:openai", "value": SECRET})
        assert r.status_code == 503 and SECRET not in r.text
    secret_refs.configure(None)


class _Auth(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", "0")); self.rfile.read(n)
        type(self).seen.append(self.headers.get("authorization"))
        ok = self.headers.get("authorization") == f"Bearer {SECRET}"
        body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                           "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}} if ok else {"error": {"message": "bad key"}}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *_):
        pass


def test_engine_resolves_the_reference_server_side(web):
    _Auth.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Auth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        web.post("/web/secrets/set", json={"key": "local:api-key", "value": SECRET})
        ref = web.post("/web/secrets/get", json={"key": "local:api-key"}).json()
        body = {"provider_config": {"provider": "local",
                "auth": {"type": "local", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": ref}, "model": "m"}}
        r = web.post("/llm/test", json=body, headers={"Authorization": "Bearer engine-token"})
        assert r.json()["ok"] is True and SECRET not in r.text
        assert _Auth.seen == [f"Bearer {SECRET}"]  # the real key reached the provider, not the reference
        # An unknown reference must never be sent literally to a third party.
        _Auth.seen = []
        body["provider_config"]["auth"]["api_key"] = "@@secret:nope@@"
        assert web.post("/llm/test", json=body, headers={"Authorization": "Bearer engine-token"}).json()["ok"] is False
        assert all("@@secret" not in (s or "") for s in _Auth.seen)
    finally:
        srv.shutdown()
