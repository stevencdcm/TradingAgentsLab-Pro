// @vitest-environment happy-dom
// Taiwan research disclosures: engine label, usage split, Dexter findings in the screen, Markdown and HTML exports.
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

afterEach(cleanup);
import DebateStream from '../../src/components/DebateStream';
import { I18nProvider } from '../../src/i18n';
import type { DebateEvent } from '../../src/lib/engine-client';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';
import { buildTranscriptMarkdown } from '../../src/lib/transcript';

const events = [
  { type: 'session.start', ticker: '2330', trade_date: '2026-10-29' },
  { type: 'evidence.pack', security_id: 'TW:2330', snapshot_id: 'abcdef0123456789', as_of: '2026-10-29T16:30:00+08:00', coverage: {}, unknowns: ['還原股價缺'] },
  { type: 'dexter.result', status: 'succeeded', reason: null, unknowns: [], usage: { measured: true, total_tokens: 4500 },
    findings: [
      { id: 'F1', claim: '收盤 2415', verification: 'verified', point_in_time: 'ok', citations: [{ kind: 'price', period: '2026-10-29' }] },
      { id: 'F2', claim: 'EPS 99', verification: 'unverified', reason: 'number 99 not found', point_in_time: 'unknown', citations: [{ kind: 'fin_is_bs_cf', period: '2026-06-30', field: 'EPS' }] },
    ] },
  { type: 'session.complete', ticker: '2330', trade_date: '2026-10-29', live: true, market: 'TW', engine: 'simplified',
    decision: { stance: 'neutral', conviction: 0.5, reasoning: 'ok' }, usage_complete: false, cost_complete: false,
    usage_breakdown: { debate: { input_tokens: 1000, output_tokens: 200 }, dexter: { input_tokens: 4000, output_tokens: 500, measured: true } } },
] as unknown as DebateEvent[];

describe('Taiwan research disclosures', () => {
  it('screen / History replay shows the simplified-engine label, usage split and incomplete warnings', () => {
    render(<I18nProvider><DebateStream events={events} isStreaming={false} /></I18nProvider>);
    const txt = screen.getByTestId('research-notes').textContent ?? '';
    expect(txt).toMatch(/simplified debate engine|簡化辯論引擎/);
    expect(txt).toMatch(/1,200|1200/);
    expect(txt).toMatch(/4,500|4500/);
    expect(txt).toMatch(/not measured|未能量測/);
    expect(txt).toMatch(/incomplete|不完整/);
  });

  it('Markdown export carries the snapshot, gaps, every finding with its verdict, and the engine label', () => {
    const md = buildTranscriptMarkdown(events, 'en');
    expect(md).toContain('abcdef012345');
    expect(md).toContain('還原股價缺');
    expect(md).toMatch(/✓ 收盤 2415 \[price 2026-10-29\]/);
    expect(md).toMatch(/✗ EPS 99 \[fin_is_bs_cf 2026-06-30 EPS\].*number 99 not found/);
    expect(md).toMatch(/simplified debate engine/);
  });

  it('HTML export carries the same sections', () => {
    const { html } = buildTranscriptHtml(events, 'zh-TW');
    expect(html).toContain('abcdef012345');
    expect(html).toContain('EPS 99');
    expect(html).toContain('簡化辯論引擎');
  });
});
