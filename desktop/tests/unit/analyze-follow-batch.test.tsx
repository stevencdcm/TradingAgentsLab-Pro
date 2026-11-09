// @vitest-environment happy-dom
// D71: Analyze FOLLOWS an active server-side batch (current ticker's live run), moves to the next ticker by itself, stops at the
// terminal state, and its Stop / shortcut / menu act on the BATCH. Leaving the page only unsubscribes.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { item, snapshot } from './helpers/batch-snapshot';

type Handle = { id: string; closed: number; cancelled: number; finish: () => void };
const st = {
  batch: null as any,
  getBatchCalls: 0, getBatchFail: 0, listCalls: 0,
  cancelBatchCalls: 0, cancelBatchFailNext: 0,
  handles: [] as Handle[],
  reserve: 0, runAnalysis: 0,
};
const runStatus = (id: string, ticker: string, idx: number) => ({
  run_id: id, state: 'running', active: true, ticker, trade_date: '2026-11-09', provider: 'anthropic', model: 'claude-sonnet-4-6', auth_kind: 'api_key',
  engine: 'pro', phase: 'debate', steps_done: 3, started_at: Date.now() / 1000 - 30, ended_at: null, elapsed_s: 30, last_seq: 0,
  last_event_type: null, since_last_event_s: 3, input_tokens: 0, output_tokens: 0, estimated_cost_usd: 0, cost_known: true, session_id: null,
  terminal_code: null, error: null, batch_id: 'b_1', batch_index: idx, batch_total: 3,
});
const TICKERS = ['AAPL', 'MSFT', 'NVDA'];

vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return {
    ...actual,
    getHealth: vi.fn(async () => ({ ok: true, version: 't', uptime_seconds: 1, engine_state: 'ready', data_provider: 'yfinance', live_supported: true, providers: [] })),
    getLocalRuntimes: vi.fn(async () => ({ runtimes: [] })),
    listLocalModels: vi.fn(async () => ({ models: [] })),
    getRunStatus: vi.fn(async (id: string) => { const n = Number(id.replace('run_', '')); return runStatus(id, TICKERS[n] ?? 'AAPL', n); }),
    getActiveRuns: vi.fn(async () => ({ runs: [], recent: [] })),
    listBatches: vi.fn(async () => { st.listCalls++; return st.batch ? [st.batch] : []; }),
    getBatch: vi.fn(async () => {
      st.getBatchCalls++;
      if (st.getBatchFail > 0) { st.getBatchFail--; throw new Error('offline'); }
      return st.batch;
    }),
    cancelBatch: vi.fn(async () => {
      st.cancelBatchCalls++;
      if (st.cancelBatchFailNext > 0) { st.cancelBatchFailNext--; throw new Error('net'); }
      return { ...st.batch, cancel_requested: true };
    }),
    resumeRun: vi.fn(async (id: string) => {
      let finish: () => void = () => {};
      const done = new Promise<void>((r) => { finish = r; });
      const h: Handle = { id, closed: 0, cancelled: 0, finish };
      st.handles.push(h);
      return { close: () => { h.closed++; finish(); }, cancel: async () => { h.cancelled++; return true; }, runId: () => id, done };
    }),
  };
});
vi.mock('../../src/lib/cost-guard', async () => {
  const actual = await vi.importActual<any>('../../src/lib/cost-guard');
  return { ...actual, reserveCostGuard: vi.fn(async () => { st.reserve++; return { reservation_id: 'r1' }; }) };
});
vi.mock('../../src/lib/run-analysis', () => ({ runAnalysis: vi.fn(async () => { st.runAnalysis++; return { kind: 'cancelled', reason: 'cost_guard' }; }) }));
vi.mock('../../src/lib/secrets', async () => {
  const actual = await vi.importActual<any>('../../src/lib/secrets');
  return { ...actual, listSecrets: vi.fn(async () => []), getSecret: vi.fn(async () => null), onSecretsChanged: () => () => {} };
});
vi.mock('../../src/lib/oauth', async () => {
  const actual = await vi.importActual<any>('../../src/lib/oauth');
  return { ...actual, getOpenAIOAuthStatus: vi.fn(async () => ({ connected: false })) };
});

import Analyze from '../../src/pages/Analyze';
import { I18nProvider } from '../../src/i18n';

const running = (idx: number, extra: Record<string, unknown> = {}) => {
  const items = TICKERS.map((t, i) => item(i, t, i < idx ? 'completed' : i === idx ? 'running' : 'pending'));
  return snapshot(items, { current_run: { run_id: `run_${idx}`, phase: 'debate', steps_done: 3, elapsed_s: 10, since_last_event_s: 2, last_event_type: null, cancelling: false }, ...extra });
};
const between = () => snapshot(TICKERS.map((t, i) => item(i, t, i < 1 ? 'completed' : 'pending')), { current_index: null, current_run: null, state: 'running' });
const finished = (extra: Record<string, unknown> = {}) => snapshot(TICKERS.map((t, i) => item(i, t, 'completed')), extra);

let menuStop: null | (() => void) = null;
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const mount = async () => {
  const r = render(<I18nProvider initialLocale="zh-TW"><Analyze /></I18nProvider>);
  await tick(50);
  return r;
};
const banner = () => screen.getByTestId('analyze-batch-follow');
const stopBtn = () => screen.getByTestId('stop-button') as HTMLButtonElement;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
  Object.assign(st, { batch: null, getBatchCalls: 0, getBatchFail: 0, listCalls: 0, cancelBatchCalls: 0, cancelBatchFailNext: 0, handles: [], reserve: 0, runAnalysis: 0 });
  sessionStorage.clear();
  menuStop = null;
  window.location.hash = '#analyze';
  (window as any).tradingAgentsLab = {
    platform: 'web', getEngineHandshake: async () => ({ port: 8765, proxy: true }), consent: { get: async () => ({}) },
    onMenuCommand: (name: string, cb: () => void) => { if (name === 'menu:stop-stream') menuStop = cb; return () => {}; },
  };
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('Analyze follows a server-side batch (D71)', () => {
  it('mount with an active batch: attaches to current_run, shows i/N + ticker + watchlist link, never remembers the run, Stop says 停止批量', async () => {
    st.batch = running(1);
    await mount();
    expect(st.handles.map((h) => h.id)).toEqual(['run_1']);
    expect(banner().textContent).toMatch(/批量分析 第 2\/3 檔：MSFT（自動跟隨中）/);
    expect(banner().querySelector('a')?.getAttribute('href')).toBe('#watchlist');
    expect((screen.getByTestId('ticker-input') as HTMLInputElement).value).toBe('MSFT');
    expect(sessionStorage.getItem('tal:last-run')).toBeNull();
    expect(stopBtn().textContent).toBe('停止批量');
    expect(screen.queryByTestId('analyze-button')).toBeNull();
    expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();
    expect(st.reserve).toBe(0);
    expect(st.runAnalysis).toBe(0);
  });

  it('a ticker finishes and the batch moves on: the next run is attached automatically (old subscription closed, not cancelled)', async () => {
    st.batch = running(0);
    await mount();
    expect(st.handles.map((h) => h.id)).toEqual(['run_0']);
    st.handles[0].finish();                       // run_0 ended; the batch still reports it for a moment
    await tick(2000);
    expect(st.handles).toHaveLength(1);           // same run id: nothing new to attach
    st.batch = running(1);
    await tick(2000);
    expect(st.handles.map((h) => h.id)).toEqual(['run_0', 'run_1']);
    expect(banner().textContent).toMatch(/第 2\/3 檔：MSFT/);
    expect(st.handles[0].cancelled).toBe(0);
    expect(st.cancelBatchCalls).toBe(0);
  });

  it('between two tickers (no current_run) shows "準備下一檔…", then attaches when a run appears', async () => {
    st.batch = between();
    await mount();
    expect(st.handles).toEqual([]);
    expect(banner().textContent).toMatch(/準備下一檔…/);
    expect(banner().textContent).not.toMatch(/NaN|null/);
    st.batch = running(1);
    await tick(2000);
    expect(st.handles.map((h) => h.id)).toEqual(['run_1']);
    expect(banner().textContent).toMatch(/第 2\/3 檔：MSFT/);
  });

  it('terminal batch: polling stops, the last ticker stays attached, banner becomes the summary with a watchlist link', async () => {
    st.batch = running(2);
    await mount();
    const calls0 = st.getBatchCalls;
    st.batch = finished();
    await tick(2000);
    expect(screen.queryByTestId('analyze-batch-follow')).toBeNull();
    const ended = screen.getByTestId('analyze-batch-ended');
    expect(ended.textContent).toMatch(/批量已完成：完成 3・失敗 0・略過 0/);
    expect(ended.querySelector('a')?.getAttribute('href')).toBe('#watchlist');
    const n = st.getBatchCalls;
    expect(n).toBeGreaterThan(calls0);
    await tick(20_000);
    expect(st.getBatchCalls).toBe(n);             // no more polling
    expect(st.handles[0].closed).toBe(0);         // the last ticker's content is kept
    st.handles[0].finish();                       // the last run's own stream ends: back to the normal page
    await tick(0);
    expect(screen.queryByTestId('stop-button')).toBeNull();
    expect((screen.getByTestId('analyze-button') as HTMLButtonElement).disabled).toBe(false);
  });

  it('terminal batch that stopped early shows the reason', async () => {
    st.batch = running(1);
    await mount();
    st.batch = snapshot(
      [item(0, 'AAPL', 'completed'), item(1, 'MSFT', 'failed'), item(2, 'NVDA', 'skipped')],
      { state: 'stopped', terminal_code: 'cost_guard_blocked' },
    );
    await tick(2000);
    const ended = screen.getByTestId('analyze-batch-ended');
    expect(ended.textContent).toMatch(/批量已停止：完成 1・失敗 1・略過 1/);
    expect(screen.getByTestId('analyze-batch-ended-reason').textContent).toMatch(/成本守門/);
  });

  it('Stop while following cancels the BATCH once (button, shortcut, menu, double click), never the run; "stopping" shows after success', async () => {
    st.batch = running(1);
    await mount();
    await act(async () => { fireEvent.click(stopBtn()); });
    await act(async () => { fireEvent.click(stopBtn()); });
    await act(async () => { fireEvent.keyDown(window, { key: '.', metaKey: true }); });
    await act(async () => { menuStop?.(); });
    expect(st.cancelBatchCalls).toBe(1);
    expect(st.handles[0].cancelled).toBe(0);
    expect(stopBtn().disabled).toBe(true);
    expect(stopBtn().textContent).toBe('停止批量中…');
    expect(screen.getByTestId('analyze-batch-stopping')).toBeTruthy();
    // stays "stopping" until the batch is terminal
    await tick(2000);
    expect(screen.getByTestId('analyze-batch-stopping')).toBeTruthy();
    st.batch = snapshot([item(0, 'AAPL', 'completed'), item(1, 'MSFT', 'cancelled'), item(2, 'NVDA', 'skipped')], { state: 'cancelled', terminal_code: 'user_cancelled' });
    await tick(2000);
    expect(screen.getByTestId('analyze-batch-ended').textContent).toMatch(/批量已取消/);
    expect(st.cancelBatchCalls).toBe(1);
  });

  it('a failed stop re-enables the button, says so, and a retry succeeds', async () => {
    st.batch = running(1);
    await mount();
    st.cancelBatchFailNext = 1;
    await act(async () => { fireEvent.click(stopBtn()); });
    expect(st.cancelBatchCalls).toBe(1);
    expect(stopBtn().disabled).toBe(false);
    expect(stopBtn().textContent).toBe('停止批量');
    expect(screen.getByTestId('analyze-batch-stop-failed')).toBeTruthy();
    expect(screen.queryByTestId('analyze-batch-stopping')).toBeNull();
    await act(async () => { fireEvent.click(stopBtn()); });
    expect(st.cancelBatchCalls).toBe(2);
    expect(screen.queryByTestId('analyze-batch-stop-failed')).toBeNull();
    expect(stopBtn().disabled).toBe(true);
    expect(st.handles[0].cancelled).toBe(0);
  });

  it('while a batch is active, starting an analysis is disabled (button gone, Cmd/Ctrl+Enter ignored) and a sentence says why', async () => {
    st.batch = running(1);
    await mount();
    await act(async () => { fireEvent.keyDown(window, { key: 'Enter', metaKey: true }); });
    expect(st.runAnalysis).toBe(0);
    expect(screen.getByTestId('analyze-batch-blocked').textContent).toMatch(/批量進行中，同時只能執行一個分析/);
    expect((screen.getByTestId('ticker-input') as HTMLInputElement).disabled).toBe(true);
  });

  it('unmount only unsubscribes: no run cancel, no batch cancel, every timer cleared', async () => {
    st.batch = running(1);
    const { unmount } = await mount();
    unmount();
    expect(st.handles[0].closed).toBe(1);
    expect(st.handles[0].cancelled).toBe(0);
    expect(st.cancelBatchCalls).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    const n = st.getBatchCalls;
    await tick(20_000);
    expect(st.getBatchCalls).toBe(n);
  });

  it('poll errors keep the screen and back off 2→5→10 s; foreground return re-reads at once', async () => {
    st.batch = running(1);
    await mount();
    st.getBatchFail = 99;
    const base = st.getBatchCalls;
    await tick(2000);                              // 1st failure
    expect(st.getBatchCalls).toBe(base + 1);
    expect(screen.getByTestId('analyze-batch-poll-error')).toBeTruthy();
    expect(banner().textContent).toMatch(/第 2\/3 檔：MSFT/);   // screen kept
    await tick(1999);
    expect(st.getBatchCalls).toBe(base + 1);
    await tick(1);                                 // +2 s → 2nd failure
    expect(st.getBatchCalls).toBe(base + 2);
    await tick(4999);
    expect(st.getBatchCalls).toBe(base + 2);
    await tick(1);                                 // +5 s → 3rd failure
    expect(st.getBatchCalls).toBe(base + 3);
    await tick(9999);
    expect(st.getBatchCalls).toBe(base + 3);
    await tick(1);                                 // +10 s
    expect(st.getBatchCalls).toBe(base + 4);
    st.getBatchFail = 0;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await tick(0);
    expect(st.getBatchCalls).toBe(base + 5);       // immediate re-read on foreground
    expect(screen.queryByTestId('analyze-batch-poll-error')).toBeNull();
  });

  it('a batch started elsewhere while this page stays mounted is picked up when the page is shown again', async () => {
    await mount();
    expect(screen.queryByTestId('analyze-batch-follow')).toBeNull();
    st.batch = running(0);
    await act(async () => { window.dispatchEvent(new HashChangeEvent('hashchange')); });
    await tick(0);
    expect(st.handles.map((h) => h.id)).toEqual(['run_0']);
    expect(banner().textContent).toMatch(/第 1\/3 檔：AAPL/);
  });

  it('no active batch: no banner, normal Analyze button', async () => {
    st.batch = finished();
    await mount();
    expect(screen.queryByTestId('analyze-batch-follow')).toBeNull();
    expect(screen.queryByTestId('analyze-batch-ended')).toBeNull();
    expect((screen.getByTestId('analyze-button') as HTMLButtonElement).disabled).toBe(false);
    expect(st.handles).toEqual([]);
  });
});
