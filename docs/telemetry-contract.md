# Compute / usage record contract (design only)

Status: **proposal — nothing here is implemented or collected by this change.** It describes the record a future bridge/workbench can write per analysis job, so usage from Lab Pro can be tracked without guessing.

One JSON document per job. Every field that cannot be obtained is `null` (never `0`), and each measured group carries a `source` and `reliability` so a reader knows whether a number is measured, derived or absent.

```jsonc
{
  "schema": "lab-pro-job-usage/1",
  "job_id": "string",              // bridge-generated; Lab Pro session id goes in `lab_pro_session_id`
  "lab_pro_session_id": "string|null",
  "started_at": "ISO-8601|null", "finished_at": "ISO-8601|null",
  "request": { "ticker": "AAPL", "trade_date": "2026-10-30", "analysts": ["market"], "debate_rounds": 1, "risk_rounds": 1 },

  "model": {
    "provider": "openai|anthropic|local|…",   // as the engine reports it
    "auth_kind": "oauth|api_key|local",
    "deep_model": "string|null", "quick_model": "string|null",
    "api_source": "chatgpt-codex-backend|api.anthropic.com|127.0.0.1:8000/v1|…",
    "host": "string|null",                      // machine that ran inference
    "placement": "local|cloud|null",
    "via_lab_pro": true
  },

  "tokens": {
    "input": 0, "output": 0,
    "reasoning": null, "cached_input": null,   // engine does not record these today
    "calls": null,
    "source": "engine _TokenMeter (LangChain callbacks)",
    "reliability": "provider-reported|derived|absent",
    "note": "cost.usage events are cumulative running totals"
  },

  "timing": {
    "wall_ms": null,                            // events carry no timestamps; bridge must stamp them
    "per_phase_ms": { "analysts": null, "researchers": null, "trader": null, "risk": null },
    "first_event_ms": null
  },

  "cost": {
    "estimated_usd": null,
    "source": "engine price table",
    "reliability": "estimate",                  // 0.0 from the engine means "unknown model / subscription", NOT free
    "billing_model": "subscription|per_token|local|unknown"
  },

  "compute": {
    "process": {                                // the Lab Pro engine process only
      "pid": null, "cpu_seconds": null, "rss_peak_mb": null, "gpu": null,
      "source": "ps/proc sampling", "reliability": "sampled|absent"
    },
    "host": {                                   // whole machine, sampled over the job window
      "cpu_percent_avg": null, "mem_used_peak_gb": null, "gpu_percent_avg": null,
      "source": "powermetrics|vm_stat|exporter", "reliability": "sampled|absent"
    },
    "shared_inference_load": null               // e.g. shared oMLX server load. NEVER attributed to a job.
  },

  "data_availability": { "tokens": "present|partial|absent", "timing": "…", "compute_process": "…", "compute_host": "…", "cost": "…" }
}
```

Rules:

- Process and whole-host figures are separate objects and are never summed or substituted for each other.
- A load measured on a **shared** server (for example a local inference server also used by other jobs) is recorded under `shared_inference_load`, not as this job's compute.
- Per-process GPU use is not available on macOS without privileged tools → `gpu: null`.
- Subscription (OAuth) runs are billed by the plan, not per token: `estimated_usd` is an upper-bound estimate at best, and any "percent of plan" figure must come from the provider's own usage page, not from tokens × an API price.
- This contract adds no daemon, exporter or scheduled collection.
