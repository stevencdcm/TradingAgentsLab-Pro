// @vitest-environment happy-dom
//
// Local (OpenAI-compatible) provider with an API key (oMLX): the key lives in
// safeStorage, is read per run into the in-memory request only, and is never
// logged. STATIC check: no engine, no model.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  secrets: new Map<string, string>(),
  streamDebate: vi.fn(async () => ({ close: () => undefined, done: Promise.resolve() })),
}));

vi.mock('../../src/lib/engine-client', async (orig) => ({
  ...(await orig<typeof import('../../src/lib/engine-client')>()),
  streamDebate: h.streamDebate,
}));
vi.mock('../../src/lib/secrets', () => ({
  getSecret: vi.fn(async (k: string) => h.secrets.get(k) ?? null),
  setSecret: vi.fn(async (k: string, v: string) => void h.secrets.set(k, v)),
  deleteSecret: vi.fn(async (k: string) => h.secrets.delete(k)),
  listSecrets: vi.fn(async () => []),
}));
vi.mock('../../src/lib/oauth', () => ({
  getOpenAICredentialsForRequest: vi.fn(async () => null),
  getOpenAIOAuthStatus: vi.fn(async () => ({ connected: false })),
}));
vi.mock('../../src/lib/cost-guard', async (orig) => ({
  ...(await orig<typeof import('../../src/lib/cost-guard')>()),
  reserveCostGuard: vi.fn(async () => ({ reservation_id: 'r1' })),
}));
vi.mock('../../src/lib/webhooks', () => ({ loadWebhooks: vi.fn(async () => []) }));

import { runAnalysis } from '../../src/lib/run-analysis';
import { loadLocalApiKey, saveLocalApiKey, saveLocalConfig } from '../../src/lib/local-llm';
import { LOCAL_API_KEY_SECRET_KEY } from '../../src/lib/engine-client';

const KEY = 'omlx-test-key-1234567890';
const BASE = 'http://127.0.0.1:8000/v1';
const MODEL = 'Qwen3.8-Flash-Next-oQ4e-mtp';

beforeEach(() => {
  h.secrets.clear();
  h.streamDebate.mockClear();
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
});
afterEach(() => vi.unstubAllGlobals());

const run = () =>
  runAnalysis(
    { ticker: 'AAPL', trade_date: '2026-10-30', provider: 'local', openaiAuthKind: 'api_key', model: '' },
    { onEvent: () => undefined },
  );
const sentAuth = () =>
  ((h.streamDebate.mock.calls[0] as unknown as [{ provider_config: { auth: Record<string, unknown>; model: string } }])[0]
    .provider_config);

describe('local provider API key (oMLX)', () => {
  it('stores and removes the key under its own safeStorage entry', async () => {
    await saveLocalApiKey(`  ${KEY}  `);
    expect(h.secrets.get(LOCAL_API_KEY_SECRET_KEY)).toBe(KEY);
    expect(await loadLocalApiKey()).toBe(KEY);
    await saveLocalApiKey('');
    expect(await loadLocalApiKey()).toBeNull();
  });

  it('sends the stored key with the run for the configured server and model', async () => {
    await saveLocalConfig({ base_url: BASE, model: MODEL });
    await saveLocalApiKey(KEY);
    await run();
    const cfg = sentAuth();
    expect(cfg.auth).toEqual({ type: 'local', base_url: BASE, api_key: KEY });
    expect(cfg.model).toBe(MODEL);
  });

  it('a keyless local runtime sends no api_key field', async () => {
    await saveLocalConfig({ base_url: 'http://localhost:11434/v1', model: 'llama3.2:latest' });
    await run();
    expect(sentAuth().auth).toEqual({ type: 'local', base_url: 'http://localhost:11434/v1' });
  });

  it('never writes the key to the console', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    await saveLocalConfig({ base_url: BASE, model: MODEL });
    await saveLocalApiKey(KEY);
    await run();
    for (const s of spies) {
      expect(JSON.stringify(s.mock.calls)).not.toContain(KEY);
      s.mockRestore();
    }
  });
});
