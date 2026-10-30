"""Traditional-Chinese free-text PM decisions must parse (or be reported as
"review") — never silently become neutral / Hold (E3 P1-a / P1-b)."""

from __future__ import annotations

import pytest

pytest.importorskip("tradingagents")

from engine.full_debate import _parse_pm_decision  # noqa: E402
from engine.rating_parse import extract_pm_rating  # noqa: E402
from engine.stance import REVIEW, normalize_stance, stance_label  # noqa: E402
from engine.webhooks import WebhookFilter  # noqa: E402


def _state(text):
    return {"final_trade_decision": text}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("**評級**：增持\n**執行摘要**：偏多", "Overweight"),   # full-width colon
        ("**評級**: 買進", "Buy"),
        ("評等：持有", "Hold"),
        ("投資評級：減持", "Underweight"),
        ("最終評級：賣出。", "Sell"),
        ("**Rating**：Overweight", "Overweight"),               # NFKC on English label
        ("**Rating**: 增持", "Overweight"),
        ("**Rating**: Buy\n**Executive Summary**: ok", "Buy"),
        ("評級：強力買進", "Buy"),
    ],
)
def test_extract_pm_rating(text, expected):
    assert extract_pm_rating(text) == expected


@pytest.mark.parametrize("text", ["", "  ", "我們認為前景不明，需要更多資料。", "持有現金等待機會，沒有明確標籤"])
def test_unparseable_is_none(text):
    # A bare Chinese word without a rating label must NOT be taken as a rating.
    assert extract_pm_rating(text) is None


def test_decision_from_chinese_text_keeps_the_real_stance():
    d = _parse_pm_decision(_state("**評級**：增持\n\n**執行摘要**：技術面轉強。"))
    assert d["stance"] == "moderately_bullish"
    assert d["rating_status"] == "parsed"
    assert d["conviction"] == pytest.approx(0.65)


def test_unparseable_chinese_decision_is_review_with_no_conviction():
    d = _parse_pm_decision(_state("目前資訊不足，無法做出結論。"))
    assert d["stance"] == "review"
    assert d["conviction"] == 0.0
    assert d["rating_status"] == "unparsed"
    assert "資訊不足" in d["reasoning"]


def test_review_is_a_distinct_wire_value_and_never_triggers_a_webhook():
    assert normalize_stance("review") == REVIEW
    assert stance_label("review") == "Needs review"
    assert normalize_stance("garbage") == "neutral"  # legacy behaviour for other junk
    assert WebhookFilter().passes(stance="review", conviction=0.9) is False
    assert WebhookFilter().passes(stance="neutral", conviction=0.9) is True


def test_free_engine_missing_stance_line_is_review():
    from engine.live_debate import _parse_decision

    d = _parse_decision("沒有任何機器標籤，只有一段中文說明。")
    assert d["stance"] == "review" and d["rating_status"] == "unparsed" and d["conviction"] == 0.0
    ok = _parse_decision("STANCE=MODERATELY_BULLISH\nCONVICTION=0.7\nBULL_STRENGTH=60\nBEAR_STRENGTH=40\nRISK=LOW\n多方略占優勢。")
    assert ok["stance"] == "moderately_bullish" and ok["rating_status"] == "parsed"


# ---- memory log must not be polluted ------------------------------------------


def test_memory_stores_the_real_rating_for_chinese_text_and_skips_unparseable(tmp_path):
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.agents.utils.rating import extract_rating

    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
    zh = "**評級**：增持\n\n偏多。"
    # What full_debate does for a parsed Chinese decision: prepend the machine label.
    stored = f"**Rating**: {extract_pm_rating(zh)}\n\n{zh}" if extract_rating(zh) is None else zh
    log.store_decision(ticker="AAPL", trade_date="2026-10-30", final_trade_decision=stored)
    text = (tmp_path / "mem.md").read_text(encoding="utf-8")
    assert "[2026-10-30 | AAPL | Overweight | pending]" in text
    assert "Hold" not in text.splitlines()[0]
    # Reference: without the fix the upstream tagger would have written Hold.
    assert extract_rating(zh) is None
