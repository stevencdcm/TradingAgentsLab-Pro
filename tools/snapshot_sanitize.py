#!/usr/bin/env python3
"""Reduce a saved public web page to the minimal evidence a pricing importer needs, and refuse anything key-shaped.

    python tools/snapshot_sanitize.py RAW.html OUT.html --source URL --retrieved YYYY-MM-DD

A whole-page save carries the site's own scripts and embedded config (public web-client identifiers that secret scanners match as
live API keys). None of that is pricing evidence, so it is dropped: <script>/<style>/<noscript>/<template>/<svg>/<iframe>, comments,
<meta>/<link> and every attribute except a tag's own name are not needed by the importers, which read the <article> text and its tables.
The result keeps the <article> element (or <main>/<body> when there is none), prefixed with a provenance comment (source URL,
retrieval date, sha256 of the raw page, what was removed). The output is then scanned: if any key- or token-shaped value is still
present nothing is written. Matches are never echoed - only the pattern name and a count.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# Credential shapes that must never appear in a tracked snapshot. Name -> regex. Reported by name and count only.
SECRET_SHAPES: dict[str, re.Pattern[str]] = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "telegram_bot_token": re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

_DROP_BLOCKS = ("script", "style", "noscript", "template", "svg", "iframe", "head")


class SecretShapeFound(ValueError):
    """Raised with pattern names and counts only - never the matched text."""


def find_secret_shapes(text: str) -> dict[str, int]:
    return {n: len(rx.findall(text)) for n, rx in SECRET_SHAPES.items() if rx.search(text)}


def assert_no_secret_shapes(text: str, where: str = "input") -> None:
    hits = find_secret_shapes(text)
    if hits:
        raise SecretShapeFound(f"{where} contains key/token-shaped values (not shown): " + ", ".join(f"{k} x{v}" for k, v in sorted(hits.items())))


def sanitize_html(raw: str) -> str:
    """Markup-only reduction. Does not look at secrets; call assert_no_secret_shapes on the result."""
    t = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    for tag in _DROP_BLOCKS:
        t = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", "", t, flags=re.S | re.I)
        t = re.sub(rf"<{tag}\b[^>]*/?>", "", t, flags=re.I)             # an unclosed/void remnant
    t = re.sub(r"<(?:meta|link|base)\b[^>]*>", "", t, flags=re.I)
    for tag in ("article", "main", "body"):
        m = re.search(rf"<{tag}\b[^>]*>(.*)</{tag}\s*>", t, flags=re.S | re.I)
        if m:
            t = m.group(1)
            break
    # attributes carry ids/handlers/data blobs; the importers only need tag names and text
    t = re.sub(r"<([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(/?)>", r"<\1\2>", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() + "\n"


def build_snapshot(raw: str, source: str, retrieved: str) -> str:
    body = sanitize_html(raw)
    header = (
        f"<!-- Sanitized public pricing evidence.\n"
        f"     source: {source}\n"
        f"     retrieved: {retrieved}\n"
        f"     raw page sha256: {hashlib.sha256(raw.encode('utf-8')).hexdigest()}\n"
        f"     kept: the pricing <article> element, tags without attributes. removed: scripts, styles, head/meta/link, comments, svg/iframe,\n"
        f"     and all attributes (site-embedded web config is not pricing evidence and is matched by secret scanners). -->\n"
    )
    out = header + body
    assert_no_secret_shapes(out, "sanitized snapshot")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("raw"), ap.add_argument("out")
    ap.add_argument("--source", required=True), ap.add_argument("--retrieved", required=True)
    a = ap.parse_args()
    raw = Path(a.raw).read_bytes().decode("utf-8")
    try:
        text = build_snapshot(raw, a.source, a.retrieved)
    except SecretShapeFound as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    Path(a.out).write_text(text, encoding="utf-8")
    print(f"wrote {a.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
