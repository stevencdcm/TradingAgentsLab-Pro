// @vitest-environment happy-dom
// Watchlist selective batch: only ticked tickers run, default none, a running queue is frozen. No models are called:
// runAnalysis, the secret store and OAuth status are fakes.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listWatchlist: vi.fn(),
  addWatchlist: vi.fn(),
  removeWatchlist: vi.fn(),
  searchTwSymbols: vi.fn(),
}));
vi.mock('../../src/lib/engine-client', async (orig) => ({ ...(await orig<typeof import('../../src/lib/engine-client')>()), ...api }));
vi.mock('../../src/lib/secrets', () => ({ listSecrets: async () => [{ key: 'llm:anthropic' }] }));
vi.mock('../../src/lib/oauth', () => ({ getOpenAIOAuthStatus: async () => ({ connected: false }) }));

const run = vi.hoisted(() => ({ calls: [] as string[], release: [] as Array<() => void>, closed: 0 }));
vi.mock('../../src/lib/run-analysis', () => ({
  runAnalysis: async (opts: { ticker: string }) => {
    run.calls.push(opts.ticker);
    let finish!: () => void;
    const done = new Promise<void>((r) => { finish = r; });
    run.release.push(finish);
    return { kind: 'started', handle: { done, close: () => { run.closed += 1; finish(); } } };
  },
}));

import Watchlist from '../../src/pages/Watchlist';
import { I18nProvider } from '../../src/i18n';

const row = (ticker: string, extra = {}) => ({ ticker, added_at: new Date().toISOString(), note: null, market: null, name: null, ...extra });
const LIST = [row('2330', { market: 'TWSE', name: '台積電' }), row('AAPL'), row('6488', { market: 'TPEx', name: '環球晶' }), row('NVDA')];

beforeEach(() => {
  run.calls = []; run.release = []; run.closed = 0;
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
const settle = async (n = 1) => { for (let i = 0; i < n; i++) { await waitFor(() => expect(run.release.length).toBeGreaterThan(i)); await act(async () => { run.release[i](); }); } };

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

  it('runs only the ticked Taiwan + US tickers, in list order, never the unticked ones', async () => {
    await mount();
    fireEvent.click(box('NVDA'));                             // ticked out of order on purpose
    fireEvent.click(box('2330'));
    fireEvent.click(box('6488'));
    expect(runBtn().textContent).toMatch(/3/);
    fireEvent.click(runBtn());
    await settle(3);
    await waitFor(() => expect(screen.getByTestId('batch-runner').textContent).toMatch(/3.*3/));
    expect(run.calls).toEqual(['2330', '6488', 'NVDA']);
    expect(screen.queryByTestId('batch-row-AAPL')).toBeNull();
  });

  it('a running batch keeps its queue and totals when the list changes; selection is locked meanwhile', async () => {
    await mount();
    fireEvent.click(box('2330')); fireEvent.click(box('AAPL'));
    fireEvent.click(screen.getByTestId('batch-run-button'));
    await waitFor(() => expect(run.calls).toEqual(['2330']));
    expect(box('2330').disabled).toBe(true);
    expect(all().disabled).toBe(true);
    expect(screen.getByTestId('batch-runner').textContent).toMatch(/1.*2/);       // "1 of 2"
    // delete a queued ticker from the list mid-run: the frozen queue still runs it and still counts 2
    fireEvent.click(screen.getByRole('button', { name: /AAPL/ }));
    await waitFor(() => expect(screen.queryByTestId('watchlist-row-AAPL')).toBeNull());
    expect(screen.getByTestId('batch-row-AAPL')).toBeTruthy();
    expect(screen.getByTestId('batch-runner').textContent).toMatch(/1.*2/);
    await settle(1);
    await waitFor(() => expect(run.calls).toEqual(['2330', 'AAPL']));
    await settle(2);
    await waitFor(() => expect(screen.getByTestId('batch-runner').textContent).toMatch(/2.*2/));   // summary "2 of 2"
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

  it('stop and leaving the page still cancel the batch (no remount, no extra runs)', async () => {
    const { unmount } = await mount();
    fireEvent.click(all());
    fireEvent.click(runBtn());
    await waitFor(() => expect(run.calls).toEqual(['2330']));
    unmount();
    await waitFor(() => expect(run.closed).toBe(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(run.calls).toEqual(['2330']);
  });
});
