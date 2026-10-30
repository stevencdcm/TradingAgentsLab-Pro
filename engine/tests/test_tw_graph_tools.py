"""Taiwan data tools for the Pro full graph: vendor registration, raw prices with no look-ahead, indicators,
fundamentals with the right basis/caveats, latest vs historical purpose, explicit unavailability (no US fallback)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from engine import tw_graph_tools as tg
from engine.tw_free_sources import FreeTaiwanSources, HttpResponse

FX = json.loads((Path(__file__).parent / "fixtures" / "tw" / "real_samples.json").read_text(encoding="utf-8"))
TODAY = date.today()


def _prices(n=240, end=TODAY):
    rows, p = [], 2400.0
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        if d.weekday() >= 5:
            continue
        p += (i % 7 - 3) * 3
        rows.append({"date": d.isoformat(), "stock_id": "2330", "Trading_Volume": 1000000 + i, "Trading_money": 1, "open": p - 2, "max": p + 5, "min": p - 6, "close": p, "spread": 1, "Trading_turnover": 1})
    return rows


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TAL_TW_HEALTH_FILE", str(tmp_path / "health.json"))
    monkeypatch.setenv("TAL_TW_SYMBOLS_FILE", str(tmp_path / "symbols.json"))
    calls = {"finmind": [], "adapter": []}
    prices = _prices()

    def handler(req: httpx.Request) -> httpx.Response:
        ds = req.url.params["dataset"]
        calls["finmind"].append(ds)
        if ds == "TaiwanStockPrice":
            end = req.url.params["end_date"]
            return httpx.Response(200, json={"status": 200, "msg": "success", "data": [r for r in prices if req.url.params["start_date"] <= r["date"] <= end]})
        if ds == "TaiwanStockMonthRevenue":
            return httpx.Response(200, json={"status": 200, "msg": "success", "data": FX["revenue_2330"]})
        return httpx.Response(404)

    monkeypatch.setattr(tg, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    tg._ohlcv_cache.clear()
    tg._health.clear()

    state = {"status": 200}

    def transport(url: str) -> HttpResponse:
        calls["adapter"].append(url)
        if state["status"] != 200:
            return HttpResponse(state["status"], b"{}", {})
        if "Financial" in url or "BalanceSheet" in url or "CashFlows" in url:
            return HttpResponse(200, json.dumps({"status": 200, "msg": "ok", "data": FX["fin_2330"]}).encode(), {})
        if "TaiwanStockNews" in url:
            return HttpResponse(200, json.dumps({"status": 200, "msg": "ok", "data": [{"date": TODAY.isoformat() + " 08:00:00", "stock_id": "2330", "link": "https://example.test/a", "source": "工商時報", "title": "台積電法說會"}]}).encode(), {})
        return HttpResponse(200, b"[]", {})

    monkeypatch.setattr(tg, "SOURCES", FreeTaiwanSources(transport=transport, sleep=lambda s: None, min_interval_seconds=0))
    monkeypatch.setattr(tg, "_disclosures", lambda code: [])
    tg.install()
    tg.begin_run(TODAY.isoformat())
    yield calls, state
    tg.end_run()


def test_vendor_is_registered_for_every_tool_and_config_uses_only_it():
    from tradingagents.dataflows import interface

    tg.install()
    for m in ("get_stock_data", "get_indicators", "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement", "get_news", "get_global_news", "get_insider_transactions"):
        assert tg.VENDOR in interface.VENDOR_METHODS[m]
    assert set(tg.library_config()["data_vendors"].values()) == {tg.VENDOR}
    assert interface.route_to_vendor.__name__  # router unchanged


def test_social_analyst_is_dropped_and_reported():
    assert tg.graph_analysts(None) == (["market", "news", "fundamentals"], ["social"])
    assert tg.graph_analysts(["market", "social"]) == (["market"], ["social"])
    assert tg.graph_analysts(["market"]) == (["market"], [])


def test_prices_are_raw_labelled_and_never_look_ahead(env):
    cutoff = (TODAY - timedelta(days=10)).isoformat()
    tg.begin_run(cutoff)
    out = tg.get_stock_data("2330", (TODAY - timedelta(days=30)).isoformat(), TODAY.isoformat())
    assert "RAW, UNADJUSTED" in out and "Date,Open,High,Low,Close,Volume" in out.splitlines()
    dates = [l.split(",")[0] for l in out.splitlines() if l[:2] == "20"]
    assert dates and max(dates) <= cutoff                                   # end clipped to the trade date


def test_non_taiwan_symbols_raise_instead_of_falling_back_to_us_data(env):
    from tradingagents.dataflows.errors import NoMarketDataError

    with pytest.raises(NoMarketDataError):
        tg.get_stock_data("NVDA", "2026-10-01", "2026-10-29")


def test_indicators_are_computed_from_the_raw_prices(env):
    out = tg.get_indicators("2330", "rsi", TODAY.isoformat(), 5)
    assert out.startswith("## rsi values") and "FinMind RAW" in out
    body = [l for l in out.splitlines() if l[:2] == "20"]
    assert len(body) == 6 and any(float(l.split(": ")[1]) > 0 for l in body if l.split(": ")[1].replace(".", "", 1).isdigit())
    with pytest.raises(ValueError):
        tg.get_indicators("2330", "not_an_indicator", TODAY.isoformat(), 5)


def test_latest_run_shows_statements_with_basis_and_unknown_publication_time(env):
    inc = tg.get_income_statement("2330", "quarterly", TODAY.isoformat())
    assert "FinMind" in inc and "2026-06-30" in inc and "EPS" in inc
    assert "Publication time: unknown" in inc and "PERIOD END, not a filing date" in inc
    cf = tg.get_cashflow("2330", "quarterly", TODAY.isoformat())
    assert "YEAR-TO-DATE CUMULATIVE" in cf and "NOT derived" in cf
    fund = tg.get_fundamentals("2330", TODAY.isoformat())
    assert "Monthly revenue" in fund and "legal deadline is NOT assumed" in fund


def test_historical_run_refuses_unprovable_fundamentals_and_news_but_keeps_prices(env):
    tg.begin_run((TODAY - timedelta(days=60)).isoformat())
    assert tg.mode() == "historical"
    for out in (tg.get_income_statement("2330", "quarterly", (TODAY - timedelta(days=60)).isoformat()),
                tg.get_balance_sheet("2330", "quarterly", (TODAY - timedelta(days=60)).isoformat()),
                tg.get_news("2330", (TODAY - timedelta(days=67)).isoformat(), (TODAY - timedelta(days=60)).isoformat())):
        assert out.startswith("NO_DATA_AVAILABLE") and "provably public" in out
    assert "Monthly revenue" not in tg.get_fundamentals("2330", (TODAY - timedelta(days=60)).isoformat()).split("NO_DATA_AVAILABLE")[0]
    assert tg.get_stock_data("2330", (TODAY - timedelta(days=90)).isoformat(), (TODAY - timedelta(days=60)).isoformat()).startswith("# Stock data")


def test_news_is_headlines_only_with_urls_and_flagged(env):
    out = tg.get_news("2330", (TODAY - timedelta(days=2)).isoformat(), TODAY.isoformat())
    assert "headlines only" in out and "NO summary" in out and "台積電法說會" in out and "https://example.test/a" in out
    assert "unverified" in out


def test_permission_and_rate_limit_are_explicit_not_retried_into_a_paid_tier(env):
    calls, state = env
    state["status"] = 403
    out = tg.get_income_statement("2330", "quarterly", TODAY.isoformat())
    assert out.startswith("DATA_UNAVAILABLE") and "access_denied" in out and "never upgraded" in out
    n = len(calls["adapter"])
    tg.get_income_statement("2330", "quarterly", TODAY.isoformat())
    assert len(calls["adapter"]) - n <= 1                                   # a 403 is not hammered
    h = tg.source_health()["finmind_free"]
    assert h["last_status"] == "access_denied" and h["last_error"] and h["last_ok_at"] is None


def test_health_records_only_what_was_observed(env):
    assert tg.source_health() == {}
    tg.get_income_statement("2330", "quarterly", TODAY.isoformat())
    h = tg.source_health()["finmind_free"]
    assert h["last_status"] == "ok" and h["last_ok_at"] and h["calls"] == 1


def test_tools_without_a_taiwan_source_say_so(env):
    assert tg.get_global_news(TODAY.isoformat()).startswith("DATA_UNAVAILABLE")
    assert tg.get_insider_transactions("2330").startswith("DATA_UNAVAILABLE")
    assert "US news is deliberately not substituted" in tg.get_global_news(TODAY.isoformat())


# ---- server routing: Taiwan code + Pro => the FULL graph on Taiwan data (never a silent downgrade) -----------------------


@pytest.fixture
def routed(tmp_db, monkeypatch):
    from engine import full_debate as fd, server as srv, tw_research

    seen = {}

    async def fake_full(ticker, trade_date, **kw):
        seen.update(ticker=ticker, trade_date=trade_date, kw=kw, run_mode=tg.mode(), run_started=tg._run["trade_date"])
        yield {"type": "session.start", "ticker": ticker, "trade_date": trade_date}
        yield {"type": "session.complete", "ticker": ticker, "trade_date": trade_date, "live": True, "provider": "openai_compatible",
               "auth_kind": "local", "model": "m", "decision": {"stance": "neutral", "conviction": 0.5, "reasoning": "x"},
               "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.0}

    async def fake_simple(**kw):
        seen["simplified"] = True
        yield {"type": "session.start", "ticker": kw["code"], "trade_date": kw["trade_date"]}
        yield {"type": "session.complete", "decision": {"stance": "neutral"}, "live": True}

    async def boom(**_):
        raise AssertionError("US data providers (yfinance) must never be used for a Taiwan code")

    async def no_dexter(**kw):                       # isolated: never builds a real evidence pack or starts the Dexter runner
        return {"event": {"type": "dexter.result", "ran": False, "status": "unsupported", "reason": "disabled", "findings": [], "unknowns": []},
                "block": {"ran": False, "status": "unsupported", "reason": "disabled", "text": "", "verified": 0},
                "d_in": 0, "d_out": 0, "d_cost": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0, "measured": False, "est_cost_usd": 0.0, "ran": False},
                "ran": False, "measured": False, "billable": False}

    monkeypatch.setattr(tw_research, "full_graph_dexter", no_dexter)
    monkeypatch.setattr(fd, "full_debate", fake_full)
    monkeypatch.setattr(tw_research, "tw_research", fake_simple)
    monkeypatch.setattr(srv, "_fetch_summary_safe", boom)
    monkeypatch.setattr(srv, "_fetch_news_safe", boom)
    monkeypatch.setattr(tg, "quote_summary_event", lambda code, td: {"type": "data.summary", "ticker": code, "trade_date": td, "source": "FinMind"})
    return srv, seen


def _run_ws(srv, frame):
    from fastapi.testclient import TestClient

    evs = []
    with TestClient(srv.build_app(token="t")) as c:
        with c.websocket_connect("/stream?token=t") as ws:
            ws.send_json(frame)
            while True:
                ev = ws.receive_json()
                evs.append(ev)
                if ev["type"] in ("session.complete", "run.error"):
                    break
    return evs


BASE = {"ticker": "2330", "trade_date": date.today().isoformat(), "report_language": "zh-TW",
        "provider_config": {"provider": "local", "auth": {"type": "local", "base_url": "http://127.0.0.1:9/v1"}, "model": "m"}}


def test_taiwan_with_pro_runs_the_full_graph_on_taiwan_data(routed):
    srv, seen = routed
    evs = _run_ws(srv, {**BASE, "engine": "pro", "pro_config": {"selected_analysts": ["market", "social", "news", "fundamentals"]}})
    assert "simplified" not in seen
    kw = seen["kw"]
    assert seen["ticker"] == "2330" and kw["selected_analysts"] == ["market", "news", "fundamentals"]        # social dropped
    assert set(kw["config"]["data_vendors"].values()) == {tg.VENDOR} and kw["config"]["tool_vendors"] == {}
    assert seen["run_started"] == BASE["trade_date"] and seen["run_mode"] == "latest"
    kinds = [e["type"] for e in evs]
    assert kinds.index("data.summary") < kinds.index("run.notice") < kinds.index("session.complete")
    notice = next(e for e in evs if e["type"] == "run.notice")
    assert notice["dropped_analysts"] == ["social"] and "社群分析師已停用" in notice["message"] and "不使用美股來源" in notice["message"]
    done = evs[-1]
    assert done["market"] == "TW" and done["tw_flow"] == "full" and done["data_vendor"] == tg.VENDOR and done["data_mode"] == "latest"
    assert tg._run["trade_date"] is None                                                                  # run context is cleared


def test_simplified_flow_only_when_explicitly_requested(routed):
    srv, seen = routed
    _run_ws(srv, {**BASE, "engine": "pro", "tw_flow": "simplified"})
    assert seen.get("simplified") is True and "kw" not in seen


def test_historical_trade_date_is_flagged_in_the_notice(routed):
    srv, seen = routed
    old = (date.today() - timedelta(days=40)).isoformat()
    evs = _run_ws(srv, {**BASE, "trade_date": old, "engine": "pro"})
    notice = next(e for e in evs if e["type"] == "run.notice")
    assert notice["data_mode"] == "historical" and "無法證明" in notice["message"]
    assert seen["run_mode"] == "historical"


# ---- settings view: catalog + wiring + OBSERVED health ---------------------------------------------------------------------


@pytest.fixture
def sources_api(tmp_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from engine import server as srv

    monkeypatch.setenv("TAL_TW_HEALTH_FILE", str(tmp_path / "health.json"))
    tg._health.clear()
    with TestClient(srv.build_app(token="t")) as c:
        c.headers["Authorization"] = "Bearer t"
        yield c


def test_sources_list_never_claims_connected_before_a_real_observation(env, sources_api):
    r = sources_api.get("/tw/sources").json()
    by = {s["id"]: s for s in r["sources"]}
    assert set(by) == {"finmind_free", "twse_material_disclosures", "tpex_material_disclosures", "mops_historical_disclosures", "twse_tpex_open_data"}
    assert by["finmind_free"]["state"] == "wired_untested" and by["finmind_free"]["last_ok_at"] is None
    assert by["mops_historical_disclosures"]["state"] == "not_implemented" and by["mops_historical_disclosures"]["implementation_status"] == "unsupported"
    assert by["finmind_free"]["used_by"] and by["finmind_free"]["limits"] and by["finmind_free"]["official_docs"] and by["finmind_free"]["verified_at"]
    assert by["finmind_free"]["token_configured"] is False and by["twse_tpex_open_data"]["token_configured"] is None
    assert "300" in by["finmind_free"]["limits"]                                  # the real limit is shown, not "unlimited"


def test_a_real_test_call_updates_state_and_last_success_and_survives_restart(env, sources_api, tmp_path):
    res = sources_api.post("/tw/sources/test", json={"id": "finmind_free"}).json()
    assert res["ok"] is True and res["rows"] > 0
    st = {s["id"]: s for s in sources_api.get("/tw/sources").json()["sources"]}["finmind_free"]
    assert st["state"] == "connected" and st["last_ok_at"] and st["calls_observed"] >= 1
    tg._health.clear()                                                            # simulate a restart: reload from disk
    again = {s["id"]: s for s in sources_api.get("/tw/sources").json()["sources"]}["finmind_free"]
    assert again["state"] == "connected" and again["last_ok_at"] == st["last_ok_at"]


def test_failures_show_as_failed_with_the_reason(env, sources_api, monkeypatch):
    def down():
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(tg, "_client", down)
    res = sources_api.post("/tw/sources/test", json={"id": "finmind_free"}).json()
    assert res["ok"] is False and "ConnectError" in res["error"]
    st = {s["id"]: s for s in sources_api.get("/tw/sources").json()["sources"]}["finmind_free"]
    assert st["state"] == "failed" and st["last_error"] and st["last_ok_at"] is None


def test_unsupported_source_cannot_be_tested(env, sources_api):
    assert sources_api.post("/tw/sources/test", json={"id": "mops_historical_disclosures"}).json()["ok"] is False


def test_graph_helpers_that_bypass_the_router_are_redirected_for_taiwan_only(env, monkeypatch):
    from tradingagents.dataflows import market_data_validator as mdv
    from tradingagents.graph import trading_graph as tgm

    monkeypatch.setattr(tg, "taiwan_identity", lambda t: {"company_name": "台積電", "exchange": "TWSE（上市）"})
    tg.install()
    df = mdv.load_ohlcv("2330", TODAY.isoformat())                      # would be Yahoo without the redirect
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"] and len(df) > 100
    assert tgm.resolve_instrument_identity("2330")["company_name"] == "台積電"
    snap = mdv.build_verified_market_snapshot("2330", TODAY.isoformat())
    assert "rsi" in snap.lower() or "RSI" in snap
    tg.end_run()
    assert tg._run["trade_date"] is None                                   # no active Taiwan run: the original path applies again


# ---- S11 follow-ups: latest-available vs as-of separation, fixed limitations, Dexter in the full graph, Sonnet 5.5 price ------


def test_recent_past_date_in_latest_mode_never_shows_rows_dated_after_it(env):
    cutoff = (TODAY - timedelta(days=2)).isoformat()
    tg.begin_run(cutoff)
    assert tg.mode() == "latest"
    out = tg.get_news("2330", (TODAY - timedelta(days=4)).isoformat(), TODAY.isoformat())
    assert "台積電法說會" not in out                                          # fixture headline is dated TODAY, after the cutoff
    assert tg._run["excluded_after_cutoff"] >= 1
    codes = {x["code"] for x in tg.limitations("zh-TW")}
    assert "withheld_after_cutoff" in codes and "publication_time_unknown" in codes


def test_explicit_historical_request_wins_over_the_recent_window(env):
    tg.begin_run(TODAY.isoformat(), None, "historical")
    assert tg.mode() == "historical"
    assert "NO_DATA_AVAILABLE" in tg.get_income_statement("2330", "quarterly", TODAY.isoformat())
    assert {x["code"] for x in tg.limitations("en")} >= {"historical_gap"}
    tg.begin_run(TODAY.isoformat(), None, "bogus")                            # unknown request values are ignored
    assert tg.mode() == "latest"


def test_limits_reach_the_model_evidence_and_are_fixed_text(env):
    for out in (tg.get_income_statement("2330", "quarterly", TODAY.isoformat()),
                tg.get_fundamentals("2330", TODAY.isoformat()),
                tg.get_news("2330", (TODAY - timedelta(days=2)).isoformat(), TODAY.isoformat())):
        assert "[DATA LIMITS — state these in your report]" in out and "publication time" in out.lower()
    lim = tg.limitations("zh-TW")
    assert {x["code"] for x in lim} >= {"publication_time_unknown", "news_headlines_only", "disclosures_current_day_only", "raw_prices",
                                        "no_social_macro_insider", "reflection_no_tw_data", "dexter_not_run"}
    assert all(x["text"] for x in lim) and tg.limitations("zh-TW") == lim     # deterministic


def test_dexter_state_is_always_stated_and_its_findings_reach_the_tools(env):
    tg.set_dexter({"ran": True, "status": "succeeded", "reason": None, "text": "FINDING-XYZ", "verified": 2})
    assert "FINDING-XYZ" in tg.get_fundamentals("2330", TODAY.isoformat())
    assert "FINDING-XYZ" in tg.get_news("2330", (TODAY - timedelta(days=2)).isoformat(), TODAY.isoformat())
    assert any(x["code"] == "dexter_ran" and "2" in x["text"] for x in tg.limitations("zh-TW"))
    tg.set_dexter({"ran": False, "status": "unsupported", "reason": "provider_not_supported", "text": "", "verified": 0})
    assert "FINDING-XYZ" not in tg.get_fundamentals("2330", TODAY.isoformat())
    assert any(x["code"] == "dexter_skipped" and "provider_not_supported" in x["text"] for x in tg.limitations("en"))
    tg.set_dexter({"ran": True, "status": "failed", "reason": "timeout", "text": "", "verified": 0})
    assert any(x["code"] == "dexter_failed" for x in tg.limitations("en"))


def test_sonnet_5_5_price_is_the_verified_official_rate():
    from engine.llm_providers import estimate_cost_or_none, is_priced

    assert is_priced("claude-sonnet-5-5") and estimate_cost_or_none("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0
    assert not is_priced("claude-unpriced-test-9")


def test_fold_dexter_adds_usage_and_keeps_unknown_cost_unknown():
    from engine.llm_providers import ProviderConfig
    from engine.tw_research import fold_dexter_into_complete

    cfg = ProviderConfig(provider="anthropic", auth={"type": "api_key", "api_key": "k"}, model="claude-haiku-4-5")
    res = {"block": {"ran": True, "status": "succeeded", "reason": None, "verified": 1}, "d_in": 1000, "d_out": 200, "d_cost": 0.002,
           "usage": {"input_tokens": 1000, "output_tokens": 200, "measured": True, "est_cost_usd": 0.002, "ran": True}, "ran": True, "measured": True}
    ev = fold_dexter_into_complete({"type": "session.complete", "input_tokens": 10, "output_tokens": 5, "estimated_cost_usd": 0.01, "cost_known": True}, res, cfg)
    assert ev["input_tokens"] == 1010 and ev["output_tokens"] == 205 and ev["estimated_cost_usd"] == 0.012 and ev["cost_known"] is True
    assert ev["dexter"]["ran"] is True and ev["usage_breakdown"]["dexter"]["est_cost_usd"] == 0.002
    unk = fold_dexter_into_complete({"type": "session.complete", "estimated_cost_usd": None, "cost_known": False}, res, cfg)
    assert unk["estimated_cost_usd"] is None and unk["cost_known"] is False        # never a fake 0
    off = fold_dexter_into_complete({"type": "session.complete"}, None, cfg)
    assert off["dexter"]["ran"] is False and off["dexter"]["status"] == "not_requested"


def test_full_graph_dexter_prestep_reports_skip_and_survives_pack_failure(monkeypatch, tmp_path):
    import asyncio

    from engine import tw_research as twr
    from engine.llm_providers import ProviderConfig

    cfg = ProviderConfig(provider="openai", auth={"type": "oauth", "access_token": "x", "account_id": "a"}, model="m")
    monkeypatch.setenv("TAL_EVIDENCE_DIR", str(tmp_path / "ev"))
    pack = {"schema": "her30.evidence/1", "security_id": "TW:2330", "snapshot_id": "a" * 64, "as_of": "x", "items": [], "unknowns": [], "coverage": {}}
    monkeypatch.setattr(twr.tw_evidence, "build_tw_evidence", lambda code, as_of: pack)
    res = asyncio.run(twr.full_graph_dexter(code="2330", trade_date="2026-10-30", config=cfg, lang="zh-TW"))
    assert res["event"]["type"] == "dexter.result" and res["event"]["ran"] is False and res["event"]["status"] == "unsupported"
    assert "ChatGPT-OAuth" in res["event"]["reason"] and res["block"]["ran"] is False and res["d_cost"] == 0.0

    def boom(code, as_of):
        raise RuntimeError("finmind down")

    monkeypatch.setattr(twr.tw_evidence, "build_tw_evidence", boom)
    res = asyncio.run(twr.full_graph_dexter(code="2330", trade_date="2026-10-30", config=cfg, lang="en"))
    assert res["event"]["ran"] is False and "stage_error" in res["event"]["reason"]    # the graph still runs; absence is stated


def test_server_streams_dexter_result_and_complete_carries_limitations_and_dexter(routed, monkeypatch):
    from engine import tw_research as twr

    srv, seen = routed
    calls = {}

    async def fake_stage(**kw):
        calls.update(kw)
        return {"event": {"type": "dexter.result", "ran": True, "status": "succeeded", "reason": None, "findings": [], "unknowns": []},
                "block": {"ran": True, "status": "succeeded", "reason": None, "text": "DX", "verified": 1},
                "d_in": 100, "d_out": 20, "d_cost": 0.0, "usage": {"input_tokens": 100, "output_tokens": 20, "measured": True, "est_cost_usd": 0.0, "ran": True},
                "ran": True, "measured": True, "billable": False}

    monkeypatch.setattr(twr, "full_graph_dexter", fake_stage)
    evs = _run_ws(srv, {**BASE, "engine": "pro"})
    kinds = [e["type"] for e in evs]
    assert kinds.index("run.notice") < kinds.index("dexter.result") < kinds.index("session.complete")
    assert calls["enabled"] is True and calls["trade_date"] == BASE["trade_date"]
    done = evs[-1]
    assert done["dexter"] == {"ran": True, "status": "succeeded", "reason": None, "verified": 1}
    assert done["input_tokens"] == 101 and done["output_tokens"] == 21
    assert any(x["code"] == "dexter_ran" for x in done["limitations"]) and any(x["code"] == "publication_time_unknown" for x in done["limitations"])
    assert next(e for e in evs if e["type"] == "run.notice")["data_mode_label"] == "最新可得資料（截至選定日期）"


def test_dexter_can_be_turned_off_per_run_and_is_then_reported_not_hidden(routed, monkeypatch):
    from engine import tw_research as twr

    srv, seen = routed
    seen_kw = {}
    base = twr.full_graph_dexter                    # the isolated default installed by the fixture

    async def spy(**kw):
        seen_kw.update(kw)
        return await base(**kw)

    monkeypatch.setattr(twr, "full_graph_dexter", spy)
    evs = _run_ws(srv, {**BASE, "engine": "pro", "tw_dexter": False})
    dx = next(e for e in evs if e["type"] == "dexter.result")
    assert seen_kw["enabled"] is False and dx["ran"] is False
    assert any(x["code"] == "dexter_skipped" for x in evs[-1]["limitations"])
