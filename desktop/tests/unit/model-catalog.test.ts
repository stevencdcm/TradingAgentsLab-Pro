// @vitest-environment happy-dom
// Refreshable model catalog: merge rules (built-in kept, provider-reported added, saved choice never dropped, no default
// change), the store (cache persistence, failed refresh keeps the last good list, nothing fetched until asked).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchModelCatalog = vi.fn();
let secret: string | null = 'sk-ant-secret-value-123';
let listing: { key: string; hint: string; updatedAt: string }[] = [{ key: 'llm:anthropic', hint: '…123', updatedAt: 'T1' }];
vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return { ...actual, fetchModelCatalog: (...a: unknown[]) => fetchModelCatalog(...a) };
});
vi.mock('../../src/lib/secrets', async () => {
  const actual = await vi.importActual<any>('../../src/lib/secrets');
  return { ...actual, getSecret: vi.fn(async () => secret), listSecrets: vi.fn(async () => listing), onSecretsChanged: () => () => {} };
});

import { PROVIDER_MODELS, getRecommendedModel } from '../../src/lib/engine-client';
import {
  _resetModelCatalogForTests, catalogChoices, getCatalogState, mergeModelChoices, refreshCatalog, resolveSelectedModel,
  validateCatalogCredential,
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
  listing = [{ key: 'llm:anthropic', hint: '…123', updatedAt: 'T1' }];
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


describe('replacing or removing the saved key', () => {
  const ok = (...ids: string[]) => ({ ok: true, source: 'live', fetched_at: 100, models: live(...ids) });

  it("a new key discards the old key's list; its failed refresh cannot bring it back", async () => {
    fetchModelCatalog.mockResolvedValueOnce(ok('acct-a-only'));
    await refreshCatalog('anthropic');
    expect(getCatalogState('anthropic').models.map((m) => m.id)).toEqual(['acct-a-only']);
    listing = [{ key: 'llm:anthropic', hint: '…999', updatedAt: 'T2' }];      // user replaced the key in Settings
    await validateCatalogCredential('anthropic');
    expect(getCatalogState('anthropic').models).toEqual([]);
    expect(localStorage.getItem('tal:model-catalog:v1:anthropic')).toBeNull();
    fetchModelCatalog.mockResolvedValueOnce({ ok: false, source: 'unavailable', models: [], error: { code: 'auth', message: 'x' } });
    await refreshCatalog('anthropic');
    expect(getCatalogState('anthropic')).toMatchObject({ models: [], stale: false, error: { code: 'auth' } });
  });

  it('a refresh run after a key change drops the old list first even if nobody validated yet', async () => {
    fetchModelCatalog.mockResolvedValueOnce(ok('acct-a-only'));
    await refreshCatalog('anthropic');
    listing = [{ key: 'llm:anthropic', hint: '…999', updatedAt: 'T2' }];
    fetchModelCatalog.mockResolvedValueOnce({ ok: false, source: 'unavailable', models: [], error: { code: 'network', message: 'x' } });
    await refreshCatalog('anthropic');
    expect(getCatalogState('anthropic').models).toEqual([]);                    // not acct-a-only
  });

  it('deleting the key drops the list; an unchanged key keeps it', async () => {
    fetchModelCatalog.mockResolvedValueOnce(ok('a-1'));
    await refreshCatalog('anthropic');
    await validateCatalogCredential('anthropic');
    expect(getCatalogState('anthropic').models).toHaveLength(1);
    listing = [];
    await validateCatalogCredential('anthropic');
    expect(getCatalogState('anthropic').models).toEqual([]);
  });

  it('a list persisted before this check (no stamp) is not trusted', async () => {
    localStorage.setItem('tal:model-catalog:v1:anthropic', JSON.stringify({ fetchedAt: 1, models: live('old-1') }));
    _resetModelCatalogForTests();
    await validateCatalogCredential('anthropic');
    expect(getCatalogState('anthropic').models).toEqual([]);
  });
});

describe('a model retired by the provider', () => {
  it('disappears from the refreshed list but a saved choice of it stays selectable, marked as saved', async () => {
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 1, models: live('claude-opus-5-5') });   // haiku-4-5 no longer listed
    await refreshCatalog('anthropic');
    const out = mergeModelChoices(PROVIDER_MODELS.anthropic, getCatalogState('anthropic').models, 'claude-retired-1');
    expect(out[0]).toMatchObject({ id: 'claude-retired-1', origin: 'saved' });
  });
});

describe('resolveSelectedModel (Batch and any non-interactive run)', () => {
  const key = 'tal:analyze:selected-model:anthropic';
  it('uses the saved Analyze choice, including a newly listed model, not the built-in recommendation', async () => {
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 1, models: live('claude-opus-5-6') });
    await refreshCatalog('anthropic');
    localStorage.setItem(key, 'claude-opus-5-6');
    expect(resolveSelectedModel('anthropic', 'api_key')).toBe('claude-opus-5-6');
  });
  it('keeps a saved legacy/custom id; with nothing saved it falls back to the recommendation; it never writes', () => {
    localStorage.setItem(key, 'claude-opus-4-7');
    expect(resolveSelectedModel('anthropic', null)).toBe('claude-opus-4-7');
    localStorage.removeItem(key);
    expect(resolveSelectedModel('anthropic', null)).toBe('claude-sonnet-4-6');
    expect(localStorage.getItem(key)).toBeNull();
  });
  it('local runs take the model from the saved local config (empty here); OAuth self-heals a retired saved model', () => {
    expect(resolveSelectedModel('local', 'local')).toBe('');
    localStorage.setItem('tal:analyze:selected-model:openai-oauth', 'gpt-5.5-retired');
    expect(resolveSelectedModel('openai', 'oauth')).toBe('gpt-6.1-sol');
  });
});

import { assembleProConfig, DEFAULT_PRO_CONFIG } from '../../src/lib/pro-config';

describe('Pro deep/quick path with a newly selected model', () => {
  it('without a deep/quick override both clients run the selected model, so any listed model is usable', () => {
    const cfg = assembleProConfig('claude-opus-5-6', 'anthropic', { ...DEFAULT_PRO_CONFIG });
    expect(cfg.deep_think_llm).toBe('claude-opus-5-6');
    expect(cfg.quick_think_llm).toBe('claude-opus-5-6');
  });
  it('an explicit deep/quick override still wins (existing settings preserved)', () => {
    const cfg = assembleProConfig('claude-opus-5-5', 'anthropic', { ...DEFAULT_PRO_CONFIG, deepModel: 'claude-opus-5-5', quickModel: 'claude-sonnet-5-5', effort: 'high' });
    expect(cfg).toMatchObject({ deep_think_llm: 'claude-opus-5-5', quick_think_llm: 'claude-sonnet-5-5', effort: 'high' });
  });
});
