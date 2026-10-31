"""Read-only, anonymous Taiwan free-source adapters for a future Lab Pro hook.

No model, credential, service, or existing graph imports. `records` contains only
point-in-time-safe rows. Rows with unverified publication time stay in
`unknown_records` and MUST NOT enter a historical/as_of research graph.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_ANNOUNCEMENTS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_ANNOUNCEMENTS_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
FINMIND_DATASETS = {
    "income": "TaiwanStockFinancialStatements",
    "balance": "TaiwanStockBalanceSheet",
    "cash_flow": "TaiwanStockCashFlowsStatement",
    "news": "TaiwanStockNews",
}
_STOCK = re.compile(r"^[0-9]{4}$")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    source: str
    source_url: str
    as_of: str
    fetched_at: str | None
    status: str  # ok | empty | access_denied | rate_limited | unavailable
    records: list[dict[str, Any]]  # point-in-time safe only
    unknown_records: list[dict[str, Any]]
    excluded_count: int
    gaps: list[str]
    cache_hit: bool = False
    cache_stale: bool = False  # expired cache was NOT served
    retry_after: float | None = None  # seconds, if upstream supplied it
    http_status: int | None = None


class SourceRequestError(RuntimeError):
    def __init__(self, status: int | None, retry_after: float | None = None):
        super().__init__(f"source request failed: HTTP {status}" if status else "source request failed")
        self.status = status
        self.retry_after = retry_after


def _default_transport(url: str) -> HttpResponse:
    request = Request(url, headers={"User-Agent": "LabPro-HER30-S11/1.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            return HttpResponse(response.status, response.read(4_000_001), dict(response.headers))
    except HTTPError as error:
        return HttpResponse(error.code, error.read(4096), dict(error.headers))


def _as_of_datetime(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("as_of must be an ISO 8601 datetime with timezone") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("as_of must include timezone; a period-end date is not publication time")
    return result


def _iso_day(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _safe_article_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None


def _roc_date(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in (6, 7):
        return None
    try:
        return date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:])).isoformat()
    except ValueError:
        return None


class FreeTaiwanSources:
    """Inject `transport`, `clock`, `sleep`, `now` for deterministic tests.

    One instance shares a small memory cache and a minimum interval across all
    anonymous requests. The server must add any cross-process quota policy.
    """

    def __init__(
        self,
        *,
        transport: Callable[[str], HttpResponse] = _default_transport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        min_interval_seconds: float = 1.0,
        max_retries: int = 1,
    ) -> None:
        if min_interval_seconds < 0 or max_retries < 0 or max_retries > 2:
            raise ValueError("invalid throttle or retry configuration")
        self.transport, self.clock, self.sleep, self.now = transport, clock, sleep, now
        self.min_interval_seconds, self.max_retries = min_interval_seconds, max_retries
        self._last_request: float | None = None
        self._cache: dict[str, tuple[float, Any, str]] = {}

    def _request(self, url: str, ttl_seconds: float) -> tuple[Any, str, bool, bool]:
        cached = self._cache.get(url)
        stale = cached is not None and self.clock() - cached[0] >= ttl_seconds
        if cached and not stale:
            return cached[1], cached[2], True, False
        for attempt in range(self.max_retries + 1):
            if self._last_request is not None:
                delay = self.min_interval_seconds - (self.clock() - self._last_request)
                if delay > 0:
                    self.sleep(delay)
            self._last_request = self.clock()
            try:
                response = self.transport(url)
            except (URLError, TimeoutError, OSError) as error:
                if attempt < self.max_retries:
                    self.sleep(min(2**attempt, 4))
                    continue
                raise SourceRequestError(None) from error
            if len(response.body) > 4_000_000:
                raise SourceRequestError(response.status)
            retry_header = response.headers.get("Retry-After") or response.headers.get("retry-after")
            try:
                retry_after = max(0.0, float(retry_header)) if retry_header is not None else None
            except ValueError:
                retry_after = None
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt < self.max_retries and (retry_after is None or retry_after <= 4.0):
                    self.sleep(min(max(retry_after or 2**attempt, 0.0), 4.0))
                    continue
                raise SourceRequestError(response.status, retry_after)
            if response.status in (402, 403) or response.status >= 400:
                raise SourceRequestError(response.status, retry_after)
            try:
                body = json.loads(response.body)
            except (ValueError, UnicodeDecodeError) as error:
                raise SourceRequestError(response.status) from error
            if isinstance(body, dict) and body.get("status") not in (None, 200):
                raise SourceRequestError(int(body["status"]))
            fetched_at = self.now().astimezone(timezone.utc).isoformat()
            self._cache[url] = (self.clock(), body, fetched_at)
            return body, fetched_at, False, stale
        raise AssertionError("unreachable")

    def _fetch(
        self,
        *,
        source: str,
        url: str,
        stock_id: str,
        as_of: str,
        ttl_seconds: float,
        normalize: Callable[[Mapping[str, Any], str, str, str], dict[str, Any] | None],
    ) -> FetchResult:
        cutoff = _as_of_datetime(as_of)
        if not _STOCK.fullmatch(stock_id):
            raise ValueError("stock_id must be exactly four digits")
        try:
            payload, fetched_at, cache_hit, stale = self._request(url, ttl_seconds)
        except SourceRequestError as error:
            status = "access_denied" if error.status in (402, 403) else "rate_limited" if error.status == 429 else "unavailable"
            return FetchResult(source, url, as_of, None, status, [], [], 0, [status],
                               cache_stale=url in self._cache, retry_after=error.retry_after,
                               http_status=error.status)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return FetchResult(source, url, as_of, fetched_at, "unavailable", [], [], 0,
                               ["invalid_response"], cache_hit, stale)
        records: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        excluded = 0
        gaps: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict):
                gaps.add("invalid_row")
                continue
            normalized = normalize(raw, stock_id, url, fetched_at)
            if normalized is None:
                continue
            normalized["as_of"] = as_of
            period = normalized.get("data_period")
            if period and period > cutoff.date().isoformat():
                excluded += 1
                continue
            published_at = normalized.get("published_at")
            if not published_at:
                normalized["availability"] = "unknown"
                gaps.add("published_at_unknown")
                unknown.append(normalized)
                continue
            try:
                published = _as_of_datetime(published_at)
            except ValueError:
                normalized["availability"] = "unknown"
                gaps.add("published_at_timezone_unknown")
                unknown.append(normalized)
                continue
            if published > cutoff:
                excluded += 1
                continue
            normalized["availability"] = "verified_before_as_of"
            records.append(normalized)
        if not records and not unknown:
            gaps.add("no_matching_rows")
        return FetchResult(source, url, as_of, fetched_at,
                           "empty" if not records and not unknown else "ok",
                           records, unknown, excluded, sorted(gaps), cache_hit, stale)

    def fetch_finmind_dataset(
        self, dataset: str, stock_id: str, start_date: str, *, as_of: str,
        end_date: str | None = None,
    ) -> FetchResult:
        """Fetch one Free dataset. News permits a single day, never end_date."""
        if dataset not in FINMIND_DATASETS:
            raise ValueError("dataset must be income, balance, cash_flow, or news")
        start = _iso_day(start_date)
        if dataset == "news":
            if end_date is not None:
                raise ValueError("TaiwanStockNews only supports one day and no end_date")
        elif end_date is not None and _iso_day(end_date) < start:
            raise ValueError("end_date precedes start_date")
        params = {"dataset": FINMIND_DATASETS[dataset], "data_id": stock_id, "start_date": start}
        if end_date is not None:
            params["end_date"] = _iso_day(end_date)
        url = FINMIND_URL + "?" + urlencode(params)
        return self._fetch(source="FinMind", url=url, stock_id=stock_id, as_of=as_of,
                           ttl_seconds=900 if dataset == "news" else 3600,
                           normalize=_normalize_news if dataset == "news" else
                           lambda row, stock, source_url, fetched: _normalize_financial(
                               row, stock, source_url, fetched, dataset))

    def fetch_disclosures(self, market: str, stock_id: str, *, as_of: str) -> FetchResult:
        """Read the current daily official feed; no historical query is implied."""
        if market not in ("listed", "otc"):
            raise ValueError("market must be listed or otc")
        url = TWSE_ANNOUNCEMENTS_URL if market == "listed" else TPEX_ANNOUNCEMENTS_URL
        return self._fetch(source="TWSE" if market == "listed" else "TPEx", url=url,
                           stock_id=stock_id, as_of=as_of, ttl_seconds=300,
                           normalize=lambda row, stock, source_url, fetched: _normalize_disclosure(
                               row, stock, source_url, fetched, market))


def _base(source_url: str, fetched_at: str) -> dict[str, Any]:
    return {"source_url": source_url, "fetched_at": fetched_at,
            "data_period": None, "published_at": None, "as_of": None,
            "availability": "unknown"}


def _normalize_financial(row: Mapping[str, Any], stock: str, url: str,
                         fetched: str, dataset: str) -> dict[str, Any] | None:
    if str(row.get("stock_id", "")) != stock:
        return None
    try:
        period = _iso_day(str(row["date"]))
    except (ValueError, KeyError):
        return None
    result = _base(url, fetched)
    result.update(stock_id=stock, dataset=FINMIND_DATASETS[dataset], data_period=period,
                  metric=row.get("type"), value=row.get("value"),
                  origin_name=row.get("origin_name"), currency="TWD",
                  unit="per_share_TWD" if row.get("type") == "EPS" else
                       "percent" if str(row.get("type", "")).endswith("_per") else "TWD",
                  period_basis="single_period" if dataset == "income" else
                               "point_in_time" if dataset == "balance" else "year_to_date_cumulative",
                  single_quarter_value=None if dataset == "cash_flow" else row.get("value") if dataset == "income" else None,
                  single_quarter_derivation="not_derived" if dataset == "cash_flow" else "source_value" if dataset == "income" else "not_applicable",
                  consolidation="unknown", publication_note="FinMind row has period end, not filing date")
    return result


def _normalize_news(row: Mapping[str, Any], stock: str, url: str,
                    fetched: str) -> dict[str, Any] | None:
    if str(row.get("stock_id", "")) != stock:
        return None
    raw_date = str(row.get("date", ""))
    try:
        period = _iso_day(raw_date[:10])
    except ValueError:
        return None
    result = _base(url, fetched)
    result.update(stock_id=stock, dataset="TaiwanStockNews", data_period=period,
                  headline=row.get("title"), summary=None,
                  article_url=_safe_article_url(row.get("link")), publisher=row.get("source"),
                  reported_at_raw=raw_date, publication_note="source timezone and publication time unverified")
    return result


def _normalize_disclosure(row: Mapping[str, Any], stock: str, url: str,
                          fetched: str, market: str) -> dict[str, Any] | None:
    key = "公司代號" if market == "listed" else "SecuritiesCompanyCode"
    if str(row.get(key, "")) != stock:
        return None
    result = _base(url, fetched)
    result.update(stock_id=stock, dataset="daily_material_disclosures",
                  data_period=_roc_date(row.get("事實發生日")),
                  subject=str(row.get("主旨", row.get("主旨 ", ""))).strip(),
                  description=row.get("說明"), company_name=row.get("公司名稱" if market == "listed" else "CompanyName"),
                  reported_speech_date=_roc_date(row.get("發言日期")),
                  reported_speech_time=str(row.get("發言時間", "")),
                  publication_note="speech/event dates are not a verified public release timestamp")
    return result
