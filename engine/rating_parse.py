"""Rating extraction for the Portfolio Manager's final decision.

Rules (E3 P1 / E5 review):

* Only an explicit **label** counts: ``**Rating**: X`` (English enum) or a Chinese
  label such as ``評級：增持`` / ``最終評級：持有`` (NFKC-normalised, so full-width
  punctuation works). Scattered English words in prose ("前次決策為 Buy…",
  "We would not Buy at this price") are NEVER read as the rating.
* Labels that refer to a previous decision (前次 / 先前 / 過去 / 歷史 / previous /
  prior / past …) are ignored.
* If several different ratings are labelled, the structured template line
  (the first non-empty line) wins; otherwise a label marked 本次 / 最終 / final /
  overall / current wins; otherwise the text is ambiguous and ``None`` is returned
  so callers surface an explicit *review* state instead of guessing.
* ``None`` means "no conclusion can be read", never Hold.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

_EN = ("Buy", "Overweight", "Hold", "Underweight", "Sell")

# Chinese rating words -> 5-tier names. Single-character "賣" is deliberately absent
# ("賣壓沉重" is not a rating). Longest alternatives first.
_ZH_TO_TIER = {
    "強力買進": "Buy", "強烈買進": "Buy", "強力買入": "Buy", "買進": "Buy", "買入": "Buy",
    "增持": "Overweight", "加碼": "Overweight", "偏多": "Overweight",
    "持有": "Hold", "中立": "Hold", "觀望": "Hold",
    "減持": "Underweight", "減碼": "Underweight", "偏空": "Underweight",
    "強力賣出": "Sell", "強烈賣出": "Sell", "賣出": "Sell",
}
_ZH_WORDS = "|".join(sorted(_ZH_TO_TIER, key=len, reverse=True))
_EN_WORDS = "|".join(_EN)

_STALE = r"(?:前次|先前|過去|歷史|上次|上一次|之前|previous|prior|past|last|earlier|former)"
_STRONG = r"(?:本次|最終|目前|當前|final|overall|current)"
_WRAP_AFTER = r"[\s*_`」』)\]]*"      # markdown / quote wrappers between label and colon
_WRAP_BEFORE = r"[\s*_`「『(\[]*"      # wrappers between colon and the word

_LABEL_RE = re.compile(
    rf"(?P<q>{_STALE}|{_STRONG})?\s*(?:投資|建議)?(?:評級|評等|rating)"
    rf"{_WRAP_AFTER}[:\-]{_WRAP_BEFORE}"
    rf"(?:(?P<en>{_EN_WORDS})\b|(?P<zh>{_ZH_WORDS})(?![一-鿿]))",
    re.IGNORECASE,
)


def _tier(m: re.Match) -> str:
    if m.group("en"):
        return m.group("en").capitalize()
    return _ZH_TO_TIER[m.group("zh")]


def extract_pm_rating(text: str) -> Optional[str]:
    """Return one of Buy/Overweight/Hold/Underweight/Sell, or None if unreadable/ambiguous."""
    if not text or not text.strip():
        return None
    norm = unicodedata.normalize("NFKC", text)

    hits: list[tuple[int, str, bool]] = []  # (line index, tier, strong)
    first_content = None
    for i, line in enumerate(norm.splitlines()):
        if first_content is None and line.strip():
            first_content = i
        for m in _LABEL_RE.finditer(line):
            q = (m.group("q") or "").lower()
            if q and re.fullmatch(_STALE, q, re.IGNORECASE):
                continue  # refers to a previous decision
            hits.append((i, _tier(m), bool(q)))

    if not hits:
        return None
    tiers = {t for _, t, _ in hits}
    if len(tiers) == 1:
        return next(iter(tiers))
    # Several different labelled ratings: trust the structured template line first.
    for i, t, _ in hits:
        if i == first_content:
            return t
    strong = {t for _, t, s in hits if s}
    if len(strong) == 1:
        return next(iter(strong))
    return None
