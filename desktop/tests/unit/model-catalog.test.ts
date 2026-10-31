// @vitest-environment happy-dom
// Refreshable model catalog: merge rules (built-in kept, provider-reported added, saved choice never dropped, no default
// change), the store (cache persistence, failed refresh keeps the last good list, nothing fetched until asked).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchModelCatalog = vi.fn();
let secret: string | null = 'sk-ant-secret-value-123';
vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return { ...actual, fetchModelCatalog: (...a: unknown[]) => fetchModelCatalog(...a) };
});
vi.mock('../../src/lib/secrets', async () => {
  const actual = await vi.importActual<any>('../../src/lib/secrets');
  return { ...actual, getSecret: vi.fn(async () => secret) };
});

import { PROVIDER_MODELS, getRecommendedModel } from '../../src/lib/engine-client';
import {
  _resetModelCatalogForTests, catalogChoices, getCatalogState, mergeModelChoices, refreshCatalog,
} from '../../src/lib/model-catalog';

const live = (...ids: string[]) => ids.map((id) => ({ id, label: id.toUpperCase(), lifecycle: 'active', priced: true }));

function stubStorage() {
  const m = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null), setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k), clear: () => m.clear(),
  });
}

beforeEach(() => {
  stubStorage();
  _resetModelCatalogForTests();
  fetchModelCatalog.mockReset();
  secret = 'sk-ant-secret-value-123';
});

describe('built-in fallback', () => {
  it('lists the Claude 5.5 family offline, with their official ids', () => {
    const ids = PROVIDER_MODELS.anthropic.map((m) => m.id);
    for (const id of ['claude-fable-5-1', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5']) expect(ids).toContain(id);
  });
  it('adding models does not change the recommended default', () => {
    expect(getRecommendedModel('anthropic', null)).toBe('claude-sonnet-4-6');
  });
});

describe('mergeModelChoices', () => {
  const builtin = PROVIDER_MODELS.anthropic;
  it('keeps every built-in entry (order, note, recommended) and adds provider-reported ones first', () => {
    const out = mergeModelChoices(builtin, live('claude-opus-5-6', 'claude-opus-5-5'));
    expect(out[0]).toMatchObject({ id: 'claude-opus-5-6', origin: 'api' });
    expect(out.slice(1).map((m) => m.id)).toEqual(builtin.map((m) => m.id));
    expect(out.find((m) => m.recommended)?.id).toBe('claude-sonnet-4-6');
    expect(out.filter((m) => m.id === 'claude-opus-5-5')).toHaveLength(1);
  });
  it('keeps a saved/legacy/custom choice that neither list has, marked as saved', () => {
    const out = mergeModelChoices(builtin, live('claude-opus-5-6'), 'claude-opus-4-7');
    expect(out[0]).toMatchObject({ id: 'claude-opus-4-7', origin: 'saved' });
    expect(mergeModelChoices(builtin, [], 'my-custom-model')[0].id).toBe('my-custom-model');
  });
  it('does not duplicate a saved choice that is already listed', () => {
    const out = mergeModelChoices(builtin, live(), 'claude-sonnet-4-6');
    expect(out.filter((m) => m.id === 'claude-sonnet-4-6')).toHaveLength(1);
  });
  it('marks deprecated and unpriced models; never invents a price', () => {
    const out = mergeModelChoices(builtin, [
      { id: 'claude-x-9', label: 'X', lifecycle: 'deprecated', priced: false },
      { id: 'claude-opus-5-5', label: 'O', lifecycle: 'deprecated', priced: true },
    ]);
    expect(out.find((m) => m.id === 'claude-x-9')).toMatchObject({ deprecated: true, priced: false });
    expect(out.find((m) => m.id === 'claude-opus-5-5')).toMatchObject({ deprecated: true });
    expect(out.find((m) => m.id === 'claude-opus-5-5')?.priced).toBeUndefined();
  });
  it('with no provider list it is exactly the built-in table', () => {
    expect(mergeModelChoices(builtin, []).map((m) => m.id)).toEqual(builtin.map((m) => m.id));
  });
});

describe('catalog store', () => {
  it('reading state never touches the network', () => {
    expect(getCatalogState('anthropic').models).toEqual([]);
    catalogChoices('anthropic');
    expect(fetchModelCatalog).not.toHaveBeenCalled();
  });

  it('a successful refresh stores the list + fetch time, persists it, and the key only travels as the request config', async () => {
    fetchModelCatalog.mockResolvedValue({ ok: true, source: 'live', fetched_at: 1_790_000_000, models: live('claude-opus-5-6') });
    await refreshCatalog('anthropic');
    const s = getCatalogState('anthropic');
    expect(s.models.map((m) => m.id)).toEqual(['claude-opus-5-6']);
    expect(s.fetchedAt).toBe(1_790_000_000);
    expect(s.error).toBeNull();
    expect(fetchModelCatalog).toHaveBeenCalledWith({ provider: 'anthropic', auth: { type: 'api_key', api_key: secret } }, true);
    const stored = localStorage.getItem('tal:model-catalog:v1:anthropic') ?? '';
    expect(stored).toContain('claude-opus-5-6');
    expect(stored).not.toContain('sk-ant-secret'); // the key is never cached
    _resetModelCatalogForTests();                  // "next launch": the persisted list is there before any network call
    expect(getCatalogState('anthropic').models.map((m) => m.id)).toEqual(['claude-opus-5-6']);
    expect(fetchModelCatalog).toHaveBeenCalledTimes(1);
  });

  it('a failed refresh keeps the last successful list and reports why', async () => {
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 100, models: live('claude-opus-5-6') });
    await refreshCatalog('anthropic');
    fetchModelCatalog.mockResolvedValueOnce({ ok: false, source: 'unavailable', models: [], error: { code: 'auth', message: 'rejected' } });
    await refreshCatalog('anthropic');
    const s = getCatalogState('anthropic');
    expect(s.models.map((m) => m.id)).toEqual(['claude-opus-5-6']);
    expect(s).toMatchObject({ fetchedAt: 100, stale: true, error: { code: 'auth' } });
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 200, models: live('claude-opus-5-6', 'claude-opus-5-7') });
    await refreshCatalog('anthropic');                        // retry recovers and clears the error
    expect(getCatalogState('anthropic')).toMatchObject({ fetchedAt: 200, stale: false, error: null });
  });

  it('an engine "stale" answer or a thrown network error never clears an existing list', async () => {
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 100, models: live('a-1') });
    await refreshCatalog('anthropic');
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'stale', fetched_at: 100, models: live('a-1'), error: { code: 'provider_error', message: 'x' } });
    await refreshCatalog('anthropic');
    expect(getCatalogState('anthropic')).toMatchObject({ stale: true, error: { code: 'provider_error' } });
    fetchModelCatalog.mockRejectedValueOnce(new Error('boom'));
    await refreshCatalog('anthropic');
    expect(getCatalogState('anthropic').models.map((m) => m.id)).toEqual(['a-1']);
  });

  it('no saved key: no request is made and the error says so', async () => {
    secret = null;
    await refreshCatalog('anthropic');
    expect(fetchModelCatalog).not.toHaveBeenCalled();
    expect(getCatalogState('anthropic').error?.code).toBe('no_credential');
  });

  it('providers without an official list endpoint are not fetched', async () => {
    await refreshCatalog('minimax');
    await refreshCatalog('local');
    expect(fetchModelCatalog).not.toHaveBeenCalled();
  });

  it('a second click while one refresh is running does not start another', async () => {
    let release: (v: unknown) => void = () => {};
    fetchModelCatalog.mockImplementation(() => new Promise((r) => { release = r; }));
    const a = refreshCatalog('anthropic');
    await new Promise((r) => setTimeout(r, 0));
    const b = refreshCatalog('anthropic');
    release({ ok: true, source: 'live', fetched_at: 1, models: live('a-1') });
    await Promise.all([a, b]);
    expect(fetchModelCatalog).toHaveBeenCalledTimes(1);
  });
});

import { effortAllowed } from '../../src/lib/pro-config';

describe('Pro effort guard covers the 5.5 models that document effort', () => {
  it('allows effort only when both deep and quick are effort-capable', () => {
    expect(effortAllowed('anthropic', 'claude-opus-5-5', 'claude-sonnet-5-5')).toBe(true);
    expect(effortAllowed('anthropic', 'claude-fable-5-1', 'claude-fable-5-1')).toBe(true);
    expect(effortAllowed('anthropic', 'claude-opus-5-5', 'claude-haiku-5-5')).toBe(false);   // Haiku 5.5: not documented, not sent
    expect(effortAllowed('anthropic', 'claude-opus-5-5', 'claude-haiku-4-5')).toBe(false);
    expect(effortAllowed('openai', 'claude-opus-5-5', 'claude-opus-5-5')).toBe(false);
  });
});
