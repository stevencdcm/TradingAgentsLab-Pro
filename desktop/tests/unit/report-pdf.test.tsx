// @vitest-environment happy-dom
// On-demand report PDF button: nothing is built or sent before the click, one export at a time, errors can be retried,
// cancel aborts, and the document that is sent is the same report HTML (zh-TW text, cost report, disclaimers). No engine.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ReportPdfButton from '../../src/components/ReportPdfButton';
import { I18nProvider } from '../../src/i18n';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';
import type { DebateEvent } from '../../src/lib/engine-client';
import type { CostReport } from '../../src/lib/cost-report';

const savePdf = vi.fn();
const setBridge = (withPdf: boolean) => {
  (window as unknown as { tradingAgentsLab: unknown }).tradingAgentsLab = { transcript: { openHtml: vi.fn(), ...(withPdf ? { savePdf } : {}) } };
};
beforeEach(() => { savePdf.mockReset(); setBridge(true); });
afterEach(cleanup);

const mount = (build: () => Promise<{ html: string; suggestedName: string }>, locale: 'zh-TW' | 'en' = 'zh-TW') =>
  render(<I18nProvider initialLocale={locale}><ReportPdfButton build={build} /></I18nProvider>);
const doc = { html: '<html><body>報告</body></html>', suggestedName: '2330-2026-10-31' };
const deferred = <T,>() => { let res!: (v: T) => void; let rej!: (e: unknown) => void; const p = new Promise<T>((a, b) => { res = a; rej = b; }); return { p, res, rej }; };

describe('ReportPdfButton', () => {
  it('does nothing until clicked: no build, no request, even after mount and re-render', () => {
    const build = vi.fn(async () => doc);
    const { rerender } = mount(build);
    rerender(<I18nProvider initialLocale="zh-TW"><ReportPdfButton build={build} /></I18nProvider>);
    expect(screen.getByTestId('export-pdf').textContent).toBe('輸出 PDF');
    expect(build).not.toHaveBeenCalled();
    expect(savePdf).not.toHaveBeenCalled();
  });

  it('one click builds once and sends the document with the safe name; double click does not start a second export', async () => {
    const d = deferred<string>();
    savePdf.mockReturnValue(d.p);
    const build = vi.fn(async () => doc);
    mount(build);
    const btn = screen.getByTestId('export-pdf');
    fireEvent.click(btn); fireEvent.click(btn); fireEvent.click(btn);
    await waitFor(() => expect(savePdf).toHaveBeenCalledTimes(1));
    expect(build).toHaveBeenCalledTimes(1);
    expect(savePdf.mock.calls[0].slice(0, 2)).toEqual([doc.html, '2330-2026-10-31']);
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(btn.getAttribute('aria-busy')).toBe('true');
    expect(btn.textContent).toBe('製作 PDF 中…');
    await act(async () => { d.res('2330-2026-10-31.pdf'); });
    await waitFor(() => expect(screen.getByTestId('export-pdf-status').textContent).toBe('PDF 已下載'));
    expect((btn as HTMLButtonElement).disabled).toBe(false);
  });

  it('a failure shows a readable error, keeps the button usable, and a retry can succeed', async () => {
    savePdf.mockRejectedValueOnce(Object.assign(new Error('x'), { code: 'pdf_busy' })).mockResolvedValueOnce('a.pdf');
    mount(async () => doc);
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/另一份 PDF 正在製作/));
    expect(screen.getByTestId('export-pdf').textContent).toBe('重試輸出 PDF');
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(screen.getByTestId('export-pdf-status').textContent).toBe('PDF 已下載'));
    expect(savePdf).toHaveBeenCalledTimes(2);
  });

  it('unavailable renderer and unknown errors are explained, not silent', async () => {
    savePdf.mockRejectedValueOnce(Object.assign(new Error('x'), { code: 'pdf_unavailable' })).mockRejectedValueOnce(new Error('HTTP 502'));
    mount(async () => doc, 'en');
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/needs Chrome or Chromium/));
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/PDF export failed: HTTP 502/));
  });

  it('cancel aborts the request and returns to the idle button without an error', async () => {
    let signal: AbortSignal | undefined;
    savePdf.mockImplementation((_h: string, _n: string, s: AbortSignal) => new Promise((_r, rej) => { signal = s; s.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); }));
    mount(async () => doc);
    fireEvent.click(screen.getByTestId('export-pdf'));
    fireEvent.click(await screen.findByTestId('export-pdf-cancel'));
    await waitFor(() => expect(screen.getByTestId('export-pdf').textContent).toBe('輸出 PDF'));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('leaving the page aborts an export in flight', async () => {
    let signal: AbortSignal | undefined;
    savePdf.mockImplementation((_h: string, _n: string, s: AbortSignal) => { signal = s; return new Promise(() => {}); });
    const { unmount } = mount(async () => doc);
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(signal).toBeDefined());
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('is hidden where the bridge cannot make PDFs (Electron preload)', () => {
    setBridge(false);
    mount(async () => doc);
    expect(screen.queryByTestId('export-pdf')).toBeNull();
  });

  it('the exported document is the report HTML: zh-TW text, API price estimate, disclaimers (same as Open in HTML)', async () => {
    const events = [
      { type: 'session.start', ticker: '2330', trade_date: '2026-10-31' },
      { type: 'agent.message', phase: 'analysts', agent: 'market_analyst', content: '## 台積電\n\n| 指標 | 數值 |\n|---|---|\n| RSI | 46 |' },
      { type: 'session.complete', decision: { stance: 'moderately_bullish', conviction: 0.7, bull_strength: 70, bear_strength: 40, risk_level: 'moderate', reasoning: '逢回承接', price_target: 1200, time_horizon: '3-6 個月' } },
    ] as unknown as DebateEvent[];
    const cr = { schema: 'tal.cost-report/1', basis: 'metered', billing: 'api_key', run_state: 'completed', price: { version: 'openai-api-2026-10-31', verified_at: '2026-10-31', sources: ['https://developers.openai.com/api/docs/pricing'] },
      lines: [{ stage: 'debate', agent: 'market_analyst', provider: 'openai', model: 'gpt-6.1-sol', calls: 1, input_tokens: 1000, cached_input_tokens: 0, uncached_input_tokens: 1000, cache_write_tokens: 0, output_tokens: 100, reasoning_tokens: 0, usd: 0.0201, usd_basis: 'official_api_estimate', unpriced_reason: null, missing: [] }],
      totals: { input_tokens: 1000, output_tokens: 100, cached_input_tokens: 0, cache_write_tokens: 0, complete: true, incomplete_reasons: [], api_estimate_usd: 0.0201, api_equivalent_reference_usd: null, external_api_usd: null }, notes: [] } as CostReport;
    savePdf.mockResolvedValue('x.pdf');
    mount(async () => buildTranscriptHtml(events, 'zh-TW', cr));
    fireEvent.click(screen.getByTestId('export-pdf'));
    await waitFor(() => expect(savePdf).toHaveBeenCalled());
    const [html, name] = savePdf.mock.calls[0];
    expect(name).toBe('2330-2026-10-31');
    const stamp = (h: string) => h.replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, 'T');       // only the generation time differs
    expect(stamp(html)).toBe(stamp(buildTranscriptHtml(events, 'zh-TW', cr).html));
    expect(html).toMatch(/台積電/); expect(html).toMatch(/\$0\.0201/); expect(html).toMatch(/openai-api-2026-10-31/);
    expect(html).toMatch(/不構成/);
    expect(html).not.toMatch(/<script/i);                      // the engine refuses active content, the report never has any
    expect(html).not.toMatch(/<link\b|<iframe|<object|<embed|<base\b/i);
  });
});
