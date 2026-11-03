#!/usr/bin/env python3
"""Opt-in bounded live probe of the official TWSE/TPEx daily sources for one listed and one OTC code. Writes METADATA only.

    python tools/tw_live_probe.py docs/tw-probes/D47-2330-6488-YYYY-MM-DD.json 2330 6488 [YYYY-MM-DD]

One request per source file and code (the shared adapter throttles and caches), anonymous, no key. The output records, per source: URL,
request criteria, status, the data date the source states, which fields were present, and a sha256 of the normalized record. It does NOT
store the values or any response body (exchange data is not redistributed from the repository).
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import tw_graph_tools as tg  # noqa: E402
from engine import tw_market_data as md  # noqa: E402
from engine.tw_free_sources import FreeTaiwanSources  # noqa: E402


def main() -> int:
    out, codes = Path(sys.argv[1]), sys.argv[2:4]
    run_date = sys.argv[4] if len(sys.argv) > 4 else (datetime.now(timezone(timedelta(hours=8)))).date().isoformat()
    m = md.OfficialMarketData(FreeTaiwanSources(min_interval_seconds=1.0))
    rows = []
    for code in codes:
        market = tg._market_of(code)
        for kind in ("valuation", "institutional", "margin"):
            r = m.valuation(code, market) if kind == "valuation" else getattr(m, kind)(code, market, run_date)
            j = md.judge(r, run_date, "latest")
            rec = r.get("record")
            rows.append({"stock_id": code, "market": market, "kind": kind, "source_id": r["source_id"], "source_url": r["source_url"], "request": r["request"],
                         "status": r["status"], "reason": r.get("reason"), "data_date": r.get("data_date"), "published_at": None, "availability": r["availability"],
                         "fetched_at": r.get("fetched_at"), "usable_in_latest_run": j["use"], "stale_days": j["stale_days"],
                         "fields_present": sorted(k for k, v in (rec or {}).items() if v is not None and not isinstance(v, dict)) if rec else [],
                         "record_sha256": hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if rec else None})
    out.write_text(json.dumps({"schema": "her30.tw_live_probe/1", "run_date": run_date, "probed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "note": "metadata only; no values, no response bodies", "results": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}: " + ", ".join(f"{r['stock_id']}/{r['kind']}={r['status']}@{r['data_date']}" for r in rows))
    return 0 if all(r["status"] in ("ok", "no_data") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
