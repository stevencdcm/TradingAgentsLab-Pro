/**
 * The engine's official-API price report (`session.complete.cost_report`, or the history detail's `cost_report`).
 *
 * It is an ESTIMATE from OpenAI's published list prices applied to measured token usage - never an invoice. The
 * budget guard's own (conservative, higher) ceilings are a different thing and are not touched here. One formatter
 * feeds the on-screen panel, the Markdown export and the HTML export so they can never disagree.
 */
import type { MessageKey } from '../i18n';

export interface CostReportLine {
  stage: string;
  agent: string | null;
  provider: string | null;
  model: string | null;
  calls: number;
  input_tokens: number;
  cached_input_tokens: number | null;
  uncached_input_tokens: number | null;
  cache_write_tokens: number;
  output_tokens: number;
  reasoning_tokens: number | null;
  usd: number | null;
  usd_basis: 'official_api_estimate' | 'api_equivalent_reference' | 'local_no_external_api_fee' | null;
  unpriced_reason: string | null;
  missing: string[];
}

export interface CostReport {
  schema: string;
  basis: 'metered' | 'recomputed_current_list_price';
  billing: string;
  run_state: string;
  price: { version: string; verified_at: string; sources: string[] };
  lines: CostReportLine[];
  totals: {
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number | null;
    cache_write_tokens: number;
    complete: boolean;
    incomplete_reasons: string[];
    api_estimate_usd: number | null;
    api_equivalent_reference_usd: number | null;
    external_api_usd: number | null;
  };
  notes: string[];
}

type T = (key: MessageKey, vars?: Record<string, string | number>) => string;
type Fmt = (n: number) => string;

const KNOWN_REASONS = new Set([
  'run_cancelled_usage_is_partial', 'run_failed_usage_is_partial', 'run_interrupted_usage_is_partial',
  'run_running_usage_is_partial', 'cached_input_tokens', 'reasoning_tokens', 'usage_not_measured',
  'no_official_price', 'official_price_not_imported_for_provider', 'long_context_price_not_listed',
  'unsupported_service_tier', 'tier_not_listed', 'model_split_and_cache_split_unknown_in_saved_session',
  'quick_model_tokens_priced_at_deep_model',
]);

export function formatUsd(v: number): string {
  return `$${v.toFixed(v >= 1 ? 2 : 4)}`;
}

export function reasonText(raw: string, t: T): string {
  const code = raw.includes(':') ? raw.slice(raw.indexOf(':') + 1) : raw;
  const stage = raw.includes(':') ? raw.slice(0, raw.indexOf(':')) : '';
  const text = KNOWN_REASONS.has(code) ? t(`costReport.reason.${code}` as MessageKey) : code;
  return stage === 'dexter' ? `Dexter: ${text}` : text;
}

export interface FormattedRow {
  agent: string;
  model: string;
  input: string;
  cached: string;
  write: string;
  output: string;
  reasoning: string;
  usd: string;
}

export interface FormattedCostReport {
  title: string;
  billingLine: string;
  totalLines: string[];
  statusLine: string;
  reasons: string[];
  basisLine: string | null;
  rows: FormattedRow[];
  sourceLine: string;
  notes: string[];
}

/** Everything the UI and both exports print, as plain strings. */
export function formatCostReport(r: CostReport, t: T, fmt: Fmt): FormattedCostReport {
  const unknown = t('costReport.unknown');
  const billingKey = (['api_key', 'oauth', 'local'].includes(r.billing) ? r.billing : 'other') as 'api_key' | 'oauth' | 'local' | 'other';
  const tot = r.totals;
  const totalLines: string[] = [];
  if (r.billing === 'api_key') {
    totalLines.push(
      tot.api_estimate_usd === null
        ? t('costReport.total.estimateUnknown')
        : t('costReport.total.estimate', { usd: formatUsd(tot.api_estimate_usd) }),
    );
  } else if (r.billing === 'oauth') {
    totalLines.push(
      tot.api_equivalent_reference_usd === null
        ? t('costReport.total.referenceNone')
        : t('costReport.total.reference', { usd: formatUsd(tot.api_equivalent_reference_usd) }),
    );
  } else if (r.billing === 'local') {
    totalLines.push(t('costReport.total.local'));
  } else {
    totalLines.push(t('costReport.total.estimateUnknown'));
  }
  const rows: FormattedRow[] = r.lines.map((ln) => {
    const who = ln.stage === 'dexter' ? t('costReport.stage.dexter') : (ln.agent ?? t('costReport.stage.debate'));
    const usd =
      ln.usd === null
        ? t('costReport.unpriced')
        : ln.usd_basis === 'api_equivalent_reference'
          ? `${formatUsd(ln.usd)} (${t('costReport.reference')})`
          : formatUsd(ln.usd);
    return {
      agent: who,
      model: ln.model ?? unknown,
      input: ln.uncached_input_tokens === null ? `${fmt(ln.input_tokens)}*` : fmt(ln.uncached_input_tokens),
      cached: ln.cached_input_tokens === null ? unknown : fmt(ln.cached_input_tokens),
      write: fmt(ln.cache_write_tokens),
      output: fmt(ln.output_tokens),
      reasoning: ln.reasoning_tokens === null ? unknown : fmt(ln.reasoning_tokens),
      usd,
    };
  });
  const notes = [t('costReport.note.reasoning'), t('costReport.note.ceiling')];
  if (r.notes.includes('gpt-5.6-sol_promotional_price_through_2026-11-21')) notes.push(t('costReport.note.promo'));
  return {
    title: t('costReport.title'),
    billingLine: t(`costReport.billing.${billingKey}` as MessageKey),
    totalLines,
    statusLine: tot.complete ? t('costReport.complete') : t('costReport.incomplete'),
    reasons: tot.complete ? [] : tot.incomplete_reasons.map((x) => reasonText(x, t)),
    basisLine: r.basis === 'recomputed_current_list_price' ? t('costReport.basis.recomputed') : null,
    rows,
    sourceLine: t('costReport.source', { version: r.price.version, date: r.price.verified_at, sources: r.price.sources.join(' · ') }),
    notes,
  };
}

/** Markdown block for the transcript export. */
export function costReportMarkdown(r: CostReport, t: T, fmt: Fmt): string[] {
  const f = formatCostReport(r, t, fmt);
  const out = [`## ${f.title}`, '', f.billingLine, '', ...f.totalLines.map((x) => `- ${x}`), `- ${f.statusLine}`, ...f.reasons.map((x) => `  - ${x}`)];
  if (f.basisLine) out.push(`- ${f.basisLine}`);
  out.push('');
  if (f.rows.length) {
    out.push(
      `| ${[t('costReport.col.agent'), t('costReport.col.model'), t('costReport.col.input'), t('costReport.col.cached'), t('costReport.col.write'), t('costReport.col.output'), t('costReport.col.reasoning'), t('costReport.col.usd')].join(' | ')} |`,
      '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
      ...f.rows.map((x) => `| ${[x.agent, x.model, x.input, x.cached, x.write, x.output, x.reasoning, x.usd].join(' | ')} |`),
      '',
    );
  }
  out.push(`_${f.sourceLine}_`, '', ...f.notes.map((x) => `_${x}_`), '');
  return out;
}

/** Standalone-HTML block for the HTML export (`esc` is the exporter's own HTML escaper). */
export function costReportHtml(r: CostReport, t: T, fmt: Fmt, esc: (s: string) => string): string {
  const f = formatCostReport(r, t, fmt);
  const cell = (v: string, left = false) => `<td style="padding:3px 8px;text-align:${left ? 'left' : 'right'};white-space:nowrap;border-bottom:1px solid #8884">${esc(v)}</td>`;
  const head = [t('costReport.col.agent'), t('costReport.col.model'), t('costReport.col.input'), t('costReport.col.cached'), t('costReport.col.write'), t('costReport.col.output'), t('costReport.col.reasoning'), t('costReport.col.usd')];
  const out = [`<section class="cost-report" data-testid="cost-report"><h4>${esc(f.title)}</h4>`, `<p>${esc(f.billingLine)}</p>`];
  for (const l of f.totalLines) out.push(`<p><b>${esc(l)}</b></p>`);
  out.push(`<p>${esc(f.statusLine)}</p>`);
  if (f.reasons.length) out.push(`<ul>${f.reasons.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`);
  if (f.basisLine) out.push(`<p>${esc(f.basisLine)}</p>`);
  if (f.rows.length) {
    out.push('<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%"><thead><tr>' + head.map((h) => `<th style="padding:3px 8px;text-align:left">${esc(h)}</th>`).join('') + '</tr></thead><tbody>');
    for (const x of f.rows) out.push(`<tr>${cell(x.agent, true)}${cell(x.model, true)}${cell(x.input)}${cell(x.cached)}${cell(x.write)}${cell(x.output)}${cell(x.reasoning)}${cell(x.usd)}</tr>`);
    out.push('</tbody></table></div>');
  }
  out.push(`<p class="meta">${esc(f.sourceLine)}</p>`, ...f.notes.map((n) => `<p class="meta">${esc(n)}</p>`), '</section>');
  return out.join('');
}
