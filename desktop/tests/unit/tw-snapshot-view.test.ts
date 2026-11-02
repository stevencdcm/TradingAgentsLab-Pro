import { describe, expect, it } from 'vitest';
import { findSnapshot, snapshotMarkdown } from '../../src/lib/research-notes';

const t = (k: string, v?: Record<string, string>) => (v ? `${k}:${JSON.stringify(v)}` : k);
const info = (complete: Record<string, unknown> | undefined) => ({ complete } as never);

describe('Taiwan run snapshot view', () => {
  it('shows nothing for non-Taiwan and for the simplified flow', () => {
    expect(findSnapshot(info({ market: 'US' }))).toBeNull();
    expect(findSnapshot(info({ market: 'TW', tw_flow: 'simplified' }))).toBeNull();
    expect(findSnapshot(info(undefined))).toBeNull();
  });

  it('an older Taiwan full-graph report says it has no snapshot (never reconstructed)', () => {
    const v = findSnapshot(info({ market: 'TW', tw_flow: 'full' }));
    expect(v?.state).toBe('missing');
    expect(snapshotMarkdown(info({ market: 'TW', tw_flow: 'full' }), t as never).join('\n')).toContain('snapshot.missing');
  });

  it('lists the saved manifest: id, tool calls, per-source results and typed gaps', () => {
    const c = {
      market: 'TW', tw_flow: 'full',
      evidence_snapshot: { status: 'ok', snapshot_id: 'tws_0123456789abcdef0123456789abcdef', calls: 5,
        coverage: { sources: { twse_valuation: { ok: 1, not_ok: 0 }, finmind_news: { ok: 0, not_ok: 2 } }, gaps: ['finmind_news:rate_limited'] } },
    };
    const v = findSnapshot(info(c));
    expect(v).toMatchObject({ state: 'present', calls: 5 });
    expect(v?.sources).toContainEqual({ id: 'finmind_news', ok: 0, notOk: 2 });
    expect(v?.gaps).toEqual(['finmind_news:rate_limited']);
  });

  it('a failed save is reported as failed, not as missing', () => {
    expect(findSnapshot(info({ market: 'TW', tw_flow: 'full', evidence_snapshot: { status: 'failed', reason: 'OSError' } }))?.state).toBe('failed');
  });
});
