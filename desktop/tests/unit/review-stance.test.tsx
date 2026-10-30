// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import DebateStream from '../../src/components/DebateStream';
import { isReview, normalizeStance, stanceLabel, stanceLean } from '../../src/lib/stance';
import type { DebateEvent } from '../../src/lib/engine-client';

afterEach(cleanup);

describe('review (unparseable rating) is its own state, not a made-up neutral', () => {
  it('normalises and labels it', () => {
    expect(normalizeStance('review')).toBe('review');
    expect(normalizeStance('REVIEW')).toBe('review');
    expect(normalizeStance('garbage')).toBe('neutral'); // unchanged legacy behaviour
    expect(isReview('review')).toBe(true);
    expect(isReview('neutral')).toBe(false);
    expect(stanceLean('review')).toBe('neutral');
    expect(stanceLabel('review')).toMatch(/Needs review/);
  });

  it('decision card says so, hides conviction and explains', () => {
    const events = [
      { type: 'session.start', ticker: 'AAPL', trade_date: '2026-10-30' },
      {
        type: 'session.complete',
        ticker: 'AAPL',
        trade_date: '2026-10-30',
        decision: { stance: 'review', conviction: 0, reasoning: '資料不足', rating_status: 'unparsed' },
      },
    ] as unknown as DebateEvent[];
    render(
      <I18nProvider initialLocale="zh-TW">
        <DebateStream events={events} isStreaming={false} />
      </I18nProvider>,
    );
    expect(screen.getByTestId('decision-stance').textContent).toBe('無法解析評級（需人工檢視）');
    expect(screen.getByTestId('decision-review-note').textContent).toMatch(/不宣稱任何結論/);
    expect(screen.queryByText('0%')).toBeNull();
  });
});

import { buildTranscriptMarkdown } from '../../src/lib/transcript';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';

describe('exports and batch rows do not show a fake 0% for review', () => {
  const events = [
    { type: 'session.start', ticker: 'AAPL', trade_date: '2026-10-30' },
    { type: 'session.complete', ticker: 'AAPL', trade_date: '2026-10-30',
      decision: { stance: 'review', conviction: 0, bull_strength: 50, bear_strength: 50, reasoning: 'x', rating_status: 'unparsed' } },
  ] as unknown as DebateEvent[];

  it('markdown: no percentage, explicit note, no bull/bear scores', () => {
    const md = buildTranscriptMarkdown(events, 'zh-TW');
    expect(md).toContain('無法解析評級');
    expect(md).not.toMatch(/0%/);
    expect(md).not.toContain('多方論點');
  });

  it('html: review class, note, no conviction percentage', () => {
    const html = buildTranscriptHtml(events, 'zh-TW').html;
    expect(html).toContain('class="action review"');
    expect(html).toContain('不宣稱任何結論');
    expect(html).not.toMatch(/信心度 0%/);
  });
});
