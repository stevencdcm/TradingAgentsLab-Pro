// @vitest-environment happy-dom
// Watchlist selective batch: only ticked tickers are posted (default none); the queue itself runs in the engine (D56), so these
// tests fake the /batches calls. No models, no network.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { item, snapshot } from './helpers/batch-snapshot';

const api = vi.hoisted(() => ({
  listWatchlist: vi.fn(),
  addWatchlist: vi.fn(),
  removeWatchlist: vi.fn(),
  searchTwSymbols: vi.fn(),
  listBatches: vi.fn(),
  getBatch: vi.fn(),
  createBatch: vi.fn(),
  cancelBatch: vi.fn(),
}));
vi.mock('../../src/lib/engine-client', async (orig) => ({ ...(await orig<typeof import('../../src/lib/engine-client')>()), ...api }));
vi.mock('../../src/lib/secrets', () => ({ listSecrets: async () => [{ key: 'llm:anthropic' }] }));
vi.mock('../../src/lib/oauth', () => ({ getOpenAIOAuthStatus: async () => ({ connected: false }) }));
vi.mock('../../src/lib/run-analysis', () => ({
  buildRunStart: async () => ({
    template: { trade_date: '2026-11-09', provider_config: { provider: 'anthropic', auth: { type: 'api_key', api_key: 'k' }, model: 'm' }, engine: 'pro' },
    reserveReq: null,
    maxTokens: 400,
  }),
}));

import Watchlist from '../../src/pages/Watchlist';
import { I18nProvider } from '../../src/i18n';

const row = (ticker: string, extra = {}) => ({ ticker, added_at: new Date().toISOString(), note: null, market: null, name: null, ...extra });
const LIST = [row('2330', { market: 'TWSE', name: '台積電' }), row('AAPL'), row('6488', { market: 'TPEx', name: '環球晶' }), row('NVDA')];

const queueOf = (tickers: string[], states: Array<'pending' | 'running' | 'completed'>) => snapshot(tickers.map((t, i) => item(i, t, states[i])));
const setVisible = () => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }); document.dispatchEvent(new Event('visibilitychange')); };

beforeEach(() => {
  api.listBatches.mockResolvedValue([]);
  api.getBatch.mockReset(); api.createBatch.mockReset(); api.cancelBatch.mockReset();
  try { localStorage.clear(); } catch { /* ignore */ }
  api.listWatchlist.mockResolvedValue(LIST);
  api.removeWatchlist.mockResolvedValue(undefined);
  api.addWatchlist.mockImplementation(async (i: { ticker: string }) => row(i.ticker));
  api.searchTwSymbols.mockResolvedValue({ ok: true, results: [], as_of: null, fetched_at: 'x', count: 0, stale: false, refresh_error: null });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const mount = async () => {
  const utils = render(<I18nProvider><Watchlist /></I18nProvider>);
  await screen.findByTestId('watchlist-row-2330');
  return utils;
};
const box = (t: string) => screen.getByTestId(`watchlist-select-${t}`) as HTMLInputElement;
const runBtn = () => screen.getByTestId('batch-run-button') as HTMLButtonElement;
const all = () => screen.getByTestId('watchlist-select-all') as HTMLInputElement;

describe('Watchlist selective batch', () => {
  it('starts with nothing selected: run is disabled and says how many (0)', async () => {
    await mount();
    for (const t of ['2330', 'AAPL', '6488', 'NVDA']) expect(box(t).checked).toBe(false);
    expect(runBtn().disabled).toBe(true);
    expect(runBtn().textContent).toMatch(/0/);
    expect(screen.getByTestId('watchlist-select-count').textContent).toMatch(/0.*4/);
  });

  it('each checkbox has its own accessible name; select-all, partial and clear states', async () => {
    await mount();
    expect(screen.getByLabelText(/2330/, { selector: 'input' })).toBe(box('2330'));
    fireEvent.click(box('AAPL'));
    expect(all().indeterminate).toBe(true);
    expect(all().checked).toBe(false);
    expect(screen.getByTestId('watchlist-select-count').textContent).toMatch(/1.*4/);
    fireEvent.click(all());                                   // partial -> select all
    expect(all().checked).toBe(true);
    expect(all().indeterminate).toBe(false);
    expect(runBtn().textContent).toMatch(/4/);
    fireEvent.click(all());                                   // all -> none
    expect(all().checked).toBe(false);
    expect(runBtn().disabled).toBe(true);
    fireEvent.click(box('NVDA'));
    fireEvent.click(screen.getByTestId('watchlist-select-clear'));
    expect(box('NVDA').checked).toBe(false);
    expect((screen.getByTestId('watchlist-select-clear') as HTMLButtonElement).disabled).toBe(true);
  });

  it('posts only the ticked Taiwan + US tickers, in list order, never the unticked ones', async () => {
    await mount();
    api.createBatch.mockResolvedValue(queueOf(['2330', '6488', 'NVDA'], ['running', 'pending', 'pending']));
    fireEvent.click(box('NVDA'));                             // ticked out of order on purpose
    fireEvent.click(box('2330'));
    fireEvent.click(box('6488'));
    expect(runBtn().textContent).toMatch(/3/);
    fireEvent.click(runBtn());
    await waitFor(() => expect(api.createBatch).toHaveBeenCalledTimes(1));
    expect(api.createBatch.mock.calls[0][0].tickers).toEqual(['2330', '6488', 'NVDA']);
    await waitFor(() => expect(screen.getByTestId('batch-row-NVDA')).toBeTruthy());
    expect(screen.queryByTestId('batch-row-AAPL')).toBeNull();
  });

  it('a running batch keeps its queue and totals when the list changes; selection is locked while it is active on the server', async () => {
    await mount();
    api.createBatch.mockResolvedValue(queueOf(['2330', 'AAPL'], ['running', 'pending']));
    fireEvent.click(box('2330')); fireEvent.click(box('AAPL'));
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await waitFor(() => expect(box('2330').disabled).toBe(true));
    expect(all().disabled).toBe(true);
    expect(screen.getByTestId('batch-runner').textContent).toMatch(/1.*2/);       // "1 / 2"
    // delete a queued ticker from the list mid-run: the server queue still has it and still counts 2
    fireEvent.click(screen.getByRole('button', { name: /AAPL/ }));
    await waitFor(() => expect(screen.queryByTestId('watchlist-row-AAPL')).toBeNull());
    expect(screen.getByTestId('batch-row-AAPL')).toBeTruthy();
    expect(screen.getByTestId('batch-runner').textContent).toMatch(/1.*2/);
    api.listBatches.mockResolvedValue([queueOf(['2330', 'AAPL'], ['completed', 'completed'])]);
    await act(async () => { setVisible(); });                 // foreground refresh picks up the finished batch
    await waitFor(() => expect(screen.getByTestId('batch-status').textContent).toBe('批次已完成'));
    expect(box('2330').disabled).toBe(false);                 // unlocked again after the batch
  });

  it('a removed ticker leaves the selection; a newly added one is not auto-selected', async () => {
    await mount();
    fireEvent.click(box('NVDA')); fireEvent.click(box('AAPL'));
    fireEvent.click(screen.getByRole('button', { name: /NVDA/ }));
    await waitFor(() => expect(screen.queryByTestId('watchlist-row-NVDA')).toBeNull());
    expect(screen.getByTestId('watchlist-select-count').textContent).toMatch(/1.*3/);
    fireEvent.change(screen.getByTestId('watchlist-ticker-input'), { target: { value: 'NVDA' } });
    fireEvent.click(screen.getByTestId('watchlist-add-button'));
    await screen.findByTestId('watchlist-row-NVDA');
    expect(box('NVDA').checked).toBe(false);                  // removed once, re-added later: not ticked
    expect(screen.getByTestId('watchlist-select-count').textContent).toMatch(/1.*4/);
  });

  it('leaving the page does NOT cancel the batch; reopening restores it from the server, even with an empty list', async () => {
    api.createBatch.mockResolvedValue(queueOf(['2330', 'AAPL', '6488', 'NVDA'], ['running', 'pending', 'pending', 'pending']));
    const { unmount } = await mount();
    fireEvent.click(all());
    fireEvent.click(runBtn());
    await waitFor(() => expect(screen.getByTestId('batch-row-2330')).toBeTruthy());
    unmount();
    await new Promise((r) => setTimeout(r, 20));
    expect(api.cancelBatch).not.toHaveBeenCalled();
    expect(api.createBatch).toHaveBeenCalledTimes(1);
    cleanup();
    // reopen with an EMPTY watchlist: the engine still reports the active batch
    api.listWatchlist.mockResolvedValue([]);
    api.listBatches.mockResolvedValue([queueOf(['2330', 'AAPL', '6488', 'NVDA'], ['completed', 'running', 'pending', 'pending'])]);
    render(<I18nProvider><Watchlist /></I18nProvider>);
    await waitFor(() => expect(screen.getByTestId('batch-current').textContent).toMatch(/AAPL/));
    expect(screen.getByTestId('batch-status').textContent).toMatch(/2.*4/);
    expect(api.createBatch).toHaveBeenCalledTimes(1);
    expect(api.cancelBatch).not.toHaveBeenCalled();
  });

  it('an empty watchlist with no batch shows no batch card', async () => {
    api.listWatchlist.mockResolvedValue([]);
    render(<I18nProvider><Watchlist /></I18nProvider>);
    await waitFor(() => expect(api.listBatches).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId('batch-runner')).toBeNull();
  });
});
