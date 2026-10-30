"""Taiwan data tools for the Pro full graph (vendor `taiwan_free`).

The upstream graph calls `route_to_vendor(method, ...)`. For a Taiwan code this module registers FREE, official /
FinMind-anonymous implementations of the same tool methods, so the full LangGraph runs on Taiwan data instead of
yfinance / Alpha Vantage. Nothing falls back to a US source: a tool with no Taiwan source returns an explicit
`DATA_UNAVAILABLE` / `NO_DATA_AVAILABLE` sentinel and the analyst must say so (never invented).

Two research purposes, never mixed:
  * latest     ("latest available data"; trade date within LATEST_WINDOW_DAYS of today): "what is known now". Rows whose
               publication time is unknown are shown, labelled `published_at unknown`; rows dated AFTER the selected trade
               date are never shown, so a recent past date cannot leak later data.
  * historical (older trade date): only rows PROVABLY public before the as-of time. The free sources give no
               verified publication time, so fundamentals / news / disclosures come back empty with the reason
               stated; raw prices (exchange close time) remain usable.
Sources used: FinMind TaiwanStockPrice / FinancialStatements / BalanceSheet / CashFlowsStatement / MonthRevenue / News
(anonymous Free tier, 300 req/h), TWSE / TPEx daily material-information open data. Adapter + quota are shared
process-wide (`SOURCES`); `source_health()` reports what was actually observed at runtime.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from . import tw_evidence
from .tw_free_sources import FetchResult, FreeTaiwanSources
from .ticker import taiwan_code

VENDOR = "taiwan_free"
LATEST_WINDOW_DAYS = 3
TZ_TW = timezone(timedelta(hours=8))
SOURCES = FreeTaiwanSources()          # one instance: shared cache + minimum request interval
_lock = threading.Lock()
_run: dict[str, Any] = {"trade_date": None, "token": None, "requested_mode": None, "dexter": None, "excluded_after_cutoff": 0}
_health: dict[str, dict[str, Any]] = {}
_ohlcv_cache: dict[str, tuple[float, list[dict]]] = {}
INDICATORS = {
    "close_50_sma": "50 SMA: medium-term trend; lags price.",
    "close_200_sma": "200 SMA: long-term trend benchmark; slow.",
    "close_10_ema": "10 EMA: responsive short-term average; noisy in chop.",
    "macd": "MACD: EMA-difference momentum.", "macds": "MACD signal line.", "macdh": "MACD histogram.",
    "rsi": "RSI: 70/30 overbought/oversold; can stay extreme in strong trends.",
    "boll": "Bollinger middle (20 SMA).", "boll_ub": "Bollinger upper band.", "boll_lb": "Bollinger lower band.",
    "atr": "ATR: average true range (volatility).", "vwma": "VWMA: volume-weighted moving average.", "mfi": "MFI: money-flow index.",
}


# ---- run context / health -------------------------------------------------------------------------------------------


def begin_run(trade_date: str, token: Optional[str] = None, requested_mode: Optional[str] = None) -> None:
    _run["trade_date"] = trade_date
    _run["token"] = token or None
    _run["requested_mode"] = requested_mode if requested_mode in ("latest", "historical") else None
    _run["dexter"] = None
    _run["excluded_after_cutoff"] = 0


def end_run() -> None:
    _run["trade_date"] = None
    _run["token"] = None
    _run["requested_mode"] = None
    _run["dexter"] = None
    _run["excluded_after_cutoff"] = 0


def set_dexter(block: Optional[dict[str, Any]]) -> None:
    """Dexter's stage outcome for this run (None = the stage did not run at all)."""
    _run["dexter"] = block


def mode() -> str:
    """`historical` when asked for explicitly or when the trade date is outside the latest window; else `latest`."""
    if _run.get("requested_mode") == "historical":
        return "historical"
    td = _run.get("trade_date")
    if not td:
        return "latest"
    try:
        return "latest" if (datetime.now(TZ_TW).date() - date.fromisoformat(td)).days <= LATEST_WINDOW_DAYS else "historical"
    except ValueError:
        return "historical"


def _as_of() -> str:
    """ISO time with offset: now for a latest run, end of the trade day for a historical one."""
    if mode() == "latest":
        return datetime.now(timezone.utc).isoformat()
    return f"{_run['trade_date']}T23:59:59+08:00"


def _health_file():
    import os
    from pathlib import Path

    from . import storage

    return Path(os.environ.get("TAL_TW_HEALTH_FILE") or storage._default_db_path().parent / "tw-source-health.json")


def _load_health() -> None:
    """Last observed results survive restarts (the settings page shows 'last success' honestly)."""
    if _health:
        return
    try:
        import json

        _health.update(json.loads(_health_file().read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — no file yet / unreadable: start empty, never invent
        pass


def _save_health() -> None:
    try:
        import json
        import os

        p = _health_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_health, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def _note(source_id: str, status: str, detail: str = "") -> None:
    _load_health()
    h = _health.setdefault(source_id, {"calls": 0, "last_ok_at": None, "last_status": None, "last_error": None, "last_error_at": None})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    h["calls"] += 1
    h["last_status"] = status
    if status in ("ok", "empty"):
        h["last_ok_at"] = now
    else:
        h["last_error"], h["last_error_at"] = (detail or status)[:200], now
    _save_health()


def source_health() -> dict[str, dict[str, Any]]:
    _load_health()
    return {k: dict(v) for k, v in _health.items()}


def _record(source_id: str, r: FetchResult) -> None:
    _note(source_id, r.status, ";".join(r.gaps)[:200])


def _unavailable(what: str, why: str) -> str:
    return f"DATA_UNAVAILABLE: {what} — {why}. Do not estimate or fabricate values; report that this is unavailable."


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": "LabPro-HER30-S11/1.0"})


# ---- prices -------------------------------------------------------------------------------------------------------


def _ohlcv(code: str, start: str, end: str) -> list[dict]:
    """FinMind raw (unadjusted) daily rows, clipped to the run's trade date (no look-ahead)."""
    td = _run.get("trade_date")
    if td and end > td:
        end = td
    key = f"{code}|{start}|{end}"
    hit = _ohlcv_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    with _lock, _client() as c:
        try:
            rows = tw_evidence.fetch_finmind(c, "TaiwanStockPrice", code, start, end, _run.get("token"))
            _note("finmind_free", "ok")
        except tw_evidence.FinMindError as exc:
            _note("finmind_free", "unavailable", str(exc))
            raise
    rows = sorted(rows, key=lambda r: r["date"])
    _ohlcv_cache[key] = (time.time(), rows)
    return rows


def get_stock_data(symbol: str, start_date: str, end_date: str):
    from tradingagents.dataflows.errors import NoMarketDataError

    code = taiwan_code(symbol)
    if not code:
        raise NoMarketDataError(symbol, symbol, "not a Taiwan stock code; no US fallback is used for the Taiwan flow")
    try:
        rows = _ohlcv(code, start_date, end_date)
    except tw_evidence.FinMindError as exc:
        return _unavailable(f"prices for {code}", str(exc))
    if not rows:
        raise NoMarketDataError(symbol, code, f"no FinMind rows between {start_date} and {end_date}")
    out = ["Date,Open,High,Low,Close,Volume"] + [
        f"{r['date']},{r.get('open')},{r.get('max')},{r.get('min')},{r.get('close')},{r.get('Trading_Volume')}" for r in rows]
    head = (f"# Stock data for {code} (Taiwan, TWD) from {start_date} to {rows[-1]['date']}\n"
            f"# Source: FinMind TaiwanStockPrice (anonymous Free tier). RAW, UNADJUSTED prices: no adjusted close exists here, so do NOT "
            f"compute returns across ex-dividend/split dates.\n# Total records: {len(rows)}\n\n")
    return head + "\n".join(out) + "\n"


def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int):
    from stockstats import wrap
    import pandas as pd
    from tradingagents.dataflows.errors import NoMarketDataError

    if indicator not in INDICATORS:
        raise ValueError(f"Indicator {indicator} is not supported. Please choose from: {list(INDICATORS)}")
    code = taiwan_code(symbol)
    if not code:
        raise NoMarketDataError(symbol, symbol, "not a Taiwan stock code")
    cur = date.fromisoformat(curr_date)
    try:
        rows = _ohlcv(code, (cur - timedelta(days=420)).isoformat(), curr_date)
    except tw_evidence.FinMindError as exc:
        return _unavailable(f"price history for indicators of {code}", str(exc))
    if len(rows) < 5:
        raise NoMarketDataError(symbol, code, "too few FinMind rows to compute indicators")
    df = pd.DataFrame([{"Date": pd.Timestamp(r["date"]), "Open": r["open"], "High": r["max"], "Low": r["min"], "Close": r["close"], "Volume": r["Trading_Volume"]} for r in rows])
    sdf = wrap(df)
    sdf[indicator]
    vals = {pd.Timestamp(d).strftime("%Y-%m-%d"): v for d, v in zip(df["Date"], sdf[indicator])}
    before = cur - timedelta(days=int(look_back_days))
    lines, d = [], cur
    while d >= before:
        ds = d.isoformat()
        v = vals.get(ds)
        lines.append(f"{ds}: " + ("N/A: Not a trading day (weekend or holiday)" if ds not in vals else "N/A" if v is None or v != v else str(v)))
        d -= timedelta(days=1)
    return (f"## {indicator} values from {before.isoformat()} to {curr_date}:\n\n" + "\n".join(lines) +
            f"\n\n{INDICATORS[indicator]}\n(Computed from FinMind RAW unadjusted prices.)")


# ---- fixed limitations (given to the model AND printed in the report; never left to the model to restate) -----------------


def limitations(lang: str = "zh-TW") -> list[dict[str, str]]:
    """Fixed, deterministic list of what this run could NOT establish. Built from the run mode and observed state only."""
    zh = lang == "zh-TW"
    hist = mode() == "historical"
    td = _run.get("trade_date") or ""
    items: list[tuple[str, str, str]] = []
    if hist:
        items.append(("historical_gap",
                      f"歷史日期 {td}：免費來源無法證明財報／新聞／公告在當時已公開，因此這些項目不使用（缺漏，非「沒有」）；價量以交易所收盤為準。",
                      f"Historical date {td}: the free sources cannot prove filings / news / announcements were public by then, so they are NOT used (missing, not absent); prices use exchange closes."))
    else:
        items.append(("publication_time_unknown",
                      f"最新可得資料模式（截至 {td}）：財報、月營收與新聞標題的公布時間未知（來源未提供），已標示；晚於 {td} 的資料一律不採用。",
                      f"Latest-available-data mode (as of {td}): publication time of filings, monthly revenue and news headlines is UNKNOWN (not provided by the source) and is labelled; anything dated after {td} is excluded."))
    held = int(_run.get("excluded_after_cutoff") or 0)
    if held:
        items.append(("withheld_after_cutoff", f"另有 {held} 筆資料因日期晚於 {td} 而被排除。", f"{held} row(s) dated after {td} were withheld."))
    items += [
        ("news_headlines_only", "新聞只有標題、來源與連結，沒有內文或摘要。", "News is headlines + source + link only; no body or summary."),
        ("disclosures_current_day_only", "證交所／櫃買重大訊息只有當日資料集；沒有該列不代表沒有公告。", "TWSE/TPEx material-information feeds cover the current day only; absence of a row is not proof of no announcement."),
        ("raw_prices", "股價為未還原的原始價格；跨除權息日不可直接計算報酬。", "Prices are raw/unadjusted; do not compute returns across ex-dividend/split dates."),
        ("no_social_macro_insider", "沒有可靠的台股社群、總經新聞與內部人交易來源；社群分析師已停用，且不以美股資料替代。", "No reliable Taiwan social, macro-news or insider-transaction source; the social analyst is disabled and US data is not substituted."),
        ("reflection_no_tw_data", "事後績效追蹤（reflection）仍使用 Yahoo，對台股沒有資料，不能當作結果驗證。", "Outcome tracking (reflection) still uses Yahoo and has no Taiwan data; it is not outcome validation."),
    ]
    d = _run.get("dexter")
    if d is None:
        items.append(("dexter_not_run", "Dexter：本次完整圖未執行 Dexter 證據核對。", "Dexter: not run in this full-graph run."))
    elif d.get("ran") and d.get("status") == "succeeded":
        items.append(("dexter_ran", f"Dexter：已執行並完成（已驗證發現 {int(d.get('verified') or 0)} 項），僅讀取證據包。", f"Dexter: ran and succeeded ({int(d.get('verified') or 0)} verified findings), over the evidence pack only."))
    elif d.get("ran"):
        items.append(("dexter_failed", f"Dexter：已執行但未成功（{d.get('status')}：{d.get('reason')}），分析不含 Dexter 發現。", f"Dexter: ran but did not succeed ({d.get('status')}: {d.get('reason')}); analysis has no Dexter findings."))
    else:
        items.append(("dexter_skipped", f"Dexter：未執行（{d.get('reason')}），分析不含 Dexter 發現。", f"Dexter: NOT run ({d.get('reason')}); analysis has no Dexter findings."))
    return [{"code": c, "text": (a if zh else b)} for c, a, b in items]


def _model_header() -> str:
    """Compact English notice prepended to every fundamentals / news tool answer so the analyst sees the limits in the evidence."""
    lim = limitations("en")
    keep = [x["text"] for x in lim if x["code"] in ("historical_gap", "publication_time_unknown", "withheld_after_cutoff", "news_headlines_only", "disclosures_current_day_only")]
    return "[DATA LIMITS — state these in your report] " + " ".join(keep)


def _dexter_text() -> str:
    d = _run.get("dexter")
    if not d or not d.get("text"):
        return ""
    return "\n\n## Dexter evidence check (sandboxed, evidence pack only)\n" + d["text"]


def _with_limits(text: str, *, dexter: bool = False) -> str:
    return text + (_dexter_text() if dexter else "") + "\n\n" + _model_header()


# ---- fundamentals / news / disclosures ------------------------------------------------------------------------------


def _not_after_cutoff(rows: list[dict]) -> list[dict]:
    """Drop rows dated after the selected trade date (or with an unreadable date when the trade date is in the past):
    a past date must never silently mix later data. Counted so the report can say how many were withheld."""
    td = _run.get("trade_date")
    if not td:
        return rows
    today_tw = datetime.now(TZ_TW).date().isoformat()
    keep: list[dict] = []
    for x in rows:
        dp = str(x.get("data_period") or "")[:10]
        try:
            ok = date.fromisoformat(dp) <= date.fromisoformat(td)
        except ValueError:
            ok = td >= today_tw                      # unreadable date: only acceptable for a same-day run
        if ok:
            keep.append(x)
        else:
            _run["excluded_after_cutoff"] = int(_run.get("excluded_after_cutoff") or 0) + 1
    return keep


def _usable(r: FetchResult) -> list[dict]:
    """latest: everything the source returned up to the trade date (publication time labelled unknown); historical: provably-public rows only."""
    return _not_after_cutoff(r.records + (r.unknown_records if mode() == "latest" else []))


def _historical_gap(what: str) -> str:
    return (f"NO_DATA_AVAILABLE: {what} cannot be used for a historical as-of date: the free sources give no verified publication "
            f"time, so nothing here is provably public before {_run.get('trade_date')}. Do not estimate or fabricate values.")


def _financial(dataset: str, label: str, symbol: str, curr_date: str, freq: str = "quarterly") -> str:
    code = taiwan_code(symbol)
    if not code or len(code) != 4:
        return _unavailable(label, f"'{symbol}' is not a 4-digit Taiwan stock code (the free adapter covers 4-digit stocks only)")
    start = (date.fromisoformat(curr_date) - timedelta(days=800)).isoformat()
    try:
        r = SOURCES.fetch_finmind_dataset(dataset, code, start, as_of=_as_of(), end_date=curr_date)
    except Exception as exc:  # noqa: BLE001 — a tool must answer, not crash the graph
        _note("finmind_free", "unavailable", f"{type(exc).__name__}")
        return _unavailable(label, f"{type(exc).__name__}")
    _record("finmind_free", r)
    if r.status in ("access_denied", "rate_limited", "unavailable"):
        extra = f" retry after {r.retry_after:.0f}s" if r.retry_after else ""
        return _unavailable(label, f"FinMind {r.status} (HTTP {r.http_status}){extra}; the free tier is never upgraded automatically")
    rows = _usable(r)
    if not rows:
        return _historical_gap(label) if mode() == "historical" else _unavailable(label, "FinMind returned no rows")
    periods = sorted({x["data_period"] for x in rows}, reverse=True)[:4]
    basis = {"income": "single-period values as reported by FinMind (EPS in TWD/share)",
             "balance": "period-end balances",
             "cash_flow": "YEAR-TO-DATE CUMULATIVE within the fiscal year; single-quarter values are NOT derived — do not treat a row as one quarter"}[dataset]
    lines = [f"## {label} for {code} — source: FinMind {dict(income='TaiwanStockFinancialStatements', balance='TaiwanStockBalanceSheet', cash_flow='TaiwanStockCashFlowsStatement')[dataset]}",
             f"# Basis: {basis}. Currency TWD. Consolidation basis: unknown. `period` is the PERIOD END, not a filing date.",
             f"# Publication time: unknown for every row (FinMind gives no filing timestamp); shown because this is a latest-data run." if mode() == "latest" else
             "# Only rows with a verified publication time before the as-of are shown.",
             f"# Fetched {r.fetched_at}; {len(rows)} rows, latest {len(periods)} periods shown."]
    for p in periods:
        for x in sorted((y for y in rows if y["data_period"] == p), key=lambda y: str(y.get("metric"))):
            lines.append(f"{p} | {x.get('metric')} | {x.get('value')} | {x.get('origin_name') or ''}")
    return "\n".join(lines)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    return _with_limits(_financial("income", "Income statement", ticker, curr_date or _run["trade_date"] or date.today().isoformat(), freq))


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    return _with_limits(_financial("balance", "Balance sheet", ticker, curr_date or _run["trade_date"] or date.today().isoformat(), freq))


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    return _with_limits(_financial("cash_flow", "Cash-flow statement", ticker, curr_date or _run["trade_date"] or date.today().isoformat(), freq))


def get_fundamentals(ticker: str, curr_date: str = None):
    """Company overview = monthly revenue (FinMind) + the latest income-statement rows."""
    cd = curr_date or _run["trade_date"] or date.today().isoformat()
    code = taiwan_code(ticker)
    if not code:
        return _unavailable("fundamentals", f"'{ticker}' is not a Taiwan stock code")
    parts: list[str] = []
    if mode() == "latest":
        try:
            with _lock, _client() as c:
                rev = tw_evidence.fetch_finmind(c, "TaiwanStockMonthRevenue", code, (date.fromisoformat(cd) - timedelta(days=500)).isoformat(), cd, _run.get("token"))
            _note("finmind_free", "ok")
            rev = sorted(rev, key=lambda r: (r.get("revenue_year"), r.get("revenue_month")))[-13:]
            parts.append("## Monthly revenue (TWD) — source: FinMind TaiwanStockMonthRevenue; publication time unknown (legal deadline is NOT assumed)")
            parts += [f"{r.get('revenue_year')}-{int(r.get('revenue_month', 0)):02d} | revenue={r.get('revenue')}" for r in rev]
        except Exception as exc:  # noqa: BLE001
            _note("finmind_free", "unavailable", type(exc).__name__)
            parts.append(_unavailable("monthly revenue", type(exc).__name__))
    else:
        parts.append(_historical_gap("monthly revenue"))
    parts.append(_financial("income", "Income statement", ticker, cd))
    return _with_limits("\n\n".join(parts), dexter=True)


def _disclosures(code: str) -> list[dict]:
    from . import tw_symbols

    markets = ["listed", "otc"]
    try:
        hit = tw_symbols.lookup(tw_symbols.load_index(), code)
        if hit:
            markets = ["listed"] if hit["market"] == "TWSE" else ["otc"]
    except Exception:  # noqa: BLE001 — index unavailable: try both feeds
        pass
    out: list[dict] = []
    for m in markets:
        r = SOURCES.fetch_disclosures(m, code, as_of=_as_of())
        _record("twse_material_disclosures" if m == "listed" else "tpex_material_disclosures", r)
        out += _usable(r)
    return out


def get_news(ticker: str, start_date: str, end_date: str):
    code = taiwan_code(ticker)
    if not code or len(code) != 4:
        return _unavailable("news", f"'{ticker}' is not a 4-digit Taiwan stock code")
    td = _run.get("trade_date") or end_date
    end = min(end_date, td)
    days: list[str] = []
    d = date.fromisoformat(end)
    while d >= date.fromisoformat(start_date) and len(days) < 7:        # news API is single-day; bound the quota use
        days.append(d.isoformat())
        d -= timedelta(days=1)
    heads: list[dict] = []
    notes: list[str] = []
    for day in days:
        try:
            r = SOURCES.fetch_finmind_dataset("news", code, day, as_of=_as_of())
        except Exception as exc:  # noqa: BLE001
            _note("finmind_free", "unavailable", type(exc).__name__)
            notes.append(f"{day}: {type(exc).__name__}")
            break
        _record("finmind_free", r)
        if r.status in ("access_denied", "rate_limited", "unavailable"):
            notes.append(f"{day}: FinMind {r.status}" + (f", retry after {r.retry_after:.0f}s" if r.retry_after else ""))
            break
        heads += _usable(r)
        if len(heads) >= 20:
            break
    disc = []
    try:
        disc = _disclosures(code)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"disclosures: {type(exc).__name__}")
    if not heads and not disc:
        base = _historical_gap("news and disclosures") if mode() == "historical" else _unavailable("news", "no headlines or same-day material disclosures were returned" + (f" ({'; '.join(notes)})" if notes else ""))
        return _with_limits(base, dexter=True)
    lines = [f"## Taiwan news for {code} — headlines only (FinMind TaiwanStockNews gives title/source/URL; NO summary or full text is available)",
             "# Publication time is unverified for every item" + ("; shown because this is a latest-data run." if mode() == "latest" else ".")]
    for h in heads[:20]:
        lines.append(f"- {h.get('data_period')} | {h.get('publisher') or '?'} | {h.get('headline')} | {h.get('article_url') or 'no URL'}")
    if disc:
        lines += ["", "## Today's TWSE/TPEx material-information feed (current-day set only; absence of a row is NOT proof of no announcement)"]
        for x in disc[:10]:
            lines.append(f"- event {x.get('data_period')} | speech {x.get('reported_speech_date')} {x.get('reported_speech_time')} (exchange-reported, not independently verified) | {x.get('subject')}")
    if notes:
        lines.append("# Gaps: " + "; ".join(notes))
    return _with_limits("\n".join(lines), dexter=True)


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 5):
    return _unavailable("global/macro news", "no reliable free Taiwan macro-news source is connected; US news is deliberately not substituted")


def get_insider_transactions(ticker: str):
    return _unavailable("insider transactions", "no free Taiwan insider-transaction source is connected")


# ---- registration / graph config --------------------------------------------------------------------------------------

_METHODS = {
    "get_stock_data": get_stock_data, "get_indicators": get_indicators, "get_fundamentals": get_fundamentals,
    "get_balance_sheet": get_balance_sheet, "get_cashflow": get_cashflow, "get_income_statement": get_income_statement,
    "get_news": get_news, "get_global_news": get_global_news, "get_insider_transactions": get_insider_transactions,
}


def load_ohlcv_tw(symbol: str, curr_date: str):
    """DataFrame in the shape `stockstats_utils.load_ohlcv` returns (Date, Open, High, Low, Close, Volume), from FinMind raw prices."""
    import pandas as pd

    code = taiwan_code(symbol)
    rows = _ohlcv(code, (date.fromisoformat(curr_date) - timedelta(days=420)).isoformat(), curr_date)
    return pd.DataFrame([{"Date": pd.Timestamp(r["date"]), "Open": r["open"], "High": r["max"], "Low": r["min"], "Close": r["close"], "Volume": r["Trading_Volume"]} for r in rows])


def taiwan_identity(ticker: str) -> dict[str, str]:
    """Company identity for the graph's instrument context, from the official TWSE/TPEx index (never Yahoo)."""
    from . import tw_symbols

    code = taiwan_code(ticker)
    try:
        hit = tw_symbols.lookup(tw_symbols.load_index(), code)
    except Exception:  # noqa: BLE001
        hit = None
    if not hit:
        return {}
    return {"company_name": hit["name"], "exchange": "TWSE（上市）" if hit["market"] == "TWSE" else "TPEx（上櫃）"}


_patched = False


def _patch_graph_helpers() -> None:
    """Two places in the upstream graph bypass the vendor router and call Yahoo directly. For a Taiwan code during a
    Taiwan run they are redirected to Taiwan data; every other ticker keeps the original behaviour."""
    global _patched
    if _patched:
        return
    from tradingagents.dataflows import market_data_validator as mdv
    from tradingagents.graph import trading_graph as tgm

    orig_ohlcv, orig_ident = mdv.load_ohlcv, tgm.resolve_instrument_identity

    def ohlcv(symbol, curr_date):
        if _run.get("trade_date") and taiwan_code(str(symbol)):
            return load_ohlcv_tw(symbol, curr_date)
        return orig_ohlcv(symbol, curr_date)

    def ident(ticker):
        if _run.get("trade_date") and taiwan_code(str(ticker)):
            return taiwan_identity(ticker)
        return orig_ident(ticker)

    mdv.load_ohlcv = ohlcv
    tgm.resolve_instrument_identity = ident
    _patched = True


def install() -> None:
    """Register the vendor with the upstream router (idempotent)."""
    from tradingagents.dataflows import interface

    _patch_graph_helpers()

    for method, fn in _METHODS.items():
        interface.VENDOR_METHODS.setdefault(method, {})[VENDOR] = fn
    if VENDOR not in interface.VENDOR_LIST:
        interface.VENDOR_LIST.append(VENDOR)


def library_config() -> dict[str, Any]:
    """Merged over DEFAULT_CONFIG for a Taiwan full-graph run: every core category uses the Taiwan vendor only."""
    return {"data_vendors": {"core_stock_apis": VENDOR, "technical_indicators": VENDOR, "fundamental_data": VENDOR, "news_data": VENDOR}}


def graph_analysts(requested: Optional[list[str]]) -> tuple[list[str], list[str]]:
    """Selected analysts for a Taiwan run and the ones dropped. The social analyst reads US sentiment sources
    (StockTwits/Reddit); there is no reliable Taiwan social-data source, so it is disabled and said so."""
    base = requested or ["market", "social", "news", "fundamentals"]
    kept = [a for a in base if a != "social"]
    return kept, [a for a in base if a == "social"]


def quote_summary_event(code: str, trade_date: str) -> Optional[dict[str, Any]]:
    """`data.summary` for the UI strip, from FinMind raw prices (never yfinance)."""
    try:
        rows = _ohlcv(code, (date.fromisoformat(trade_date) - timedelta(days=40)).isoformat(), trade_date)
    except Exception:  # noqa: BLE001
        return None
    rows = rows[-25:]
    if len(rows) < 2 or not rows[0].get("close"):
        return None
    closes = [r["close"] for r in rows if r.get("close") is not None]
    vols = [r.get("Trading_Volume") or 0 for r in rows]
    return {
        "type": "data.summary", "ticker": code, "trade_date": trade_date, "as_of": rows[-1]["date"], "last_close": float(rows[-1]["close"]),
        "period_open": float(rows[0].get("open") or rows[0]["close"]), "period_high": max(float(r["max"]) for r in rows if r.get("max") is not None),
        "period_low": min(float(r["min"]) for r in rows if r.get("min") is not None),
        "period_change_pct": (float(rows[-1]["close"]) - float(rows[0]["close"])) / float(rows[0]["close"]) * 100,
        "avg_volume": sum(vols) / len(vols), "sessions": len(rows), "source": "FinMind (raw, unadjusted)", "asset_class": "equity",
    }


# ---- settings view (catalog + wiring + OBSERVED health; never a static claim of "connected") --------------------------------

# Which flows actually call each source. Kept next to the adapters so the page cannot drift from the code.
WIRING = {
    "finmind_free": ["Pro 完整圖（價量、技術指標、財報、月營收、新聞標題）", "簡化流程（證據包 + Dexter）"],
    "twse_material_disclosures": ["Pro 完整圖（新聞工具中的當日重大訊息）"],
    "tpex_material_disclosures": ["Pro 完整圖（新聞工具中的當日重大訊息）"],
    "twse_tpex_open_data": ["關注清單代號／名稱索引", "簡化流程（官方收盤價交叉核對）"],
}
_EXTRA = {
    "id": "twse_tpex_open_data",
    "name": "證交所／櫃買中心 公司資料與每日行情（官方開放資料）",
    "official_docs": ["https://openapi.twse.com.tw/", "https://www.tpex.org.tw/openapi/"],
    "data_types": ["symbol_name_index", "daily_quotes_crosscheck"],
    "free_scope": "上市／上櫃公司基本資料與每日行情；用於關注清單的代號／名稱索引與收盤價交叉核對。",
    "token_requirement": "匿名，無需 token。",
    "limits": "官方端點未公告固定額度；索引快取 24 小時，更新失敗時沿用舊快取並標示。每日行情只含當日有成交的商品。",
    "update_cadence": "交易所每日更新；Lab Pro 索引最多 24 小時延遲。",
    "license_notes": "呈現須標明證交所／櫃買中心來源。",
    "verified_at": "2026-10-30",
    "verification_scope": "四個官方端點的匿名 GET 與真實樣本測試。",
    "implementation_status": "adapter_available_unwired",
}


def sources_view(token_configured: bool) -> list[dict[str, Any]]:
    from .tw_source_catalog import get_source_catalog

    health = source_health()
    out = []
    for c in [*get_source_catalog(), dict(_EXTRA)]:
        sid = c["id"]
        impl = "unsupported" if c["implementation_status"] == "unsupported" else "wired"
        h = health.get(sid)
        if impl == "unsupported":
            state = "not_implemented"
        elif not h:
            state = "wired_untested"
        elif h["last_status"] in ("ok", "empty"):
            state = "connected"
        else:
            state = "failed"
        out.append({
            **c, "implementation_status": impl, "state": state, "used_by": WIRING.get(sid, []),
            "last_ok_at": (h or {}).get("last_ok_at"), "last_error": (h or {}).get("last_error"), "last_error_at": (h or {}).get("last_error_at"),
            "calls_observed": (h or {}).get("calls", 0),
            "token_configured": token_configured if sid == "finmind_free" else None,
        })
    return out


def test_source(source_id: str, token: Optional[str] = None) -> dict[str, Any]:
    """One minimal real request for a source; records the observed result. Never retried into a paid tier."""
    t0 = time.time()
    td = datetime.now(TZ_TW).date().isoformat()
    try:
        if source_id == "finmind_free":
            with _lock, _client() as c:
                rows = tw_evidence.fetch_finmind(c, "TaiwanStockPrice", "2330", (date.fromisoformat(td) - timedelta(days=7)).isoformat(), td, token)
            _note("finmind_free", "ok" if rows else "empty")
            return {"ok": True, "rows": len(rows), "ms": int((time.time() - t0) * 1000), "token_used": bool(token)}
        if source_id in ("twse_material_disclosures", "tpex_material_disclosures"):
            r = SOURCES.fetch_disclosures("listed" if source_id.startswith("twse") else "otc", "2330" if source_id.startswith("twse") else "6488", as_of=datetime.now(timezone.utc).isoformat())
            _record(source_id, r)
            return {"ok": r.status in ("ok", "empty"), "status": r.status, "http_status": r.http_status, "retry_after": r.retry_after, "ms": int((time.time() - t0) * 1000),
                    "note": "當日無該檔公告屬正常（只代表當日集合），不是端點失效" if r.status == "empty" else None}
        if source_id == "twse_tpex_open_data":
            from . import tw_symbols

            idx = tw_symbols.load_index(refresh=True)
            _note(source_id, "ok")
            return {"ok": True, "rows": len(idx["entries"]), "as_of": idx.get("as_of"), "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        _note(source_id, "unavailable", f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "ms": int((time.time() - t0) * 1000)}
    return {"ok": False, "error": "source is not implemented"}
