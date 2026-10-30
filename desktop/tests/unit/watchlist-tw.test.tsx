// @vitest-environment happy-dom
// Watchlist: Taiwan code/name search from the official index, same add/remove/analyze flow as US tickers.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listWatchlist: vi.fn(),
  addWatchlist: vi.fn(),
  removeWatchlist: vi.fn(),
  searchTwSymbols: vi.fn(),
}));
vi.mock('../../src/lib/engine-client', () => api);
vi.mock('../../src/components/BatchRunner', () => ({ default: () => null }));

import Watchlist from '../../src/pages/Watchlist';
import { consumePendingTicker } from '../../src/lib/handoff';
import { I18nProvider } from '../../src/i18n';

const TSMC = { code: '2330', name: '台積電', market: 'TWSE', source: 'twse_companies' };
const GWC = { code: '6488', name: '環球晶', market: 'TPEx', source: 'tpex_companies' };
const ok = (results: unknown[], extra = {}) => ({ ok: true, results, as_of: '2026-10-29', fetched_at: 'x', count: 3000, stale: false, refresh_error: null, ...extra });

beforeEach(() => {
  api.listWatchlist.mockResolvedValue([]);
  api.addWatchlist.mockImplementation(async (i: { ticker: string; market?: string }) => ({ ticker: i.ticker, added_at: new Date().toISOString(), note: null, market: i.market ?? null, name: i.ticker === '2330' ? '台積電' : null }));
  api.removeWatchlist.mockResolvedValue(undefined);
  api.searchTwSymbols.mockImplementation(async (q: string) => ok(q === '6488' || q === '環球晶' ? [GWC] : [TSMC]));
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const mount = () => render(<I18nProvider><Watchlist /></I18nProvider>);
const type = (v: string) => fireEvent.change(screen.getByTestId('watchlist-ticker-input'), { target: { value: v } });

describe('Taiwan watchlist search', () => {
  it('a name search offers the official match with its market, picking it fills the code, Add stores code + market', async () => {
    mount();
    type('台積');
    const item = await screen.findByTestId('watchlist-suggestion-2330');
    expect(item.textContent).toMatch(/2330.*台積電.*(上市|Listed)/);
    expect(screen.getByTestId('watchlist-tw-box').textContent).toMatch(/2026-10-29/);        // source + exchange data date shown
    fireEvent.mouseDown(item);
    expect((screen.getByTestId('watchlist-ticker-input') as HTMLInputElement).value).toBe('2330');
    fireEvent.click(screen.getByTestId('watchlist-add-button'));
    await waitFor(() => expect(api.addWatchlist).toHaveBeenCalledWith({ ticker: '2330', market: 'TWSE', note: undefined }));
    expect(await screen.findByTestId('watchlist-row-2330')).toBeTruthy();
  });

  it('OTC stocks keep their own market (環球晶 = 上櫃), and a typed code resolves through the index', async () => {
    mount();
    type('6488');
    await screen.findByTestId('watchlist-suggestion-6488');
    fireEvent.click(screen.getByTestId('watchlist-add-button'));            // typed code, not clicked
    await waitFor(() => expect(api.addWatchlist).toHaveBeenCalledWith({ ticker: '6488', market: 'TPEx', note: undefined }));
  });

  it('keyboard: arrows + Enter select a suggestion', async () => {
    mount();
    type('2330');
    await screen.findByTestId('watchlist-suggestion-2330');
    fireEvent.keyDown(screen.getByTestId('watchlist-ticker-input'), { key: 'Enter' });
    expect((screen.getByTestId('watchlist-ticker-input') as HTMLInputElement).value).toBe('2330');
    expect(api.addWatchlist).not.toHaveBeenCalled();                       // Enter picked, did not submit
  });

  it('a typed Chinese name that was not picked is not guessed', async () => {
    api.searchTwSymbols.mockResolvedValue(ok([TSMC, { ...TSMC, code: '2331', name: '台積電甲' }]));
    mount();
    type('台積');
    await screen.findByTestId('watchlist-suggestion-2330');
    fireEvent.click(screen.getByTestId('watchlist-add-button'));
    expect(api.addWatchlist).not.toHaveBeenCalled();
    expect(await screen.findByText(/建議清單選擇|Choose a Taiwan stock/)).toBeTruthy();
  });

  it('index unavailable is shown as an error, not as "no results"', async () => {
    api.searchTwSymbols.mockResolvedValue({ ok: false, error: 'Taiwan symbol index unavailable and no cache exists' });
    mount();
    type('2330');
    const err = await screen.findByTestId('watchlist-tw-error');
    expect(err.textContent).toMatch(/不會猜測|Nothing was guessed/);
  });

  it('a stale cache is labelled with the failed refresh', async () => {
    api.searchTwSymbols.mockResolvedValue(ok([TSMC], { stale: true, refresh_error: 'ConnectError' }));
    mount();
    type('2330');
    await screen.findByTestId('watchlist-suggestion-2330');
    expect(screen.getByTestId('watchlist-tw-box').textContent).toMatch(/ConnectError/);
  });

  it('US tickers never call the Taiwan index and keep the old flow', async () => {
    mount();
    type('NVDA');
    await new Promise((r) => setTimeout(r, 400));
    expect(api.searchTwSymbols).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('watchlist-add-button'));
    await waitFor(() => expect(api.addWatchlist).toHaveBeenCalledWith({ ticker: 'NVDA', market: undefined, note: undefined }));
  });

  it('Analyze hands the bare Taiwan code to the analysis page; remove works like a US row', async () => {
    api.listWatchlist.mockResolvedValue([{ ticker: '2330', added_at: new Date().toISOString(), note: null, market: 'TWSE', name: '台積電' }, { ticker: 'AAPL', added_at: new Date().toISOString(), note: null }]);
    mount();
    const row = await screen.findByTestId('watchlist-row-2330');
    expect(row.textContent).toMatch(/台積電/);
    expect(row.textContent).toMatch(/上市|Listed/);
    expect(screen.getByTestId('watchlist-row-AAPL').textContent).not.toMatch(/上市|上櫃|Listed|OTC/);   // legacy US row untouched
    fireEvent.click(row.querySelector('button')!);                          // first button = Analyze
    expect(consumePendingTicker()).toBe('2330');
    fireEvent.click(screen.getAllByRole('button', { name: /移除|Remove/ })[0]);
    await waitFor(() => expect(api.removeWatchlist).toHaveBeenCalled());
  });
});
