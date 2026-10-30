"""Report output language for generated research.

The desktop UI language ("zh-TW" / "en") is sent with every run as
``report_language`` and has to reach the prompts the LLM actually sees, not
just the labels around the result. This module is the single place that maps
the code to prompt text so every provider path (OpenAI API key, ChatGPT OAuth,
Anthropic, OpenRouter, Gemini, xAI, MiniMax, local) and both engines behave the
same:

* Full LangGraph engine: the upstream library already honours the
  ``output_language`` config key (``get_language_instruction()`` is appended to
  every analyst / researcher / debater / manager / trader prompt), so we only
  have to set that key — see ``library_output_language``.
* Free simplified engine (``live_debate``): its prompts are ours, so the
  instruction is appended to each agent's system prompt — see
  ``free_engine_suffix``.

English (the default) adds nothing, so English runs are byte-for-byte what they
were before this module existed. Previously saved sessions are never rewritten.
"""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_REPORT_LANGUAGE = "en"
SUPPORTED_REPORT_LANGUAGES = ("en", "zh-TW")

# Wording handed to the model. Kept explicit about Traditional characters and
# Taiwan usage (e.g. 資料 / 軟體 / 網路 / 品質 / 風險, not the mainland forms),
# and about what must NOT be translated, because tickers, numbers, sources and
# identifiers are what a reader cross-checks against the data.
_ZH_TW_NAME = (
    "Traditional Chinese (繁體中文) as written and used in Taiwan, using Taiwan "
    "vocabulary and never Simplified Chinese. Keep stock tickers, numbers, "
    "dates, units, currency symbols, source and publisher names, citations, "
    "URLs, and model / API / field identifiers exactly as given (do not "
    "translate or transliterate them)"
)


def normalize_report_language(raw: Any) -> str:
    """Return a supported language code; anything unknown falls back to English."""
    if isinstance(raw, str):
        value = raw.strip()
        if value in SUPPORTED_REPORT_LANGUAGES:
            return value
        if value.lower() in ("zh-tw", "zh_tw", "zh-hant", "zh-hant-tw"):
            return "zh-TW"
    return DEFAULT_REPORT_LANGUAGE


def library_output_language(code: str) -> Optional[str]:
    """Value for the upstream library's ``output_language`` config key.

    ``None`` means "leave the library default (English)".
    """
    return _ZH_TW_NAME if normalize_report_language(code) == "zh-TW" else None


def free_engine_suffix(code: str) -> str:
    """Text appended to every free-engine system prompt ('' for English)."""
    if normalize_report_language(code) != "zh-TW":
        return ""
    return (
        f"\n\nWrite your entire response in {_ZH_TW_NAME}. "
        "If this task asks for lines in a fixed machine-readable format "
        "(for example `STANCE=...`, `CONVICTION=...`, `BULL_STRENGTH=...`, "
        "`BEAR_STRENGTH=...`, `RISK=...`), keep those keys and their values "
        "exactly in the given English/numeric form and write only the "
        "free-text reasoning in Traditional Chinese."
    )
