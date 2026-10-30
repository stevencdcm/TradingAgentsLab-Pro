"""Taiwan evidence pack: real recorded FinMind / TWSE / TPEx samples (2026-10-26..30)
replayed through an httpx MockTransport, plus an opt-in LIVE smoke (TAL_LIVE_TW=1)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from engine.tw_evidence import _crosscheck, build_tw_evidence, roc_to_iso, snapshot_hash

FX = json.loads((Path(__file__).parent / "fixtures" / "tw" / "real_samples.json").read_text(encoding="utf-8"))


def _client(fail: set[str] | None = None, twse=True, tpex=True) -> httpx.Client:
    fail = fail or set()
    table = {"TaiwanStockPrice": FX["price_2330"], "TaiwanStockMonthRevenue": FX["revenue_2330"],
             "TaiwanStockFinancialStatements": FX["fin_2330"], "TaiwanStockInstitutionalInvestorsBuySell": FX["chips_2330"]}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "finmindtrade" in url:
            ds = req.url.params["dataset"]
            if ds in fail:
                return httpx.Response(429, json={"msg": "limit"})
            end = req.url.params["end_date"]
            rows = [r for r in table[ds] if r["date"] <= end]
            return httpx.Response(200, json={"msg": "success", "status": 200, "data": rows})
        if "twse.com.tw" in url:
            return httpx.Response(200, json=FX["twse_row_2330"] if twse else [])
        if "tpex.org.tw" in url:
            return httpx.Response(200, json=FX["tpex_row_6488"] if tpex else [])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_roc_date_conversion():
    assert roc_to_iso("1151029") == "2026-10-29"
    assert roc_to_iso("990101") == "2010-01-01"
    assert roc_to_iso("") is None and roc_to_iso("20261029") is None


def test_pack_structure_and_never_inferred_dates():
    pack = build_tw_evidence("2330", "2026-10-29T16:30:00+08:00", client=_client())
    kinds = {it["kind"] for it in pack["items"]}
    assert {"price", "revenue", "fin_is_bs_cf", "chips", "price_crosscheck"} <= kinds
    assert pack["schema"] == "her30.evidence/1" and pack["security_id"] == "TW:2330" and len(pack["snapshot_id"]) == 64
    prices = [i for i in pack["items"] if i["kind"] == "price"]
    assert all(p["basis"] == "raw" and p["available_at_basis"] == "exchange_close_time" for p in prices)
    assert max(p["period"] for p in prices) == "2026-10-29"        # nothing after as_of leaks in
    for kind in ("revenue", "fin_is_bs_cf", "chips"):
        rows = [i for i in pack["items"] if i["kind"] == kind]
        assert rows and all(r["available_at"] is None and r["available_at_basis"] == "unknown" for r in rows)
    text = " ".join(pack["unknowns"])
    assert "還原股價" in text and "法定公布時點" in text


def test_change_is_a_price_change_not_a_percentage():
    pack = build_tw_evidence("2330", "2026-10-29", client=_client())
    p = [i for i in pack["items"] if i["kind"] == "price" and i["period"] == "2026-10-29"][0]
    assert p["values"]["change"] == -10.0 and p["values"]["close"] == 2415.0


def test_official_crosscheck_matches_finmind_on_the_same_day():
    pack = build_tw_evidence("2330", "2026-10-29", client=_client())
    cc = [i for i in pack["items"] if i["kind"] == "price_crosscheck"][0]
    assert cc["source"].startswith("TWSE:") and cc["period"] == "2026-10-29"
    assert cc["all_match"] is True and cc["compared_with"] == "FinMind:TaiwanStockPrice"
    assert pack["coverage"]["official_crosscheck"]["all_match"] is True


def test_crosscheck_is_skipped_when_dates_differ():
    pack = build_tw_evidence("2330", "2026-10-30", client=_client())   # FinMind has 10-30, official snapshot is 10-29
    cc = [i for i in pack["items"] if i["kind"] == "price_crosscheck"][0]
    assert cc["compared_with"] is None and "differs" in cc["note"] and "all_match" not in cc


def test_tpex_lenient_parsing_of_typos_and_padding():
    cc = _crosscheck("6488", None, None, FX["tpex_row_6488"], "t")
    assert cc["source"].startswith("TPEx:") and cc["period"] == "2026-10-29"
    assert cc["values"]["change"] == -35.0 and cc["values"]["close"] == 814.0        # "-35.00 " padded
    assert cc["values"]["transactions"] == 28241.0


def test_unknown_code_has_no_crosscheck_and_says_so():
    pack = build_tw_evidence("2330", "2026-10-29", client=_client(twse=False, tpex=False))
    assert not any(i["kind"] == "price_crosscheck" for i in pack["items"])
    assert any("找不到此代碼" in u for u in pack["unknowns"])


def test_rate_limit_is_recorded_not_invented_and_not_fatal():
    pack = build_tw_evidence("2330", "2026-10-29", client=_client(fail={"TaiwanStockMonthRevenue"}))
    assert not any(i["kind"] == "revenue" for i in pack["items"])
    assert any("TaiwanStockMonthRevenue" in u and "429" in u for u in pack["unknowns"])
    assert any(i["kind"] == "price" for i in pack["items"])


def test_snapshot_hash_ignores_fetch_time_but_tracks_data():
    a = build_tw_evidence("2330", "2026-10-29", client=_client())
    b = build_tw_evidence("2330", "2026-10-29", client=_client())
    assert a["snapshot_id"] == b["snapshot_id"]
    items = copy.deepcopy(a["items"])
    items[0]["values"]["close"] = 1.0
    assert snapshot_hash(items, "TW:2330", "2026-10-29") != a["snapshot_id"]


@pytest.mark.parametrize("bad", ["TSM", "AAPL", "", "23", "2330.TW"])
def test_only_taiwan_codes_are_accepted(bad):
    with pytest.raises(ValueError):
        build_tw_evidence(bad, "2026-10-29", client=_client())


@pytest.mark.skipif(os.environ.get("TAL_LIVE_TW") != "1", reason="live network smoke (set TAL_LIVE_TW=1)")
def test_live_smoke_anonymous_finmind_and_official():
    pack = build_tw_evidence("2330", "2026-10-30", price_days=7)
    assert any(i["kind"] == "price" for i in pack["items"])
    cc = [i for i in pack["items"] if i["kind"] == "price_crosscheck"]
    print("LIVE coverage:", json.dumps(pack["coverage"], ensure_ascii=False), "snapshot", pack["snapshot_id"][:12])
    assert cc


def test_evidence_endpoint_builds_saves_and_reports_gaps(tmp_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from engine import server as srv, tw_evidence

    monkeypatch.setenv("TAL_EVIDENCE_DIR", str(tmp_path / "ev"))
    real = tw_evidence.build_tw_evidence
    monkeypatch.setattr(tw_evidence, "build_tw_evidence", lambda code, as_of, **kw: real(code, as_of, client=_client(), **{k: v for k, v in kw.items() if k != "client"}))
    with TestClient(srv.build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        r = c.post("/tw/evidence", json={"code": "2330.TW", "as_of": "2026-10-29"}).json()
        assert r["ok"] is True and r["coverage"]["price"]["latest"] == "2026-10-29" and r["unknowns"]
        saved = json.loads(Path(r["path"]).read_text(encoding="utf-8"))
        assert saved["snapshot_id"] == r["snapshot_id"] and saved["security_id"] == "TW:2330"
        assert c.post("/tw/evidence", json={"code": "TSM"}).json() == {"ok": False, "error": "not a Taiwan stock code"}
