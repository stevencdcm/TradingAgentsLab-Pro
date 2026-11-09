// @vitest-environment happy-dom
// D53: the batch runs in the engine; the page restores it, polls it and stops it on an explicit click. The engine-client batch
// calls are fakes (no network, no models); timers are fake so the 2 s poll and the 2 → 5 → 10 s backoff are exact.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { item, snapshot } from './helpers/batch-snapshot';

const api = vi.hoisted(() => ({
  listBatches: vi.fn(),
  getBatch: vi.fn(),
  createBatch: vi.fn(),
  cancelBatch: vi.fn(),
}));
vi.mock('../../src/lib/engine-client', async (orig) => ({ ...(await orig<typeof import('../../src/lib/engine-client')>()), ...api }));
vi.mock('../../src/lib/secrets', () => ({ listSecrets: async () => [{ key: 'llm:anthropic' }] }));
vi.mock('../../src/lib/oauth', () => ({ getOpenAIOAuthStatus: async () => ({ connected: false }) }));
const startParts = vi.hoisted(() => ({
  buildRunStart: vi.fn(async () => ({
    template: { trade_date: '2026-11-09', provider_config: { provider: 'anthropic', auth: { type: 'api_key', api_key: 'k' }, model: 'm' }, engine: 'pro' },
    reserveReq: null,
    maxTokens: 400,
  })),
}));
vi.mock('../../src/lib/run-analysis', () => startParts);

import BatchRunner from '../../src/components/BatchRunner';
import { BatchApiError } from '../../src/lib/engine-client';
import { I18nProvider } from '../../src/i18n';

const ui = (props: Partial<React.ComponentProps<typeof BatchRunner>> = {}) => (
  <I18nProvider initialLocale="zh-TW"><BatchRunner tickers={[]} watchlistEmpty {...props} /></I18nProvider>
);
const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const text = () => screen.getByTestId('batch-runner').textContent ?? '';
const setVisibility = (v: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
  document.dispatchEvent(new Event('visibilitychange'));
};

const RUNNING = () => snapshot([item(0, 'HIMX', 'completed', { elapsed_s: 100, session_id: 's1', stance: 'bullish', conviction: 0.7 }), item(1, 'NVDA', 'running'), item(2, 'AAPL', 'pending')]);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  for (const f of Object.values(api)) f.mockReset();
  startParts.buildRunStart.mockClear();
  try { localStorage.clear(); } catch { /* storage may be unavailable */ }
  setVisibility('visible');
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('restore + polling', () => {
  it('mount restores the latest batch from the server (empty watchlist, nothing selected) and shows the current-item card', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    render(ui());
    await flush();
    expect(api.listBatches).toHaveBeenCalledWith(1, expect.anything());
    expect(screen.getByTestId('batch-runner')).toBeTruthy();
    expect(screen.getByTestId('batch-status').textContent).toMatch(/第 2 \/ 3 檔/);
    const card = screen.getByTestId('batch-current');
    expect(card.textContent).toMatch(/NVDA/);
    expect(screen.getByTestId('batch-current-phase').textContent).toBe('辯論');
    expect(screen.getByTestId('batch-current-steps').textContent).toMatch(/3/);
    expect(screen.getByTestId('batch-current-elapsed').textContent).toMatch(/42 秒/);
    expect(screen.getByTestId('batch-current-since').textContent).toMatch(/5 秒前/);
    expect(screen.getByTestId('batch-counts').textContent).toMatch(/完成 1・失敗 0・略過 0・剩 2/);
    expect(screen.getByTestId('batch-bar-fill').getAttribute('style')).toMatch(/33%/);
    expect(screen.getByTestId('batch-hint').textContent).toMatch(/可以關閉此頁/);
    expect(screen.getByTestId('batch-stop-button')).toBeTruthy();
  });

  it('an empty watchlist with no batch renders nothing', async () => {
    api.listBatches.mockResolvedValue([]);
    const { container } = render(ui());
    await flush();
    expect(container.querySelector('[data-testid="batch-runner"]')).toBeNull();
  });

  it('polls GET /batches/{id} every 2 s and updates; a terminal state stops the polling', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    api.getBatch.mockResolvedValueOnce(snapshot([item(0, 'HIMX', 'completed', { elapsed_s: 100 }), item(1, 'NVDA', 'running'), item(2, 'AAPL', 'pending')], { current_run: { run_id: 'r', phase: 'risk', steps_done: 9, elapsed_s: 60, since_last_event_s: 1, last_event_type: 'agent.message', cancelling: false } }));
    api.getBatch.mockResolvedValue(snapshot([item(0, 'HIMX', 'completed'), item(1, 'NVDA', 'completed'), item(2, 'AAPL', 'failed', { error: 'boom', error_code: 'run_error' })], { terminal_code: null }));
    render(ui());
    await flush();
    expect(api.getBatch).not.toHaveBeenCalled();
    await flush(2000);
    expect(api.getBatch).toHaveBeenCalledTimes(1);
    expect(api.getBatch).toHaveBeenCalledWith('b_1', expect.anything());
    expect(screen.getByTestId('batch-current-phase').textContent).toBe('風險評估');
    expect(screen.getByTestId('batch-current-steps').textContent).toMatch(/9/);
    await flush(2000);
    expect(api.getBatch).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('batch-status').textContent).toBe('批次已完成');
    expect(screen.queryByTestId('batch-current')).toBeNull();
    await flush(60_000);
    expect(api.getBatch).toHaveBeenCalledTimes(2);            // terminal: no more polling
    expect(api.cancelBatch).not.toHaveBeenCalled();
  });

  it('errors back off 2 → 5 → 10 → 10 s with a retry notice, and the notice clears on the next success', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    const t0 = Date.now();
    const times: number[] = [];
    api.getBatch.mockImplementation(async () => { times.push(Date.now() - t0); throw new BatchApiError(0, null, 'offline'); });
    render(ui());
    await flush();
    await flush(2000);                                          // 1st poll fails
    expect(screen.getByTestId('batch-poll-error').textContent).toMatch(/暫時無法取得進度，重試中/);
    await flush(2000 + 5000 + 10000 + 10000);
    expect(times).toEqual([2000, 4000, 9000, 19000, 29000]);
    api.getBatch.mockImplementation(async () => RUNNING());
    await flush(10000);
    expect(screen.queryByTestId('batch-poll-error')).toBeNull();
    const before = api.getBatch.mock.calls.length;
    await flush(2000);
    expect(api.getBatch.mock.calls.length).toBe(before + 1);    // back to the 2 s cadence
  });

  it('returning to the foreground re-reads immediately, without waiting for the timer', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    api.getBatch.mockResolvedValue(RUNNING());
    render(ui());
    await flush();
    expect(api.getBatch).not.toHaveBeenCalled();
    setVisibility('hidden');
    await flush();
    expect(api.listBatches).toHaveBeenCalledTimes(1);           // hidden: nothing extra
    setVisibility('visible');
    await flush();
    expect(api.listBatches).toHaveBeenCalledTimes(2);           // foreground always re-reads the latest batch
  });

  it('an older (slow) response never overwrites a newer state', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    const mk = (steps: number) => snapshot([item(0, 'HIMX', 'completed'), item(1, 'NVDA', 'running'), item(2, 'AAPL', 'pending')], { current_run: { run_id: 'r', phase: 'debate', steps_done: steps, elapsed_s: 1, since_last_event_s: 1, last_event_type: null, cancelling: false } });
    let slow!: (v: unknown) => void;
    api.getBatch.mockImplementationOnce(() => new Promise((r) => { slow = r; }));   // issued first, answers last
    render(ui());
    await flush();
    await flush(2000);                                          // poll #1 in flight
    api.listBatches.mockResolvedValueOnce([mk(9)]);             // issued second (foreground), answers first
    setVisibility('visible');                                   // poll #2
    await flush();
    expect(screen.getByTestId('batch-current-steps').textContent).toMatch(/9/);
    await act(async () => { slow(mk(2)); });
    await flush();
    expect(screen.getByTestId('batch-current-steps').textContent).toMatch(/9/);     // not rolled back to 2
  });

  it('unmounting aborts the in-flight read and never cancels the batch', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    let signal: AbortSignal | undefined;
    api.getBatch.mockImplementation((_id: string, s: AbortSignal) => { signal = s; return new Promise(() => {}); });
    const { unmount } = render(ui());
    await flush();
    await flush(2000);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
    await flush(30_000);
    expect(api.cancelBatch).not.toHaveBeenCalled();
    expect(api.getBatch).toHaveBeenCalledTimes(1);              // timers are gone too
  });
});

describe('display', () => {
  it('failed rows are not shown as done; completed rows link to the report; cost partial/unknown is marked', async () => {
    api.listBatches.mockResolvedValue([snapshot([
      item(0, 'HIMX', 'completed', { session_id: 's1', stance: 'moderately_bullish', conviction: 0.65, estimated_cost_usd: 0.123, cost_known: true, elapsed_s: 95 }),
      item(1, 'NVDA', 'failed', { error: 'RateLimitError: slow down', error_code: 'run_error', elapsed_s: 7, estimated_cost_usd: 0.01, cost_known: true, cost_partial: true }),
      item(2, 'AAPL', 'skipped'),
      item(3, 'MSFT', 'interrupted'),
    ], { state: 'stopped', terminal_code: 'cost_guard_blocked', cost: { known_usd: 0.133, unknown_items: 0, partial: true } })]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-row-state-HIMX').textContent).toBe('完成');
    expect(screen.getByTestId('batch-row-NVDA').getAttribute('data-state')).toBe('failed');
    expect(screen.getByTestId('batch-row-state-NVDA').textContent).toMatch(/^失敗：/);
    expect(screen.getByTestId('batch-row-state-NVDA').textContent).not.toMatch(/完成/);
    expect(screen.getByTestId('batch-row-NVDA').textContent).toMatch(/部分/);
    expect(screen.getByTestId('batch-row-state-AAPL').textContent).toBe('已略過');
    expect(screen.getByTestId('batch-row-state-MSFT').textContent).toBe('已中斷');
    expect(screen.getByTestId('batch-view-HIMX').getAttribute('href')).toBe('#history');
    expect(screen.queryByTestId('batch-view-NVDA')).toBeNull();
    expect(screen.getByTestId('batch-row-HIMX').textContent).toMatch(/65%/);
    expect(screen.getByTestId('batch-status').textContent).toBe('批次已提前停止');
    expect(screen.getByTestId('batch-terminal-reason').textContent).toMatch(/成本守門/);
    expect(screen.getByTestId('batch-summary-cost').textContent).toMatch(/\$0\.133.*部分或未知/);
    expect(screen.getByTestId('batch-summary').textContent).toMatch(/已完成 1 \/ 4/);
    expect(screen.getByTestId('batch-summary').textContent).toMatch(/失敗：1/);
    expect(screen.queryByTestId('batch-stop-button')).toBeNull();
  });

  it('current_index null shows "準備中", never "第 NaN/1 檔"', async () => {
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'pending'), item(1, 'NVDA', 'pending')], { state: 'queued', current_index: null, current_run: null })]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-status').textContent).toBe('準備中…');
    expect(screen.getByTestId('batch-current').getAttribute('data-state')).toBe('preparing');
    expect(text()).not.toMatch(/NaN|null|undefined/);
  });

  it('waiting_slot says it waits for another analysis; a quiet run (> 120 s) is flagged as possibly stuck', async () => {
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'waiting_slot')], { current_run: { run_id: 'r', phase: 'waiting_slot', steps_done: 0, elapsed_s: 3, since_last_event_s: 3, last_event_type: null, cancelling: false } })]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-current-waiting').textContent).toMatch(/等待其他分析結束後開始/);
    expect(screen.queryByTestId('batch-current-stuck')).toBeNull();
    cleanup();
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'running')], { current_run: { run_id: 'r', phase: 'debate', steps_done: 4, elapsed_s: 400, since_last_event_s: 130, last_event_type: 'agent.message', cancelling: false } })]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-current-stuck').textContent).toMatch(/可能卡住/);
  });

  it('shows an estimated remaining time only after a finished item (average × remaining)', async () => {
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'running'), item(1, 'NVDA', 'pending')])]);
    render(ui());
    await flush();
    expect(screen.queryByTestId('batch-eta')).toBeNull();
    cleanup();
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'completed', { elapsed_s: 100 }), item(1, 'NVDA', 'completed', { elapsed_s: 200 }), item(2, 'AAPL', 'running'), item(3, 'MSFT', 'pending')])]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-eta').textContent).toMatch(/約 5 分 0 秒/);     // avg 150 s × 2 left
  });

  it('English locale renders English strings', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    render(<I18nProvider initialLocale="en"><BatchRunner tickers={[]} watchlistEmpty /></I18nProvider>);
    await flush();
    expect(screen.getByTestId('batch-status').textContent).toBe('Running 2 of 3');
    expect(screen.getByTestId('batch-hint').textContent).toMatch(/close this page/);
    expect(screen.getByTestId('batch-current-phase').textContent).toBe('Debate');
  });
});

describe('foreground return always re-reads the latest batch (D62)', () => {
  it('a finished batch on screen: a new batch started elsewhere shows up on return and locks the selection', async () => {
    const seen: boolean[] = [];
    api.listBatches.mockResolvedValueOnce([snapshot([item(0, 'HIMX', 'completed')])]);
    render(ui({ onRunningChange: (r) => seen.push(r), watchlistEmpty: false }));
    await flush();
    expect(screen.getByTestId('batch-status').textContent).toBe('批次已完成');
    await flush(60_000);
    expect(api.listBatches).toHaveBeenCalledTimes(1);           // terminal: no routine polling
    expect(seen[seen.length - 1]).toBe(false);
    api.listBatches.mockResolvedValue([{ ...RUNNING(), batch_id: 'b_new' }]);
    setVisibility('visible');
    await flush();
    expect(screen.getByTestId('batch-current').textContent).toMatch(/NVDA/);
    expect(seen[seen.length - 1]).toBe(true);                   // selection locked
    api.getBatch.mockResolvedValue({ ...RUNNING(), batch_id: 'b_new' });
    await flush(2000);
    expect(api.getBatch).toHaveBeenLastCalledWith('b_new', expect.anything());   // and followed from now on
  });

  it('an empty screen (no batch at all) picks up a batch started elsewhere on return', async () => {
    api.listBatches.mockResolvedValueOnce([]);
    const { container } = render(ui());
    await flush();
    expect(container.querySelector('[data-testid="batch-runner"]')).toBeNull();
    await flush(60_000);
    expect(api.listBatches).toHaveBeenCalledTimes(1);
    api.listBatches.mockResolvedValue([RUNNING()]);
    setVisibility('visible');
    await flush();
    expect(screen.getByTestId('batch-current')).toBeTruthy();
  });

  it('shows the invalid_credential stop reason in both languages', async () => {
    const done = snapshot([item(0, 'HIMX', 'skipped')], { state: 'stopped', terminal_code: 'invalid_credential' });
    api.listBatches.mockResolvedValue([done]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-terminal-reason').textContent).toMatch(/憑證格式不符.*檢查設定後重跑/);
    cleanup();
    render(<I18nProvider initialLocale="en"><BatchRunner tickers={[]} watchlistEmpty /></I18nProvider>);
    await flush();
    expect(screen.getByTestId('batch-terminal-reason').textContent).toMatch(/not in a valid format/);
  });
});

describe('view report handoff (D59)', () => {
  it('clicking "查看報告" stores only that session id for History, and keeps href #history', async () => {
    sessionStorage.clear();
    api.listBatches.mockResolvedValue([snapshot([item(0, 'HIMX', 'completed', { session_id: 'sess_himx' }), item(1, 'NVDA', 'completed', { session_id: 'sess_nvda' })])]);
    render(ui());
    await flush();
    const link = screen.getByTestId('batch-view-NVDA');
    expect(link.getAttribute('href')).toBe('#history');
    expect(sessionStorage.getItem('tal:pending-history-session')).toBeNull();
    fireEvent.click(link);
    expect(sessionStorage.getItem('tal:pending-history-session')).toBe('sess_nvda');
  });
});

describe('clear', () => {
  it('only a finished batch can be cleared; the choice survives a remount; an active batch has no clear button', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    const { unmount } = render(ui({ watchlistEmpty: false }));
    await flush();
    expect(screen.queryByTestId('batch-clear-button')).toBeNull();
    unmount();
    const done = snapshot([item(0, 'HIMX', 'completed')]);
    api.listBatches.mockResolvedValue([done]);
    render(ui());
    await flush();
    fireEvent.click(screen.getByTestId('batch-clear-button'));
    expect(screen.queryByTestId('batch-runner')).toBeNull();   // empty watchlist + hidden batch → nothing
    cleanup();
    api.listBatches.mockResolvedValue([done]);
    const { container } = render(ui());
    await flush();
    expect(container.querySelector('[data-testid="batch-runner"]')).toBeNull();   // still hidden after reopening
    cleanup();
    api.listBatches.mockResolvedValue([{ ...done, batch_id: 'b_2' }]);            // a new batch replaces it
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-runner')).toBeTruthy();
  });

  it('an active batch is never hidden, even if its id was cleared before', async () => {
    localStorage.setItem('tal:batch:hidden', 'b_1');
    api.listBatches.mockResolvedValue([RUNNING()]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-current')).toBeTruthy();
  });
});

describe('stop', () => {
  it('sends cancel once, shows "停止中…" only after it succeeded, and the button cannot fire twice', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    let resolve!: (v: unknown) => void;
    api.cancelBatch.mockImplementation(() => new Promise((r) => { resolve = r; }));
    render(ui());
    await flush();
    const btn = () => screen.getByTestId('batch-stop-button') as HTMLButtonElement;
    fireEvent.click(btn());
    fireEvent.click(btn());                                     // second click while the request is in flight
    await flush();
    expect(api.cancelBatch).toHaveBeenCalledTimes(1);
    expect(btn().textContent).toBe('停止批次');                  // not "stopping" before the server confirmed
    await act(async () => { resolve({ ...RUNNING(), cancel_requested: true }); });
    await flush();
    expect(btn().textContent).toBe('停止中…');
    expect(btn().disabled).toBe(true);
    expect(screen.getByTestId('batch-status').textContent).toBe('停止中…');
    fireEvent.click(btn());
    await flush();
    expect(api.cancelBatch).toHaveBeenCalledTimes(1);
    expect(api.cancelBatch).toHaveBeenCalledWith('b_1');
  });

  it('a failed stop restores the button and can be sent again (same batch id)', async () => {
    api.listBatches.mockResolvedValue([RUNNING()]);
    api.cancelBatch.mockRejectedValueOnce(new BatchApiError(0, null, 'offline'));
    api.cancelBatch.mockResolvedValueOnce({ ...RUNNING(), cancel_requested: true });
    render(ui());
    await flush();
    const btn = () => screen.getByTestId('batch-stop-button') as HTMLButtonElement;
    fireEvent.click(btn());
    await flush();
    expect(screen.getByTestId('batch-stop-error').textContent).toMatch(/再按一次/);
    expect(btn().textContent).toBe('停止批次');
    expect(btn().disabled).toBe(false);
    fireEvent.click(btn());
    await flush();
    expect(api.cancelBatch).toHaveBeenCalledTimes(2);
    expect(api.cancelBatch.mock.calls.map((c) => c[0])).toEqual(['b_1', 'b_1']);
    expect(btn().textContent).toBe('停止中…');
    expect(screen.queryByTestId('batch-stop-error')).toBeNull();
  });

  it('a batch that already has cancel_requested (another tab pressed Stop) shows stopping on load', async () => {
    api.listBatches.mockResolvedValue([{ ...RUNNING(), cancel_requested: true }]);
    render(ui());
    await flush();
    expect(screen.getByTestId('batch-stop-button').textContent).toBe('停止中…');
    expect(api.cancelBatch).not.toHaveBeenCalled();
  });
});

describe('start', () => {
  const tickers = ['NVDA', 'AAPL', 'NVDA'];

  it('posts the deduped tickers + a start template with no ticker / reservation, then shows the new batch', async () => {
    api.listBatches.mockResolvedValue([]);
    api.createBatch.mockResolvedValue(snapshot([item(0, 'NVDA', 'pending'), item(1, 'AAPL', 'pending')], { state: 'queued', current_index: null, current_run: null }));
    render(ui({ tickers, watchlistEmpty: false }));
    await flush();
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await flush();
    expect(api.createBatch).toHaveBeenCalledTimes(1);
    const arg = api.createBatch.mock.calls[0][0];
    expect(arg.tickers).toEqual(['NVDA', 'AAPL']);
    expect(arg.client_batch_key).toMatch(/^[A-Za-z0-9_-]{8,80}$/);
    expect(arg.start).toMatchObject({ trade_date: expect.any(String), engine: 'pro' });
    expect(arg.start).not.toHaveProperty('ticker');
    expect(arg.start).not.toHaveProperty('reservation_id');
    expect(screen.getByTestId('batch-status').textContent).toBe('準備中…');
    expect(screen.getByTestId('batch-stop-button')).toBeTruthy();
  });

  it('a network failure keeps the key: the same click retried sends the SAME client_batch_key', async () => {
    api.listBatches.mockResolvedValue([]);
    api.createBatch.mockRejectedValueOnce(new BatchApiError(0, null, 'offline'));
    api.createBatch.mockResolvedValueOnce(snapshot([item(0, 'NVDA', 'pending')], { state: 'queued', current_index: null, current_run: null }));
    render(ui({ tickers: ['NVDA'], watchlistEmpty: false }));
    await flush();
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await flush();
    expect(screen.getByTestId('batch-start-msg').textContent).toMatch(/無法啟動批次/);
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await flush();
    expect(api.createBatch.mock.calls[1][0].client_batch_key).toBe(api.createBatch.mock.calls[0][0].client_batch_key);
  });

  it('409 batch_active loads and shows the existing batch instead of an error', async () => {
    api.listBatches.mockResolvedValue([]);
    api.createBatch.mockRejectedValue(new BatchApiError(409, 'batch_active', 'busy', 'b_exist'));
    api.getBatch.mockResolvedValue({ ...RUNNING(), batch_id: 'b_exist' });
    render(ui({ tickers: ['NVDA'], watchlistEmpty: false }));
    await flush();
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await flush();
    expect(api.getBatch).toHaveBeenCalledWith('b_exist');
    expect(screen.getByTestId('batch-current').textContent).toMatch(/NVDA/);
    expect(screen.getByTestId('batch-start-msg').textContent).toMatch(/已有批次正在執行/);
    await flush(2000);
    expect(api.getBatch).toHaveBeenLastCalledWith('b_exist', expect.anything());   // and it is followed from now on
  });

  it.each([
    ['no_provider', 400, /尚未設定 LLM 供應商/],
    ['invalid_batch', 400, /引擎拒絕了這次批次要求/],
    ['secret_ref_rejected', 400, /引擎拒絕了這次批次要求/],
    ['idempotency_conflict', 409, /不同設定送出/],
    ['storage_unavailable', 503, /無法儲存批次/],
  ])('%s shows its own message', async (code, status, re) => {
    api.listBatches.mockResolvedValue([]);
    api.createBatch.mockRejectedValue(new BatchApiError(status, code, 'x'));
    render(ui({ tickers: ['NVDA'], watchlistEmpty: false }));
    await flush();
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await flush();
    expect(screen.getByTestId('batch-start-msg').textContent).toMatch(re);
  });

  it('reports a server-side active batch to the caller so the selection is locked', async () => {
    const seen: boolean[] = [];
    api.listBatches.mockResolvedValue([RUNNING()]);
    render(ui({ onRunningChange: (r) => seen.push(r), watchlistEmpty: false }));
    await flush();
    expect(seen[seen.length - 1]).toBe(true);
    api.getBatch.mockResolvedValue(snapshot([item(0, 'HIMX', 'completed')]));
    await flush(2000);
    expect(seen[seen.length - 1]).toBe(false);
  });
});
