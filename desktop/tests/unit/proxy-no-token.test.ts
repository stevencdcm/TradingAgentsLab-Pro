// @vitest-environment happy-dom
// Behind the trusted workbench proxy the handshake has no engine token: the renderer must send no Authorization header
// and no ?token= in the WebSocket URL. Local mode (token present) keeps sending the bearer.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { authHeader } from '../../src/lib/endpoints';

describe('authHeader', () => {
  it('is empty without a token and a Bearer header with one', () => {
    expect(authHeader(undefined)).toEqual({});
    expect(authHeader('')).toEqual({});
    expect(authHeader('abc')).toEqual({ Authorization: 'Bearer abc' });
  });
});

describe('renderer API calls', () => {
  const seen: Array<{ url: string; headers: Record<string, string> }> = [];
  beforeEach(() => {
    seen.length = 0;
    vi.resetModules();
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      seen.push({ url: String(url), headers: { ...(init?.headers as Record<string, string>) } });
      return new Response(JSON.stringify({ config: {}, spend: {} }), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  async function run(handshake: object) {
    (window as any).tradingAgentsLab = { platform: 'web', getEngineHandshake: async () => handshake };
    const cg = await import('../../src/lib/cost-guard');
    await cg.getCostGuardState();
    return seen[0];
  }

  it('proxy mode (no token in the handshake): no Authorization header', async () => {
    const r = await run({ port: 8765, proxy: true });
    expect(Object.keys(r.headers).map((k) => k.toLowerCase())).not.toContain('authorization');
    expect(JSON.stringify(r)).not.toMatch(/Bearer|undefined/);
  });

  it('local mode: the bearer token is still sent', async () => {
    const r = await run({ port: 8765, token: 'engine-token', proxy: false });
    expect(r.headers.Authorization).toBe('Bearer engine-token');
  });
});
