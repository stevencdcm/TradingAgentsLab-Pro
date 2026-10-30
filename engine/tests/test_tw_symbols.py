"""Taiwan symbol/name index: real recorded rows from the four official exchange datasets
(2026-10-29/30), replayed through MockTransport. Search, TWSE/TPEx identity, cache honesty, watchlist."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from engine import tw_symbols as sym

FX = json.loads((Path(__file__).parent / "fixtures" / "tw" / "symbol_samples.json").read_text(encoding="utf-8"))
URL_KEY = {sym.TWSE_COMPANIES_URL: "twse_companies", sym.TPEX_COMPANIES_URL: "tpex_companies", sym.TWSE_ALL_URL: "twse_daily", sym.TPEX_ALL_URL: "tpex_daily"}


def _client(fail: set[str] | None = None) -> httpx.Client:
    fail = fail or set()

    def h(req: httpx.Request) -> httpx.Response:
        key = URL_KEY.get(str(req.url))
        if key is None or key in fail:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=FX[key])

    return httpx.Client(transport=httpx.MockTransport(h))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TAL_TW_SYMBOLS_FILE", str(tmp_path / "tw-symbols.json"))
    return tmp_path / "tw-symbols.json"


def test_code_and_name_find_the_same_instrument_and_markets_stay_distinct(cache):
    idx = sym.load_index(client=_client())
    a = sym.search(idx, "2330")[0]
    b = sym.search(idx, "台積電")[0]
    assert a == b and a["code"] == "2330" and a["market"] == "TWSE" and a["name"] == "台積電"
    g = sym.search(idx, "環球晶")[0]
    assert (g["code"], g["market"]) == ("6488", "TPEx")          # OTC, not listed
    assert sym.search(idx, "6488")[0]["market"] == "TPEx"
    assert sym.search(idx, "台積")[0]["code"] == "2330"           # partial name
    assert sym.search(idx, "臺積電")[0]["code"] == "2330"         # 臺/台 variants
    assert sym.search(idx, "NVDA") == []                          # US symbols are not in the Taiwan index


def test_leading_zeros_etfs_special_shares_kept_and_derivatives_excluded(cache):
    idx = sym.load_index(client=_client())
    codes = {e["code"] for e in idx["entries"]}
    assert {"0050", "00400A", "006201", "2882A"} <= codes         # leading zeros / letter suffix preserved
    assert not any(c.startswith("02") for c in codes)             # 6-digit derivatives (not 00xxxx) are not indexed
    assert sym.lookup(idx, "0050")["market"] == "TWSE" and sym.lookup(idx, "006201")["market"] == "TPEx"


def test_index_records_source_and_exchange_data_date(cache):
    idx = sym.load_index(client=_client())
    assert idx["as_of"] and idx["as_of"].startswith("2026-10-") and idx["fetched_at"]
    assert set(idx["sources"]) == {"twse_companies", "tpex_companies", "twse_daily", "tpex_daily"}
    assert all(e["source"] in idx["sources"] for e in idx["entries"])


def test_failures_are_reported_truthfully_never_as_empty_results(cache):
    with pytest.raises(sym.SymbolIndexError):
        sym.load_index(client=_client(fail={"tpex_companies", "tpex_daily"}))      # a whole market missing -> refuse
    assert not cache.exists()
    sym.load_index(client=_client())                                               # good cache
    stale = sym.load_index(refresh=True, client=_client(fail=set(URL_KEY.values())))
    assert stale["stale"] is True and stale["error"] and sym.search(stale, "2330")                # old data, flagged
    fresh = sym.load_index()                                                       # within TTL: served from cache, no network
    assert fresh["stale"] is False


def test_partial_dataset_failure_still_works_but_is_recorded(cache):
    idx = sym.load_index(client=_client(fail={"twse_daily"}))
    assert "twse_daily" in idx["partial_errors"] and sym.lookup(idx, "2330")                 # company list still covers it
    assert sym.lookup(idx, "0050") is None                                                   # ETF only in the daily file: honestly absent


@pytest.fixture
def api(tmp_db, cache, monkeypatch):
    real = sym.build_index
    monkeypatch.setattr(sym, "build_index", lambda client=None: real(_client()))
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        yield c


def test_search_endpoint(api):
    r = api.get("/tw/symbols", params={"q": "台積電"}).json()
    assert r["ok"] and r["results"][0]["code"] == "2330" and r["as_of"] and r["stale"] is False and r["sources"]
    assert api.get("/tw/symbols", params={"q": "環球晶"}).json()["results"][0]["market"] == "TPEx"


def test_search_endpoint_reports_unavailable(tmp_db, cache, monkeypatch):
    def boom(client=None):
        raise sym.SymbolIndexError("down")

    monkeypatch.setattr(sym, "build_index", boom)
    from engine.server import build_app

    with TestClient(build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        r = c.get("/tw/symbols", params={"q": "2330"}).json()
    assert r["ok"] is False and "unavailable" in r["error"]


def test_watchlist_add_stores_canonical_identity_and_prevents_duplicates(api):
    r = api.post("/watchlist", json={"ticker": "2330.TW", "market": "TWSE"})            # suffix input -> bare code
    assert r.status_code == 200 and r.json()["ticker"] == "2330" and r.json()["market"] == "TWSE" and r.json()["name"] == "台積電"
    assert api.post("/watchlist", json={"ticker": "2330"}).status_code == 409           # duplicate
    assert api.post("/watchlist", json={"ticker": "6488", "market": "TPEx"}).json()["name"] == "環球晶"
    assert api.post("/watchlist", json={"ticker": "6488", "market": "TWSE"}).status_code in (400, 409)   # wrong market never accepted
    assert api.post("/watchlist", json={"ticker": "9999", "market": "TWSE"}).status_code == 400          # not an official code
    rows = {e["ticker"]: e for e in api.get("/watchlist").json()["watchlist"]}
    assert rows["2330"]["market"] == "TWSE" and rows["6488"]["market"] == "TPEx"


def test_us_entries_and_old_rows_are_unchanged(api):
    r = api.post("/watchlist", json={"ticker": "nvda"}).json()
    assert r["ticker"] == "NVDA" and r["market"] is None and r["name"] is None
    assert api.post("/watchlist", json={"ticker": "TSM"}).json()["market"] is None        # US ADR is not 2330
    assert api.delete("/watchlist/NVDA").status_code == 200


def test_old_database_without_the_new_columns_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    from engine import storage

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE watchlist (ticker TEXT PRIMARY KEY, added_at TEXT NOT NULL, note TEXT);"
                      "INSERT INTO watchlist VALUES ('AAPL','2026-01-01T00:00:00Z','old row');")
    con.commit(); con.close()
    monkeypatch.setenv("TAL_SESSIONS_DB", str(db))
    from engine import cost_guard

    storage._reset_for_tests()
    cost_guard._reset_for_tests()
    rows = storage.list_watchlist()
    assert rows and rows[0].ticker == "AAPL" and rows[0].market is None and rows[0].note == "old row"
