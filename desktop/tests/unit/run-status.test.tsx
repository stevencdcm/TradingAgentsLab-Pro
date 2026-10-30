// @vitest-environment happy-dom
// Live run status: what it may claim (engine alive, waiting for a response, measured tokens) and what it must not.
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import RunStatus from '../../src/components/RunStatus';
import { I18nProvider } from '../../src/i18n';
import type { DebateEvent } from '../../src/lib/engine-client';
import { deriveRunStatus, NO_HEARTBEAT_AFTER_MS, QUIET_AFTER_MS, type RunClock } from '../../src/lib/run-status';

afterEach(cleanup);
const T0 = 1_000_000;
const clock = (o: Partial<RunClock> = {}): RunClock => ({ startedAt: T0, endedAt: null, lastEventAt: null, lastHeartbeatAt: null, heartbeat: null, streamError: null, ...o });
const start = { type: 'session.start', ticker: 'NVDA', trade_date: '2026-10-29' } as unknown as DebateEvent;
const msg = (agent: string) => ({ type: 'agent.message', agent, phase: 'analysts', content: 'SECRET-REASONING-TEXT' }) as unknown as DebateEvent;
const ctx = { provider: 'local', model: 'Q3.8 Flash Next 原版' };

describe('deriveRunStatus', () => {
  it('connecting before anything arrives', () => {
    expect(deriveRunStatus([], clock(), T0 + 2000, true, ctx).state).toBe('connecting');
  });

  it('waiting for the model response (not "generating") with elapsed and time since last real event', () => {
    const v = deriveRunStatus([start, msg('technical_analyst')], clock({ lastEventAt: T0 + 10_000, lastHeartbeatAt: T0 + 12_000 }), T0 + 15_000, true, ctx);
    expect(v.state).toBe('waiting_model');
    expect(v.elapsedMs).toBe(15_000);
    expect(v.sinceEventMs).toBe(5_000);
    expect(v.heartbeatAgeMs).toBe(3_000);
    expect(v.quiet).toBe('none');
    expect(v.lastCompletedAgent).toBe('technical_analyst');
    expect(v.activeAgent).toBeNull();                           // a finished step is never shown as the current one
  });

  it('agent.activity means the agent is using tools', () => {
    const ev = [start, { type: 'agent.activity', agent: 'news_analyst', status: 'tool' } as unknown as DebateEvent];
    const v = deriveRunStatus(ev, clock({ lastEventAt: T0 + 1000, lastHeartbeatAt: T0 + 1000 }), T0 + 2000, true, ctx);
    expect(v.state).toBe('using_tools');
    expect(v.activeAgent).toBe('news_analyst');
  });

  it('no real event for a long time is "quiet" (uncertain), while heartbeats keep arriving', () => {
    const now = T0 + QUIET_AFTER_MS + 5_000;
    const v = deriveRunStatus([start], clock({ lastEventAt: T0 + 1000, lastHeartbeatAt: now - 2000 }), now, true, ctx);
    expect(v.quiet).toBe('quiet');
    expect(v.state).toBe('waiting_model');
  });

  it('heartbeats stopping is reported as a probable lost engine/connection, not as progress', () => {
    const now = T0 + NO_HEARTBEAT_AFTER_MS + 10_000;
    const v = deriveRunStatus([start], clock({ lastEventAt: T0 + 1000, lastHeartbeatAt: T0 + 2000 }), now, true, ctx);
    expect(v.quiet).toBe('no_heartbeat');
  });

  it('tokens are shown only when measured, otherwise unknown', () => {
    expect(deriveRunStatus([start], clock({ lastEventAt: T0 }), T0 + 1, true, ctx).tokens).toBeNull();
    const usage = { type: 'cost.usage', input_tokens: 1200, output_tokens: 300, est_cost_usd: 0, free: true } as unknown as DebateEvent;
    expect(deriveRunStatus([start, usage], clock({ lastEventAt: T0 }), T0 + 1, true, ctx).tokens).toEqual({ input: 1200, output: 300 });
  });

  it('terminal states: completed, failed, token cap, disconnected', () => {
    const done = { type: 'session.complete', decision: {}, input_tokens: 5, output_tokens: 6, engine: 'full' } as unknown as DebateEvent;
    const c = deriveRunStatus([start, done], clock({ endedAt: T0 + 9000 }), T0 + 99_000, false, ctx);
    expect(c.state).toBe('completed');
    expect(c.elapsedMs).toBe(9000);                             // frozen at the end, not still counting
    expect(c.route).toBe('full');
    expect(deriveRunStatus([start, { type: 'run.error', error: 'x' } as unknown as DebateEvent], clock({ endedAt: T0 + 1 }), T0 + 5, false, ctx).state).toBe('failed');
    expect(deriveRunStatus([start, { type: 'run.token_cap', used: 1, cap: 1, message: '' } as unknown as DebateEvent], clock({ endedAt: T0 + 1 }), T0 + 5, false, ctx).state).toBe('stopped');
    expect(deriveRunStatus([start], clock({ endedAt: T0 + 1, streamError: 'stream closed with code 1006' }), T0 + 5, false, ctx).state).toBe('disconnected');
  });
});

describe('<RunStatus />', () => {
  const show = (events: DebateEvent[], c: RunClock, streaming: boolean) =>
    render(<I18nProvider><RunStatus events={events} clock={c} streaming={streaming} provider="local" model="Q3.8 Flash Next 原版" /></I18nProvider>);

  it('shows provider/model/route, elapsed, engine heartbeat and an explicit non-streaming note, without any message content', () => {
    show([start, msg('technical_analyst')],
      clock({ startedAt: Date.now() - 20_000, lastEventAt: Date.now() - 5000, lastHeartbeatAt: Date.now() - 2000, heartbeat: { type: 'run.heartbeat', elapsed_s: 20, since_event_s: 5, provider: 'local', model: 'Q3.8 Flash Next 原版', route: 'full' } }),
      true);
    const panel = screen.getByTestId('run-status');
    expect(panel.getAttribute('data-state')).toBe('waiting_model');
    expect(screen.getByTestId('run-status-model').textContent).toMatch(/local \/ Q3\.8 Flash Next 原版/);
    expect(screen.getByTestId('run-status-model').textContent).toMatch(/Pro 完整圖|Pro full graph/);
    expect(screen.getByTestId('run-status-heartbeat').textContent).toMatch(/存活|alive/);
    expect(screen.getByTestId('run-status-tokens').textContent).toMatch(/未知|unknown/);
    expect(screen.getByTestId('run-status-note').textContent).toMatch(/非串流|without streaming/);
    expect(panel.textContent).not.toContain('SECRET-REASONING-TEXT');
  });

  it('shows a warning (not an endless spinner) when the engine goes silent', () => {
    show([start], clock({ startedAt: Date.now() - 120_000, lastEventAt: Date.now() - 110_000, lastHeartbeatAt: Date.now() - 60_000 }), true);
    expect(screen.getByTestId('run-status').getAttribute('data-quiet')).toBe('no_heartbeat');
    expect(screen.getByTestId('run-status-note').textContent).toMatch(/心跳|heartbeat/);
  });

  it('renders nothing before a run, and a failed/disconnected end is shown with the error', () => {
    const { container } = show([], clock({ startedAt: 0 }), false);
    expect(container.textContent).toBe('');
    cleanup();
    show([start], clock({ startedAt: Date.now() - 5000, endedAt: Date.now(), streamError: 'stream closed with code 1006' }), false);
    expect(screen.getByTestId('run-status').getAttribute('data-state')).toBe('disconnected');
    expect(screen.getByTestId('run-status-note').textContent).toMatch(/1006/);
  });
});
