// @vitest-environment happy-dom
// Taiwan full graph: Dexter ran / did NOT run is always visible, and the fixed limitations block reaches the screen, Markdown and HTML.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import DebateStream from '../../src/components/DebateStream';
import { I18nProvider } from '../../src/i18n';
import type { DebateEvent } from '../../src/lib/engine-client';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';
import { buildTranscriptMarkdown } from '../../src/lib/transcript';

afterEach(cleanup);

const LIMITS = [
  { code: 'publication_time_unknown', text: '最新可得資料模式（截至 2026-10-30）：財報、月營收與新聞標題的公布時間未知，已標示。' },
  { code: 'news_headlines_only', text: '新聞只有標題、來源與連結，沒有內文或摘要。' },
  { code: 'dexter_skipped', text: 'Dexter：未執行（provider_not_supported），分析不含 Dexter 發現。' },
];
const events = (dexter: object) =>
  [
    { type: 'session.start', ticker: '2330', trade_date: '2026-10-30' },
    { type: 'dexter.result', findings: [], unknowns: [], ...dexter },
    { type: 'session.complete', ticker: '2330', trade_date: '2026-10-30', live: true, market: 'TW', tw_flow: 'full', data_mode: 'latest',
      decision: { stance: 'neutral', conviction: 0.5, reasoning: 'ok' }, limitations: LIMITS },
  ] as unknown as DebateEvent[];

describe('fixed data limitations + Dexter state (full graph)', () => {
  it('screen shows a limitations card and says Dexter did not run', () => {
    render(<I18nProvider initialLocale="zh-TW"><DebateStream events={events({ ran: false, status: 'unsupported', reason: 'provider_not_supported' })} isStreaming={false} /></I18nProvider>);
    const lim = screen.getByTestId('run-limitations');
    expect(lim.textContent).toMatch(/公布時間未知/);
    expect(lim.querySelectorAll('li').length).toBe(3);
    expect(screen.getByTestId('dexter-result').textContent).toMatch(/本次分析沒有執行 Dexter/);
  });

  it('Dexter that ran does not carry the not-run label', () => {
    render(<I18nProvider initialLocale="zh-TW"><DebateStream events={events({ ran: true, status: 'succeeded', reason: null })} isStreaming={false} /></I18nProvider>);
    expect(screen.getByTestId('dexter-result').textContent).not.toMatch(/沒有執行 Dexter/);
  });

  it('Markdown and HTML exports carry the same fixed block', () => {
    const ev = events({ ran: false, status: 'unsupported', reason: 'provider_not_supported' });
    const md = buildTranscriptMarkdown(ev, 'zh-TW');
    expect(md).toContain('資料限制');
    expect(md).toContain('公布時間未知');
    expect(md).toContain('本次分析沒有執行 Dexter');
    const { html } = buildTranscriptHtml(ev, 'zh-TW');
    expect(html).toContain('公布時間未知');
  });

  it('runs without limitations (non-Taiwan) show no card', () => {
    render(<I18nProvider initialLocale="zh-TW"><DebateStream events={[{ type: 'session.start', ticker: 'AAPL', trade_date: '2026-10-30' }] as unknown as DebateEvent[]} isStreaming={false} /></I18nProvider>);
    expect(screen.queryByTestId('run-limitations')).toBeNull();
  });
});
