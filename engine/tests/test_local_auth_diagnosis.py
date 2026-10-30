"""A local OpenAI-compatible server (oMLX) answers 401 both for NO key and for a WRONG key. The
engine knows which one it sent and must say so, on every path the user can hit: Settings List,
Settings Test, and a real analysis run (full graph)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

GOOD = "GOOD-KEY-0123456789"


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {GOOD}"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
        self._send(200, {"data": [{"id": "Q3.8 Flash Next 原版"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
        self._send(200, {"id": "x", "object": "chat.completion", "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


@pytest.fixture
def srv():
    s = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/v1"
    s.shutdown()


@pytest.fixture
def client(tmp_db):
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        yield c


def _cfg(url, key):
    auth = {"type": "local", "base_url": url}
    if key:
        auth["api_key"] = key
    return {"provider": "local", "auth": auth, "model": "Q3.8 Flash Next 原版"}


def test_list_models_distinguishes_missing_wrong_and_right_key(client, srv):
    r = client.post("/llm/local-models", json={"base_url": srv}).json()
    assert r["ok"] is False and r["status"] == 401 and r["reason"] == "key_missing"
    r = client.post("/llm/local-models", json={"base_url": srv, "api_key": "WRONG-KEY-000000"}).json()
    assert r["ok"] is False and r["reason"] == "key_rejected"
    r = client.post("/llm/local-models", json={"base_url": srv, "api_key": GOOD}).json()
    assert r["ok"] is True and r["models"] == ["Q3.8 Flash Next 原版"]


def test_test_connection_says_which_case_it_is_and_never_echoes_the_key(client, srv):
    none = client.post("/llm/test", json={"provider_config": _cfg(srv, None)}).json()
    assert none["ok"] is False and "none is stored" in none["error"]
    wrong = client.post("/llm/test", json={"provider_config": _cfg(srv, "WRONG-KEY-000000")}).json()
    assert wrong["ok"] is False and "rejected the stored API key" in wrong["error"] and "WRONG-KEY-000000" not in wrong["error"]
    ok = client.post("/llm/test", json={"provider_config": _cfg(srv, GOOD)}).json()
    assert ok["ok"] is True


def test_full_graph_run_error_carries_the_same_diagnosis(client, srv, monkeypatch, tmp_db):
    pytest.importorskip("tradingagents")
    from engine import server as srv_mod

    async def no_data(**_):
        return None

    async def no_news(**_):
        return []

    monkeypatch.setattr(srv_mod, "_fetch_summary_safe", no_data)
    monkeypatch.setattr(srv_mod, "_fetch_news_safe", no_news)
    frame = {"ticker": "NVDA", "trade_date": "2026-10-29", "engine": "pro", "pro_config": {"selected_analysts": ["market"]},
             "provider_config": _cfg(srv, None)}
    with client.websocket_connect("/stream?token=t") as ws:
        ws.send_json(frame)
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("run.error", "session.complete"):
                break
    assert ev["type"] == "run.error", ev
    assert "401" in ev["error"] or "Invalid API key" in ev["error"]
    assert "none is stored" in ev["error"], ev["error"]
