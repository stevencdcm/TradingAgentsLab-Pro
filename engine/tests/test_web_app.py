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


# ---- OpenAI OAuth (server side; the page never gets tokens) -----------------------------

FAKE_HELPER = r'''
import json, sys, time
mode = sys.argv[1]
def out(o): print(json.dumps(o), flush=True)
if mode == "login":
    out({"type": "auth", "url": "https://auth.example/authorize?x=1", "instructions": None})
    out({"type": "progress", "message": "waiting"})
    out({"type": "done", "credentials": {"access": "ACCESS-TOKEN-AAAA", "refresh": "REFRESH-TOKEN-BBBB",
         "expires": int(time.time() * 1000) + 3600_000, "email": "me@example.com", "accountId": "acct-123", "planType": "plus"}})
elif mode == "refresh":
    req = json.loads(sys.stdin.readline())
    out({"type": "done", "credentials": {"access": "ACCESS-NEW", "refresh": "REFRESH-NEW", "expires": int(time.time() * 1000) + 3600_000,
         "email": req.get("email"), "accountId": "acct-123", "planType": "plus"}})
'''


@pytest.fixture
def oauth_web(tmp_path, tmp_db, monkeypatch):
    import sys

    helper = tmp_path / "helper.py"
    helper.write_text(FAKE_HELPER)
    monkeypatch.setenv("TAL_NODE", sys.executable)
    monkeypatch.setenv("TAL_OAUTH_HELPER", str(helper))
    opened: list[str] = []
    static = tmp_path / "dist"; (static / "assets").mkdir(parents=True); (static / "index.html").write_text("<head></head>")
    kr = MemKeyring()
    store = SecretStore(kr)
    from engine.web_oauth import OpenAIOAuth

    oauth = OpenAIOAuth(store, open_url=opened.append)
    app = build_web_app(token="engine-token", port=PORT, data_dir=tmp_path / "d", static_dir=static,
                        launch_secret=LAUNCH, cookie_value=COOKIE, store=store, oauth=oauth)
    with TestClient(app, base_url=BASE) as c:
        c.cookies.set(COOKIE_NAME, COOKIE)
        c.kr, c.opened, c.oauth = kr, opened, oauth  # type: ignore[attr-defined]
        yield c
    secret_refs.configure(None)


def _wait_login(c):
    import time

    for _ in range(100):
        d = c.get("/web/oauth/events").json()
        if d["state"] in ("done", "error"):
            return d
        time.sleep(0.05)
    raise AssertionError("login did not finish")


def test_oauth_login_stores_tokens_only_in_the_keychain(oauth_web):
    c = oauth_web
    assert c.get("/web/oauth/status").json() == {"connected": False}
    assert c.post("/web/oauth/start").json()["success"] is True
    done = _wait_login(c)
    assert done["state"] == "done"
    assert c.opened == ["https://auth.example/authorize?x=1"]          # server opened the browser
    assert ("TradingAgentsLab-Web", "oauth:openai") in c.kr.d           # stored in the keychain
    st = c.get("/web/oauth/status")
    assert st.json()["connected"] is True and st.json()["email"] == "me@example.com"
    creds = c.get("/web/oauth/credentials")
    assert creds.json()["access"] == "@@secret:oauth:openai:access@@"
    assert creds.json()["accountId"] == "acct-123"
    for resp in (st, creds, c.get("/web/secrets/list"), c.get("/web/oauth/events")):
        assert "ACCESS-TOKEN-AAAA" not in resp.text and "REFRESH-TOKEN-BBBB" not in resp.text
    from engine import secret_refs as sr

    assert sr.resolve("@@secret:oauth:openai:access@@") == "ACCESS-TOKEN-AAAA"
    assert sr.resolve("@@secret:oauth:openai:refresh@@") == "REFRESH-TOKEN-BBBB"


def test_page_cannot_write_or_overwrite_oauth_entries(oauth_web):
    r = oauth_web.post("/web/secrets/set", json={"key": "oauth:openai", "value": "{}"})
    assert r.status_code == 403
    assert oauth_web.post("/web/secrets/delete", json={"key": "oauth:openai"}).status_code == 403


def test_expired_token_is_refreshed_server_side_before_use(oauth_web):
    c = oauth_web
    c.kr.d[("TradingAgentsLab-Web", "oauth:openai")] = json.dumps(
        {"access": "OLD", "refresh": "REFRESH-OLD", "expires": 1, "email": "me@example.com", "accountId": "acct-123"}
    )
    assert c.get("/web/oauth/status").json()["needsRefresh"] is True
    creds = c.get("/web/oauth/credentials").json()
    assert creds["access"] == "@@secret:oauth:openai:access@@"
    from engine import secret_refs as sr

    assert sr.resolve(creds["access"]) == "ACCESS-NEW"
    assert c.get("/web/oauth/status").json()["needsRefresh"] is False


def test_disconnect_removes_credentials(oauth_web):
    c = oauth_web
    c.post("/web/oauth/start"); _wait_login(c)
    assert c.post("/web/oauth/disconnect").json() is True
    assert c.get("/web/oauth/status").json() == {"connected": False}
    assert c.get("/web/oauth/credentials").json() is None


# ---- reference allow-list + error scrubbing -------------------------------------------------


def test_only_provider_credential_names_resolve(web):
    from engine import secret_refs as sr

    web.kr.d[("TradingAgentsLab-Web", "webhooks:configs")] = "https://hooks/SECRETTOKEN"
    web.kr.d[("TradingAgentsLab-Web", "llm:openai")] = "sk-llm-value-12345678"
    assert sr.resolve("@@secret:llm:openai@@") == "sk-llm-value-12345678"
    assert sr.resolve("@@secret:webhooks:configs@@") == ""        # not a provider credential
    assert sr.resolve("@@secret:anything:else@@") == ""
    assert sr.resolve({"a": ["@@secret:local:api-key@@"]}) == {"a": [""]}  # absent -> empty


def test_provider_errors_are_scrubbed_of_the_key(web):
    from engine import secret_refs as sr

    key = "sk-leaky-value-abcdefgh12345678"
    web.kr.d[("TradingAgentsLab-Web", "llm:openai")] = key
    sr.resolve("@@secret:llm:openai@@")          # registers the value for scrubbing
    msg = f"Incorrect API key provided: {key}. You can find it at ..., ending {key[-8:]}"
    cleaned = sr.redact(msg)
    assert key not in cleaned and key[-8:] not in cleaned and "[redacted]" in cleaned
