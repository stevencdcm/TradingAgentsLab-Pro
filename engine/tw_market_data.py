"""Official TWSE / TPEx daily market data for one Taiwan stock: valuation (P/E, P/B, dividend yield), institutional flows and margin/short
balances.

Free, anonymous, public exchange endpoints only (no key, no scraping of HTML pages):

  TWSE valuation     openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL                (latest file; every row carries its own Date)
  TPEx valuation     www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis      (latest file)
  TWSE institutional www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD                   (dated; stat != OK on a closed/unpublished day)
  TPEx institutional www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading            (latest file)
  TWSE margin        www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD     (dated)
  TPEx margin        www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance        (latest file; the date can lag the other TPEx files)

Rules (the same ones the rest of the Taiwan pipeline keeps):
* every record carries the date the SOURCE states (`data_date`); a "latest file" is never relabelled as the requested day, and a
  record dated after the requested trade date is never returned;
* the exchange gives no verified release time, so `published_at` is null and `availability` says so - historical (as-of) runs must not
  use these rows (the caller gates on that); a latest run shows them with their data date and staleness;
* an unavailable / denied / rate-limited / empty source is a typed gap, never zero; an empty P/E field means "not reported" (typically a
  loss-making company), not 0;
* ROC dates ("1151102") are converted; thousands separators removed; units are stated per field. Nothing is cached on disk and no
  response body is stored - only the one normalized row for the requested code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from .tw_free_sources import FreeTaiwanSources, SourceRequestError

TWSE_VALUATION_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_VALUATION_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTITUTIONAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"

AVAILABILITY = "exchange_daily_file_release_time_unverified"
COMPARABILITY = ("Each exchange's own definition. 'foreign' excludes foreign dealers on both markets (foreign dealers are shown separately); 'dealers' follows "
                 "each exchange's own grouping and margin units/limits are each exchange's own. TWSE and TPEx values with the same name are NOT one series "
                 "and must not be added or compared directly.")
SCHEMA = "her30.tw_market/1"


def roc_to_iso(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    if len(s) == 7 and s.isdigit():
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])
    elif len(s) == 8 and s.isdigit():
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    else:
        return None
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def num(v: Any) -> Optional[float]:
    s = str(v if v is not None else "").replace(",", "").strip()
    if s in ("", "-", "--", "N/A", "X"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f == f else None


def _gap(kind: str, source: str, url: str, market: str, code: str, status: str, reason: str, request: dict, http: Optional[int] = None,
         retry_after: Optional[float] = None) -> dict[str, Any]:
    return {"schema": SCHEMA, "kind": kind, "source_id": source, "market": market, "stock_id": code, "status": status, "reason": reason,
            "source_url": url, "request": request, "http_status": http, "retry_after": retry_after, "record": None, "data_date": None,
            "published_at": None, "availability": AVAILABILITY, "fetched_at": None}


def _ok(kind: str, source: str, url: str, market: str, code: str, data_date: Optional[str], record: dict, request: dict, fetched_at: str,
        cache_hit: bool) -> dict[str, Any]:
    return {"schema": SCHEMA, "kind": kind, "source_id": source, "market": market, "stock_id": code, "status": "ok", "reason": None,
            "source_url": url, "request": request, "http_status": 200, "retry_after": None, "record": record, "data_date": data_date,
            "published_at": None, "availability": AVAILABILITY, "fetched_at": fetched_at, "cache_hit": cache_hit}


class OfficialMarketData:
    def __init__(self, sources: FreeTaiwanSources) -> None:
        self.s = sources

    # ---- transport -------------------------------------------------------------------------------------------------

    def _get(self, kind: str, source: str, url: str, market: str, code: str, request: dict, ttl: float = 900.0):
        try:
            body, fetched_at, hit, _stale = self.s._request(url, ttl)       # shared throttle / memory cache / size cap
        except SourceRequestError as e:
            status = "access_denied" if e.status in (402, 403) else "rate_limited" if e.status == 429 else "unavailable"
            return None, _gap(kind, source, url, market, code, status, f"source request failed ({status})", request, e.status, e.retry_after), None, None
        except Exception as e:  # noqa: BLE001 - a tool must answer, not crash the graph
            return None, _gap(kind, source, url, market, code, "unavailable", type(e).__name__, request), None, None
        return body, None, fetched_at, hit

    # ---- valuation -------------------------------------------------------------------------------------------------

    def valuation(self, code: str, market: str) -> dict[str, Any]:
        req = {"stock_id": code, "market": market}
        if market == "TWSE":
            url, src = TWSE_VALUATION_URL, "twse_valuation"
        else:
            url, src = TPEX_VALUATION_URL, "tpex_valuation"
        body, gap, fetched, hit = self._get("valuation", src, url, market, code, req)
        if gap:
            return gap
        if not isinstance(body, list):
            return _gap("valuation", src, url, market, code, "unavailable", "unexpected response shape", req)
        key = "Code" if market == "TWSE" else "SecuritiesCompanyCode"
        row = next((r for r in body if isinstance(r, dict) and str(r.get(key, "")).strip() == code), None)
        if row is None:
            return _gap("valuation", src, url, market, code, "no_data", "code not in the exchange's valuation file (no trade / not listed there)", req, 200)
        dd = roc_to_iso(row.get("Date"))
        pe, pb, dy = num(row.get("PEratio" if market == "TWSE" else "PriceEarningRatio")), num(row.get("PBratio" if market == "TWSE" else "PriceBookRatio")), \
            num(row.get("DividendYield" if market == "TWSE" else "YieldRatio"))
        rec = {"pe_ratio": pe, "pb_ratio": pb, "dividend_yield_pct": dy,
               "pe_note": None if pe is not None else "P/E not reported by the exchange (usually no positive trailing earnings) - not zero",
               "units": {"pe_ratio": "times", "pb_ratio": "times", "dividend_yield_pct": "percent"}}
        if market != "TWSE":                      # the TWSE all-stocks file has no price / dividend-year columns; only TPEx adds dividend per share
            rec.update(dividend_per_share_twd=num(row.get("DividendPerShare")))
        return _ok("valuation", src, url, market, code, dd, rec, req, fetched, hit)

    # ---- institutional ---------------------------------------------------------------------------------------------

    def institutional(self, code: str, market: str, trade_date: str) -> dict[str, Any]:
        if market == "TWSE":
            return self._t86(code, trade_date)
        req = {"stock_id": code, "market": market}
        body, gap, fetched, hit = self._get("institutional", "tpex_institutional", TPEX_INSTITUTIONAL_URL, market, code, req, ttl=900.0)
        if gap:
            return gap
        row = next((r for r in body if isinstance(r, dict) and str(r.get("SecuritiesCompanyCode", "")).strip() == code), None) if isinstance(body, list) else None
        if row is None:
            return _gap("institutional", "tpex_institutional", TPEX_INSTITUTIONAL_URL, market, code, "no_data", "code not in today's TPEx institutional file", req, 200)
        r = {k.strip(): v for k, v in row.items()}          # exact keys: two 'Dealers' sell columns differ only by a space

        def grp(prefix_buy: str, prefix_sell: str, prefix_diff: str) -> dict[str, Any]:
            b, s_, d = num(r.get(prefix_buy)), num(r.get(prefix_sell)), num(r.get(prefix_diff))
            return {"buy": b, "sell": s_, "net": d, "consistent": None if None in (b, s_, d) else abs((b - s_) - d) < 0.5}
        ex = "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)"
        rec = {"unit": "shares", "comparability": COMPARABILITY,
               "foreign": grp(f"{ex}-Total Buy", f"{ex}-Total Sell", f"{ex}-Difference"),              # same caliber as TWSE: foreign dealers excluded
               "foreign_dealers": grp("Foreign Dealers-Total Buy", "Foreign Dealers-TotalSell", "ForeignDealers-Difference"),
               "investment_trust": grp("SecuritiesInvestmentTrustCompanies-TotalBuy", "SecuritiesInvestmentTrustCompanies-TotalSell",
                                       "SecuritiesInvestmentTrustCompanies-Difference"),
               "dealers": grp("Dealers-TotalBuy", "Dealers-TotalSell", "Dealers-Difference"),
               "total_net": num(r.get("TotalDifference"))}
        return _ok("institutional", "tpex_institutional", TPEX_INSTITUTIONAL_URL, market, code, roc_to_iso(row.get("Date")), rec, req, fetched, hit)

    def _t86(self, code: str, trade_date: str) -> dict[str, Any]:
        d8 = trade_date.replace("-", "")
        url = f"{TWSE_T86_URL}?date={d8}&selectType=ALL&response=json"
        req = {"stock_id": code, "market": "TWSE", "date": trade_date, "selectType": "ALL"}
        body, gap, fetched, hit = self._get("institutional", "twse_institutional", url, "TWSE", code, req)
        if gap:
            return gap
        if not isinstance(body, dict) or body.get("stat") != "OK":
            return _gap("institutional", "twse_institutional", url, "TWSE", code, "no_data", f"TWSE returned no table for {trade_date} (non-trading day or not yet published)", req, 200)
        fields, rows = body.get("fields") or [], body.get("data") or []
        row = next((r for r in rows if str(r[0]).strip() == code), None)
        if row is None:
            return _gap("institutional", "twse_institutional", url, "TWSE", code, "no_data", "code not in the day's institutional table", req, 200)
        idx = {f: i for i, f in enumerate(fields)}

        def g(label: str) -> Optional[float]:
            i = idx.get(label)
            return num(row[i]) if i is not None and i < len(row) else None
        fb, fs, fn = g("外陸資買進股數(不含外資自營商)"), g("外陸資賣出股數(不含外資自營商)"), g("外陸資買賣超股數(不含外資自營商)")
        tb, ts, tn = g("投信買進股數"), g("投信賣出股數"), g("投信買賣超股數")
        dn = g("自營商買賣超股數")
        fdn = g("外資自營商買賣超股數")
        rec = {"unit": "shares", "comparability": COMPARABILITY,
               "foreign_dealers": {"buy": g("外資自營商買進股數"), "sell": g("外資自營商賣出股數"), "net": fdn, "consistent": None},
               "foreign": {"buy": fb, "sell": fs, "net": fn, "consistent": None if None in (fb, fs, fn) else abs((fb - fs) - fn) < 0.5},
               "investment_trust": {"buy": tb, "sell": ts, "net": tn, "consistent": None if None in (tb, ts, tn) else abs((tb - ts) - tn) < 0.5},
               "dealers": {"buy": None, "sell": None, "net": dn, "consistent": None},
               "total_net": g("三大法人買賣超股數")}
        return _ok("institutional", "twse_institutional", url, "TWSE", code, roc_to_iso(body.get("date")) or body.get("date"), rec, req, fetched, hit)

    # ---- margin / short --------------------------------------------------------------------------------------------

    def margin(self, code: str, market: str, trade_date: str) -> dict[str, Any]:
        if market == "TWSE":
            d8 = trade_date.replace("-", "")
            url = f"{TWSE_MARGIN_URL}?date={d8}&selectType=STOCK&response=json"
            req = {"stock_id": code, "market": "TWSE", "date": trade_date, "selectType": "STOCK"}
            body, gap, fetched, hit = self._get("margin", "twse_margin", url, "TWSE", code, req)
            if gap:
                return gap
            tables = body.get("tables") if isinstance(body, dict) else None
            if not isinstance(body, dict) or body.get("stat") != "OK" or not tables:
                return _gap("margin", "twse_margin", url, "TWSE", code, "no_data", f"TWSE returned no margin table for {trade_date} (non-trading day or not yet published)", req, 200)
            tbl = next((t for t in tables if "融資融券彙總" in str(t.get("title", ""))), None)
            row = next((r for r in (tbl or {}).get("data", []) if str(r[0]).strip() == code), None)
            if row is None or len(row) < 15:
                return _gap("margin", "twse_margin", url, "TWSE", code, "no_data", "code not in the day's margin summary table", req, 200)
            v = [num(x) for x in row]
            rec = {"unit": "trading units (張, 1,000 shares) as stated by the exchange",
                   "margin_buy": v[2], "margin_sell": v[3], "margin_cash_repay": v[4], "margin_balance_prev": v[5], "margin_balance": v[6], "margin_limit_next": v[7],
                   "short_cover_buy": v[8], "short_sell": v[9], "short_stock_repay": v[10], "short_balance_prev": v[11], "short_balance": v[12], "short_limit_next": v[13],
                   "offset": v[14]}
            return _ok("margin", "twse_margin", url, "TWSE", code, roc_to_iso(body.get("date")) or body.get("date"), rec, req, fetched, hit)
        req = {"stock_id": code, "market": market}
        body, gap, fetched, hit = self._get("margin", "tpex_margin", TPEX_MARGIN_URL, market, code, req)
        if gap:
            return gap
        row = next((r for r in body if isinstance(r, dict) and str(r.get("SecuritiesCompanyCode", "")).strip() == code), None) if isinstance(body, list) else None
        if row is None:
            return _gap("margin", "tpex_margin", TPEX_MARGIN_URL, market, code, "no_data", "code not in the TPEx margin file", req, 200)
        rec = {"unit": "trading units (張, 1,000 shares) as stated by the exchange",
               "margin_buy": num(row.get("MarginPurchase")), "margin_sell": num(row.get("MarginSales")), "margin_cash_repay": num(row.get("CashRedemption")),
               "margin_balance_prev": num(row.get("MarginPurchaseBalancePreviousDay")), "margin_balance": num(row.get("MarginPurchaseBalance")),
               "margin_limit_next": num(row.get("MarginPurchaseQuota")), "margin_utilization_pct": num(row.get("MarginPurchaseUtilizationRate")),
               "short_sell": num(row.get("ShortSale")), "short_cover_buy": num(row.get("ShortConvering")), "short_stock_repay": num(row.get("StockRedemption")),
               "short_balance_prev": num(row.get("ShortSaleBalancePreviousDay")), "short_balance": num(row.get("ShortSaleBalance")),
               "short_limit_next": num(row.get("ShortSaleQuota")), "short_utilization_pct": num(row.get("ShortSaleUtilizationRate")), "offset": num(row.get("Offsetting"))}
        return _ok("margin", "tpex_margin", TPEX_MARGIN_URL, market, code, roc_to_iso(row.get("Date")), rec, req, fetched, hit)


def judge(result: dict[str, Any], trade_date: str, mode: str, today: Optional[str] = None) -> dict[str, Any]:
    """Whether a fetched record may be shown for this run, and how it must be labelled. Returns the result plus `use` (bool),
    `use_reason`, and `stale_days` (trade_date - data_date, latest runs)."""
    out = dict(result)
    if result.get("status") != "ok":
        return {**out, "use": False, "use_reason": result.get("reason") or result.get("status"), "stale_days": None}
    dd = result.get("data_date")
    try:
        d_data, d_run = date.fromisoformat(str(dd)), date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return {**out, "use": False, "use_reason": "source did not state a readable data date", "stale_days": None}
    if d_data > d_run:
        return {**out, "use": False, "use_reason": f"source data date {dd} is after the run date {trade_date}: withheld (no future data)", "stale_days": None}
    if mode != "latest":
        return {**out, "use": False, "use_reason": "historical as-of run: the exchange gives no verified release time for this file, so it is not provably public before the as-of time", "stale_days": None}
    return {**out, "use": True, "use_reason": None, "stale_days": (d_run - d_data).days}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
