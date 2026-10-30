// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import DebateStream from '../../src/components/DebateStream';
import type { DebateEvent } from '../../src/lib/engine-client';

afterEach(cleanup);
const base = [{ type: 'session.start', ticker: '2330', trade_date: '2026-10-29' }] as unknown as DebateEvent[];
const pack = { type: 'evidence.pack', security_id: 'TW:2330', snapshot_id: 'abcdef0123456789', as_of: '2026-10-29T16:30:00+08:00', coverage: {}, unknowns: ['還原股價缺'] } as unknown as DebateEvent;

function show(dexter: object) {
  render(
    <I18nProvider initialLocale="zh-TW">
      <DebateStream events={[...base, pack, { type: 'dexter.result', ...dexter } as unknown as DebateEvent]} isStreaming={false} />
    </I18nProvider>,
  );
}

describe('Taiwan evidence + Dexter result display', () => {
  it('shows the pack snapshot and its unknowns', () => {
    show({ status: 'succeeded', reason: null, findings: [], unknowns: [] });
    expect(screen.getByTestId('evidence-pack').textContent).toMatch(/abcdef012345/);
    expect(screen.getByTestId('evidence-pack').textContent).toMatch(/還原股價缺/);
  });

  it('verified finding with citation; failed Dexter says so plainly', () => {
    show({ status: 'succeeded', reason: null, usage: { measured: true, total_tokens: 1234 },
      findings: [{ id: 'F1', claim: '收盤 2415', verification: 'verified', point_in_time: 'unknown', citations: [{ kind: 'price', period: '2026-10-29' }] }] });
    const box = screen.getByTestId('dexter-result');
    expect(box.getAttribute('data-status')).toBe('succeeded');
    expect(box.textContent).toMatch(/✓ 收盤 2415/);
    expect(box.textContent).toMatch(/\[price 2026-10-29\]/);
    expect(box.textContent).toMatch(/公布時點未知/);
    expect(box.textContent).toMatch(/1,234 tokens/);
    cleanup();
    show({ status: 'failed', reason: 'max_iterations', findings: [], unknowns: [] });
    expect(screen.getByTestId('dexter-result').textContent).toMatch(/未完成 · max_iterations/);
  });

  it('unverified findings are marked and never shown as checked', () => {
    show({ status: 'partial', reason: 'no_verified_finding',
      findings: [{ id: 'F1', claim: '亂寫的數字', verification: 'unverified', reason: 'number 9999 not found in the cited items', point_in_time: 'unknown', citations: [{ kind: 'price', period: '2026-10-29' }] }] });
    const t = screen.getByTestId('dexter-result').textContent ?? '';
    expect(t).toMatch(/✗ 亂寫的數字/);
    expect(t).toMatch(/未通過驗證: number 9999/);
    expect(t).not.toMatch(/✓/);
  });
});
