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

import { useCallback, useSyncExternalStore } from 'react';
import {
  fetchModelCatalog,
  PROVIDER_MODELS,
  PROVIDER_SECRET_KEY,
  type CatalogModel,
  type LLMProvider,
  type ModelChoice,
  type ProviderConfig,
} from './engine-client';
import { getSecret } from './secrets';

const CACHE_PREFIX = 'tal:model-catalog:v1:';
/** Providers whose official list endpoint the engine uses (engine/model_catalog.py SPECS). Others: "not supported". */
export const CATALOG_PROVIDERS: ReadonlySet<LLMProvider> = new Set<LLMProvider>([
  'anthropic', 'openai', 'xai', 'openrouter', 'gemini',
]);

export type CatalogStatus = 'idle' | 'loading';

export interface CatalogState {
  models: CatalogModel[];
  /** Epoch seconds the list was fetched from the provider; null = never refreshed. */
  fetchedAt: number | null;
  status: CatalogStatus;
  /** Last refresh failure (code + credential-free message); cleared by the next success. */
  error: { code: string; message: string } | null;
  /** True when the visible list is older than the failed refresh (engine kept the last good list). */
  stale: boolean;
}

const EMPTY: CatalogState = { models: [], fetchedAt: null, status: 'idle', error: null, stale: false };

const states = new Map<LLMProvider, CatalogState>();
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((l) => l());
}

function readCache(provider: LLMProvider): CatalogState {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + provider);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as { models?: unknown; fetchedAt?: unknown };
    if (!Array.isArray(parsed.models)) return EMPTY;
    const models = parsed.models.filter(
      (m): m is CatalogModel => !!m && typeof m === 'object' && typeof (m as CatalogModel).id === 'string',
    );
    const fetchedAt = typeof parsed.fetchedAt === 'number' ? parsed.fetchedAt : null;
    return { ...EMPTY, models, fetchedAt };
  } catch {
    return EMPTY;
  }
}

function writeCache(provider: LLMProvider, s: CatalogState): void {
  try {
    localStorage.setItem(CACHE_PREFIX + provider, JSON.stringify({ models: s.models, fetchedAt: s.fetchedAt }));
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

/** Ask the provider for its current model list. One refresh at a time per provider; never throws. */
export async function refreshCatalog(provider: LLMProvider): Promise<void> {
  if (!CATALOG_PROVIDERS.has(provider)) return;
  const before = getCatalogState(provider);
  if (before.status === 'loading') return;
  setState(provider, { ...before, status: 'loading' });
  try {
    const cfg = await catalogConfig(provider);
    if (!cfg) {
      setState(provider, { ...before, status: 'idle', error: { code: 'no_credential', message: 'no saved key for this provider' } });
      return;
    }
    const res = await fetchModelCatalog(cfg, true);
    const models = res.models ?? [];
    if (res.ok && models.length > 0 && (res.source === 'live' || res.source === 'cache')) {
      const next: CatalogState = { models, fetchedAt: res.fetched_at ?? Date.now() / 1000, status: 'idle', error: null, stale: false };
      writeCache(provider, next);
      setState(provider, next);
      return;
    }
    // Failure (or the engine's own stale copy): keep the best list we have, say why it is not fresh.
    const keep = before.models.length > 0 ? before : models.length > 0
      ? { ...before, models, fetchedAt: res.fetched_at ?? before.fetchedAt }
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
): ModelChoice[] {
  const liveById = new Map(live.map((m) => [m.id, m]));
  const known = new Set(builtin.map((m) => m.id));
  const out: ModelChoice[] = builtin.map((m) => {
    const l = liveById.get(m.id);
    return {
      ...m,
      origin: 'builtin' as const,
      ...(l?.lifecycle === 'deprecated' ? { deprecated: true } : {}),
      ...(l && l.priced === false ? { priced: false } : {}),
    };
  });
  const added: ModelChoice[] = live
    .filter((m) => !known.has(m.id))
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
    merged.unshift({ id: savedId, label: savedId, origin: 'saved' });
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
  return { ...state, supported: !!provider && CATALOG_PROVIDERS.has(provider), refresh };
}

/** Choices for a provider from the built-in table plus its cached/refreshed list (no network). */
export function catalogChoices(provider: LLMProvider, savedId?: string | null): ModelChoice[] {
  return mergeModelChoices(PROVIDER_MODELS[provider], getCatalogState(provider).models, savedId);
}

export function _resetModelCatalogForTests(): void {
  states.clear();
  emit();
}
