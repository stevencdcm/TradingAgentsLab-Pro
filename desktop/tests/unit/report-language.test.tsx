// @vitest-environment happy-dom
//
// Generated-report language: the UI locale must travel from the screen all the
// way into the engine's start frame. STATIC check only — no engine, no model.
// (Prompt-level and wire-level coverage lives in engine/tests/test_report_language.py;
// whether a real model answers in Traditional Chinese needs a real generation run.)
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { streamDebate } = vi.hoisted(() => ({
  streamDebate: vi.fn(async () => ({ close: () => undefined, done: Promise.resolve() })),
}));

vi.mock('../../src/lib/engine-client', async (orig) => ({
  ...(await orig<typeof import('../../src/lib/engine-client')>()),
  streamDebate,
}));
vi.mock('../../src/lib/secrets', () => ({
  getSecret: vi.fn(async (k: string) => (k.startsWith('llm:') ? 'sk-test-not-real' : null)),
  listSecrets: vi.fn(async () => []),
}));
vi.mock('../../src/lib/oauth', () => ({
  getOpenAICredentialsForRequest: vi.fn(async () => ({
    access: 'a', refresh: 'r', expires: 1, accountId: 'acct',
  })),
  getOpenAIOAuthStatus: vi.fn(async () => ({ connected: true })),
}));
vi.mock('../../src/lib/local-llm', () => ({
  loadLocalConfig: vi.fn(async () => ({ base_url: 'http://127.0.0.1:8000/v1', model: 'local-model' })),
}));
vi.mock('../../src/lib/cost-guard', async (orig) => ({
  ...(await orig<typeof import('../../src/lib/cost-guard')>()),
  reserveCostGuard: vi.fn(async () => ({ reservation_id: 'r1' })),
}));
vi.mock('../../src/lib/webhooks', () => ({
  loadWebhooks: vi.fn(async () => []),
}));

import { runAnalysis } from '../../src/lib/run-analysis';

beforeEach(() => {
  streamDebate.mockClear();
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
});
afterEach(() => vi.unstubAllGlobals());

const callbacks = { onEvent: () => undefined };

describe('runAnalysis forwards the report language for every provider path', () => {
  const cases: Array<[string, 'openai' | 'anthropic' | 'local' | 'openrouter' | 'gemini' | 'xai' | 'minimax', 'oauth' | 'api_key']> = [
    ['OpenAI ChatGPT OAuth', 'openai', 'oauth'],
    ['OpenAI API key', 'openai', 'api_key'],
    ['Anthropic API key', 'anthropic', 'api_key'],
    ['OpenRouter', 'openrouter', 'api_key'],
    ['Gemini', 'gemini', 'api_key'],
    ['xAI', 'xai', 'api_key'],
    ['MiniMax', 'minimax', 'api_key'],
    ['local runtime', 'local', 'api_key'],
  ];

  it.each(cases)('%s → report_language "zh-TW"', async (_name, provider, authKind) => {
    await runAnalysis(
      { ticker: 'AAPL', trade_date: '2026-10-30', provider, openaiAuthKind: authKind, model: 'm', reportLanguage: 'zh-TW' },
      callbacks,
    );
    expect(streamDebate).toHaveBeenCalledTimes(1);
    const req = (streamDebate.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(req.report_language).toBe('zh-TW');
    // Everything else about the request is unchanged by the language.
    expect(req.ticker).toBe('AAPL');
    expect(req.engine).toBe('pro');
  });

  it('English and omitted stay compatible with older behaviour', async () => {
    await runAnalysis(
      { ticker: 'AAPL', trade_date: '2026-10-30', provider: 'anthropic', openaiAuthKind: 'api_key', model: 'm', reportLanguage: 'en' },
      callbacks,
    );
    await runAnalysis(
      { ticker: 'AAPL', trade_date: '2026-10-30', provider: 'anthropic', openaiAuthKind: 'api_key', model: 'm' },
      callbacks,
    );
    const first = (streamDebate.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    const second = (streamDebate.mock.calls[1] as unknown as [Record<string, unknown>])[0];
    expect(first.report_language).toBe('en');
    expect(second.report_language).toBeUndefined();
  });
});

describe('start frame on the wire', () => {
  it('serialises report_language into the WebSocket start frame', async () => {
    const sent: string[] = [];
    class FakeWS {
      listeners: Record<string, () => void> = {};
      constructor(public url: string) {}
      addEventListener(ev: string, cb: () => void) {
        this.listeners[ev] = cb;
        if (ev === 'open') queueMicrotask(cb);
      }
      send(data: string) {
        sent.push(data);
      }
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWS);
    (window as unknown as { tradingAgentsLab: unknown }).tradingAgentsLab = {
      getEngineHandshake: async () => ({ port: 1, token: 't' }),
      onEngineExited: () => () => undefined,
    };
    const real = await vi.importActual<typeof import('../../src/lib/engine-client')>(
      '../../src/lib/engine-client',
    );
    await real.streamDebate(
      { ticker: 'AAPL', trade_date: '2026-10-30', report_language: 'zh-TW' },
      () => undefined,
    );
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(sent.length).toBe(1);
    expect(JSON.parse(sent[0]).report_language).toBe('zh-TW');
  });
});
