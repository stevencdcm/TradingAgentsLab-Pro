"""Rating extraction for the Portfolio Manager's final decision.

The upstream parser only understands the English 5-tier words. When the report
language is Traditional Chinese and the model answers in free text (a local
Qwen / oMLX or an OAuth model whose structured output failed), the PM often
writes "**評級**：增持". Without help that decision was silently shown as a
neutral stance and recorded as "Hold" in the memory log — a fabricated
conclusion. This module:

* keeps the machine vocabulary (English 5-tier enum) as the single wire form;
* accepts the labelled Chinese forms (NFKC-normalised so full-width "：" works);
* returns ``None`` when nothing can be parsed, so callers surface an explicit
  *review* state instead of inventing Hold/neutral.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from tradingagents.agents.utils.rating import extract_rating

# Chinese rating words -> upstream 5-tier names. Longest alternatives first so
# "強力買進" is not read as "買進" + noise. Only used after an explicit label.
_ZH_TO_TIER = {
    "強力買進": "Buy", "強烈買進": "Buy", "買進": "Buy", "買入": "Buy", "強力買入": "Buy",
    "增持": "Overweight", "加碼": "Overweight", "偏多": "Overweight",
    "持有": "Hold", "中立": "Hold", "觀望": "Hold",
    "減持": "Underweight", "減碼": "Underweight", "偏空": "Underweight",
    "強力賣出": "Sell", "強烈賣出": "Sell", "賣出": "Sell", "賣": "Sell",
}
_ZH_WORDS = "|".join(sorted(_ZH_TO_TIER, key=len, reverse=True))
_ZH_LABEL_RE = re.compile(
    r"(?:評級|評等|投資評級|最終評級|建議評級|Rating)"
    r"[\s*_`」』)\]]*[:\-]?[\s*_`「『(\[]*(" + _ZH_WORDS + r")"
)


def extract_pm_rating(text: str) -> Optional[str]:
    """Return one of Buy/Overweight/Hold/Underweight/Sell, or None if unparseable."""
    if not text:
        return None
    rating = extract_rating(text)  # English: NFKC + label pass + standalone-word pass
    if rating:
        return rating
    norm = unicodedata.normalize("NFKC", text)
    for line in norm.splitlines():
        m = _ZH_LABEL_RE.search(line)
        if m:
            return _ZH_TO_TIER[m.group(1)]
    return None
