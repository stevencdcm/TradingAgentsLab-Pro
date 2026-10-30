"""Taiwan symbol/name index from OFFICIAL exchange open data (no scraping, no hand-kept catalogue).

Sources (all public exchange APIs):
  TWSE  listed companies   openapi.twse.com.tw  opendata/t187ap03_L        (code + short name)
  TPEx  OTC companies      www.tpex.org.tw      openapi/v1/mopsfin_t187ap03_O
  TWSE  daily quotes       exchangeReport/STOCK_DAY_ALL                    (adds ETFs / special shares)
  TPEx  daily quotes       openapi/v1/tpex_mainboard_daily_close_quotes    (adds OTC ETFs)

Company lists win for the name; daily quotes only add codes the lists do not have. Warrants, bonds and
other 5-6 digit derivatives are NOT indexed: only 4-digit stock codes, 4-digit + letter special shares and
`00…` ETF codes, which is what the evidence pipeline (FinMind) and Dexter flow support.

Identity is `{code, market}` with market TWSE (上市) or TPEx (上櫃). A code is never suffixed with
.TW/.TWO: the engine's Taiwan contract takes the bare code. The index is cached on disk with `as_of`
(the exchange's own data date) and `fetched_at`; when a refresh fails the last good cache is served
flagged `stale`, and with no cache the failure is reported as-is (never an empty "no results").
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from .tw_evidence import TPEX_ALL_URL, TWSE_ALL_URL, roc_to_iso

TWSE_COMPANIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
SCHEMA = "her30.tw-symbols/1"
CACHE_TTL_S = 24 * 3600
_CODE_OK = re.compile(r"^(\d{4}[A-Z]?|00\d{2,4}[A-Z]?)$")
_UA = {"User-Agent": "Mozilla/5.0 (TradingAgentsLab symbol index)"}
_lock = threading.Lock()


class SymbolIndexError(RuntimeError):
    pass


def _cache_path() -> Path:
    from . import storage

    return Path(os.environ.get("TAL_TW_SYMBOLS_FILE") or storage._default_db_path().parent / "tw-symbols.json")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip().casefold()
    return s.replace("臺", "台")                      # 臺/台 are interchangeable in company names


def _get(client: httpx.Client, url: str) -> list[dict]:
    r = client.get(url, headers=_UA, timeout=40)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise SymbolIndexError(f"empty or unexpected response from {url}")
    return data


def build_index(client: Optional[httpx.Client] = None) -> dict:
    """Fetch the four official datasets. Raises SymbolIndexError unless BOTH markets could be loaded."""
    own = client is None
    client = client or httpx.Client()
    errors: dict[str, str] = {}
    out: dict[str, dict] = {}
    as_of: dict[str, str | None] = {}

    def add(code: str, name: str, market: str, source: str) -> None:
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if not _CODE_OK.match(code) or not name or code in out:
            return
        out[code] = {"code": code, "name": name, "market": market, "source": source}

    def load(key: str, url: str, code_f: str, name_f: str, market: str, date_f: str) -> None:
        try:
            rows = _get(client, url)
        except Exception as exc:  # noqa: BLE001 — recorded, reported, never invented
            errors[key] = f"{type(exc).__name__}: {exc}"
            return
        dates = [roc_to_iso(r.get(date_f, "")) for r in rows[:50]]
        as_of[key] = max((d for d in dates if d), default=None)
        for r in rows:
            add(str(r.get(code_f, "")), str(r.get(name_f, "")), market, key)

    try:
        load("twse_companies", TWSE_COMPANIES_URL, "公司代號", "公司簡稱", "TWSE", "出表日期")
        load("tpex_companies", TPEX_COMPANIES_URL, "SecuritiesCompanyCode", "CompanyAbbreviation", "TPEx", "Date")
        load("twse_daily", TWSE_ALL_URL, "Code", "Name", "TWSE", "Date")
        load("tpex_daily", TPEX_ALL_URL, "SecuritiesCompanyCode", "CompanyName", "TPEx", "Date")
    finally:
        if own:
            client.close()
    have = {v["market"] for v in out.values()}
    if not {"TWSE", "TPEx"} <= have:
        raise SymbolIndexError("could not load both TWSE and TPEx lists: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    dates = [d for d in as_of.values() if d]
    return {
        "schema": SCHEMA,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": max(dates) if dates else None,
        "sources": {"twse_companies": TWSE_COMPANIES_URL, "tpex_companies": TPEX_COMPANIES_URL, "twse_daily": TWSE_ALL_URL, "tpex_daily": TPEX_ALL_URL},
        "partial_errors": errors,
        "entries": sorted(out.values(), key=lambda e: e["code"]),
    }


def _read_cache() -> Optional[dict]:
    p = _cache_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if d.get("schema") == SCHEMA and d.get("entries") else None
    except Exception:  # noqa: BLE001
        return None


def _write_cache(idx: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def load_index(*, refresh: bool = False, client: Optional[httpx.Client] = None) -> dict:
    """The index plus honesty flags: `stale` (served from an old cache because a refresh failed or is due)
    and `error`. Raises SymbolIndexError only when there is nothing to serve."""
    with _lock:
        cached = _read_cache()
        age = None
        if cached:
            try:
                age = time.time() - datetime.strptime(cached["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except Exception:  # noqa: BLE001
                age = None
        if cached and not refresh and age is not None and age < CACHE_TTL_S:
            return {**cached, "stale": False, "error": None}
        try:
            idx = build_index(client)
            _write_cache(idx)
            return {**idx, "stale": False, "error": None}
        except Exception as exc:  # noqa: BLE001
            if cached:
                return {**cached, "stale": True, "error": f"{type(exc).__name__}: {exc}"[:300]}
            raise SymbolIndexError(f"Taiwan symbol index unavailable and no cache exists: {exc}") from exc


def search(idx: dict, q: str, limit: int = 20) -> list[dict]:
    """Code or name. '2330' and '台積電' both find 2330. Ranking: exact code, code prefix, exact name,
    name prefix, name contains. Markets are never merged: each hit carries its own market."""
    n = _norm(q)
    if not n:
        return []
    scored: list[tuple[int, str, dict]] = []
    for e in idx["entries"]:
        code, name = e["code"].casefold(), _norm(e["name"])
        if code == n: rank = 0
        elif code.startswith(n): rank = 1
        elif name == n: rank = 2
        elif name.startswith(n): rank = 3
        elif n in name: rank = 4
        else: continue
        scored.append((rank, e["code"], e))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored[:limit]]


def lookup(idx: dict, code: str) -> Optional[dict]:
    c = (code or "").strip().upper()
    for e in idx["entries"]:
        if e["code"] == c:
            return e
    return None
