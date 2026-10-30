// @vitest-environment happy-dom
// Whole-run duration badge: the same stored value in result, History and exports; old rows say "unknown".
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import DebateStream from '../../src/components/DebateStream';
import { I18nProvider, makeTranslator } from '../../src/i18n';
import type { DebateEvent } from '../../src/lib/engine-client';
import { formatRunDuration } from '../../src/lib/duration';
import { buildTranscriptMarkdown } from '../../src/lib/transcript';

afterEach(cleanup);
const zh = makeTranslator('zh-TW');
const en = makeTranslator('en');
const ev = (elapsed?: number) => [
  { type: 'session.start', ticker: '2330', trade_date: '2026-10-30' },
  { type: 'session.complete', ticker: '2330', trade_date: '2026-10-30', live: true, provider: 'anthropic', model: 'claude-haiku-4-5',
    decision: { stance: 'neutral', conviction: 0.5, reasoning: 'ok' }, ...(elapsed === undefined ? {} : { elapsed_s: elapsed }) },
] as unknown as DebateEvent[];

describe('formatRunDuration', () => {
  it('seconds, minutes and hours, in both languages', () => {
    expect(formatRunDuration(45, zh)).toBe('耗時 45 秒');
    expect(formatRunDuration(85, zh)).toBe('耗時 1 分 25 秒');
    expect(formatRunDuration(3725, zh)).toBe('耗時 1 小時 2 分');
    expect(formatRunDuration(2125, en)).toBe('took 35 min 25 s');
  });
  it('null / missing / invalid is unknown, never zero', () => {
    for (const v of [null, undefined, NaN, -1]) expect(formatRunDuration(v as number, zh)).toBe('耗時未知');
  });
});

describe('where it shows', () => {
  it('result badge shows the stored whole-run time after the model', () => {
    render(<I18nProvider><DebateStream events={ev(85)} isStreaming={false} /></I18nProvider>);
    const badge = screen.getByTestId('decision-duration');
    expect(badge.textContent).toMatch(/1 分 25 秒|1 min 25 s/);
    expect(badge.parentElement!.textContent).toMatch(/claude-haiku-4-5.*(1 分 25 秒|1 min 25 s)/);
  });
  it('an old session without a recorded duration says unknown (no guess from created_at)', () => {
    render(<I18nProvider><DebateStream events={ev()} isStreaming={false} /></I18nProvider>);
    expect(screen.getByTestId('decision-duration').textContent).toMatch(/耗時未知|duration unknown/);
  });
  it('exports carry the same value', () => {
    expect(buildTranscriptMarkdown(ev(85), 'zh-TW')).toContain('耗時 1 分 25 秒');
    expect(buildTranscriptMarkdown(ev(), 'zh-TW')).toContain('耗時未知');
  });
});
