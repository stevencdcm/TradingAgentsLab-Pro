// @vitest-environment happy-dom
// History list/detail cost: a subscription's budget figure (0) is never shown as a cost. Fake sessions only.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

let sessions: any[] = [];
vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return { ...actual, listSessions: vi.fn(async () => sessions), getSession: vi.fn(async () => null), deleteSession: vi.fn(async () => true) };
});

import History from '../../src/pages/History';
import { I18nProvider, makeTranslator } from '../../src/i18n';
import { historyCost, type CostSummary } from '../../src/lib/cost-report';

const base = { decision_action: 'neutral', decision_confidence: 0.5, decision_reasoning: 'r', live: true, input_tokens: 165248, output_tokens: 17087,
  created_at: '2026-10-31T00:20:07Z', trade_date: '2026-10-30', elapsed_s: 100 };
const cs = (o: Partial<CostSummary>): CostSummary => ({ billing: 'api_key', basis: 'metered', complete: true, api_estimate_usd: null, api_equivalent_reference_usd: null,
  external_api_usd: null, price_version: 'openai-api-2026-10-31', verified_at: '2026-10-31', ...o });
const t = makeTranslator('zh-TW');

beforeEach(() => {});
afterEach(cleanup);
const mount = () => render(<I18nProvider initialLocale="zh-TW"><History /></I18nProvider>);

describe('historyCost()', () => {
  it('QCOM-shaped legacy OAuth row: reference at today\'s list price, labelled not-billed + recomputed + incomplete; never $0', () => {
    const c = historyCost({ ...base, auth_kind: 'oauth', estimated_cost_usd: 0, cost_summary: cs({ billing: 'oauth', basis: 'recomputed_current_list_price', complete: false, api_equivalent_reference_usd: 0.501366 }) }, t);
    expect(c.kind).toBe('reference'); expect(c.usd).toBe(0.501366);
    expect(c.text).toMatch(/訂閱 · API 等值參考 \$0\.5014（非帳單）/); expect(c.text).toMatch(/不完整/); expect(c.text).toMatch(/以今日牌價重算/);
    expect(c.text).not.toMatch(/預估|\$0\.0000/);
  });
  it('OAuth without an exact official price (or no digest at all) is unknown, not $0', () => {
    for (const cost_summary of [cs({ billing: 'oauth', complete: false, api_equivalent_reference_usd: null }), null]) {
      const c = historyCost({ ...base, auth_kind: 'oauth', estimated_cost_usd: 0, cost_summary }, t);
      expect(c.text).toBe('訂閱 · API 等值參考未知'); expect(c.usd).toBeNull(); expect(c.incomplete).toBe(true);
    }
  });
  it('API key: official estimate, a TTL range when only the range is known, unknown when nothing is priced', () => {
    expect(historyCost({ ...base, auth_kind: 'api_key', estimated_cost_usd: 0.9, cost_summary: cs({ api_estimate_usd: 0.0399 }) }, t).text).toBe('API 估算 $0.0399');
    expect(historyCost({ ...base, auth_kind: 'api_key', estimated_cost_usd: 0.9, cost_summary: cs({ api_estimate_usd: 0.0299, api_estimate_usd_max: 0.0474, complete: false }) }, t).text).toBe('API 估算 $0.0299–$0.0474 · 不完整');
    const u = historyCost({ ...base, auth_kind: 'api_key', estimated_cost_usd: 0, cost_summary: cs({ api_estimate_usd: null, complete: false }) }, t);
    expect(u.kind).toBe('unknown'); expect(u.text).toMatch(/成本未知/); expect(u.text).not.toMatch(/\$0/);
  });
  it('local: external API fee $0 and nothing else claimed', () => {
    const c = historyCost({ ...base, auth_kind: 'local', estimated_cost_usd: 0, cost_summary: cs({ billing: 'local', external_api_usd: 0 }) }, t);
    expect(c.text).toBe('本機模型 · 外部 API 費 $0'); expect(c.kind).toBe('local');
  });
  it('an older engine (no digest): API-key keeps the budget figure, a demo run shows nothing', () => {
    expect(historyCost({ ...base, auth_kind: 'api_key', estimated_cost_usd: 0.0123 }, t).text).toMatch(/\$0\.0123/);
    expect(historyCost({ ...base, live: false, estimated_cost_usd: 0 }, t).text).toBeNull();
  });
});

describe('History page', () => {
  const rows = () => [
    { ...base, id: 'qcom', ticker: 'QCOM', provider: 'openai', model: 'gpt-6.1-sol', auth_kind: 'oauth', estimated_cost_usd: 0,
      cost_summary: cs({ billing: 'oauth', basis: 'recomputed_current_list_price', complete: false, api_equivalent_reference_usd: 0.501366 }) },
    { ...base, id: 'key', ticker: 'AAPL', provider: 'openai', model: 'gpt-5.5', auth_kind: 'api_key', estimated_cost_usd: 0.9, cost_summary: cs({ api_estimate_usd: 0.0399 }) },
    { ...base, id: 'unk', ticker: 'MSFT', provider: 'openai', model: 'gpt-x', auth_kind: 'api_key', estimated_cost_usd: null, cost_summary: cs({ complete: false }) },
  ];
  it('the QCOM card no longer says 預估 $0.0000; subscription and API-key totals are separate', async () => {
    sessions = rows();
    mount();
    await waitFor(() => expect(screen.getByTestId('history-cost-qcom')).toBeTruthy());
    expect(screen.getByTestId('history-cost-qcom').textContent).toMatch(/訂閱 · API 等值參考 \$0\.5014（非帳單）/);
    expect(document.body.textContent).not.toMatch(/預估 \$0\.0000/);
    expect(screen.getByTestId('history-cost-key').textContent).toBe('API 估算 $0.0399');
    expect(screen.getByTestId('history-cost-unk').textContent).toMatch(/成本未知/);
    expect(screen.getByTestId('history-stat-api').textContent).toMatch(/不完整.*\$0\.0399/);        // an unknown row makes the API total "incomplete"
    expect(screen.getByTestId('history-stat-reference').textContent).toMatch(/非帳單.*\$0\.5014/);
  });
  it('a list with only subscription rows has no API total at all', async () => {
    sessions = [rows()[0]];
    mount();
    await waitFor(() => expect(screen.getByTestId('history-stat-reference')).toBeTruthy());
    expect(screen.queryByTestId('history-stat-api')).toBeNull();
  });

  it('the subscription reference total says it is incomplete (lower bound) when any row is unknown, recomputed-incomplete or a range', async () => {
    sessions = [
      { ...base, id: 'a', ticker: 'A', provider: 'openai', model: 'gpt-6.1-sol', auth_kind: 'oauth', estimated_cost_usd: 0,
        cost_summary: cs({ billing: 'oauth', basis: 'recomputed_current_list_price', complete: false, api_equivalent_reference_usd: 0.5014 }) },
      { ...base, id: 'b', ticker: 'B', provider: 'openai', model: 'gpt-x', auth_kind: 'oauth', estimated_cost_usd: 0, cost_summary: cs({ billing: 'oauth', complete: false }) },
      { ...base, id: 'c', ticker: 'C', provider: 'openai', model: 'gpt-6.1-sol', auth_kind: 'oauth', estimated_cost_usd: 0,
        cost_summary: cs({ billing: 'oauth', complete: false, api_equivalent_reference_usd: 0.03, api_equivalent_reference_usd_max: 0.05 }) },
    ];
    mount();
    await waitFor(() => expect(screen.getByTestId('history-stat-reference')).toBeTruthy());
    expect(screen.getByTestId('history-stat-reference').textContent).toMatch(/不完整.*下限.*\$0\.5314/);
  });
  it('a fully measured subscription list keeps the plain reference label', async () => {
    sessions = [{ ...base, id: 'a', ticker: 'A', provider: 'openai', model: 'gpt-6.1-sol', auth_kind: 'oauth', estimated_cost_usd: 0,
      cost_summary: cs({ billing: 'oauth', complete: true, api_equivalent_reference_usd: 0.2734 }) }];
    mount();
    await waitFor(() => expect(screen.getByTestId('history-stat-reference')).toBeTruthy());
    expect(screen.getByTestId('history-stat-reference').textContent).not.toMatch(/不完整/);
  });
});
