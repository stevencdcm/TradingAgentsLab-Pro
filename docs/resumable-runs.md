# Resumable runs (D7)

An analysis is a **run owned by the engine**, not by a WebSocket. A refresh, a dropped tunnel or a second tab never cancels it,
never reserves cost again and never calls the model again.

* `POST`-less start: the page opens `/stream` and sends the start frame with `resumable: true` and a `client_run_key`
  (the idempotency key; `reservation_id` is the fallback). A repeated start with the same key attaches to the same run.
  Without `resumable` the old event stream is unchanged (extra `seq` field only; no `run.attached` / `run.finished`).
* Every event has a `seq`. `GET /runs/{id}/events?after=N&limit=` and `WS /runs/{id}/stream?after=N` replay what was missed;
  the snapshot and the live registration happen in one synchronous step (no gap, no duplicate). Heartbeats are live-only.
* Status: `GET /runs/active` (active runs + recent finished ones of the caller), `GET /runs/{id}` (model, ticker, phase,
  steps, elapsed, tokens, last event, terminal state). Stop: `POST /runs/{id}/cancel` (explicit only; settles the reservation).
* Slow subscribers use bounded queues; one that falls behind gets `run.resync` + close 1013 and re-subscribes with `after=`.
  Generation is never blocked by a consumer.
* States: queued, running, completed, failed, cancelled, interrupted. Failed/cancelled/interrupted runs keep a terminal event
  and their usage. History (`sessions`) is written once, only for a completed run.
* Storage: tables `runs` and `run_events` in the local SQLite database (additive; older engines ignore them). Events are
  scrubbed (key shapes, Bearer headers, secret references); the start frame, provider_config, secrets and headers are never
  stored. Bounds: 5000 events/run, 256 KB/event, 200 runs / 7 days kept. A restart marks unfinished runs `interrupted` and
  never re-runs them.
* Auth: same as the engine API (bearer / cookie behind the workbench proxy). Runs belong to the authenticated owner (`local`
  for the loopback entry, the verified owner login behind the proxy); another owner gets 404 for status, replay, stream and cancel.
* Renderer: remembers the run id in `sessionStorage`, re-attaches on load (this tab's run, else the one active run), shows
  "re-attached" and the socket state separately from the model's progress, and Stop now means cancel.

## Cost and settlement (D8)

* Explicit stop only: Stop, the menu's Stop, the shortcut and **New analysis** call `POST /runs/{id}/cancel`; a reload or leaving the
  page never does. A reload re-attaches (`GET /runs/{id}` + `WS /runs/{id}/stream?after=`) without reserving cost or starting anything.
* Cancel does not pretend a thread stopped: the full graph's worker threads cannot be interrupted, so after a cancel no NEW model call
  may start (`RunCancelled`), the call already in flight is awaited (bounded by `TAL_CANCEL_DRAIN_S`, default 240 s), and only then is the
  reservation settled with the measured usage (including the late call) and the run slot released. While waiting the run reports
  `cancelling: true`. The free engine's in-flight request is cancelled outright; its usage is measured through the last completed call.
* Spend of runs without a session row (failed / cancelled / interrupted) now counts toward the daily/weekly/monthly caps: the
  reservation is settled `incomplete` at its measured cost, or, when the cost is unknown, at the reserved ESTIMATE as a conservative
  budget hold (never 0, never an excluded NULL, never presented as an actual cost). A completed run is counted once, via its session.
  Nothing billable started (guard error, cost block) -> the hold is released at 0. Local / OAuth runs are never API spend.
* A long run renews its reservation while events/heartbeats arrive, so the 15-minute TTL cannot release the budget mid-run.
* Restart: `running` runs become `interrupted` (never re-run) and their holds are settled as above before any expiry sweep.
* Replay buffer: bounded by events AND bytes; the OLDEST events are dropped and a late client gets `run.replay_truncated`
  (`earliest_seq`). A finished run drops its memory buffer after 10 minutes (the database copy remains).
* `run_busy` (one Pro/Taiwan run at a time) carries `active_run_id` for the SAME owner so the page can attach instead of retrying.

Known limits: single engine process (no cross-process run sharing); a Dexter sandbox job already started keeps running to its
own limit after a cancel; history rows (`sessions`) have no owner column: owner isolation covers the run APIs, not the History list, so this is meant for a
single-owner deployment; a stopped free-engine call's usage is a lower bound; deploying a new engine restarts the process and so
interrupts runs of the old one (they come back as `interrupted`).
