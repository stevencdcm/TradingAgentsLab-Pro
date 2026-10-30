# Official-API cost report (D15)

Two different numbers, never mixed:

| | Budget guard (`cost_guard.py`, `llm_providers._COST_PER_M_TOKENS`) | Cost report (`cost_report.py`, `official_pricing.py`) |
|---|---|---|
| Question | how much may a run spend? | what do OpenAI's published prices say the measured usage costs? |
| Prices | conservative **ceilings** (gpt-5 family deliberately ~2x official) | OpenAI's official list prices, versioned |
| Used for | reserving / capping spend | showing the user a report |
| Changed by D15 | no | new |

## Prices
`engine/openai_price_data.py` is **generated** (`tools/gen_openai_pricing.py`) from a saved copy of
`https://developers.openai.com/api/docs/pricing.md` (`docs/pricing-snapshots/openai-pricing-2026-10-31.md`, sha256 in the module
docstring). `https://openai.com/api/pricing` was checked for the same four flagship rows. Version `openai-api-2026-10-31`,
verified 2026-10-31. Covered: 37 text models x Standard / Batch / Flex / Fast / Ultrafast, short (<=272K input) and long context, cached
input and 30m/12h cache-write rates where published. Not covered (not selectable here): cyber, life-science, realtime/audio, image,
fine-tuning, tools. To refresh: save the page, run the generator, review the diff.

Model resolution: exact id first; an unlisted dated snapshot (`gpt-4o-2024-08-06`) falls back to its undated alias and is labelled
`snapshot_of_alias`; anything else (OAuth-only aliases, other vendors) is **unknown**, never guessed. `gpt-6.1-sol`, `gpt-6-astra`,
`gpt-6-luna` etc. are listed on the official page and so have exact rows.

## Unit rules
`input_tokens` includes the cached part; billable = (input - cached - cache_write) x input rate + cached x cached rate +
cache_write x write rate + output x output rate. Reasoning tokens are part of output: shown, never added again. A request above 272K input
tokens uses the long-context row (unknown if none is published). Standard tier is assumed unless the response names a tier. Regional
(+10%), FedRAMP, tool and storage fees are not included. A missing usage field is reported as missing (the line is partial), never as 0.

### Cache-write TTL (D17)
A cache write is billed at the 30m rate (1.25x input) or the 12h rate (2.5x input) depending on its TTL. OpenAI reports the split in
`cache_write_tokens_details.ttl_30m_tokens / ttl_12h_tokens`. LangChain's own `usage_metadata` keeps only the total, so the engine reads the
split from the provider's raw usage (`llm_output["token_usage"]`, non-streaming Chat Completions) and prices each TTL separately
(official guide example: gpt-6.1-sol, 12,000 in / 4,000 cached / 3,000 x 30m + 4,000 x 12h written / 1,000 out = $0.0399). When only a write total
is available (Responses API, streaming) the writes are priced as a **range** (30m rate to 12h rate), the line is flagged `cache_write_ttl` and the
total is **not** complete. A model with a published write price whose response says nothing about writes is flagged `cache_write_tokens`.
Models whose page row has no short/long split (e.g. gpt-4.1) use the single published price at any context length.

### Runs that end without a result
The engine hands its latest per-agent partial report to the run record after every model call. A cancelled, failed or interrupted run keeps it
(`runs.cost_report`, additive column), marked partial, on `GET /runs/{id}` (`cost_report`), on the `run.finished` event, and after a restart.
A model call that finishes without usage numbers is kept as a visible `usage_not_reported` gap, never dropped.

## Billing kinds
`api_key` -> `api_estimate_usd`. `oauth` -> no API billing; `api_equivalent_reference_usd` only when the exact model has an official row
(a reference, not an invoice, not a plan percentage). "Exact" includes an unlisted dated snapshot of a listed alias (e.g. `gpt-6.1-sol-2026-10-01`), which is priced at the alias and labelled `priced_via_undated_alias_of_snapshot`. `local` -> external API fee 0, electricity not priced. Other vendors -> tokens listed,
USD unknown. Totals are `complete` only if every line was priced from reported usage; cancelled/failed/interrupted runs are partial.

## Where it appears
`session.complete.cost_report` (per agent, Dexter as its own line); `cost.usage.official_cost` (totals only, running = incomplete);
`GET /sessions/{id}` -> `cost_report` (saved one, or for older sessions a recomputation at today's list price labelled
`recomputed_current_list_price`, never written back); the analysis result, the History detail and the Markdown/HTML exports.
