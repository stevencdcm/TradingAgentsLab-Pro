"""Anthropic request-layer regression: the REAL SDK builds and sends the request to a fake
Messages-API server (ANTHROPIC_BASE_URL). The SDK validates keyword arguments client-side, so a
removed parameter (anthropic 2.x dropped temperature/top_p/top_k) fails here, not in production.
Covers the adapter path (/llm/test + free engine) and the LangChain path (full graph)."""

from __future__ import annotations

import asyncio

import pytest

from engine.llm_providers import ProviderConfig, adapter_for
from engine.tests.fake_anthropic import FakeAnthropic

KEY = "sk-ant-test-key-1234567890"
ALLOWED = {"model", "max_tokens", "system", "messages"}


@pytest.fixture
def srv(monkeypatch):
    s = FakeAnthropic(KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield s
    s.close()


def test_adapter_complete_sends_only_sdk_supported_parameters(srv):
    cfg = ProviderConfig.from_dict({"provider": "anthropic", "auth": {"type": "api_key", "api_key": KEY}, "model": "claude-haiku-4-5"})
    adapter = adapter_for(cfg)

    async def go():
        await adapter.open(api_key=cfg.bearer_token)
        try:
            return await adapter.complete(system="系統", user="你好", model="claude-haiku-4-5", max_tokens=32)
        finally:
            await adapter.close()

    text, tin, tout = asyncio.run(go())
    assert text == "好的，這是回覆。" and (tin, tout) == (12, 7)
    assert len(srv.bodies) == 1 and set(srv.bodies[0]) <= ALLOWED, srv.bodies
    assert srv.bodies[0]["system"] == "系統" and srv.bodies[0]["messages"][0]["content"] == "你好"
    h = srv.headers[0]
    assert KEY in (h.get("x-api-key"), (h.get("authorization") or "").removeprefix("Bearer "))   # SDK 2.x uses a Bearer header


def test_llm_test_endpoint_end_to_end(srv, tmp_db):
    from fastapi.testclient import TestClient
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        r = c.post("/llm/test", json={"provider_config": {"provider": "anthropic", "auth": {"type": "api_key", "api_key": KEY}, "model": "claude-haiku-4-5", "max_tokens": 1}})
        assert r.json()["ok"] is True, r.json()
        bad = c.post("/llm/test", json={"provider_config": {"provider": "anthropic", "auth": {"type": "api_key", "api_key": "wrong-key-value-123"}, "model": "claude-haiku-4-5"}})
        assert bad.json()["ok"] is False and "wrong-key-value-123" not in bad.text


def test_free_engine_live_debate_runs_on_anthropic(srv):
    from engine.live_debate import live_debate

    cfg = ProviderConfig.from_dict({"provider": "anthropic", "auth": {"type": "api_key", "api_key": KEY}, "model": "claude-haiku-4-5"})

    async def go():
        return [e async for e in live_debate(ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg, report_language="zh-TW")]

    events = asyncio.run(go())
    assert not any("live debate error" in str(e.get("content", "")) for e in events if e["type"] == "agent.message"), events[:3]
    assert len(srv.bodies) == 12 and all(set(b) <= ALLOWED for b in srv.bodies)


def test_langchain_path_used_by_the_full_graph_builds_valid_requests(srv):
    """The full graph's Anthropic client (langchain-anthropic through the upstream factory): plain
    invoke, tool binding and structured output must all pass the real SDK's validation."""
    from pydantic import BaseModel

    from tradingagents.llm_clients.factory import create_llm_client

    llm = create_llm_client("anthropic", "claude-haiku-4-5", base_url=srv.url, max_tokens=64).get_llm()
    assert "好的" in str(llm.invoke("hi").content)

    class Out(BaseModel):
        x: int

    try:
        llm.with_structured_output(Out).invoke("give x")   # fake server returns a tool_use block with empty input
    except Exception as exc:  # noqa: BLE001 — a parse failure of the FAKE reply is fine; an SDK TypeError is not
        assert not isinstance(exc, TypeError), exc
    for b in srv.bodies:
        assert "temperature" not in b or b["temperature"] is not None
        assert {"model", "max_tokens", "messages"} <= set(b)

