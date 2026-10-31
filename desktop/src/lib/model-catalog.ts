/**
 * Refreshable provider model catalog for the pickers (Analyze, Settings, and anything else that lists models).
 *
 * The built-in `PROVIDER_MODELS` table is the offline fallback and is never replaced: a refresh only ADDS the models the
 * provider's own list endpoint reports (fetched by the engine with the saved key, discovery only, no inference). The
 * last successful list is kept in localStorage with its fetch time, so pickers are right instantly on the next launch and
 * a failed refresh never discards it. Nothing here fetches on render: the network is touched only by `refreshCatalog`,
 * which the Settings button calls.
 *
 * Honest limits (the UI says so too): a model being listed is not proof that every request this app sends is accepted
 * for it, and listing says nothing about price (`priced === false` means cost will show as unknown).
 */

import { useCallback, useEffect, useSyncExternalStore } from 'react';
import {
  fetchModelCatalog,
  getAvailableModels,
  getModelStorageKey,
  PROVIDER_MODELS,
  PROVIDER_SECRET_KEY,
  type CatalogModel,
  type LLMProvider,
  type ModelChoice,
  type ProviderConfig,
} from './engine-client';
import { cachedCodexModels } from './codex-models';
import { getSecret, listSecrets, onSecretsChanged } from './secrets';

const CACHE_PREFIX = 'tal:model-catalog:v1:';
/** Providers whose official list endpoint the engine uses (engine/model_catalog.py SPECS). Others: "not supported". */
export const CATALOG_PROVIDERS: ReadonlySet<LLMProvider> = new Set<LLMProvider>([
  'anthropic', 'openai', 'xai', 'openrouter', 'gemini',
]);

export type CatalogStatus = 'idle' | 'loading';

export interface CatalogState {
  models: CatalogModel[];
  /** Ids the provider reports as retired in its latest successful answer. */
  retired: string[];
  /** Epoch seconds the list was fetched from the provider; null = never refreshed. */
  fetchedAt: number | null;
  status: CatalogStatus;
  /** Last refresh failure (code + credential-free message); cleared by the next success. */
  error: { code: string; message: string } | null;
  /** True when the visible list is older than the failed refresh (engine kept the last good list). */
  stale: boolean;
  /** Which saved key the list was fetched with (update time + hint of its Settings entry; never the key). A different
   * stamp means the key was replaced or removed, so the list belongs to another account and is discarded. */
  credStamp: string | null;
}

const EMPTY: CatalogState = { models: [], retired: [], fetchedAt: null, status: 'idle', error: null, stale: false, credStamp: null };

const states = new Map<LLMProvider, CatalogState>();
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((l) => l());
}

function readCache(provider: LLMProvider): CatalogState {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + provider);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as { models?: unknown; retired?: unknown; fetchedAt?: unknown; credStamp?: unknown };
    if (!Array.isArray(parsed.models)) return EMPTY;
    const models = parsed.models.filter(
      (m): m is CatalogModel => !!m && typeof m === 'object' && typeof (m as CatalogModel).id === 'string',
    );
    const fetchedAt = typeof parsed.fetchedAt === 'number' ? parsed.fetchedAt : null;
    const retired = Array.isArray(parsed.retired) ? parsed.retired.filter((x): x is string => typeof x === 'string') : [];
    return { ...EMPTY, models, retired, fetchedAt, credStamp: typeof parsed.credStamp === 'string' ? parsed.credStamp : null };
  } catch {
    return EMPTY;
  }
}

function writeCache(provider: LLMProvider, s: CatalogState): void {
  try {
    localStorage.setItem(CACHE_PREFIX + provider, JSON.stringify({ models: s.models, retired: s.retired, fetchedAt: s.fetchedAt, credStamp: s.credStamp }));
  } catch {
    /* storage unavailable: the in-memory list still works for this session */
  }
}

export function getCatalogState(provider: LLMProvider): CatalogState {
  let s = states.get(provider);
  if (!s) {
    s = readCache(provider);
    states.set(provider, s);
  }
  return s;
}

function setState(provider: LLMProvider, next: CatalogState): void {
  states.set(provider, next);
  emit();
}

/** Build the same api-key provider_config live runs send (the key is a secret reference in web mode; the engine resolves it). */
async function catalogConfig(provider: LLMProvider): Promise<ProviderConfig | null> {
  const apiKey = await getSecret(PROVIDER_SECRET_KEY[provider]);
  if (!apiKey) return null;
  return { provider, auth: { type: 'api_key', api_key: apiKey } };
}

/** Identity of the saved key for this provider as Settings knows it (never the key). null = no key saved / unknown. */
async function credentialStamp(provider: LLMProvider): Promise<string | null> {
  const entry = (await listSecrets()).find((e) => e.key === PROVIDER_SECRET_KEY[provider]);
  return entry ? `${entry.updatedAt}|${entry.hint}` : null;
}

function forget(provider: LLMProvider): void {
  try {
    localStorage.removeItem(CACHE_PREFIX + provider);
  } catch {
    /* storage unavailable */
  }
  setState(provider, EMPTY);
}

/**
 * Drop the cached list when the saved key changed (replaced or deleted): a list fetched with one account's key must never
 * be shown, or kept as the "last good" fallback, for another. If the key identity cannot be read, nothing is dropped.
 */
export async function validateCatalogCredential(provider: LLMProvider): Promise<void> {
  const cur = getCatalogState(provider);
  if (cur.models.length === 0 && cur.retired.length === 0 && cur.credStamp === null) return;
  let stamp: string | null;
  try {
    stamp = await credentialStamp(provider);
  } catch {
    return;
  }
  if (stamp !== cur.credStamp) forget(provider);
}

/** Ask the provider for its current model list. One refresh at a time per provider; never throws. */
export async function refreshCatalog(provider: LLMProvider): Promise<void> {
  if (!CATALOG_PROVIDERS.has(provider)) return;
  if (getCatalogState(provider).status === 'loading') return;
  await validateCatalogCredential(provider);         // a replaced key must not inherit the previous key's list
  const before = getCatalogState(provider);
  if (before.status === 'loading') return;
  setState(provider, { ...before, status: 'loading' });
  try {
    const stamp = await credentialStamp(provider).catch(() => null);
    const cfg = await catalogConfig(provider);
    if (!cfg) {
      setState(provider, { ...before, status: 'idle', error: { code: 'no_credential', message: 'no saved key for this provider' } });
      return;
    }
    const res = await fetchModelCatalog(cfg, true);
    const models = res.models ?? [];
    const retired = res.retired ?? [];
    const answered = res.source === 'live' || res.source === 'cache';
    // An authoritative answer replaces the list, even when it leaves nothing usable (everything retired): the old list must
    // not stay selectable once the provider has declared its models dead.
    if (answered && (models.length > 0 || retired.length > 0)) {
      const next: CatalogState = {
        models, retired, fetchedAt: res.fetched_at ?? Date.now() / 1000, status: 'idle',
        error: models.length > 0 ? null : (res.error ?? { code: 'all_retired', message: 'every listed model is retired' }),
        stale: false, credStamp: stamp,
      };
      writeCache(provider, next);
      setState(provider, next);
      return;
    }
    // Failure (or the engine's own stale copy): keep the best list we have, say why it is not fresh.
    const keep: CatalogState = res.source === 'stale' && models.length > 0
      ? { ...before, models, retired, fetchedAt: res.fetched_at ?? before.fetchedAt, credStamp: stamp }
      : before;
    setState(provider, {
      ...keep,
      status: 'idle',
      stale: keep.models.length > 0,
      error: res.error ?? { code: 'unavailable', message: 'model list unavailable' },
    });
  } catch (err) {
    setState(provider, {
      ...before, status: 'idle', stale: before.models.length > 0,
      error: { code: 'network', message: err instanceof Error ? err.message : String(err) },
    });
  }
}

/**
 * Built-in table + provider-reported models, ready for a picker.
 * - Built-in entries keep their order, notes and `recommended` flag (a refresh never changes anyone's default).
 * - Provider-reported models the table lacks are added, newest-first as the provider lists them, and marked origin 'api'.
 * - Provider-reported retired models are not offered (the engine drops them); deprecated ones are marked.
 * - `savedId` (the user's current choice) is kept even when neither list has it: it is shown as origin 'saved', never dropped.
 */
export function mergeModelChoices(
  builtin: ModelChoice[],
  live: CatalogModel[],
  savedId?: string | null,
  retired: readonly string[] = [],
): ModelChoice[] {
  const dead = new Set(retired);
  const liveById = new Map(live.map((m) => [m.id, m]));
  // The built-in table is only a fallback: an id the provider says is retired is not offered from it either.
  const base = builtin.filter((m) => !dead.has(m.id));
  const known = new Set(base.map((m) => m.id));
  const out: ModelChoice[] = base.map((m) => {
    const l = liveById.get(m.id);
    return {
      ...m,
      origin: 'builtin' as const,
      ...(l?.lifecycle === 'deprecated' ? { deprecated: true } : {}),
      ...(l && l.priced === false ? { priced: false } : {}),
    };
  });
  const added: ModelChoice[] = live
    .filter((m) => !known.has(m.id) && !dead.has(m.id) && m.lifecycle !== 'retired')
    .map((m) => ({
      id: m.id,
      label: m.label || m.id,
      origin: 'api' as const,
      ...(m.lifecycle === 'deprecated' ? { deprecated: true } : {}),
      ...(m.priced === false ? { priced: false } : {}),
    }));
  // Models the provider reports first (newest), so a new release is visible without scrolling past old ones.
  const merged = [...added, ...out];
  if (savedId && !merged.some((m) => m.id === savedId)) {
    // The user's own choice stays (never auto-switched) but a retired one is labelled as such, not offered as usable.
    merged.unshift({ id: savedId, label: savedId, origin: 'saved', ...(dead.has(savedId) ? { retired: true } : {}) });
  }
  return merged;
}

export interface ModelCatalogHook extends CatalogState {
  supported: boolean;
  refresh: () => void;
}

/** Subscribe to one provider's catalog state (shared by every picker, so a refresh in Settings updates Analyze too). */
export function useModelCatalog(provider: LLMProvider | null): ModelCatalogHook {
  const subscribe = useCallback((cb: () => void) => {
    listeners.add(cb);
    return () => {
      listeners.delete(cb);
    };
  }, []);
  const snapshot = useCallback(() => (provider ? getCatalogState(provider) : EMPTY), [provider]);
  const state = useSyncExternalStore(subscribe, snapshot, snapshot);
  const refresh = useCallback(() => {
    if (provider) void refreshCatalog(provider);
  }, [provider]);
  // Check the cached list against the saved key now and whenever Settings changes a secret.
  useEffect(() => {
    if (!provider || !CATALOG_PROVIDERS.has(provider)) return undefined;
    void validateCatalogCredential(provider);
    return onSecretsChanged(() => void validateCatalogCredential(provider));
  }, [provider]);
  return { ...state, supported: !!provider && CATALOG_PROVIDERS.has(provider), refresh };
}

/** Choices for a provider from the built-in table plus its cached/refreshed list (no network). */
export function catalogChoices(provider: LLMProvider, savedId?: string | null): ModelChoice[] {
  const st = getCatalogState(provider);
  return mergeModelChoices(PROVIDER_MODELS[provider], st.models, savedId, st.retired);
}

export function _resetModelCatalogForTests(): void {
  states.clear();
  emit();
}

/**
 * The model a non-interactive run (Batch) should use: the user's saved choice for this provider when it is still selectable,
 * else the list's recommended entry. Same sources as the Analyze picker (built-in table + refreshed provider list + the saved
 * id itself), so a model picked in Analyze, including a newly listed 5.5 one or a legacy id, is what Batch runs. Never writes
 * the selection: nothing is switched automatically. Local runs take their model from the saved local config (returns '').
 */
export function resolveSelectedModel(provider: LLMProvider, authKind: 'oauth' | 'api_key' | 'local' | null): string {
  if (provider === 'local') return '';
  let saved: string | null = null;
  try {
    saved = localStorage.getItem(getModelStorageKey(provider, authKind));
  } catch {
    /* storage unavailable: fall through to the recommendation */
  }
  const list = provider === 'openai' && authKind === 'oauth'
    ? cachedCodexModels()                       // retired ChatGPT-sign-in models self-heal to the account default
    : mergeModelChoices(getAvailableModels(provider, authKind), getCatalogState(provider).models, saved, getCatalogState(provider).retired);
  if (saved && list.some((m) => m.id === saved)) return saved;
  return (list.find((m) => m.recommended) ?? list[0]).id;
}

/** The provider has retired this model: runs naming it fail. A saved retired choice is kept and labelled, never silently
 * replaced, but nothing may START a run with it (Analyze and Batch stop and ask the user to pick another). */
export function isRetiredModel(provider: LLMProvider | null, modelId: string | null | undefined): boolean {
  return !!provider && !!modelId && getCatalogState(provider).retired.includes(modelId);
}
