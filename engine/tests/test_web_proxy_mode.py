"""Trusted workbench reverse-proxy mode (S12): hop secret + owner + public Host/Origin + Secure cookie scoped
to the base path + cookie-authenticated WebSocket (no token in the URL). Default behaviour is unchanged."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine import secret_refs
from engine.secret_store import SecretStore
from engine.tests.test_web_app import COOKIE, COOKIE_NAME, LAUNCH, MemKeyring, PORT
from engine.web_app import ProxyConfig, build_web_app

PUBLIC = "https://m3.tailnet.example:47900"
HOST = "m3.tailnet.example:47900"
SECRET = "p" * 40
OWNER = "steven@example.com"


def _cfg(**over):
    kw = dict(public_origin=PUBLIC, base_path="/lab-pro", secret=SECRET, owners=frozenset({OWNER}))
    kw.update(over)
    return ProxyConfig(**kw)


@pytest.fixture
def app(tmp_path, tmp_db):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<!doctype html><html><head><script type="module" src="./assets/a.js"></script></head><body>app</body></html>')
    a = build_web_app(token="engine-token", port=PORT, data_dir=tmp_path / "data", static_dir=static, launch_secret=LAUNCH,
                      cookie_value=COOKIE, store=SecretStore(MemKeyring()), proxy=_cfg())
    yield a
    secret_refs.configure(None)


def hop(**over):
    h = {"host": HOST, "x-tal-proxy-auth": SECRET, "x-tal-owner": OWNER}
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def client(app, peer="127.0.0.1"):
    return TestClient(app, base_url=PUBLIC, client=(peer, 54321))


def test_config_is_validated_fail_closed():
    for bad in (dict(secret="short"), dict(owners=frozenset()), dict(public_origin="ftp://x"), dict(public_origin="")):
        with pytest.raises(ValueError):
            _cfg(**bad)


def test_page_through_the_proxy_gets_a_secure_base_path_cookie_and_relative_urls(app):
    r = client(app).get("/", headers=hop())
    assert r.status_code == 200
    sc = r.headers["set-cookie"]
    assert sc.startswith(f"{COOKIE_NAME}=") and "HttpOnly" in sc and "SameSite=Strict" in sc and "Secure" in sc and "Path=/lab-pro" in sc
    assert "Domain" not in sc
    assert '<base href="/lab-pro/">' in r.text and 'src="web-bridge.js"' in r.text and 'src="/web-bridge.js"' not in r.text
    assert r.text.index("<base") < r.text.index("assets/a.js")          # base precedes every relative URL


@pytest.mark.parametrize("override", [
    {"x-tal-proxy-auth": None}, {"x-tal-proxy-auth": "wrong" * 10}, {"x-tal-owner": None}, {"x-tal-owner": "mallory@example.com"},
    {"host": "evil.example"}, {"origin": "https://evil.example"},
])
def test_every_hop_condition_is_required(app, override):
    # With the proxy secret missing the request falls back to the loopback-only rules (public Host => 403 too).
    assert client(app).get("/", headers=hop(**override)).status_code == 403


def test_a_forged_hop_from_a_non_loopback_peer_is_refused(app):
    assert client(app, peer="10.1.2.3").get("/", headers=hop()).status_code == 403


def test_owner_header_alone_does_not_bypass_the_local_guard(app):
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 1))
    assert c.get("/", headers={"x-tal-owner": OWNER}).status_code == 403          # no cookie, no hop secret


def test_state_changing_requests_need_origin_and_the_session_cookie(app):
    c = client(app)
    c.get("/", headers=hop())                                                       # first page load issues the cookie
    post = lambda **h: c.post("/web/consent/accept", headers=hop(**h))
    assert post().status_code == 403                                                # no Origin
    assert post(origin="https://evil.example").status_code == 403                  # wrong Origin
    c.cookies.clear()
    assert post(origin=PUBLIC).status_code == 403                                   # Origin ok but cookie missing
    c.cookies.set(COOKIE_NAME, COOKIE)
    assert post(origin=PUBLIC).status_code == 200


def test_websocket_authenticates_by_cookie_without_a_token_in_the_url(app):
    c = client(app)
    c.cookies.set(COOKIE_NAME, COOKIE)
    ok = {**hop(), "origin": PUBLIC, "cookie": f"{COOKIE_NAME}={COOKIE}"}
    with c.websocket_connect("/stream", headers=ok) as ws:                         # no ?token=
        ws.send_json({"ticker": "AAPL", "trade_date": "2026-10-30"})
        assert ws.receive_json()["type"]                                            # stub debate answers
    for bad in ({**ok, "cookie": ""}, {**ok, "origin": "https://evil.example"}, {k: v for k, v in ok.items() if k != "x-tal-owner"}):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/stream", headers=bad):
                pass


def test_handshake_tells_the_page_it_is_proxied_and_local_mode_is_unchanged(app, tmp_path, tmp_db):
    c = client(app)
    c.cookies.set(COOKIE_NAME, COOKIE)
    assert c.get("/web/handshake", headers=hop()).json()["proxy"] is True
    # same app, plain loopback + nonce flow still works and the socket still needs the engine token
    local = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 9))
    assert local.get(f"/?k={LAUNCH}", follow_redirects=False).status_code == 302
    local.cookies.set(COOKIE_NAME, COOKIE)
    lh = {"host": f"127.0.0.1:{PORT}", "cookie": f"{COOKIE_NAME}={COOKIE}"}
    with pytest.raises(WebSocketDisconnect):
        with local.websocket_connect("/stream", headers=lh):
            pass
    with local.websocket_connect("/stream?token=engine-token", headers=lh):
        pass


def test_from_env(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text(SECRET + "\n")
    monkeypatch.delenv("TAL_WEB_PROXY_MODE", raising=False)
    assert ProxyConfig.from_env() is None
    monkeypatch.setenv("TAL_WEB_PROXY_MODE", "1")
    monkeypatch.setenv("TAL_WEB_PROXY_SECRET_FILE", str(f))
    monkeypatch.setenv("TAL_WEB_PUBLIC_ORIGIN", PUBLIC)
    monkeypatch.setenv("TAL_WEB_BASE_PATH", "/lab-pro/")
    monkeypatch.setenv("TAL_WEB_OWNER", f" {OWNER.upper()} , other@x ")
    cfg = ProxyConfig.from_env()
    assert cfg.base_path == "/lab-pro" and OWNER in cfg.owners and cfg.secure
    monkeypatch.setenv("TAL_WEB_PROXY_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        ProxyConfig.from_env()
