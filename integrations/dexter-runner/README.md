# Dexter runner (S5)

Drives the **real** Dexter agent loop (`Agent.create` / `agent.run`, v1.1.0 `ec1b64c`, unmodified) as a one-shot child process.

- **Only tool: `read_file`** (`toolAllowlist`), `channel: "api"`, memory off, every approval request denied. No bash, write/edit, browser, web fetch/search, cron, heartbeat, subagents, skills.
- **Sandbox**: per-job working directory (cwd only; `HOME` is neither overridden nor passed through the env whitelist); the only file in it is a copy of the evidence pack. Dexter's `read_file` sandbox (root = cwd) refuses `..`, absolute paths outside and symlinks (tested against the real tool).
- **Evidence pack** (`her30.evidence/1`, built by `engine/tw_evidence.py`) is verified by hash before the job starts. Native Dexter finance tools need a paid Financial Datasets key and cover US only; they are **not bound**. No pack ⇒ `unsupported` (never a silent web search).
- **Result** (`her30.dexter.result/1`): status `succeeded | partial | failed | timeout | canceled | unsupported`. `succeeded` only if Dexter finished, did not hit max iterations, the answer parsed, and ≥1 finding is **verified**: its citations exist in the pack (kind+period), none is from after `as_of`, every claimed number appears in a cited item, and the pack was actually read through `read_file`. Unverified findings are kept, labelled, and their summary is not surfaced. `point_in_time` is `unknown` when the cited item has no known availability time.
- **Limits**: wall 600 s, 8 iterations, 20 tool calls, 100k tokens (a request can only tighten them).
- **Usage**: Dexter's input/output/total tokens; reasoning/cached are `null` (not recorded), never 0.
- Run through `engine/dexter_job.run_dexter_job` (env whitelist, process-group kill, schema check; a missing/garbled child result is `failed`, never invented).

Tests: `engine/tests/test_dexter_runner.py` (real Dexter loop + real tool; only the model is a scripted fake Ollama server) and `sandbox.test.ts`. They do **not** prove a real LLM follows the prompt — that needs a real run with a model key.
