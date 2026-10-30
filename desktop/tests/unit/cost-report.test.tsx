// @vitest-environment happy-dom
// Official-API price report: panel, history detail, Markdown + HTML export. Fake reports only; no engine, no network.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import CostReportPanel from '../../src/components/CostReportPanel';
import DebateStream from '../../src/components/DebateStream';
import { I18nProvider, makeTranslator } from '../../src/i18n';
import { buildTranscriptMarkdown } from '../../src/lib/transcript';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';
import { costReportMarkdown, formatCostReport, type CostReport, type CostReportLine } from '../../src/lib/cost-report';
import type { DebateEvent } from '../../src/lib/engine-client';

afterEach(cleanup);

const line = (o: Partial<CostReportLine>): CostReportLine => ({
  stage: 'debate', agent: 'market_analyst', provider: 'openai', model: 'gpt-6.1-sol', calls: 2, input_tokens: 10_000,
  cached_input_tokens: 4_000, uncached_input_tokens: 6_000, cache_write_tokens: 0, output_tokens: 800, reasoning_tokens: 300,
  usd: 0.0201, usd_basis: 'official_api_estimate', unpriced_reason: null, missing: [], ...o,
});
const report = (o: Partial<CostReport> = {}, t: Partial<CostReport['totals']> = {}): CostReport => ({
  schema: 'tal.cost-report/1', basis: 'metered', billing: 'api_key', run_state: 'completed',
  price: { version: 'openai-api-2026-10-31', verified_at: '2026-10-31', sources: ['https://developers.openai.com/api/docs/pricing', 'https://openai.com/api/pricing'] },
  lines: [line({}), line({ stage: 'dexter', agent: 'dexter', model: 'gpt-6-luna', usd: 0.0005 })],
  totals: { input_tokens: 20_000, output_tokens: 1_600, cached_input_tokens: 8_000, cache_write_tokens: 0, complete: true, incomplete_reasons: [],
    api_estimate_usd: 0.0206, api_equivalent_reference_usd: null, external_api_usd: null, ...t },
  notes: [], ...o,
});
const wrap = (el: React.ReactElement, l: 'zh-TW' | 'en' = 'zh-TW') => render(<I18nProvider initialLocale={l}>{el}</I18nProvider>);

describe('CostReportPanel', () => {
  it('shows the API estimate, per-agent + Dexter rows, price version, date and both sources', () => {
    wrap(<CostReportPanel report={report()} />);
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/API 價格估算（官方牌價）：\$0\.0206/);
    expect(screen.getByTestId('cost-report-status').textContent).toMatch(/完整/);
    const text = screen.getByTestId('cost-report').textContent ?? '';
    expect(text).toMatch(/market_analyst/); expect(text).toMatch(/Dexter/); expect(text).toMatch(/gpt-6-luna/);
    expect(text).toMatch(/openai-api-2026-10-31/); expect(text).toMatch(/2026-10-31/);
    expect(text).toMatch(/developers\.openai\.com\/api\/docs\/pricing/); expect(text).toMatch(/openai\.com\/api\/pricing/);
    expect(text).toMatch(/推理 token 已包含在輸出內/);
  });
  it('an unknown model total is "unknown", never $0, and the incomplete reason is spelled out', () => {
    wrap(<CostReportPanel report={report({ lines: [line({ usd: null, usd_basis: null, unpriced_reason: 'no_official_price', model: 'gpt-x' })] },
      { api_estimate_usd: null, complete: false, incomplete_reasons: ['debate:no_official_price'] })} />);
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/未知/);
    expect(screen.getByTestId('cost-report-total').textContent).not.toMatch(/\$0/);
    expect(screen.getByTestId('cost-report-status').textContent).toMatch(/不完整/);
    expect(screen.getByTestId('cost-report').textContent).toMatch(/沒有官方 API 價格/);
  });
  it('OAuth: subscription wording, API-equivalent reference only, never an estimate or a percentage', () => {
    wrap(<CostReportPanel report={report({ billing: 'oauth', lines: [line({ usd_basis: 'api_equivalent_reference' })] },
      { api_estimate_usd: null, api_equivalent_reference_usd: 0.0201 })} />);
    const text = screen.getByTestId('cost-report').textContent ?? '';
    expect(text).toMatch(/ChatGPT 訂閱——不適用額外 API 計費/);
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/API 等值參考：\$0\.0201/);
    expect(text).toMatch(/不是你實際被收的帳單/); expect(text).not.toMatch(/官方牌價/); expect(text).not.toMatch(/%/);
  });
  it('local: external fee $0 with electricity explicitly not counted', () => {
    wrap(<CostReportPanel report={report({ billing: 'local', lines: [line({ provider: 'local', usd: 0, usd_basis: 'local_no_external_api_fee' })] },
      { api_estimate_usd: null, external_api_usd: 0 })} />);
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/外部 API 費用：\$0.*電費.*不代表為零/);
  });
  it('a cancelled run is flagged partial', () => {
    wrap(<CostReportPanel report={report({ run_state: 'cancelled' }, { complete: false, incomplete_reasons: ['run_cancelled_usage_is_partial'] })} />);
    expect(screen.getByTestId('cost-report').textContent).toMatch(/已取消，只計入目前量測到的用量/);
  });
  it('unreported cache/reasoning fields render as unknown, not 0', () => {
    wrap(<CostReportPanel report={report({ lines: [line({ cached_input_tokens: null, uncached_input_tokens: null, reasoning_tokens: null, missing: ['cached_input_tokens'] })] },
      { complete: false, incomplete_reasons: ['debate:cached_input_tokens'] })} />);
    const cells = Array.from(document.querySelectorAll('tbody td')).map((c) => c.textContent);
    expect(cells[2]).toBe('10,000*'); expect(cells[3]).toBe('未知'); expect(cells[6]).toBe('未知');
  });
  it('legacy session: labelled as a recomputation at today\'s list price', () => {
    wrap(<CostReportPanel report={report({ basis: 'recomputed_current_list_price' }, { complete: false, incomplete_reasons: ['model_split_and_cache_split_unknown_in_saved_session'] })} />);
    expect(screen.getByTestId('cost-report').textContent).toMatch(/以今天的牌價重算.*不是當時實際收費.*沒有被更動/);
  });
  it('english copy', () => {
    wrap(<CostReportPanel report={report()} />, 'en');
    expect(screen.getByTestId('cost-report-total').textContent).toMatch(/API price estimate \(official list price\): \$0\.0206/);
  });
});

describe('where the report appears', () => {
  const events = (cr?: CostReport | null): DebateEvent[] => [
    { type: 'session.start', ticker: 'NVDA', trade_date: '2026-10-30' } as DebateEvent,
    { type: 'session.complete', ticker: 'NVDA', trade_date: '2026-10-30', live: true, provider: 'openai', model: 'gpt-6.1-sol',
      decision: { stance: 'bullish', conviction: 0.7, bull_strength: 60, bear_strength: 40, risk_level: 'moderate', reasoning: 'r' },
      estimated_cost_usd: null, cost_report: cr } as unknown as DebateEvent,
  ];
  it('completed analysis (stream) and history detail show the panel; history override is used for legacy rows', () => {
    wrap(<DebateStream events={events(report())} isStreaming={false} />);
    expect(screen.getByTestId('cost-report')).toBeTruthy();
    cleanup();
    wrap(<DebateStream events={events(null)} isStreaming={false} costReport={report({ basis: 'recomputed_current_list_price' }, { complete: false, incomplete_reasons: [] })} />);
    expect(screen.getByTestId('cost-report').textContent).toMatch(/以今天的牌價重算/);
    cleanup();
    wrap(<DebateStream events={events(null)} isStreaming={false} />);
    expect(screen.queryByTestId('cost-report')).toBeNull();
  });
  it('Markdown export lists totals, per-agent and Dexter rows, sources and the incomplete flag', () => {
    const md = buildTranscriptMarkdown(events(report({}, { complete: false, incomplete_reasons: ['dexter:usage_not_measured'] })), 'zh-TW');
    expect(md).toMatch(/## Token 用量與 API 價格估算/);
    expect(md).toMatch(/API 價格估算（官方牌價）：\$0\.0206/); expect(md).toMatch(/不完整/); expect(md).toMatch(/Dexter: 有階段執行了但沒有量測到用量/);
    expect(md).toMatch(/\| market_analyst \| gpt-6\.1-sol \|/); expect(md).toMatch(/\| Dexter \| gpt-6-luna \|/);
    expect(md).toMatch(/openai-api-2026-10-31/);
    expect(buildTranscriptMarkdown(events(null), 'zh-TW')).not.toMatch(/API 價格估算/);
    expect(buildTranscriptMarkdown(events(null), 'zh-TW', report({ basis: 'recomputed_current_list_price' }))).toMatch(/以今天的牌價重算/);
  });
  it('HTML export contains the same block, escaped', () => {
    const r = report({ lines: [line({ model: '<script>x</script>' })] });
    const { html } = buildTranscriptHtml(events(r), 'zh-TW');
    expect(html).toMatch(/data-testid="cost-report"/); expect(html).toMatch(/API 價格估算（官方牌價）/);
    expect(html).not.toMatch(/<script>x<\/script>/); expect(html).toMatch(/&lt;script&gt;/);
    expect(buildTranscriptHtml(events(null), 'zh-TW').html).not.toMatch(/cost-report/);
  });
  it('formatter and markdown block agree', () => {
    const t = makeTranslator('en'); const fmt = (n: number) => String(n);
    const f = formatCostReport(report(), t, fmt);
    expect(costReportMarkdown(report(), t, fmt).join('\n')).toContain(f.totalLines[0]);
  });
});
