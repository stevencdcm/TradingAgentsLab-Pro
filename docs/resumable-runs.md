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

Known limits: single engine process (no cross-process run sharing); a Dexter sandbox job already started keeps running to its
own limit after a cancel; an `interrupted` run's cost reservation is released by the normal 15-minute expiry.
