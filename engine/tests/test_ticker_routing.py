"""Taiwan codes and bad tickers are refused up front, never run on the wrong market's data."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from engine.ticker import taiwan_code, taiwan_unsupported_message


@pytest.mark.parametrize("raw,code", [("2330", "2330"), ("2330.TW", "2330"), ("6488.two", "6488"), ("0050", "0050"),
                                       ("00878", "00878"), ("00679B", "00679B"), (" 2317 ", "2317")])
def test_taiwan_codes_detected(raw, code):
    assert taiwan_code(raw) == code


@pytest.mark.parametrize("raw", ["TSM", "UMC", "AAPL", "NVDA", "BTC", "BRK-B", "", "123", "1234567"])
def test_us_adrs_and_others_are_not_taiwan(raw):
    assert taiwan_code(raw) is None


def test_message_is_explicit_and_localised():
    en = taiwan_unsupported_message("2330", "en")
    zh = taiwan_unsupported_message("2330", "zh-TW")
    assert "FinMind" in en and "TSM" in en and "2330" in en
    assert "FinMind" in zh and "TSM" in zh and "台股" in zh


def _ws_error(client, token, frame):
    with client.websocket_connect(f"/stream?token={token}") as ws:
        ws.send_json(frame)
        return ws.receive_json()


def test_ws_refuses_taiwan_and_invalid_tickers_before_any_provider_call(tmp_db, monkeypatch):
    from engine import server as srv

    called = {"n": 0}

    async def boom(**_):
        called["n"] += 1
        raise AssertionError("provider path must not run")

    monkeypatch.setattr(srv, "live_debate", boom)
    token = "t"
    with TestClient(srv.build_app(token=token)) as c:
        ev = _ws_error(c, token, {"ticker": "2330", "trade_date": "2026-10-30", "report_language": "zh-TW",
                                  "provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "k"}, "model": "m"}})
        assert ev["type"] == "run.error" and ev["code"] == "ticker_unsupported" and "台股" in ev["error"]
        ev = _ws_error(c, token, {"ticker": "2330.TW", "trade_date": "2026-10-30"})
        assert ev["type"] == "run.error" and "Taiwan" in ev["error"]
        ev = _ws_error(c, token, {"ticker": "$$$", "trade_date": "2026-10-30"})
        assert ev["type"] == "run.error" and ev["error"].startswith("Invalid ticker")
    assert called["n"] == 0
