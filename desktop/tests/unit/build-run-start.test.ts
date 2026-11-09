// @vitest-environment happy-dom
// D53: buildRunStart is the ticker-independent start template POST /batches repeats per ticker. It must reserve nothing and
// open no socket; runAnalysis builds its single-run frame from the same function and behaves as before.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { streamDebate, reserve } = vi.hoisted(() => ({
  streamDebate: vi.fn(async () => ({ close: () => undefined, done: Promise.resolve() })),
  reserve: vi.fn(async () => ({ reservation_id: 'r1' })),
}));
vi.mock('../../src/lib/engine-client', async (orig) => ({ ...(await orig<typeof import('../../src/lib/engine-client')>()), streamDebate }));
vi.mock('../../src/lib/secrets', () => ({
  getSecret: vi.fn(async (k: string) => (k === 'llm:anthropic' ? 'sk-test-not-real' : k === 'data:finmind' ? 'fm-token-123456' : null)),
  listSecrets: vi.fn(async () => []),
}));
vi.mock('../../src/lib/oauth', () => ({ getOpenAICredentialsForRequest: vi.fn(async () => null), getOpenAIOAuthStatus: vi.fn(async () => ({ connected: false })) }));
vi.mock('../../src/lib/local-llm', () => ({ loadLocalConfig: vi.fn(async () => null), loadLocalApiKey: vi.fn(async () => null) }));
vi.mock('../../src/lib/cost-guard', async (orig) => ({ ...(await orig<typeof import('../../src/lib/cost-guard')>()), reserveCostGuard: reserve }));
vi.mock('../../src/lib/webhooks', () => ({
  loadWebhooks: vi.fn(async () => [
    { id: 'w1', kind: 'telegram', telegram_chat_id: '42' },
    { id: 'w2', kind: 'generic', url: 'https://example.test/hook' },
  ]),
}));

import { buildRunStart, runAnalysis } from '../../src/lib/run-analysis';

beforeEach(() => {
  streamDebate.mockClear();
  reserve.mockClear();
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
});
afterEach(() => vi.unstubAllGlobals());

const base = { trade_date: '2026-11-09', provider: 'anthropic' as const, openaiAuthKind: 'api_key' as const, model: 'claude-sonnet-4-6', reportLanguage: 'zh-TW' as const };

describe('buildRunStart', () => {
  it('returns the start template without reserving cost, opening a socket, or carrying a ticker / reservation', async () => {
    const { template, reserveReq } = await buildRunStart(base);
    expect(reserve).not.toHaveBeenCalled();
    expect(streamDebate).not.toHaveBeenCalled();
    expect(template).not.toHaveProperty('ticker');
    expect(template).not.toHaveProperty('reservation_id');
    expect(template).not.toHaveProperty('client_run_key');
    expect(template).toMatchObject({
      trade_date: '2026-11-09', engine: 'pro', report_language: 'zh-TW', finmind_token: 'fm-token-123456',
      provider_config: { provider: 'anthropic', auth: { type: 'api_key', api_key: 'sk-test-not-real' }, model: 'claude-sonnet-4-6' },
      telegram_chat_ids: { w1: '42' },
    });
    expect((template.webhooks as unknown[]).length).toBe(2);
    expect(template.pro_config).toBeTruthy();
    expect(reserveReq).toMatchObject({ auth_kind: 'api_key', max_tokens: 400 });
  });

  it('with no usable provider the template has no provider_config (the batch UI refuses it) and no reserve request', async () => {
    const { template, reserveReq } = await buildRunStart({ ...base, provider: null });
    expect(template.provider_config).toBeUndefined();
    expect(reserveReq).toBeNull();
  });
});

describe('runAnalysis (single run) is unchanged', () => {
  it('reserves once, then streams the SAME template plus ticker and reservation_id', async () => {
    const { template } = await buildRunStart(base);
    const result = await runAnalysis({ ...base, ticker: 'AAPL' }, { onEvent: () => undefined });
    expect(result.kind).toBe('streaming');
    expect(reserve).toHaveBeenCalledTimes(1);
    expect(streamDebate).toHaveBeenCalledTimes(1);
    const req = (streamDebate.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(req).toMatchObject({ ...template, ticker: 'AAPL', reservation_id: 'r1' });
  });

  it('a cost-guard block without a handler still throws; with a handler returning false it cancels without streaming', async () => {
    const { CostGuardBlocked } = await import('../../src/lib/cost-guard');
    reserve.mockRejectedValueOnce(new CostGuardBlocked({ blocked: true, reason: 'cap' } as never));
    const r = await runAnalysis({ ...base, ticker: 'AAPL' }, { onEvent: () => undefined, onCostBlocked: async () => false });
    expect(r).toEqual({ kind: 'cancelled', reason: 'cost_guard' });
    expect(streamDebate).not.toHaveBeenCalled();
  });
});
