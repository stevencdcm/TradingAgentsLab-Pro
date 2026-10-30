// @vitest-environment happy-dom
// Client side of resumable runs: dedupe by seq, resubscribe with ?after=, idempotent start key, cancel vs close.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeWS {
  static all: FakeWS[] = [];
  static OPEN = 1; static CONNECTING = 0; static CLOSED = 3;
  readyState = 0;
  sent: string[] = [];
  listeners: Record<string, Array<(e: any) => void>> = {};
  constructor(public url: string) { FakeWS.all.push(this); }
  addEventListener(t: string, f: (e: any) => void) { (this.listeners[t] ??= []).push(f); }
  send(d: string) { this.sent.push(d); }
  close() { this.readyState = 3; this.fire('close', { code: 1000, reason: '' }); }
  fire(t: string, e: any = {}) { (this.listeners[t] ?? []).forEach((f) => f(e)); }
  open() { this.readyState = 1; this.fire('open'); }
  msg(o: object) { this.fire('message', { data: JSON.stringify(o) }); }
  drop(code = 1006) { this.readyState = 3; this.fire('close', { code, reason: '' }); }
}

const calls: Array<{ url: string; init?: RequestInit }> = [];

async function load(handshake: object) {
  vi.resetModules();
  (window as any).tradingAgentsLab = { platform: 'web', getEngineHandshake: async () => handshake };
  return import('../../src/lib/engine-client');
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeWS.all = []; calls.length = 0;
  vi.stubGlobal('WebSocket', FakeWS);
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ run_id: 'run_1', state: 'cancelled' }), { status: 200 });
  }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

const req = { ticker: 'AAPL', trade_date: '2026-10-30' } as any;
const flush = async () => { await vi.advanceTimersByTimeAsync(0); };

describe('subscribeRun (streamDebate / resumeRun)', () => {
  it('sends the start frame with a client_run_key and resumable flag, and de-duplicates events by seq', async () => {
    const ec = await load({ port: 8765, proxy: true });
    const got: any[] = [];
    const h = await ec.streamDebate(req, (e) => got.push(e));
    const ws = FakeWS.all[0];
    expect(ws.url).toBe('ws://localhost:3000/stream'.replace('localhost:3000', new URL(document.baseURI).host)) ;
    ws.open();
    const frame = JSON.parse(ws.sent[0]);
    expect(frame.resumable).toBe(true);
    expect(typeof frame.client_run_key).toBe('string');
    expect(frame.client_run_key.length).toBeGreaterThan(8);
    ws.msg({ type: 'run.attached', run_id: 'run_1' });
    ws.msg({ type: 'session.start', seq: 1 });
    ws.msg({ type: 'agent.message', seq: 2 });
    ws.msg({ type: 'agent.message', seq: 2 });            // duplicate
    ws.msg({ type: 'agent.message', seq: 1 });            // stale
    ws.msg({ type: 'agent.message', seq: 3 });
    expect(got.filter((e) => e.seq).map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(h.runId()).toBe('run_1');
  });

  it('a dropped socket re-subscribes with ?after=<last seq> and never re-sends the start frame; finished ends it', async () => {
    const ec = await load({ port: 8765, token: 'tok', proxy: false });
    const got: any[] = []; const conn: string[] = [];
    const h = await ec.streamDebate(req, (e) => got.push(e), undefined, (s) => conn.push(s));
    const a = FakeWS.all[0];
    a.open(); a.msg({ type: 'run.attached', run_id: 'run_9' }); a.msg({ type: 'agent.message', seq: 4 });
    a.drop(1006);
    expect(conn).toContain('reconnecting');
    await vi.advanceTimersByTimeAsync(600);
    const b = FakeWS.all[1];
    expect(b.url).toContain('/runs/run_9/stream');
    expect(b.url).toContain('after=4');
    expect(b.url).toContain('token=tok');                // local mode still authenticates by token
    b.open();
    expect(b.sent).toEqual([]);                          // the run exists: no second start, no second reservation
    b.msg({ type: 'agent.message', seq: 5 });
    b.msg({ type: 'run.finished', seq: 6, state: 'completed' });
    b.close();
    await expect(h.done).resolves.toBeUndefined();
    expect(got.map((e) => e.seq).filter(Boolean)).toEqual([4, 5, 6]);
  });

  it('a drop BEFORE the run is attached retries the start with the SAME key (idempotent)', async () => {
    const ec = await load({ port: 8765, proxy: true });
    await ec.streamDebate(req, () => {});
    const a = FakeWS.all[0]; a.open(); const k1 = JSON.parse(a.sent[0]).client_run_key;
    a.drop(1006);
    await vi.advanceTimersByTimeAsync(600);
    const b = FakeWS.all[1]; b.open();
    expect(b.url.endsWith('/stream')).toBe(true);
    expect(JSON.parse(b.sent[0]).client_run_key).toBe(k1);
  });

  it('proxy mode: no token in the URL and no Authorization header on run calls', async () => {
    const ec = await load({ port: 8765, proxy: true });
    const h = await ec.resumeRun('run_2', () => {});
    expect(FakeWS.all[0].url).not.toContain('token');
    expect(FakeWS.all[0].url).toContain('/runs/run_2/stream?after=0');
    await ec.getRunStatus('run_2');
    expect(Object.keys((calls[0].init?.headers as Record<string, string>) ?? {}).map((k) => k.toLowerCase())).not.toContain('authorization');
    h.close();
  });

  it('close() only unsubscribes; cancel() asks the engine to stop the run', async () => {
    const ec = await load({ port: 8765, token: 'tok', proxy: false });
    const h = await ec.resumeRun('run_3', () => {});
    FakeWS.all[0].open();
    h.close();
    await flush();
    expect(calls.filter((c) => c.url.includes('/cancel'))).toHaveLength(0);
    const h2 = await ec.resumeRun('run_4', () => {});
    FakeWS.all[1].open();
    await h2.cancel();
    const c = calls.find((x) => x.url.includes('/runs/run_4/cancel'))!;
    expect(c.init?.method).toBe('POST');
    expect((c.init?.headers as Record<string, string>).Authorization).toBe('Bearer tok');
  });

  it('gives up after the bounded number of retries and reports the failure', async () => {
    const ec = await load({ port: 8765, proxy: true });
    const conn: string[] = [];
    const h = await ec.resumeRun('run_5', () => {}, undefined, (s) => conn.push(s));
    const p = h.done.catch((e) => e);
    for (let i = 0; i < 12; i++) {
      const ws = FakeWS.all[FakeWS.all.length - 1];
      ws.drop(1006);
      await vi.advanceTimersByTimeAsync(16000);
    }
    expect(conn[conn.length - 1]).toBe('offline');
    expect(String(await p)).toMatch(/stream closed with code 1006/);
  });

  it('cancel() is idempotent (one POST however many callers), stays subscribed, and can be retried after a failed request', async () => {
    const ec = await load({ port: 8765, token: 'tok', proxy: false });
    const h = await ec.resumeRun('run_7', () => {});
    FakeWS.all[0].open();
    const [a, b, c] = await Promise.all([h.cancel(), h.cancel(), h.cancel()]);
    expect([a, b, c]).toEqual([true, true, true]);
    expect(calls.filter((x) => x.url.includes('/runs/run_7/cancel'))).toHaveLength(1);
    // still subscribed: a drop while cancelling re-subscribes instead of giving up
    FakeWS.all[0].drop(1006);
    await vi.advanceTimersByTimeAsync(600);
    expect(FakeWS.all[1].url).toContain('/runs/run_7/stream');
    // failed request -> false, and a retry is allowed
    (fetch as any).mockImplementationOnce(async () => new Response('x', { status: 500 }));
    const h2 = await ec.resumeRun('run_8', () => {});
    FakeWS.all[2].open();
    expect(await h2.cancel()).toBe(false);
    expect(await h2.cancel()).toBe(true);
  });
});
