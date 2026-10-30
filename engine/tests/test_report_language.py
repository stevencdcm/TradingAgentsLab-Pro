"""Generated-report language (zh-TW) — static / wire-level coverage.

IMPORTANT: nothing in this file calls a real model. It proves the language
parameter reaches the prompts every provider path sends (fake adapters, a fake
OpenAI-compatible HTTP server for the real local adapter, a stubbed WS handler).
Whether a real model then answers in Traditional Chinese must be verified by an
actual generation run (see docs/i18n-zh-TW.md).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from engine import live_debate as ld
from engine.live_debate import ProviderConfig, _AGENTS, live_debate
from engine.report_language import (
    free_engine_suffix,
    library_output_language,
    normalize_report_language,
)


# ---- pure mapping ----------------------------------------------------------


def test_normalize_defaults_to_english_and_accepts_variants():
    assert normalize_report_language(None) == "en"
    assert normalize_report_language("") == "en"
    assert normalize_report_language("fr") == "en"
    assert normalize_report_language("en") == "en"
    assert normalize_report_language("zh-TW") == "zh-TW"
    assert normalize_report_language("zh_tw") == "zh-TW"
    assert normalize_report_language("zh-Hant-TW") == "zh-TW"
    assert normalize_report_language(123) == "en"


def test_english_adds_nothing():
    assert free_engine_suffix("en") == ""
    assert free_engine_suffix("unknown") == ""
    assert library_output_language("en") is None


def test_zh_tw_instruction_is_explicit_about_script_usage_and_protected_tokens():
    suffix = free_engine_suffix("zh-TW")
    lib = library_output_language("zh-TW")
    assert lib and "Traditional Chinese" in lib and "Taiwan" in lib
    assert "never Simplified Chinese" in lib
    for protected in ("tickers", "numbers", "source", "citations", "identifiers"):
        assert protected in lib
    assert lib in suffix
    # Machine-readable decision lines must stay parseable.
    for key in ("STANCE=", "CONVICTION=", "BULL_STRENGTH=", "BEAR_STRENGTH=", "RISK="):
        assert key in suffix


# ---- free engine: prompts per agent ---------------------------------------


class _RecordingAdapter:
    def __init__(self):
        self.systems: list[str] = []

    async def open(self, **_):
        return None

    async def close(self):
        return None

    async def complete(self, *, system, user, model, max_tokens):
        self.systems.append(system)
        if "portfolio manager" in system:
            return (
                "STANCE=NEUTRAL\nCONVICTION=0.6\nBULL_STRENGTH=55\nBEAR_STRENGTH=45\nRISK=MODERATE\n"
                "多空力道接近，維持中立。",
                10,
                10,
            )
        return ("分析內容", 10, 10)


def _config(provider: str, auth: dict, model: str = "m") -> ProviderConfig:
    cfg = ProviderConfig.from_dict({"provider": provider, "auth": auth, "model": model})
    assert cfg is not None, provider
    return cfg


async def _run(adapter, cfg, report_language=None):
    kwargs = {} if report_language is None else {"report_language": report_language}
    events = []
    orig = ld.adapter_for
    ld.adapter_for = lambda _cfg: adapter
    try:
        async for ev in live_debate(
            ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg, **kwargs
        ):
            events.append(ev)
    finally:
        ld.adapter_for = orig
    return events


PROVIDERS = [
    ("openai-api-key", "openai", {"type": "api_key", "api_key": "k"}),
    ("openai-oauth", "openai", {"type": "oauth", "access": "a", "refresh": "r", "expires": 1, "account_id": "x"}),
    ("anthropic", "anthropic", {"type": "api_key", "api_key": "k"}),
    ("openrouter", "openrouter", {"type": "api_key", "api_key": "k"}),
    ("gemini", "gemini", {"type": "api_key", "api_key": "k"}),
    ("xai", "xai", {"type": "api_key", "api_key": "k"}),
    ("minimax", "minimax", {"type": "api_key", "api_key": "k"}),
    ("local", "local", {"type": "local", "base_url": "http://127.0.0.1:1/v1"}),
]


@pytest.mark.parametrize("name,provider,auth", PROVIDERS, ids=[p[0] for p in PROVIDERS])
def test_zh_tw_reaches_every_agent_prompt_for_every_provider(name, provider, auth):
    adapter = _RecordingAdapter()
    events = asyncio.run(_run(adapter, _config(provider, auth), "zh-TW"))
    assert len(adapter.systems) == len(_AGENTS)
    suffix = free_engine_suffix("zh-TW")
    for agent, system in zip(_AGENTS, adapter.systems):
        assert system == agent.system_prompt + suffix, agent.name
    start = next(e for e in events if e["type"] == "session.start")
    assert start["report_language"] == "zh-TW"
    done = next(e for e in events if e["type"] == "session.complete")
    # Decision still parses from the English machine-readable lines.
    assert done["decision"]["stance"] == "neutral"
    assert done["decision"]["conviction"] == pytest.approx(0.6)
    assert "中立" in done["decision"]["reasoning"]


@pytest.mark.parametrize("name,provider,auth", PROVIDERS, ids=[p[0] for p in PROVIDERS])
def test_english_and_omitted_language_leave_prompts_byte_identical(name, provider, auth):
    for lang in (None, "en", "xx"):
        adapter = _RecordingAdapter()
        events = asyncio.run(_run(adapter, _config(provider, auth), lang))
        assert adapter.systems == [a.system_prompt for a in _AGENTS]
        assert next(e for e in events if e["type"] == "session.start")["report_language"] == "en"


# ---- wire level: real local adapter against a fake OpenAI-compatible server --


class _FakeOpenAI(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802 — http.server API
        n = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).bodies.append(body)
        payload = {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "m"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "好"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):  # silence
        pass


def test_local_adapter_http_request_carries_the_language_instruction():
    _FakeOpenAI.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = _config(
            "local",
            {"type": "local", "base_url": f"http://127.0.0.1:{server.server_port}/v1"},
            model="fake-local",
        )
        events = asyncio.run(_run_real_adapter(cfg, "zh-TW"))
    finally:
        server.shutdown()
    assert len(_FakeOpenAI.bodies) == len(_AGENTS)
    suffix = free_engine_suffix("zh-TW")
    for body in _FakeOpenAI.bodies:
        system_msgs = [m["content"] for m in body["messages"] if m["role"] == "system"]
        assert system_msgs and system_msgs[0].endswith(suffix)
    assert any(e["type"] == "session.complete" for e in events)


async def _run_real_adapter(cfg, lang):
    events = []
    async for ev in live_debate(
        ticker="AAPL", trade_date="2026-10-30", summary=None, headlines=None, config=cfg, report_language=lang
    ):
        events.append(ev)
    return events


# ---- full LangGraph engine: config key + upstream instruction --------------


def test_full_engine_sets_upstream_output_language():
    pytest.importorskip("tradingagents")
    from engine.full_debate import _apply_report_language

    merged = _apply_report_language({"output_language": "English"}, "en")
    assert merged["output_language"] == "English"  # untouched
    merged = _apply_report_language({"output_language": "English"}, "zh-TW")
    assert "Traditional Chinese" in merged["output_language"]


def test_upstream_prompt_helper_picks_up_the_language():
    pytest.importorskip("tradingagents")
    from tradingagents.agents.utils.agent_utils import get_language_instruction
    from tradingagents.dataflows import config as df_config
    from engine.full_debate import _apply_report_language
    from tradingagents.default_config import DEFAULT_CONFIG

    saved = dict(df_config.get_config())
    try:
        df_config.set_config(_apply_report_language(dict(DEFAULT_CONFIG), "zh-TW"))
        text = get_language_instruction()
        assert "Traditional Chinese" in text and "Taiwan" in text
        df_config.set_config(_apply_report_language(dict(DEFAULT_CONFIG), "en"))
        assert get_language_instruction() == ""
    finally:
        df_config.set_config(saved)


# ---- WebSocket start frame ---------------------------------------------------


def test_ws_start_frame_report_language_reaches_both_engines(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from engine import server as srv

    seen: dict = {}

    async def fake_live(**kwargs):
        seen["free"] = kwargs.get("report_language")
        yield {"type": "session.start", "ticker": kwargs["ticker"], "trade_date": kwargs["trade_date"]}
        yield {
            "type": "session.complete",
            "ticker": kwargs["ticker"],
            "trade_date": kwargs["trade_date"],
            "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "ok"},
            "live": True,
        }

    async def fake_full(*_a, **kwargs):
        seen["full"] = kwargs.get("report_language")
        yield {"type": "session.start", "ticker": "AAPL", "trade_date": "2026-10-30"}
        yield {
            "type": "session.complete",
            "ticker": "AAPL",
            "trade_date": "2026-10-30",
            "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "ok"},
            "live": True,
        }

    async def no_data(**_):
        return None

    pytest.importorskip("tradingagents")
    from engine import full_debate as fd

    monkeypatch.setattr(srv, "live_debate", fake_live)
    monkeypatch.setattr(fd, "full_debate", fake_full)
    monkeypatch.setattr(srv, "_fetch_summary_safe", no_data)

    async def no_news(**_):
        return []

    monkeypatch.setattr(srv, "_fetch_news_safe", no_news)

    async def no_sent(*_a, **_k):
        return ""

    monkeypatch.setattr(srv.sentiment_sources, "fetch_stocktwits_messages_async", no_sent)
    monkeypatch.setattr(srv.sentiment_sources, "fetch_reddit_posts_async", no_sent)

    token = "t"
    app = srv.build_app(token=token)
    frame = {
        "ticker": "AAPL",
        "trade_date": "2026-10-30",
        "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}, "model": "m"},
    }
    with TestClient(app) as c:
        for lang, expect in (("zh-TW", "zh-TW"), (None, "en"), ("bogus", "en")):
            seen.clear()
            f = dict(frame)
            if lang is not None:
                f["report_language"] = lang
            with c.websocket_connect(f"/stream?token={token}") as ws:
                ws.send_json(f)
                while True:
                    ev = ws.receive_json()
                    if ev["type"] == "session.complete":
                        break
            assert seen["free"] == expect

            # Pro / full-graph engine takes the same frame field.
            seen.clear()
            pro = dict(f, engine="full", pro_config={"selected_analysts": ["market"]})
            with c.websocket_connect(f"/stream?token={token}") as ws:
                ws.send_json(pro)
                while True:
                    ev = ws.receive_json()
                    if ev["type"] == "session.complete":
                        break
            assert seen["full"] == expect


def test_ws_to_real_local_adapter_to_http_end_to_end(monkeypatch, tmp_db):
    """WS start frame -> real server handler -> real live_debate -> real local
    adapter -> HTTP request body. Only the model (a fake HTTP server) is fake."""
    from fastapi.testclient import TestClient
    from engine import server as srv

    async def no_data(**_):
        return None

    async def no_news(**_):
        return []

    async def no_sent(*_a, **_k):
        return ""

    monkeypatch.setattr(srv, "_fetch_summary_safe", no_data)
    monkeypatch.setattr(srv, "_fetch_news_safe", no_news)
    monkeypatch.setattr(srv.sentiment_sources, "fetch_stocktwits_messages_async", no_sent)
    monkeypatch.setattr(srv.sentiment_sources, "fetch_reddit_posts_async", no_sent)

    _FakeOpenAI.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = "t"
    frame = {
        "ticker": "AAPL",
        "trade_date": "2026-10-30",
        "report_language": "zh-TW",
        "provider_config": {
            "provider": "local",
            "auth": {"type": "local", "base_url": f"http://127.0.0.1:{server.server_port}/v1"},
            "model": "fake-local",
        },
    }
    try:
        with TestClient(srv.build_app(token=token)) as c:
            with c.websocket_connect(f"/stream?token={token}") as ws:
                ws.send_json(frame)
                events = []
                while True:
                    ev = ws.receive_json()
                    events.append(ev)
                    if ev["type"] == "session.complete":
                        break
    finally:
        server.shutdown()
    suffix = free_engine_suffix("zh-TW")
    assert len(_FakeOpenAI.bodies) == len(_AGENTS)
    assert all(b["messages"][0]["content"].endswith(suffix) for b in _FakeOpenAI.bodies)
    assert next(e for e in events if e["type"] == "session.start")["report_language"] == "zh-TW"
