// @vitest-environment happy-dom
// The socket state is shown separately from the model's progress: a dropped connection is not "the model stopped".
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import RunStatus from '../../src/components/RunStatus';
import { I18nProvider } from '../../src/i18n';
import type { RunClock } from '../../src/lib/run-status';

afterEach(cleanup);
const base: RunClock = { startedAt: Date.now() - 90_000, endedAt: null, lastEventAt: Date.now() - 20_000, lastHeartbeatAt: Date.now() - 2000, heartbeat: null, streamError: null };

const show = (c: Partial<RunClock>, streaming = true) =>
  render(<I18nProvider initialLocale="zh-TW"><RunStatus events={[]} clock={{ ...base, ...c }} streaming={streaming} provider="openai" model="gpt-6.1-sol" /></I18nProvider>);

describe('RunStatus connection line', () => {
  it('says the run was re-attached after a reload', () => {
    show({ resumed: true, connection: 'connected' });
    expect(screen.getByTestId('run-status-connection').textContent).toMatch(/重新整理頁面不會中斷/);
  });
  it('a reconnecting socket is explained as a page problem while the model keeps running', () => {
    show({ connection: 'reconnecting' });
    const t = screen.getByTestId('run-status-connection').textContent ?? '';
    expect(t).toMatch(/重新連線/); expect(t).toMatch(/不是模型停了/);
    expect(screen.getByTestId('run-status-model').textContent).toMatch(/gpt-6.1-sol/);
  });
  it('offline tells the user to reload, not that the analysis failed', () => {
    show({ connection: 'offline', streamError: 'stream closed with code 1006' });
    expect(screen.getByTestId('run-status-connection').textContent).toMatch(/可能仍在引擎上執行/);
  });
  it('no connection line for a normal connected run', () => {
    show({ connection: 'connected' });
    expect(screen.queryByTestId('run-status-connection')).toBeNull();
  });

  it('cancelling and partial-usage notes', () => {
    show({ cancelling: true });
    expect(screen.getByTestId('run-status-cancelling').textContent).toMatch(/取消中/);
    cleanup();
    show({ usagePartial: true }, false);
    expect(screen.getByTestId('run-status-usage-partial').textContent).toMatch(/不是最終數字/);
  });
});
