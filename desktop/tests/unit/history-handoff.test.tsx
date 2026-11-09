// @vitest-environment happy-dom
// D59: History opens the session a batch row handed off; with nothing handed off it behaves as before.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

const api = vi.hoisted(() => ({ listSessions: vi.fn(), getSession: vi.fn() }));
vi.mock('../../src/lib/engine-client', async () => ({ ...(await vi.importActual<any>('../../src/lib/engine-client')), ...api }));

import History from '../../src/pages/History';
import { I18nProvider } from '../../src/i18n';
import { consumePendingSession, setPendingSession } from '../../src/lib/handoff';

beforeEach(() => {
  sessionStorage.clear();
  api.listSessions.mockReset().mockResolvedValue([]);
  api.getSession.mockReset();
});
afterEach(cleanup);
const mount = () => render(<I18nProvider initialLocale="zh-TW"><History /></I18nProvider>);

describe('pending session handoff', () => {
  it('set/consume round-trips once and clears the key', () => {
    setPendingSession('s9');
    expect(consumePendingSession()).toBe('s9');
    expect(consumePendingSession()).toBeNull();
  });

  it('History mount opens the handed-off session via getSession and clears the handoff', async () => {
    api.getSession.mockResolvedValue({ id: 'sess_1', ticker: 'NVDA', trade_date: '2026-11-09', decision_action: 'neutral', decision_confidence: 0.5, decision_reasoning: 'r', live: true, events: [], created_at: '2026-11-09T00:00:00Z' });
    setPendingSession('sess_1');
    mount();
    await waitFor(() => expect(api.getSession).toHaveBeenCalledWith('sess_1'));
    expect(sessionStorage.getItem('tal:pending-history-session')).toBeNull();
  });

  it('an unavailable session shows the existing error text, not a crash', async () => {
    api.getSession.mockRejectedValue(new Error('session not found: sess_x'));
    setPendingSession('sess_x');
    mount();
    await waitFor(() => expect(document.body.textContent).toMatch(/session not found/));
  });

  it('with nothing handed off, History stays on the list and loads no session', async () => {
    mount();
    await waitFor(() => expect(api.listSessions).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(api.getSession).not.toHaveBeenCalled();
    expect(screen.queryByTestId('history-detail')).toBeNull();
  });
});
