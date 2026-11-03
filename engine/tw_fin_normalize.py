"""Taiwan financial-statement normalization (pure functions, no I/O).

FinMind returns one row per (period end, metric). The three statements use different bases, and mixing them is the classic way to get a
wrong margin or free-cash-flow figure:

* income statement  - SINGLE-quarter values (Q4 is already a single quarter);
* balance sheet     - point-in-time balances at the period end;
* cash-flow         - YEAR-TO-DATE cumulative inside a fiscal year (Q1 = 3 months, Q2 = 6, Q3 = 9, Q4 = the full year), so a single
                      quarter is the difference of two cumulatives and Q4 = annual - (Q1..Q3 cumulative, i.e. the Q3 YTD figure).

Rules: an absent or non-numeric figure stays ``None`` with a reason - never 0; a ratio needs a strictly positive denominator; a YoY needs
the same quarter of the prior year and a strictly positive base (a loss or zero base is "not meaningful", not a huge percentage);
banks/insurers/holding companies use a different statement layout and are reported unsupported rather than forced into commercial
ratios. Amounts are in TWD (FinMind reports full dollars); per-share values in TWD/share. Revisions/restatements are not tracked: the
adapter sees only the figure the source currently serves.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

QUARTER_ENDS = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}
FINANCIAL_CODE_HINT = {"5880", "6005", "6015", "6016", "6020", "6021", "6023", "6024", "5820", "2855", "2867", "2889"}


def quarter_of(period: str) -> Optional[tuple[int, int]]:
    """(fiscal year, quarter) for a calendar-quarter period end ``YYYY-MM-DD``; None for anything else (e.g. a non-December fiscal year)."""
    p = str(period)[:10]
    q = QUARTER_ENDS.get(p[5:10])
    if len(p) != 10 or q is None or not p[:4].isdigit():
        return None
    return int(p[:4]), q


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def is_financial_sector(code: str, income_metrics: set[str]) -> Optional[str]:
    """Reason string when the company is a bank / insurer / securities / holding company, else None. Structure first (those filers have no
    gross-profit layout), the code hint second (28xx 金融保險 and a few 58xx/60xx names)."""
    commercial = {"Revenue", "GrossProfit", "CostOfGoodsSold"}
    if income_metrics and not (income_metrics & commercial):
        return "statement layout has no revenue/gross-profit lines (bank/insurer/holding layout)"
    c = str(code)
    if c.startswith("28") or c in FINANCIAL_CODE_HINT:
        return "financial-sector code (bank/insurer/securities/holding): commercial margins and debt ratios do not apply"
    return None


def to_single_quarters(series: Mapping[str, Any], basis: str) -> dict[str, dict[str, Any]]:
    """``series`` maps period end -> reported value. Returns period -> {value, method, status, reason}.

    basis ``single``: the reported value is already one quarter. basis ``ytd``: cumulative within the fiscal year.
    """
    if basis not in ("single", "ytd"):
        raise ValueError("basis must be 'single' or 'ytd'")
    by_q: dict[tuple[int, int], tuple[str, Optional[float]]] = {}
    out: dict[str, dict[str, Any]] = {}
    for p, v in series.items():
        key = quarter_of(p)
        if key is None:
            out[str(p)[:10]] = {"value": None, "method": None, "status": "unsupported_period", "reason": "not a calendar quarter end (non-December fiscal year?)"}
            continue
        by_q[key] = (str(p)[:10], _num(v))
    for (y, q), (p, v) in sorted(by_q.items()):
        if v is None:
            out[p] = {"value": None, "method": None, "status": "missing", "reason": "figure absent or not numeric"}
        elif basis == "single":
            out[p] = {"value": v, "method": "reported_single_quarter", "status": "ok", "reason": None}
        elif q == 1:
            out[p] = {"value": v, "method": "ytd_q1", "status": "ok", "reason": None}
        else:
            prev = by_q.get((y, q - 1))
            if prev is None or prev[1] is None:
                out[p] = {"value": None, "method": None, "status": "missing_prior_cumulative",
                          "reason": f"{y}Q{q - 1} cumulative is needed to derive the {y}Q{q} single quarter"}
            else:
                out[p] = {"value": v - prev[1], "method": "annual_minus_q1_to_q3" if q == 4 else f"ytd_q{q}_minus_ytd_q{q - 1}", "status": "ok", "reason": None}
    return out


def _ratio(num: Optional[float], den: Optional[float], what: str) -> dict[str, Any]:
    if num is None or den is None:
        return {"value": None, "status": "missing", "reason": f"{what}: input absent"}
    if den <= 0:
        return {"value": None, "status": "not_meaningful", "reason": f"{what}: denominator is zero or negative"}
    return {"value": round(num / den * 100, 2), "status": "ok", "reason": None}


def _yoy(cur: Optional[float], prior: Optional[float], what: str) -> dict[str, Any]:
    if cur is None or prior is None:
        return {"value": None, "status": "missing", "reason": f"{what}: current or prior-year quarter absent"}
    if prior <= 0:
        return {"value": None, "status": "not_meaningful", "reason": f"{what}: prior-year base is zero or negative"}
    return {"value": round((cur - prior) / prior * 100, 2), "status": "ok", "reason": None}


def _metric(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """rows -> {metric: {period: value}}"""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        m, p = r.get("metric"), r.get("data_period")
        if m and p:
            out.setdefault(str(m), {})[str(p)[:10]] = r.get("value")
    return out


def build_financial_summary(code: str, income: list[Mapping[str, Any]], balance: list[Mapping[str, Any]], cash_flow: list[Mapping[str, Any]],
                            max_quarters: int = 4) -> dict[str, Any]:
    """Latest ``max_quarters`` single quarters with margins, YoY, balance ratios and free cash flow. Inputs are the normalized adapter rows
    (``metric`` / ``data_period`` / ``value``). Output is plain JSON; every figure carries its own status."""
    inc, bal, cf = _metric(income), _metric(balance), _metric(cash_flow)
    unsupported = is_financial_sector(code, set(inc))
    base: dict[str, Any] = {"stock_id": code, "currency": "TWD", "amount_unit": "TWD (full dollars)", "per_share_unit": "TWD/share", "revisions": "not_tracked",
                            "bases": {"income": "single_quarter", "balance": "period_end", "cash_flow": "ytd_cumulative_converted_to_single_quarter"}}
    if unsupported:
        return {**base, "supported": False, "unsupported_reason": unsupported, "quarters": []}
    if not inc:
        return {**base, "supported": True, "quarters": [], "gaps": ["income statement rows absent"]}

    single = {m: to_single_quarters(s, "single") for m, s in inc.items()}
    cf_single = {m: to_single_quarters(s, "ytd") for m, s in cf.items()}

    def val(src: Mapping[str, Mapping[str, dict]], m: str, p: str) -> Optional[float]:
        return (src.get(m, {}).get(p) or {}).get("value")

    def pit(m: str, p: str) -> Optional[float]:
        return _num(bal.get(m, {}).get(p))

    periods = sorted({p for s in single.values() for p, d in s.items() if d["status"] != "unsupported_period"}, reverse=True)
    quarters: list[dict[str, Any]] = []
    for p in periods[:max_quarters]:
        y, q = quarter_of(p) or (0, 0)
        prior_p = f"{y - 1}-{p[5:]}"
        prev_q = (y, q - 1) if q > 1 else (y - 1, 4)
        prev_p = f"{prev_q[0]}-{[k for k, v in QUARTER_ENDS.items() if v == prev_q[1]][0]}"
        rev, gp, oi, ni, eps = (val(single, m, p) for m in ("Revenue", "GrossProfit", "OperatingIncome", "IncomeAfterTaxes", "EPS"))
        ocf = val(cf_single, "CashFlowsFromOperatingActivities", p)
        capex = val(cf_single, "PropertyAndPlantAndEquipment", p)          # outflow is negative in the statement
        fcf = None if ocf is None or capex is None else ocf + capex
        quarters.append({
            "period": p, "fiscal_quarter": f"{y}Q{q}",
            "revenue": rev, "gross_profit": gp, "operating_income": oi, "net_income": ni, "eps": eps,
            "gross_margin_pct": _ratio(gp, rev, "gross margin"), "operating_margin_pct": _ratio(oi, rev, "operating margin"),
            "net_margin_pct": _ratio(ni, rev, "net margin"),
            "revenue_yoy_pct": _yoy(rev, val(single, "Revenue", prior_p), "revenue YoY"),
            "net_income_yoy_pct": _yoy(ni, val(single, "IncomeAfterTaxes", prior_p), "net income YoY"),
            "eps_yoy_pct": _yoy(eps, val(single, "EPS", prior_p), "EPS YoY"),
            "revenue_qoq_pct": _yoy(rev, val(single, "Revenue", prev_p), "revenue QoQ"),
            "debt_ratio_pct": _ratio(pit("Liabilities", p), pit("TotalAssets", p), "debt ratio"),
            "current_ratio_pct": _ratio(pit("CurrentAssets", p), pit("CurrentLiabilities", p), "current ratio"),
            "operating_cash_flow": ocf, "capex": capex, "free_cash_flow": fcf,
            "cash_flow_method": (cf_single.get("CashFlowsFromOperatingActivities", {}).get(p) or {}).get("method"),
            "cash_flow_status": (cf_single.get("CashFlowsFromOperatingActivities", {}).get(p) or {}).get("status", "missing"),
        })
    return {**base, "supported": True, "quarters": quarters}
