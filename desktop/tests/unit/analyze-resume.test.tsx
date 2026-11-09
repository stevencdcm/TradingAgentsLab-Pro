// @vitest-environment happy-dom
// Analyze page: a reload re-attaches to the engine-side run WITHOUT reserving cost or starting anything, and only an explicit
// Stop / New analysis cancels it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const calls = { onEvent: null as null | ((e: any) => void), resume: [] as string[], cancel: 0, close: 0, reserve: 0, runAnalysis: 0, cancelRun: [] as string[] };
let doneResolve: () => void = () => {};

let statusOverride: Record<string, unknown> = {};
let batches: unknown[] = [];
let batchesFail = false;
let activeRunsOverride: unknown[] = [];
const status = {
  run_id: 'run_abc', state: 'running', active: true, ticker: 'AAPL', trade_date: '2026-10-30', provider: 'openai', model: 'gpt-6.1-sol',
  auth_kind: 'oauth', engine: 'pro', phase: 'debate', steps_done: 3, started_at: Date.now() / 1000 - 120, ended_at: null, elapsed_s: 120, last_seq: 4,
  last_event_type: 'agent.message', since_last_event_s: 7, input_tokens: 10, output_tokens: 5, estimated_cost_usd: 0, cost_known: true,
  session_id: null, terminal_code: null, error: null,
};

vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return {
    ...actual,
    getHealth: vi.fn(async () => ({ ok: true, version: 't', uptime_seconds: 1, engine_state: 'ready', data_provider: 'yfinance', live_supported: true, providers: [] })),
    getLocalRuntimes: vi.fn(async () => ({ runtimes: [] })),
    listLocalModels: vi.fn(async () => ({ models: [] })),
    getRunStatus: vi.fn(async (id: string) => (id === 'run_abc' ? { ...status, ...statusOverride } : null)),
    getActiveRuns: vi.fn(async () => ({ runs: activeRunsOverride, recent: [] })),
    listBatches: vi.fn(async () => { if (batchesFail) throw new Error('offline'); return batches; }),
    resumeRun: vi.fn(async (id: string, onEvent: (e: any) => void) => {
      calls.onEvent = onEvent;
      calls.resume.push(id);
      const done = new Promise<void>((r) => { doneResolve = r; });
      return { close: () => { calls.close++; doneResolve(); }, cancel: async () => { calls.cancel++; return true; }, runId: () => id, done };
    }),
  };
});
vi.mock('../../src/lib/cost-guard', async () => {
  const actual = await vi.importActual<any>('../../src/lib/cost-guard');
  return { ...actual, reserveCostGuard: vi.fn(async () => { calls.reserve++; return { reservation_id: 'r1' }; }) };
});
vi.mock('../../src/lib/run-analysis', () => ({ runAnalysis: vi.fn(async () => { calls.runAnalysis++; return { kind: 'cancelled', reason: 'cost_guard' }; }) }));
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

beforeEach(() => {
  calls.resume = []; calls.cancel = 0; calls.close = 0; calls.reserve = 0; calls.runAnalysis = 0; statusOverride = {}; batches = []; batchesFail = false; activeRunsOverride = [];
  sessionStorage.clear();
  (window as any).tradingAgentsLab = { platform: 'web', getEngineHandshake: async () => ({ port: 8765, proxy: true }), consent: { get: async () => ({}) } };
});
afterEach(() => { cleanup(); });

const mount = (props: { resetSignal?: number } = {}) =>
  render(<I18nProvider initialLocale="zh-TW"><Analyze {...props} /></I18nProvider>);

describe('Analyze + engine-side runs', () => {
  it('reload: re-attaches to the remembered run, never reserves cost, never starts a run, never cancels', async () => {
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    mount();
    await waitFor(() => expect(calls.resume).toEqual(['run_abc']));
    expect(calls.reserve).toBe(0);
    expect(calls.runAnalysis).toBe(0);
    expect(calls.cancel).toBe(0);
    await waitFor(() => expect(screen.getByTestId('run-status-connection').textContent).toMatch(/重新整理頁面不會中斷/));
  });

  it('Stop cancels the engine-side run (it does not just close the socket)', async () => {
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    mount();
    await waitFor(() => expect(calls.resume).toHaveLength(1));
    const stop = await screen.findByRole('button', { name: /停止/ });
    await act(async () => { fireEvent.click(stop); });
    expect(calls.cancel).toBe(1);
    expect(calls.close).toBe(0);
  });

  it('New analysis (menu reset) cancels the run and forgets it', async () => {
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    const { rerender } = mount({ resetSignal: 0 });
    await waitFor(() => expect(calls.resume).toHaveLength(1));
    rerender(<I18nProvider initialLocale="zh-TW"><Analyze resetSignal={1} /></I18nProvider>);
    await waitFor(() => expect(calls.cancel).toBe(1));
    expect(sessionStorage.getItem('tal:last-run')).toBeNull();
  });

  it('nothing remembered and nothing active: the normal empty page, no resume, no cancel', async () => {
    mount();
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.resume).toEqual([]);
    expect(calls.cancel).toBe(0);
  });

  it('every stop gesture asks the engine ONCE; the page shows "cancelling" and Stop is disabled meanwhile', async () => {
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    const { rerender } = mount({ resetSignal: 0 });
    await waitFor(() => expect(calls.resume).toHaveLength(1));
    const stop = (await screen.findByTestId('stop-button')) as HTMLButtonElement;
    await act(async () => { fireEvent.click(stop); });
    expect(calls.cancel).toBe(1);
    await waitFor(() => expect(screen.getByTestId('run-status-cancelling').textContent).toMatch(/取消中/));
    expect((screen.getByTestId('stop-button') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { fireEvent.click(stop); });                                              // double click
    await act(async () => { fireEvent.keyDown(window, { key: '.', metaKey: true }); });             // shortcut
    rerender(<I18nProvider initialLocale="zh-TW"><Analyze resetSignal={1} /></I18nProvider>);       // New analysis
    await new Promise((r) => setTimeout(r, 30));
    expect(calls.cancel).toBe(1);
  });

  it('reload while the engine is still cancelling: shows "cancelling", Stop disabled, and does not cancel again', async () => {
    statusOverride = { cancelling: true };
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    mount();
    await waitFor(() => expect(calls.resume).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId('run-status-cancelling')).toBeTruthy());
    expect((screen.getByTestId('stop-button') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { fireEvent.keyDown(window, { key: '.', ctrlKey: true }); });
    expect(calls.cancel).toBe(0);
  });

  it('a finished-but-not-completed run replayed after reload says its usage is partial', async () => {
    statusOverride = { state: 'cancelled', active: false, ended_at: Date.now() / 1000 - 5, terminal_code: 'user_cancelled' };
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    mount();
    await waitFor(() => expect(screen.getByTestId('run-status-usage-partial').textContent).toMatch(/不是最終數字/));
  });

  it('a cancelled run can be exported with its CURRENT partial usage report (no session.complete is needed or invented)', async () => {
    const line = { stage: 'debate', agent: 'market_analyst', provider: 'openai', model: 'gpt-6.1-sol', calls: 1, input_tokens: 0, cached_input_tokens: 0,
      uncached_input_tokens: 0, cache_write_tokens: 0, output_tokens: 0, reasoning_tokens: 0, usd: null, usd_basis: null, unpriced_reason: 'usage_not_reported',
      missing: ['usage_not_reported'], usage_unreported_calls: 1 };
    statusOverride = {
      state: 'cancelled', active: false, ended_at: Date.now() / 1000 - 5, terminal_code: 'user_cancelled',
      cost_report: { schema: 'tal.cost-report/1', basis: 'metered', billing: 'api_key', run_state: 'cancelled',
        price: { version: 'openai-api-2026-10-31', verified_at: '2026-10-31', sources: ['https://developers.openai.com/api/docs/pricing'] }, lines: [line],
        totals: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, cache_write_tokens: 0, complete: false,
          incomplete_reasons: ['run_cancelled_usage_is_partial', 'debate:usage_not_reported'], api_estimate_usd: null, api_equivalent_reference_usd: null, external_api_usd: null }, notes: [] },
    };
    const opened: string[] = [];
    (window as any).tradingAgentsLab.transcript = { openHtml: async (html: string) => { opened.push(html); } };
    sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
    mount();
    await waitFor(() => expect(calls.resume).toHaveLength(1));
    await act(async () => { calls.onEvent?.({ type: 'agent.message', agent: 'market_analyst', phase: 'analysts', content: 'partial note', seq: 1 }); });
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/未知/);
    expect(screen.getByTestId('cost-report').textContent).not.toMatch(/\$0/);
    await act(async () => { fireEvent.click(await screen.findByTestId('open-html-transcript')); });
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(opened[0]).toMatch(/data-testid="cost-report"/); expect(opened[0]).toMatch(/已取消，只計入目前量測到的用量/);
    expect(opened[0]).not.toMatch(/\$0\.0/);
  });

  describe('batch runs (D56/D58 A9)', () => {
    const batchSnap = (extra: Record<string, unknown> = {}) => ({ batch_id: 'b_1', active: true, state: 'running', total: 3, current_index: 1, items: [], ...extra });
    const flush = async () => { await act(async () => { await new Promise((r) => setTimeout(r, 30)); }); };

    it('a remembered run that belongs to a batch is NOT re-attached, is forgotten, and an active batch shows the notice', async () => {
      statusOverride = { batch_id: 'b_1', batch_index: 1, batch_total: 3 };
      batches = [batchSnap()];
      sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
      mount();
      await flush();
      expect(calls.resume).toEqual([]);
      expect(calls.reserve).toBe(0);
      expect(calls.cancel).toBe(0);
      expect(sessionStorage.getItem('tal:last-run')).toBeNull();
      const notice = await screen.findByTestId('analyze-batch-notice');
      expect(notice.textContent).toMatch(/批量分析進行中（第 2\/3 檔）/);
      expect(notice.querySelector('a')?.getAttribute('href')).toBe('#watchlist');
    });

    it('a remembered batch run whose batch has finished: forgotten, not attached, and NO notice', async () => {
      statusOverride = { batch_id: 'b_1', batch_index: 1, batch_total: 3, active: false, state: 'completed' };
      batches = [batchSnap({ active: false, state: 'completed', current_index: null })];
      sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
      mount();
      await flush();
      expect(calls.resume).toEqual([]);
      expect(sessionStorage.getItem('tal:last-run')).toBeNull();
      expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();
    });

    it('an active batch with no run yet (between tickers) shows the notice; preparing has its own wording', async () => {
      batches = [batchSnap({ current_index: null, state: 'queued' })];
      mount();
      await flush();
      expect(calls.resume).toEqual([]);
      expect((await screen.findByTestId('analyze-batch-notice')).textContent).toMatch(/準備中/);
      expect(screen.getByTestId('analyze-batch-notice').textContent).not.toMatch(/NaN|null/);
    });

    it('the notice re-checks every 10 s and on foreground return, and disappears once the batch is no longer active; unmount clears the timer', async () => {
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
      try {
        batches = [batchSnap()];
        const { unmount } = mount();
        await act(async () => { await vi.advanceTimersByTimeAsync(0); });
        await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
        expect(screen.getByTestId('analyze-batch-notice')).toBeTruthy();
        const { listBatches } = await import('../../src/lib/engine-client');
        const n0 = (listBatches as any).mock.calls.length;
        await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
        expect((listBatches as any).mock.calls.length).toBe(n0);          // not before 10 s
        batches = [batchSnap({ active: false, state: 'completed', current_index: null })];
        await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
        await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
        expect((listBatches as any).mock.calls.length).toBe(n0 + 1);
        expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();   // gone
        // foreground return re-checks too: a new active batch brings the notice back
        batches = [batchSnap({ batch_id: 'b_2' })];
        await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
        await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
        expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();   // no notice shown -> no timer/listener; the next mount sees it
        unmount();
        expect(vi.getTimerCount()).toBe(0);
      } finally {
        vi.useRealTimers();
      }
    });

    it('foreground return while the notice shows re-checks at once and removes it when the batch ended', async () => {
      batches = [batchSnap()];
      mount();
      await screen.findByTestId('analyze-batch-notice');
      batches = [batchSnap({ active: false, state: 'cancelled', current_index: null })];
      await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
      await waitFor(() => expect(screen.queryByTestId('analyze-batch-notice')).toBeNull());
    });

    it('the active-run fallback also skips a batch run (second tab)', async () => {
      activeRunsOverride = [{ ...status, batch_id: 'b_1', batch_index: 0, batch_total: 2 }];
      batches = [batchSnap({ total: 2, current_index: 0 })];
      mount();
      await flush();
      expect(calls.resume).toEqual([]);
      expect((await screen.findByTestId('analyze-batch-notice')).textContent).toMatch(/第 1\/2 檔/);
    });

    it('a single (non-batch) run still re-attaches even when /batches is unavailable, and no notice is shown', async () => {
      batchesFail = true;
      sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
      mount();
      await waitFor(() => expect(calls.resume).toEqual(['run_abc']));
      expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();
    });

    it('no active batch: no notice, and a remembered single run attaches as before', async () => {
      batches = [batchSnap({ active: false, state: 'completed', current_index: null })];
      sessionStorage.setItem('tal:last-run', JSON.stringify({ runId: 'run_abc', ticker: 'AAPL' }));
      mount();
      await waitFor(() => expect(calls.resume).toEqual(['run_abc']));
      expect(screen.queryByTestId('analyze-batch-notice')).toBeNull();
    });
  });
});
