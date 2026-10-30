"""Taiwan evidence pack (S2).

Builds ONE immutable JSON document of facts for a Taiwan listing from:

* FinMind (free, anonymous tier is enough for a small pack): daily price,
  monthly revenue, financial statements, institutional flows — history;
* the official TWSE / TPEx open-data snapshots: an independent same-day price
  cross-check (latest trading day only — those endpoints take no date).

Rules this module follows (E3/E4):

* Nothing is inferred. Where a date that matters is not known (when a revenue
  figure or a financial statement became public), `available_at` is `null` with
  an explicit basis, and the gap is listed under `unknowns` — the FinMind row
  `date` is a period, not a disclosure date, so it must never be used as one.
* Prices are RAW (unadjusted); the free tier has no adjusted prices, so total
  return / back-tests are not claimed (listed under `unknowns`).
* TWSE/TPEx dates are ROC years ("1151029" → 2026-10-29); TPEx field names have
  typos and padded values, so parsing is lenient; FinMind `spread` is the price
  CHANGE (TWD), not a percentage, and is stored as `change`.
* Every item carries source, period, fetched_at; the pack carries a content hash
  (`snapshot_id`) computed WITHOUT fetched_at so identical data hashes equal.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
SCHEMA = "her30.evidence/1"
TZ_TPE = timezone(timedelta(hours=8))

CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def roc_to_iso(roc: str) -> Optional[str]:
    """'1151029' -> '2026-10-29' (ROC year + 1911). None when not parseable."""
    s = str(roc or "").strip()
    m = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class FinMindError(Exception):
    pass


def fetch_finmind(client: httpx.Client, dataset: str, data_id: str, start: str, end: str, token: str | None = None) -> list[dict]:
    params = {"dataset": dataset, "data_id": data_id, "start_date": start, "end_date": end}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.get(FINMIND_URL, params=params, headers=headers, timeout=30)
    if r.status_code == 429:
        raise FinMindError("FinMind rate limit (HTTP 429): retry in the next hourly window; not escalating to a paid tier")
    if r.status_code != 200:
        raise FinMindError(f"FinMind HTTP {r.status_code}")
    body = r.json()
    if body.get("status") != 200:
        raise FinMindError(f"FinMind status {body.get('status')}: {body.get('msg')}")
    return body.get("data") or []


def _price_item(row: dict, fetched_at: str) -> dict:
    d = row.get("date")
    return {
        "kind": "price",
        "period": d,
        "values": {
            "open": row.get("open"), "high": row.get("max"), "low": row.get("min"), "close": row.get("close"),
            "volume_shares": row.get("Trading_Volume"), "turnover_twd": row.get("Trading_money"),
            "change": row.get("spread"),          # price change in TWD (FinMind calls it "spread")
            "transactions": row.get("Trading_turnover"),
        },
        "unit": "TWD", "basis": "raw",
        "source": "FinMind:TaiwanStockPrice",
        # A trading day's final prices are public after the close; the exchange close time is
        # an exchange rule, not an inference about this row.
        "available_at": f"{d}T13:30:00+08:00", "available_at_basis": "exchange_close_time",
        "fetched_at": fetched_at,
    }


def _crosscheck(code: str, finmind_row: Optional[dict], twse: list[dict] | None, tpex: list[dict] | None, fetched_at: str) -> Optional[dict]:
    """Independent same-day check against the official TWSE / TPEx snapshot."""
    official: Optional[dict] = None
    source = None
    for row in twse or []:
        if str(row.get("Code", "")).strip() == code:
            official, source = {
                "date": roc_to_iso(row.get("Date", "")), "open": _num(row.get("OpeningPrice")), "high": _num(row.get("HighestPrice")),
                "low": _num(row.get("LowestPrice")), "close": _num(row.get("ClosingPrice")), "volume_shares": _num(row.get("TradeVolume")),
                "turnover_twd": _num(row.get("TradeValue")), "change": _num(row.get("Change")), "transactions": _num(row.get("Transaction")),
            }, f"TWSE:{TWSE_ALL_URL}"
            break
    if official is None:
        for row in tpex or []:
            if str(row.get("SecuritiesCompanyCode", "")).strip() == code:
                official, source = {
                    "date": roc_to_iso(row.get("Date", "")), "open": _num(row.get("Open")), "high": _num(row.get("High")),
                    "low": _num(row.get("Low")), "close": _num(row.get("Close")), "volume_shares": _num(row.get("TradingShares")),
                    "turnover_twd": _num(row.get("TransactionAmount")), "change": _num(row.get("Change")),
                    "transactions": _num(row.get("TransactionNumber")),
                }, f"TPEx:{TPEX_ALL_URL}"
                break
    if official is None:
        return None
    out = {"kind": "price_crosscheck", "period": official["date"], "values": official, "unit": "TWD", "basis": "raw",
           "source": source, "available_at": f"{official['date']}T13:30:00+08:00" if official["date"] else None,
           "available_at_basis": "exchange_close_time", "fetched_at": fetched_at}
    if finmind_row and official["date"] == finmind_row.get("date"):
        fm = {"open": finmind_row.get("open"), "high": finmind_row.get("max"), "low": finmind_row.get("min"), "close": finmind_row.get("close"),
              "volume_shares": finmind_row.get("Trading_Volume"), "turnover_twd": finmind_row.get("Trading_money"),
              "change": finmind_row.get("spread"), "transactions": finmind_row.get("Trading_turnover")}
        out["compared_with"] = "FinMind:TaiwanStockPrice"
        out["field_match"] = {k: (fm[k] is not None and official[k] is not None and abs(float(fm[k]) - float(official[k])) < 1e-6) for k in fm}
        out["all_match"] = all(out["field_match"].values())
    else:
        out["compared_with"] = None
        out["note"] = "official snapshot date differs from the latest FinMind date; not compared"
    return out


def snapshot_hash(items: list[dict], security_id: str, as_of: str) -> str:
    stable = [{k: v for k, v in it.items() if k != "fetched_at"} for it in items]
    blob = json.dumps({"security_id": security_id, "as_of": as_of, "items": stable}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_tw_evidence(
    code: str,
    as_of: str,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
    price_days: int = 30,
    include_official: bool = True,
    official_snapshots: tuple[list[dict] | None, list[dict] | None] | None = None,
) -> dict:
    """Build the pack. Network failures of one dataset are recorded as `unknowns`, never invented."""
    if not CODE_RE.match(code or ""):
        raise ValueError(f"not a Taiwan stock code: {code!r}")
    end = as_of[:10]
    own = client is None
    client = client or httpx.Client()
    fetched_at = _now()
    items: list[dict] = []
    coverage: dict[str, Any] = {}
    unknowns: list[str] = []
    errors: dict[str, str] = {}
    try:
        def get(dataset: str, start: str):
            try:
                return fetch_finmind(client, dataset, code, start, end, token)
            except (FinMindError, httpx.HTTPError, ValueError) as exc:
                errors[dataset] = f"{type(exc).__name__}: {exc}"
                return None

        start_price = (date.fromisoformat(end) - timedelta(days=price_days)).isoformat()
        prices = get("TaiwanStockPrice", start_price)
        latest = None
        if prices:
            prices = sorted(prices, key=lambda r: r["date"])
            latest = prices[-1]
            items += [_price_item(r, fetched_at) for r in prices]
        coverage["price"] = {"rows": len(prices or []), "latest": latest["date"] if latest else None, "basis": "raw"}

        start_year = (date.fromisoformat(end) - timedelta(days=500)).isoformat()
        revenue = get("TaiwanStockMonthRevenue", start_year)
        for r in revenue or []:
            items.append({
                "kind": "revenue", "period": f"{r.get('revenue_year')}-{int(r.get('revenue_month', 0)):02d}",
                "values": {"revenue_twd": r.get("revenue")}, "unit": "TWD", "basis": "monthly",
                "source": "FinMind:TaiwanStockMonthRevenue",
                # The row `date` is the first day of the following month, NOT the legal disclosure time.
                "available_at": None, "available_at_basis": "unknown",
                "provider_create_time": r.get("create_time") or None, "fetched_at": fetched_at,
            })
        coverage["revenue"] = {"rows": len(revenue or [])}

        fin = get("TaiwanStockFinancialStatements", start_year)
        for r in fin or []:
            items.append({
                "kind": "fin_is_bs_cf", "period": r.get("date"), "values": {r.get("type"): r.get("value")},
                "label_zh": r.get("origin_name"), "unit": "TWD (EPS: TWD/share)", "basis": "as_reported_unknown",
                "source": "FinMind:TaiwanStockFinancialStatements", "available_at": None, "available_at_basis": "unknown", "fetched_at": fetched_at,
            })
        coverage["financial_statements"] = {"rows": len(fin or [])}

        chips = get("TaiwanStockInstitutionalInvestorsBuySell", (date.fromisoformat(end) - timedelta(days=14)).isoformat())
        for r in chips or []:
            items.append({
                "kind": "chips", "period": r.get("date"), "values": {"investor": r.get("name"), "buy_shares": r.get("buy"), "sell_shares": r.get("sell")},
                "unit": "shares", "basis": "raw", "source": "FinMind:TaiwanStockInstitutionalInvestorsBuySell",
                "available_at": None, "available_at_basis": "unknown", "fetched_at": fetched_at,
            })
        coverage["institutional_flows"] = {"rows": len(chips or [])}

        if include_official:
            if official_snapshots is not None:
                twse, tpex = official_snapshots
            else:
                twse = tpex = None
                for name, url in (("twse", TWSE_ALL_URL), ("tpex", TPEX_ALL_URL)):
                    try:
                        resp = client.get(url, timeout=30)
                        resp.raise_for_status()
                        data = resp.json()
                        if name == "twse":
                            twse = data
                        else:
                            tpex = data
                    except Exception as exc:  # noqa: BLE001 — recorded, never invented
                        errors[name] = f"{type(exc).__name__}"
            cc = _crosscheck(code, latest, twse, tpex, fetched_at)
            if cc:
                items.append(cc)
                coverage["official_crosscheck"] = {"source": cc["source"], "period": cc["period"], "all_match": cc.get("all_match")}
            else:
                coverage["official_crosscheck"] = None
                unknowns.append("官方 TWSE/TPEx 同日快照中找不到此代碼，未做交叉核對")
    finally:
        if own:
            client.close()

    for ds, msg in errors.items():
        unknowns.append(f"資料集 {ds} 取得失敗：{msg}")
    unknowns += [
        "還原股價（除權息調整）：免費版無此資料；不得據此計算報酬或回測",
        "月營收與財報的法定公布時點未取得（FinMind date 只是期間）；不可作嚴格的 point-in-time 前視檢查",
        "財報為 FinMind 原樣欄位，單季或累計尚未逐科目核對；做單季推算前須先核對",
    ]
    return {
        "schema": SCHEMA, "security_id": f"TW:{code}", "as_of": as_of,
        "snapshot_id": snapshot_hash(items, f"TW:{code}", as_of),
        "fetched_at": fetched_at, "coverage": coverage, "unknowns": unknowns, "items": items,
    }
