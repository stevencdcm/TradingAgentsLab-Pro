/**
 * Live run status for the Analyze page. Pure derivation (no timers, no I/O) so it can be tested.
 *
 * What it can and cannot know:
 *  - `run.heartbeat` events prove the ENGINE PROCESS and the socket are alive, and say how long it has
 *    been since the last real event. They never mean "the model is generating".
 *  - The engines call models non-streaming, so while a request is outstanding the honest state is
 *    "waiting for the model's response" with an elapsed time, not "generating".
 *  - Tokens are shown only from the engine's measured `cost.usage` / `session.complete`; otherwise unknown.
 *  - Nothing here reads message content or reasoning.
 */
import type { DebateEvent } from './engine-client';

export interface HeartbeatEvent {
  type: 'run.heartbeat';
  elapsed_s: number;
  since_event_s: number;
  provider?: string;
  model?: string;
  auth_kind?: string;
  route?: 'full' | 'simplified' | 'taiwan';
}

export interface RunClock {
  startedAt: number;
  endedAt: number | null;
  /** Arrival time of the last REAL (non-heartbeat) event. */
  lastEventAt: number | null;
  /** Arrival time and payload of the last heartbeat. */
  lastHeartbeatAt: number | null;
  heartbeat: HeartbeatEvent | null;
  /** Transport-level error text when the socket failed / closed abnormally. */
  streamError: string | null;
}

export type RunState =
  | 'connecting'
  | 'waiting_model'
  | 'using_tools'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'disconnected';

/** Uncertainty flag, orthogonal to `state`. */
export type Quiet = 'none' | 'quiet' | 'no_heartbeat';

export interface RunStatusView {
  state: RunState;
  quiet: Quiet;
  provider: string | null;
  model: string | null;
  route: 'full' | 'simplified' | 'taiwan' | null;
  /** Agent currently using tools (only when the engine said so); otherwise null. */
  activeAgent: string | null;
  /** Most recently COMPLETED agent step (never presented as "current"). */
  lastCompletedAgent: string | null;
  stepsDone: number;
  stepsTotal: number | null;
  elapsedMs: number;
  sinceEventMs: number | null;
  heartbeatAgeMs: number | null;
  tokens: { input: number; output: number } | null;
}

/** No real event for this long: say so, without claiming a stall (slow local models legitimately take minutes). */
export const QUIET_AFTER_MS = 90_000;
/** Heartbeats arrive every ~5 s; four missed means the engine or socket is probably gone. */
export const NO_HEARTBEAT_AFTER_MS = 20_000;

export function deriveRunStatus(
  events: DebateEvent[],
  clock: RunClock,
  now: number,
  streaming: boolean,
  ctx: { provider?: string | null; model?: string | null; totalSteps?: number | null },
): RunStatusView {
  const end = clock.endedAt ?? now;
  let terminal: RunState | null = null;
  let lastCompleted: string | null = null;
  let steps = 0;
  let tokens: RunStatusView['tokens'] = null;
  let route: RunStatusView['route'] = clock.heartbeat?.route ?? null;
  const last = events[events.length - 1];
  for (const ev of events) {
    if (ev.type === 'agent.message') {
      lastCompleted = ev.agent;
      steps += 1;
    } else if (ev.type === 'cost.usage') {
      tokens = { input: ev.input_tokens, output: ev.output_tokens };
    } else if (ev.type === 'session.complete') {
      terminal = 'completed';
      if (ev.input_tokens !== undefined && ev.output_tokens !== undefined) {
        tokens = { input: ev.input_tokens, output: ev.output_tokens };
      }
      if (!route) route = ev.engine === 'full' ? 'full' : ev.market === 'TW' ? 'taiwan' : 'simplified';
    } else if (ev.type === 'run.error') {
      terminal = 'failed';
    } else if (ev.type === 'run.token_cap') {
      terminal = 'stopped';
    }
  }
  let state: RunState;
  if (terminal) state = terminal;
  else if (!streaming) state = clock.streamError ? 'disconnected' : events.length ? 'stopped' : 'failed';
  else if (events.length === 0 && clock.lastHeartbeatAt === null) state = 'connecting';
  else if (last && last.type === 'agent.activity') state = 'using_tools';
  else state = 'waiting_model';

  const sinceEventMs = clock.lastEventAt === null ? null : Math.max(0, end - clock.lastEventAt);
  const heartbeatAgeMs = clock.lastHeartbeatAt === null ? null : Math.max(0, end - clock.lastHeartbeatAt);
  let quiet: Quiet = 'none';
  if (streaming && !terminal) {
    const sinceStart = now - clock.startedAt;
    const hbSilent = clock.lastHeartbeatAt !== null ? now - clock.lastHeartbeatAt : sinceStart;
    if (hbSilent > NO_HEARTBEAT_AFTER_MS && sinceStart > NO_HEARTBEAT_AFTER_MS) quiet = 'no_heartbeat';
    else if ((clock.lastEventAt !== null ? now - clock.lastEventAt : sinceStart) > QUIET_AFTER_MS) quiet = 'quiet';
  }

  return {
    state,
    quiet,
    provider: clock.heartbeat?.provider ?? ctx.provider ?? null,
    model: clock.heartbeat?.model ?? ctx.model ?? null,
    route,
    activeAgent: state === 'using_tools' && last && last.type === 'agent.activity' ? last.agent : null,
    lastCompletedAgent: lastCompleted,
    stepsDone: steps,
    stepsTotal: ctx.totalSteps ?? null,
    elapsedMs: Math.max(0, end - clock.startedAt),
    sinceEventMs,
    heartbeatAgeMs,
    tokens,
  };
}
