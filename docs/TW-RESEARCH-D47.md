# Taiwan research data (D47)

What a Taiwan **Pro full-graph** run feeds its analysts, from which free source, and what it cannot establish. All sources are anonymous and
free; nothing here needs a key, signup or purchase. Exchange data is shown to the user with its source and date; the repository stores no
exchange payloads (the live probe in `docs/tw-probes/` is metadata only).

| Data | Source (official) | Dated? | Used in |
|---|---|---|---|
| Daily prices (raw) | FinMind `TaiwanStockPrice` (anonymous tier, 300 req/h) | by date | market analyst |
| Income / balance / cash-flow statements, monthly revenue | FinMind `TaiwanStockFinancialStatements`, `…BalanceSheet`, `…CashFlowsStatement`, `…MonthRevenue` | by period end (no filing date) | fundamentals analyst (+ quarterly ratios) |
| P/E, P/B, dividend yield | TWSE `openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL`; TPEx `…/openapi/v1/tpex_mainboard_peratio_analysis` | latest file; each row states its own `Date` | fundamentals analyst |
| Institutional net buy/sell | TWSE `www.twse.com.tw/rwd/zh/fund/T86?date=`; TPEx `…/openapi/v1/tpex_3insti_daily_trading` | TWSE by date; TPEx latest | market + fundamentals analysts |
| Margin / short balances | TWSE `www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=`; TPEx `…/openapi/v1/tpex_mainboard_margin_balance` | TWSE by date; TPEx latest (its date can lag the other TPEx files) | market + fundamentals analysts |
| Headlines | FinMind `TaiwanStockNews` | single day per call | news analyst (**headline only**, never the article) |
| Material disclosures | TWSE `…/opendata/t187ap04_L`; TPEx `…/openapi/v1/mopsfin_t187ap04_O` | current-day set | news analyst (exchange announcement text, bounded) |

## Rules the code enforces
* **Financial normalization** (`engine/tw_fin_normalize.py`): income is single-quarter; balance is period-end; cash flow is year-to-date and is
  converted (Q2 = H1 − Q1, Q3 = 9M − H1, Q4 = full year − 9M). A missing prior cumulative is a typed gap. Margins need a positive denominator;
  YoY/QoQ need the comparison period and a positive base (otherwise "not meaningful", never a huge percentage). Banks, insurers, securities and
  holding companies are reported unsupported. Restatements are not tracked.
* **As-of safety**: none of these sources states a verified release time, so `published_at` is always null. A *latest* run shows each record
  with the date the source states and how stale it is; a *historical* (older trade date) run uses none of them and says why. A record dated
  after the run date is never shown.
* **Gaps are typed** (unavailable / access denied / rate limited / no data / withheld / unsupported sector), never zero. An empty P/E means
  "not reported", not 0.
* **News text is untrusted data**: single line, markup stripped, bounded, labelled; article URLs are displayed only and never fetched.
* **Analysts**: Taiwan fresh default = market, fundamentals, news. Social is off for Taiwan only (no reliable Taiwan social source). A selection
  the user made is kept. Single runs and the watchlist batch use the same assembly.
* **Run snapshot** (`engine/tw_snapshot.py`): per run, the exact text each tool returned (bounded; sha256 covers the whole answer), plus per
  source: id, URL, request criteria, as_of, data date, `published_at` (null), fetched_at, content hash, status. Written once (read-only file) under
  the app data directory in a per-owner folder, served only to its owner (`GET /tw/run-snapshots/{id}`), count/size bounded. Credentials,
  headers and raw pages are never stored. `session.complete` carries only a manifest; older reports say they have no snapshot.
* **Concurrent runs**: run context is a ContextVar (LangGraph copies it into worker threads), so a batch or two models on one code cannot see
  each other's trade date, token or snapshot.

## Limits
* Not a back-test source; no adjusted prices; no Taiwan social, macro-news or insider data.
* A snapshot exists only for a run that reached `session.complete` (a cancelled/failed run has none).
* Identical content from the same owner shares one snapshot file (immutable); the manifest's `created_at` is the run's own.
