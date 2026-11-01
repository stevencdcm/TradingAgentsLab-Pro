"""Pricing snapshots are public evidence: no site scripts/config, no key-shaped values, and the importers refuse contaminated input."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAPS = ROOT / "docs" / "pricing-snapshots"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


san = _load("snapshot_sanitize")
# built at runtime so this file itself contains no key-shaped literal
G_KEY = "AIza" + "Sy" + "A" * 33
TG_TOKEN = "123456789" + ":" + "B" * 35
PAGE = (
    "<html><head><meta name=x content=y><script>var cfg=['" + G_KEY + "','ai.google.dev'];</script><style>.a{}</style></head>"
    "<body><nav>menu</nav><article class='c' data-x='1'><h2 id=z>gemini-2.5-pro</h2><table><tr><td>Input price</td><td>$1.25</td></tr></table>"
    "</article><script>track('" + TG_TOKEN + "')</script></body></html>"
)


def test_sanitizer_keeps_pricing_article_and_drops_scripts_config_and_attributes():
    out = san.build_snapshot(PAGE, "https://ai.google.dev/gemini-api/docs/pricing", "2026-10-31")
    assert "gemini-2.5-pro" in out and "Input price" in out and "$1.25" in out
    for gone in ("<script", "<style", "<meta", "var cfg", "menu", "class=", "data-x", "track("):
        assert gone not in out
    assert "source: https://ai.google.dev/gemini-api/docs/pricing" in out and "retrieved: 2026-10-31" in out and "raw page sha256:" in out
    assert san.find_secret_shapes(out) == {}


@pytest.mark.parametrize("val,name", [(G_KEY, "google_api_key"), (TG_TOKEN, "telegram_bot_token"), ("sk-" + "c" * 30, "openai_style_key")])
def test_key_shaped_values_are_rejected_and_never_echoed(val, name):
    with pytest.raises(san.SecretShapeFound) as e:
        san.assert_no_secret_shapes("x " + val + " y", "input")
    assert name in str(e.value) and val not in str(e.value)


def test_sanitized_text_that_still_holds_a_key_is_refused_without_writing(tmp_path):
    raw, out = tmp_path / "raw.html", tmp_path / "out.html"
    raw.write_text("<article><p>price</p><p>" + G_KEY + "</p></article>", encoding="utf-8")        # a key in visible text survives markup stripping
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "snapshot_sanitize.py"), str(raw), str(out), "--source", "https://x", "--retrieved", "2026-10-31"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and not out.exists() and G_KEY not in r.stdout + r.stderr


@pytest.mark.parametrize("gen,snap", [("gen_gemini_pricing", "gemini-pricing-2026-10-31.html")])
def test_importer_refuses_a_contaminated_snapshot(gen, snap, tmp_path):
    bad = tmp_path / snap
    bad.write_text((SNAPS / snap).read_text(encoding="utf-8") + "\n<!-- " + G_KEY + " -->\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / f"{gen}.py"), str(bad), "2026-10-31"], capture_output=True, text=True)
    assert r.returncode != 0 and "key/token-shaped" in r.stderr and G_KEY not in r.stdout + r.stderr


def test_tracked_snapshots_and_sources_contain_no_key_shaped_values():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True).stdout.split("\0")
    bad = []
    for f in files:
        p = ROOT / f
        if not f or not p.is_file() or p.stat().st_size > 5_000_000 or "node_modules" in f:
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = san.find_secret_shapes(t)
        if not f.startswith("docs/pricing-snapshots/"):                  # elsewhere only provider-issued shapes; sk-... strings are fake test keys
            hits = {k: v for k, v in hits.items() if k not in ("openai_style_key", "anthropic_key")}
        if hits:
            bad.append((f, hits))
    assert bad == [], f"key-shaped values in tracked files (values not shown): {bad}"
    gem = (SNAPS / "gemini-pricing-2026-10-31.html").read_text(encoding="utf-8")
    assert len(re.findall(r"AIza[0-9A-Za-z_-]{35}", gem)) == 0 and "<script" not in gem


def test_gemini_rates_from_the_sanitized_snapshot_are_the_documented_ones():
    gen = _load("gen_gemini_pricing")
    data, long_from = gen.parse((SNAPS / "gemini-pricing-2026-10-31.html").read_text(encoding="utf-8"))
    p = data["gemini-2.5-pro"]["standard"]
    assert (p["short"]["input"], p["short"]["output"], p["long"]["input"], p["long"]["output"]) == (1.25, 10.0, 2.5, 15.0)
    assert long_from == {"gemini-2.5-pro": 200001} and "gemini-2.0-flash" not in data
