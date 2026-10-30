"""Local (OpenAI-compatible) provider with an API key, and the full LangGraph
engine on that provider.

Models are FAKE (a tiny OpenAI-compatible HTTP server that enforces a Bearer
key, like oMLX). These tests prove auth, endpoint, wiring and that the full
graph runs to completion against a plain chat-completions server; they do not
prove a real Qwen/oMLX model behaves (tool-calling quality, latency) — that
needs a real run.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from engine.live_debate import ProviderConfig, live_debate

SECRET = "omlx-test-key-1234567890"


class _Server(BaseHTTPRequestHandler):
    log: list[dict] = []

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_ok(self):
        return self.headers.get("authorization") == f"Bearer {SECRET}"

    def do_GET(self):  # noqa: N802
        type(self).log.append({"method": "GET", "path": self.path, "auth": self._auth_ok()})
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "Invalid API key"}})
        if self.path.endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "Qwen3.8-Flash-Next-oQ4e-mtp"}, {"id": "other"}]})
        self._send(404, {})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).log.append({"method": "POST", "path": self.path, "auth": self._auth_ok(), "body": body})
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "Invalid API key"}})
        text = "STANCE=NEUTRAL\nCONVICTION=0.5\nBULL_STRENGTH=50\nBEAR_STRENGTH=50\nRISK=MODERATE\nok"
        self._send(
            200,
            {
                "id": "x", "object": "chat.completion", "created": 0, "model": body.get("model", "m"),
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            },
        )

    def log_message(self, *_):
        pass


@pytest.fixture
def server():
    _Server.log = []
    srv = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


# ---- ProviderConfig ---------------------------------------------------------


def test_local_auth_keeps_optional_api_key_and_bearer():
    cfg = ProviderConfig.from_dict(
        {"provider": "local", "auth": {"type": "local", "base_url": "http://x/v1", "api_key": f"  {SECRET} "}, "model": "m"}
    )
    assert cfg and cfg.bearer_token == SECRET and cfg.credential == SECRET
    keyless = ProviderConfig.from_dict({"provider": "local", "auth": {"type": "local", "base_url": "http://x/v1"}, "model": "m"})
    assert keyless and keyless.bearer_token == "local" and keyless.credential is None


def test_api_key_is_not_in_repr_of_log_labels():
    cfg = ProviderConfig.from_dict(
        {"provider": "local", "auth": {"type": "local", "base_url": "http://x/v1", "api_key": SECRET}, "model": "m"}
    )
    # The label the server logs per run contains provider/auth kind/model only.
    label = f"{cfg.provider}/{cfg.auth_kind}/{cfg.model}"
    assert SECRET not in label


# ---- free engine -------------------------------------------------------------


def _run_free(base_url, api_key):
    auth = {"type": "local", "base_url": base_url}
    if api_key:
        auth["api_key"] = api_key
    cfg = ProviderConfig.from_dict({"provider": "local", "auth": auth, "model": "Qwen3.8-Flash-Next-oQ4e-mtp"})

    async def go():
        return [e async for e in live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg)]

    return asyncio.run(go())


def test_free_engine_sends_the_key_to_an_authenticated_server(server):
    events = _run_free(server, SECRET)
    posts = [r for r in _Server.log if r["method"] == "POST"]
    assert posts and all(r["auth"] for r in posts)
    done = next(e for e in events if e["type"] == "session.complete")
    assert done["decision"]["stance"] == "neutral"


def test_without_a_key_the_server_rejects_like_omlx(server):
    events = _run_free(server, None)
    assert any("401" in str(e.get("content", "")) or "AuthenticationError" in str(e.get("content", "")) for e in events if e["type"] == "agent.message")


# ---- /llm/local-models --------------------------------------------------------


def test_local_models_endpoint_lists_models_with_key_and_never_echoes_it(server, tmp_db):
    from fastapi.testclient import TestClient
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        ok = c.post("/llm/local-models", json={"base_url": server, "api_key": SECRET})
        assert ok.status_code == 200 and ok.json()["models"][0] == "Qwen3.8-Flash-Next-oQ4e-mtp"
        assert SECRET not in ok.text
        bad = c.post("/llm/local-models", json={"base_url": server})
        assert bad.json() == {"ok": False, "status": 401, "error": "HTTP 401", "reason": "key_missing"}
        assert c.post("/llm/local-models", json={"base_url": "ftp://x"}).json()["ok"] is False


def test_llm_test_endpoint_accepts_local_with_key(server, tmp_db):
    from fastapi.testclient import TestClient
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        pc = {"provider": "local", "auth": {"type": "local", "base_url": server, "api_key": SECRET}, "model": "m"}
        r = c.post("/llm/test", json={"provider_config": pc})
        assert r.json()["ok"] is True
        pc["auth"].pop("api_key")
        assert c.post("/llm/test", json={"provider_config": pc}).json()["ok"] is False


# ---- full LangGraph engine on the local provider -------------------------------


def test_pro_library_config_maps_local_to_openai_compatible():
    from engine.server import _build_pro_library_config

    cfg = ProviderConfig.from_dict(
        {"provider": "local", "auth": {"type": "local", "base_url": "http://127.0.0.1:8000/v1", "api_key": SECRET}, "model": "Qwen3.8-Flash-Next-oQ4e-mtp"}
    )
    lib = _build_pro_library_config(cfg, {})
    assert lib["llm_provider"] == "openai_compatible"
    assert lib["backend_url"] == "http://127.0.0.1:8000/v1"
    assert lib["deep_think_llm"] == lib["quick_think_llm"] == "Qwen3.8-Flash-Next-oQ4e-mtp"
    assert SECRET not in json.dumps(lib)


def test_full_graph_runs_against_an_authenticated_openai_compatible_server(server):
    pytest.importorskip("tradingagents")
    from engine.full_debate import full_debate

    prev = os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)

    async def go():
        events = []
        async for ev in full_debate(
            "AAPL",
            "2026-10-30",
            config={
                "llm_provider": "openai_compatible",
                "deep_think_llm": "Qwen3.8-Flash-Next-oQ4e-mtp",
                "quick_think_llm": "Qwen3.8-Flash-Next-oQ4e-mtp",
                "backend_url": server,
                "max_debate_rounds": 1,
                "max_risk_discuss_rounds": 1,
            },
            selected_analysts=["market"],
            api_key=SECRET,
            auth_kind="local",
            report_language="zh-TW",
        ):
            events.append(ev)
            if ev["type"] in ("session.complete", "run.error"):
                break
        return events

    try:
        events = asyncio.run(asyncio.wait_for(go(), timeout=120))
    finally:
        # The key must not outlive graph construction in the process environment.
        leaked = os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        if prev is not None:
            os.environ["OPENAI_COMPATIBLE_API_KEY"] = prev
    assert leaked in (None, prev)
    types = [e["type"] for e in events]
    assert "run.error" not in types, [e for e in events if e["type"] == "run.error"]
    assert types[-1] == "session.complete"
    posts = [r for r in _Server.log if r["method"] == "POST"]
    assert len(posts) >= 5  # analyst + researchers + trader + risk + manager turns
    assert all(r["auth"] for r in posts), "every chat-completions call must carry the Bearer key"
    assert all(r["path"].endswith("/chat/completions") for r in posts)
    assert all(r["body"]["model"] == "Qwen3.8-Flash-Next-oQ4e-mtp" for r in posts)
    # tool_choice is not forced on generic servers (they often reject the object form)
    assert not any(isinstance(r["body"].get("tool_choice"), dict) for r in posts)
    # report language reached the prompts of the full graph too
    blob = json.dumps([r["body"]["messages"] for r in posts], ensure_ascii=False)
    assert "Traditional Chinese" in blob
