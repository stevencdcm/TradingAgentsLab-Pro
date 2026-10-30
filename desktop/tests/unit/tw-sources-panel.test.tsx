// @vitest-environment happy-dom
// Settings > Data providers: Taiwan sources show limits and OBSERVED state; nothing is "connected" without a real success.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ listTwSources: vi.fn(), testTwSource: vi.fn() }));
vi.mock('../../src/lib/engine-client', () => api);

import TwSourcesPanel from '../../src/components/TwSourcesPanel';
import { I18nProvider } from '../../src/i18n';

const src = (o: object) => ({
  id: 'finmind_free', name: 'FinMind Free（匿名）', official_docs: ['https://finmind.github.io/Pricing/'], data_types: [],
  free_scope: '個股四碼；損益表等', token_requirement: '匿名無需 token', limits: 'Free 匿名 300 次／小時；非商業用途', update_cadence: 'unknown',
  license_notes: '不得再散布原始資料', verified_at: '2026-10-30', verification_scope: '官方文件與樣本', implementation_status: 'wired', state: 'wired_untested',
  used_by: ['Pro 完整圖'], last_ok_at: null, last_error: null, last_error_at: null, calls_observed: 0, token_configured: false, ...o,
});

beforeEach(() => {
  api.listTwSources.mockResolvedValue([
    src({}),
    src({ id: 'mops_historical_disclosures', name: 'MOPS 歷史重大訊息', implementation_status: 'unsupported', state: 'not_implemented', used_by: [], token_configured: null, limits: 'unknown' }),
  ]);
  api.testTwSource.mockResolvedValue({ ok: true, ms: 120, rows: 5 });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const mount = () => render(<I18nProvider><TwSourcesPanel tokenSaved={false} /></I18nProvider>);

describe('TwSourcesPanel', () => {
  it('shows scope, token need, the real limit, cadence caveat and docs; untested is not "connected"', async () => {
    mount();
    const card = await screen.findByTestId('tw-source-finmind_free');
    expect(card.getAttribute('data-state')).toBe('wired_untested');
    expect(card.textContent).toMatch(/300 次／小時|300 requests/);
    expect(card.textContent).toMatch(/非商業|non-commercial/i);
    expect(card.textContent).toMatch(/未儲存 token|no token saved/);
    expect(card.textContent).toMatch(/不要假設為即時|do not assume real-time|來源未說明/);
    expect(screen.getByTestId('tw-source-last-finmind_free').textContent).toMatch(/尚未觀察到成功|never observed/);
    expect(card.querySelector('a[href="https://finmind.github.io/Pricing/"]')).toBeTruthy();
  });

  it('an unsupported source is listed as not implemented and has no test button', async () => {
    mount();
    const card = await screen.findByTestId('tw-source-mops_historical_disclosures');
    expect(card.getAttribute('data-state')).toBe('not_implemented');
    expect(screen.queryByTestId('tw-source-test-mops_historical_disclosures')).toBeNull();
  });

  it('a real test result is displayed and the list is reloaded with the observed state', async () => {
    mount();
    await screen.findByTestId('tw-source-finmind_free');
    api.listTwSources.mockResolvedValue([src({ state: 'connected', last_ok_at: '2026-10-30T09:00:00Z', calls_observed: 1 }), ]);
    fireEvent.click(screen.getByTestId('tw-source-test-finmind_free'));
    await waitFor(() => expect(api.testTwSource).toHaveBeenCalledWith('finmind_free'));
    await waitFor(() => expect(screen.getByTestId('tw-source-finmind_free').getAttribute('data-state')).toBe('connected'));
    expect(screen.getByTestId('tw-source-last-finmind_free').textContent).not.toMatch(/尚未觀察到成功|never observed/);
  });

  it('failures show the reason and do not leave a green state', async () => {
    api.testTwSource.mockResolvedValue({ ok: false, error: 'access_denied', http_status: 403 });
    api.listTwSources.mockResolvedValue([src({ state: 'failed', last_error: 'access_denied' })]);
    mount();
    const card = await screen.findByTestId('tw-source-finmind_free');
    expect(card.getAttribute('data-state')).toBe('failed');
    fireEvent.click(screen.getByTestId('tw-source-test-finmind_free'));
    expect(await screen.findByText(/access_denied.*403|403.*access_denied/)).toBeTruthy();
  });

  it('a load error is shown, not an empty list', async () => {
    api.listTwSources.mockRejectedValue(new Error('engine down'));
    mount();
    expect((await screen.findByRole('alert')).textContent).toMatch(/engine down/);
  });
});
