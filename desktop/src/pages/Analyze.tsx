import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import styles from './Analyze.module.css';
import ReportPdfButton from '../components/ReportPdfButton';
import DebateStream from '../components/DebateStream';
import RunStatus from '../components/RunStatus';
import type { RunClock, HeartbeatEvent } from '../lib/run-status';
import { CostGuardModal, type OverDimension } from '../components/CostGuardModal';
import {
  getHandshake,
  getHealth,
  type DebateEvent,
  type LLMProvider,
  type StreamHandle,
  getAvailableModels,
  getModelStorageKey,
  getRecommendedModel,
  type ModelChoice,
  PROVIDER_PRIORITY,
  PROVIDER_SECRET_KEY,
} from '../lib/engine-client';
import {
  type CostGuardConfig,
  type SpendState,
} from '../lib/cost-guard';
import { runAnalysis } from '../lib/run-analysis';
import { forgetRun, recallRun, rememberRun } from '../lib/run-memory';
import type { CostReport } from '../lib/cost-report';
import { cancelBatch, getActiveRuns, getBatch, getRunStatus, listBatches, resumeRun, type BatchSnapshot, type RunConnection, type RunStatusSnapshot } from '../lib/engine-client';
import { buildTranscriptMarkdown } from '../lib/transcript';
import { listSecrets, onSecretsChanged } from '../lib/secrets';
import { consumePendingTicker } from '../lib/handoff';
import { loadLocalApiKey, loadLocalConfig, saveLocalConfig } from '../lib/local-llm';
import ProviderSetupCallout from '../components/ProviderSetupCallout';
import { getLocalRuntimes, listLocalModels } from '../lib/engine-client';
import { getOpenAIOAuthStatus } from '../lib/oauth';
import { useCodexModels } from '../lib/codex-models';
import { isRetiredModel, mergeModelChoices, useModelCatalog } from '../lib/model-catalog';
import { useI18n } from '../i18n';
import { describeEngineError } from '../lib/engine-errors';
import { providerLabel } from '../lib/provider-label';
import { modelMarkerSuffix, modelNoteLabel } from '../lib/model-notes';

/** Persists the dropdown choice across app sessions (per founder, 2026-05-09). */
const SELECTED_PROVIDER_STORAGE_KEY = 'tal:analyze:selected-provider';

type EngineStatus = 'pending' | 'running' | 'error';

const FOLLOW_TERMINAL_CODES = ['invalid_credential', 'user_cancelled', 'cost_guard_blocked', 'oauth_expired', 'oauth_refresh_failed', 'storage_unavailable', 'server_restart'];

interface AnalyzeProps {
  /** Increments when the App menu fires "New analysis" — clears prior results. */
  resetSignal?: number;
}

function Analyze({ resetSignal = 0 }: AnalyzeProps) {
  const i18n = useI18n();
  const { t, rich, locale } = i18n;
  // Honor a watchlist hand-off if one is queued in sessionStorage. Falls
  // back to the default NVDA when there isn't one. Since 2026-05-17 App.tsx
  // hides-not-unmounts pages (so an in-flight debate's WebSocket survives
  // navigation), the useState initializer only runs once at app start; the
  // hashchange effect below re-consumes the pending ticker on subsequent
  // navigations to #analyze.
  const [ticker, setTicker] = useState(() => consumePendingTicker() ?? 'NVDA');
  const [date, setDate] = useState(new Date().toISOString().split('T')[0]);
  const [engineStatus, setEngineStatus] = useState<EngineStatus>('pending');
  const [engineError, setEngineError] = useState<string | null>(null);
  const [dataProvider, setDataProvider] = useState<string | null>(null);
  /** Per-stream asset class set from the latest data.summary event. Used to
   * surface a "crypto" badge on the Data card so users can confirm the
   * engine routed to the crypto endpoint, not the equities one. */
  const [assetClass, setAssetClass] = useState<'equity' | 'crypto' | null>(null);
  const [events, setEvents] = useState<DebateEvent[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const EMPTY_CLOCK: RunClock = { startedAt: 0, endedAt: null, lastEventAt: null, lastHeartbeatAt: null, heartbeat: null, streamError: null };
  const [runClock, setRunClock] = useState<RunClock>(EMPTY_CLOCK);
  const [twFlow, setTwFlow] = useState<'full' | 'simplified'>('full');
  const [twDexter, setTwDexter] = useState(true);
  const [runCtx, setRunCtx] = useState<{ provider: string | null; model: string | null }>({ provider: null, model: null });
  const [streamError, setStreamError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  /**
   * Set of providers the user has configured (any auth flow). Drives both
   * the dropdown's enabled/disabled state and the priority resolver
   * fallback when no manual selection is saved.
   */
  const [configuredProviders, setConfiguredProviders] = useState<Set<LLMProvider>>(
    () => new Set(),
  );
  /**
   * The user's *manual* dropdown choice (persisted to localStorage). null
   * means "use the priority resolver" — the dropdown still pre-fills with
   * the resolver's pick so the user can see what's about to run.
   */
  const [manualProvider, setManualProvider] = useState<LLMProvider | null>(() => {
    try {
      const raw = localStorage.getItem(SELECTED_PROVIDER_STORAGE_KEY);
      if (raw && (PROVIDER_PRIORITY as readonly string[]).includes(raw)) {
        return raw as LLMProvider;
      }
    } catch {
      // localStorage can throw in private mode — fall through.
    }
    return null;
  });
  /**
   * Whether OpenAI auth resolves to OAuth (subscription plan) vs API key.
   * Internal to the OpenAI provider — OAuth wins when both are stored.
   * Surfaced in the LLM status hint so the founder can see which billing
   * path the next debate will hit when the active provider is OpenAI.
   */
  const [openaiAuthKind, setOpenaiAuthKind] = useState<'oauth' | 'api_key' | null>(null);
  // Live Codex model list for the OAuth path; inert unless OpenAI OAuth is active.
  const codexModels = useCodexModels(openaiAuthKind === 'oauth');

  /**
   * CostGuard override modal state. When `blocked` is non-null, the modal
   * is open and the user must Cancel or Override before the debate runs.
   * The Promise resolver lets the async onAnalyze flow await the user's
   * choice without resorting to event-based callback gymnastics.
   */
  const [costGuardBlocked, setCostGuardBlocked] = useState<{
    over_dimension: OverDimension;
    spend: SpendState;
    config: CostGuardConfig;
    est_cost_usd: number;
  } | null>(null);
  const costGuardResolverRef = useRef<((proceed: boolean) => void) | null>(null);

  /**
   * The provider that will actually run the next debate, after considering
   * (a) the user's manual dropdown choice and (b) the priority resolver.
   * Stays in sync with `configuredProviders` and `manualProvider` via
   * `useMemo` rather than living in its own state — single source of truth.
   */
  const activeProvider = useMemo<LLMProvider | null>(() => {
    if (manualProvider && configuredProviders.has(manualProvider)) {
      return manualProvider;
    }
    return (
      PROVIDER_PRIORITY.find((p) => configuredProviders.has(p)) ?? null
    );
  }, [manualProvider, configuredProviders]);
  // Provider-reported models (cached from the last Settings refresh; this page never fetches by itself).
  const catalog = useModelCatalog(activeProvider === 'local' ? null : activeProvider);

  // Ref mirrors of the resolution state. `onAnalyze` is async (multiple
  // awaits between mousedown and the WS open frame) — reading via refs
  // means a Settings-driven state change racing with a click can't leave
  // the request using a now-stale provider/auth combo. Same pattern as
  // `isStreamingRef`/`engineReadyRef` above.
  const activeProviderRef = useRef<LLMProvider | null>(null);
  const openaiAuthKindRef = useRef<'oauth' | 'api_key' | null>(null);
  useEffect(() => {
    activeProviderRef.current = activeProvider;
  }, [activeProvider]);
  useEffect(() => {
    openaiAuthKindRef.current = openaiAuthKind;
  }, [openaiAuthKind]);

  // Re-consume any watchlist hand-off when the user navigates BACK to
  // #analyze. Since the page stays mounted across nav (so an in-flight
  // debate's WS survives), the useState initialiser only runs once at
  // app start — this listener catches the hand-off on every subsequent
  // arrival.
  useEffect(() => {
    const onHashChange = () => {
      const route = window.location.hash.replace(/^#/, '');
      if (route === 'analyze') {
        const pending = consumePendingTicker();
        if (pending) setTicker(pending);
      }
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  /**
   * Local LLM dynamic state. The model list is whatever the saved runtime
   * exposes right now — populated by probing `/llm/local-runtimes` and
   * filtering to the runtime whose base_url matches the saved config.
   * `localSavedBaseUrl` is the (URL) half of the saved pair so we know
   * which runtime to filter the detection result against and so we can
   * persist a new model choice back to safeStorage without re-loading.
   */
  const [localModels, setLocalModels] = useState<string[]>([]);
  const [localSavedBaseUrl, setLocalSavedBaseUrl] = useState<string | null>(null);
  const [localSavedModel, setLocalSavedModel] = useState<string | null>(null);

  /**
   * Available models for the current provider+auth combo. Recomputed
   * whenever the active provider, OpenAI auth flow, or local-runtime
   * model list changes. For local, the list is dynamic (per-runtime);
   * for everyone else it's the static `PROVIDER_MODELS` table.
   */
  const availableModels = useMemo(() => {
    if (!activeProvider) return [];
    if (activeProvider === 'local') {
      // Build ModelChoice entries from the detected runtime. If the
      // saved model isn't in the detected list (runtime offline or
      // model was uninstalled), prepend it so the dropdown still shows
      // the user's last choice — better than silently swapping models.
      const ids = new Set(localModels);
      const seed: ModelChoice[] = localSavedModel && !ids.has(localSavedModel)
        ? [{ id: localSavedModel, label: t('analyze.model.lastUsedOffline', { model: localSavedModel }) }]
        : [];
      const detected: ModelChoice[] = localModels.map((m) => ({ id: m, label: m }));
      return [...seed, ...detected];
    }
    // OpenAI via OAuth: the live Codex catalog for this ChatGPT account
    // (OpenAI retires sign-in models on its own schedule; see codex-models.ts).
    if (activeProvider === 'openai' && openaiAuthKind === 'oauth') {
      return codexModels.models;
    }
    // API-key providers: the built-in table plus whatever the provider's refreshed model list added (Settings → 更新模型清單).
    // The user's saved choice stays in the list even when neither source has it, so a refresh or a retired id never
    // silently swaps their model.
    let savedChoice: string | null = null;
    try {
      savedChoice = localStorage.getItem(getModelStorageKey(activeProvider, openaiAuthKind));
    } catch {
      // storage unavailable: no saved choice to protect
    }
    return mergeModelChoices(getAvailableModels(activeProvider, openaiAuthKind), catalog.models, savedChoice, catalog.retired);
  }, [activeProvider, openaiAuthKind, localModels, localSavedModel, codexModels.models, catalog.models, catalog.retired, t]);

  /**
   * The model that will run the next debate. Pulls from per-(provider,auth)
   * localStorage memory if the user picked one before; falls back to the
   * recommended entry. Validates against the current available list — if
   * the saved id no longer exists in the registry (model deprecated), we
   * drop it and use the recommendation.
   */
  const [activeModel, setActiveModel] = useState<string | null>(null);

  // Resync the model selection whenever the (provider, authKind) tuple
  // changes. Reads from localStorage; falls back to recommended.
  //
  // For local: source-of-truth is safeStorage (`local:model`), not
  // localStorage — the same value the Settings "Active:" line shows.
  // We mirror it into `activeModel` so the dropdown initializes
  // correctly when switching to local.
  useEffect(() => {
    if (!activeProvider) {
      setActiveModel(null);
      return;
    }
    if (activeProvider === 'local') {
      // localSavedModel was just hydrated by the refreshLocal effect
      // above. If the saved model is in the live detected list, use
      // it; if not, still use it (the dropdown surfaces "runtime
      // offline" inline via the seed entry in `availableModels`).
      setActiveModel(localSavedModel ?? null);
      return;
    }
    const key = getModelStorageKey(activeProvider, openaiAuthKind);
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(key);
    } catch {
      // ignore
    }
    const inList = saved && availableModels.some((m) => m.id === saved);
    // Recommended comes from the list actually shown (for OAuth that is the
    // live catalog), so a saved model OpenAI has retired falls through to the
    // account's current default instead of a stale built-in pick.
    const recommended =
      (availableModels.find((m) => m.recommended) ?? availableModels[0])?.id ??
      getRecommendedModel(activeProvider, openaiAuthKind);
    setActiveModel(inList ? saved : recommended);
  }, [activeProvider, openaiAuthKind, availableModels, localSavedModel]);

  /** Ref mirror so onAnalyze sees the right model under racing state. */
  const activeModelRef = useRef<string | null>(null);
  useEffect(() => {
    activeModelRef.current = activeModel;
  }, [activeModel]);

  const onSelectModel = useCallback(
    (modelId: string) => {
      if (!activeProvider) return;
      setActiveModel(modelId);
      if (activeProvider === 'local') {
        // For local, the canonical store is safeStorage so the Settings
        // "Active:" line and the WS frame agree on the same model.
        // Write back immediately so a subsequent Analyze run picks it up
        // even before the refreshLocal effect re-fires.
        if (localSavedBaseUrl) {
          setLocalSavedModel(modelId);
          void saveLocalConfig({
            base_url: localSavedBaseUrl,
            model: modelId,
          }).catch(() => {
            // safeStorage offline — the activeModel state still reflects
            // the choice for the current session but persistence failed.
          });
        }
        return;
      }
      try {
        const key = getModelStorageKey(activeProvider, openaiAuthKind);
        localStorage.setItem(key, modelId);
      } catch {
        // ignore
      }
    },
    [activeProvider, openaiAuthKind, localSavedBaseUrl],
  );
  const handleRef = useRef<StreamHandle | null>(null);
  const isStreamingRef = useRef(false);
  const engineReadyRef = useRef(false);

  useEffect(() => {
    // Engine startup is racy — Vite renders the React app before the
    // Python sidecar has emitted its handshake JSON. Retry the
    // handshake + health check with backoff for up to ~10s before
    // surfacing 'error'. Without this, every fresh app launch shows
    // "Engine error" for a beat before flipping to 'running' — visually
    // alarming for a non-error state. Stays in 'pending' during retries.
    let cancelled = false;
    const ATTEMPTS = [0, 500, 1000, 1500, 2000, 3000, 3000]; // ~11s total
    const tryOnce = async (): Promise<void> => {
      await getHandshake();
      const health = await getHealth();
      if (cancelled) return;
      setEngineStatus('running');
      engineReadyRef.current = true;
      setEngineError(null);
      // Seed from /health (the engine's default — usually yfinance).
      // The data.summary event handler below overrides this per-debate
      // when the user has Alpaca configured.
      setDataProvider(health.data_provider ?? null);
    };

    (async () => {
      let lastErr: unknown = null;
      for (const delay of ATTEMPTS) {
        if (cancelled) return;
        if (delay > 0) await new Promise((r) => setTimeout(r, delay));
        try {
          await tryOnce();
          return; // success — done
        } catch (err) {
          lastErr = err;
        }
      }
      if (!cancelled) {
        setEngineStatus('error');
        engineReadyRef.current = false;
        setEngineError(
          lastErr instanceof Error ? lastErr.message : String(lastErr ?? t('status.unknown')),
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Reflect the actual data provider + asset class used per stream. The
  // engine emits source ("yfinance" | "alpaca") and asset_class ("equity"
  // | "crypto") on every data.summary event. The /health seed only knows
  // the engine's default provider; per-stream routing + asset class show up
  // here once the first data.summary arrives. Also dispatches a
  // window-level CustomEvent so the App-shell <StatusStrip> can reflect
  // per-stream changes without us having to lift state.
  //
  // While we're here, dispatch cost.usage + session.complete to the strip
  // so the Spend pill can tick mid-stream and re-poll the daily total the
  // instant a debate ends. The idx ref avoids re-firing the same event
  // when this effect re-runs on unrelated state changes.
  const lastDispatchedIdxRef = useRef(0);
  useEffect(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const evt = events[i];
      if (evt.type === 'data.summary') {
        const src = (evt as { source?: string }).source;
        const ac = (evt as { asset_class?: 'equity' | 'crypto' }).asset_class;
        if (src && src !== dataProvider) setDataProvider(src);
        if (ac && ac !== assetClass) setAssetClass(ac);
        if (src) {
          window.dispatchEvent(
            new CustomEvent('tal:data-provider', {
              detail: { source: src, asset_class: ac },
            }),
          );
        }
        break;
      }
    }
    for (let i = lastDispatchedIdxRef.current; i < events.length; i++) {
      const evt = events[i];
      if (evt.type === 'cost.usage') {
        window.dispatchEvent(
          new CustomEvent('tal:cost-usage', {
            detail: { est_cost_usd: evt.est_cost_usd, free: evt.free },
          }),
        );
      } else if (evt.type === 'session.complete') {
        window.dispatchEvent(new CustomEvent('tal:session-complete'));
      }
    }
    lastDispatchedIdxRef.current = events.length;
  }, [events, dataProvider, assetClass]);

  // When the stream resets (new analysis), reset the dispatched-idx so the
  // first event of the next run isn't skipped.
  useEffect(() => {
    if (events.length === 0) lastDispatchedIdxRef.current = 0;
  }, [events.length]);

  // Reset state when the menu fires "New analysis".
  useEffect(() => {
    if (resetSignal === 0) return;
    // While following a batch, "New analysis" must not touch the batch (only Stop batch does).
    if (followIdRef.current) return;
    // "New analysis" is an explicit stop: cancel the engine-side run (leaving or reloading the page never does).
    requestCancel();
    forgetRun();
    setEvents([]);
    setStreamError(null);
    setCopied(false);
  }, [resetSignal]);

  // Settings writes secrets in the same window; re-read availability right away (the page can stay
  // mounted while another tab is shown, so a remount cannot be relied on).
  const [secretsTick, setSecretsTick] = useState(0);
  useEffect(() => onSecretsChanged(() => setSecretsTick((n) => n + 1)), []);

  // Re-poll secret presence + OAuth status whenever the page mounts, when
  // the user returns from Settings (resetSignal), and when a session ends.
  // The *only* meaningful transition for is-streaming is `true → false` —
  // skip the redundant call when streaming begins (the key set didn't
  // change). Updates `configuredProviders` (drives the dropdown) and
  // `openaiAuthKind` (drives the OAuth-vs-API-key hint).
  useEffect(() => {
    if (isStreaming) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const [rows, oauth] = await Promise.all([
          listSecrets(),
          getOpenAIOAuthStatus().catch(() => ({ connected: false } as const)),
        ]);
        if (cancelled) return;
        const stored = new Set(rows.map((r) => r.key));
        const next = new Set<LLMProvider>();
        for (const p of PROVIDER_PRIORITY) {
          const hasKey = stored.has(PROVIDER_SECRET_KEY[p]);
          if (p === 'openai' && (oauth.connected || hasKey)) next.add(p);
          // Local needs BOTH base_url AND model — the secrets:list result
          // is keyed by individual entries, so we check both directly.
          else if (p === 'local' && stored.has(PROVIDER_SECRET_KEY.local) && stored.has('local:model')) next.add(p);
          else if (p !== 'openai' && p !== 'local' && hasKey) next.add(p);
        }
        setConfiguredProviders(next);
        // OAuth wins over API key when both are present (founder
        // preference). Surfaced when openai is the active provider.
        setOpenaiAuthKind(
          oauth.connected
            ? 'oauth'
            : stored.has(PROVIDER_SECRET_KEY.openai)
              ? 'api_key'
              : null,
        );
      } catch {
        if (!cancelled) {
          setConfiguredProviders(new Set());
          setOpenaiAuthKind(null);
        }
      }
    };
    void refresh();
    return () => {
      cancelled = true;
    };
  }, [resetSignal, isStreaming, secretsTick]);

  // Refresh local runtime detection + saved-model state whenever the
  // Settings tab might have updated them. Probes /llm/local-runtimes and
  // filters to the saved base_url so the Analyze model dropdown shows
  // exactly the models for the user's chosen runtime. Empty list when
  // runtime is offline is the realistic state; we still seed the dropdown
  // with the saved model so the user can see what's about to run.
  useEffect(() => {
    if (isStreaming) return;
    if (!configuredProviders.has('local')) {
      setLocalModels([]);
      setLocalSavedBaseUrl(null);
      setLocalSavedModel(null);
      return;
    }
    let cancelled = false;
    const refreshLocal = async () => {
      try {
        const cfg = await loadLocalConfig();
        if (cancelled) return;
        if (!cfg) {
          setLocalSavedBaseUrl(null);
          setLocalSavedModel(null);
          setLocalModels([]);
          return;
        }
        setLocalSavedBaseUrl(cfg.base_url);
        setLocalSavedModel(cfg.model);
        // Probe the engine for the live model list. If the runtime is
        // offline this returns an empty array — the saved model is
        // surfaced via the seed in `availableModels`.
        // Authenticated servers (oMLX) are invisible to the unauthenticated
        // auto-detect, so ask the saved server directly (with its stored key).
        const listed = await listLocalModels({
          baseUrl: cfg.base_url,
          apiKey: await loadLocalApiKey().catch(() => null),
        });
        if (cancelled) return;
        if (listed.ok && listed.models.length > 0) {
          setLocalModels(listed.models);
          return;
        }
        const runtimes = await getLocalRuntimes().catch(() => []);
        if (cancelled) return;
        const match = runtimes.find((r) => r.base_url === cfg.base_url);
        setLocalModels(match?.models ?? []);
      } catch {
        if (!cancelled) {
          setLocalModels([]);
        }
      }
    };
    void refreshLocal();
    return () => {
      cancelled = true;
    };
  }, [resetSignal, isStreaming, configuredProviders, secretsTick]);

  // When the saved manual choice no longer has credentials (user deleted
  // the key in Settings), drop it so the priority resolver takes over.
  // localStorage stays in sync — a stale key shouldn't survive across
  // app launches once we know it's invalid.
  useEffect(() => {
    if (manualProvider && !configuredProviders.has(manualProvider)) {
      setManualProvider(null);
      try {
        localStorage.removeItem(SELECTED_PROVIDER_STORAGE_KEY);
      } catch {
        // ignore
      }
    }
  }, [manualProvider, configuredProviders]);

  const onSelectProvider = useCallback((value: LLMProvider | '') => {
    if (value === '') {
      // "Auto" sentinel — clear the manual override and let priority resolver win.
      setManualProvider(null);
      try {
        localStorage.removeItem(SELECTED_PROVIDER_STORAGE_KEY);
      } catch {
        // ignore
      }
      return;
    }
    setManualProvider(value);
    try {
      localStorage.setItem(SELECTED_PROVIDER_STORAGE_KEY, value);
    } catch {
      // ignore
    }
  }, []);

  /**
   * Reset BOTH provider and model overrides. Provider falls back to the
   * priority resolver; model falls back to the recommended entry for
   * whatever provider then resolves. Per-provider model memories in
   * localStorage are also cleared so "Reset" really means "give me the
   * recommended setup."
   *
   * Reads the current provider/auth via refs so the callback's identity
   * doesn't churn on every state change AND doesn't capture stale state
   * if the user changed providers between renders. Reset of `activeModel`
   * has to happen explicitly here — the sync effect at line ~132 only
   * re-fires when `[activeProvider, openaiAuthKind, availableModels]`
   * changes, so a model-only reset (provider unchanged) wouldn't trigger
   * it on its own.
   */
  const onResetOverrides = useCallback(() => {
    setManualProvider(null);
    try {
      localStorage.removeItem(SELECTED_PROVIDER_STORAGE_KEY);
      // Clear per-(provider,auth) model memories.
      for (const p of PROVIDER_PRIORITY) {
        localStorage.removeItem(getModelStorageKey(p, null));
        if (p === 'openai') {
          localStorage.removeItem(getModelStorageKey('openai', 'oauth'));
        }
      }
    } catch {
      // ignore
    }
    // Snap the in-memory model state back to the recommendation for the
    // currently-resolved provider so a model-only override clears
    // immediately (not only after the next provider/auth change).
    const provider = activeProviderRef.current;
    const authKind = openaiAuthKindRef.current;
    if (provider) {
      // OAuth: the live catalog's default, not the built-in fallback's.
      setActiveModel(
        provider === 'openai' && authKind === 'oauth' && codexModels.models[0]
          ? codexModels.models[0].id
          : getRecommendedModel(provider, authKind),
      );
    } else {
      setActiveModel(null);
    }
  }, [codexModels.models]);

  // Event / error / connection handlers shared by a fresh run and a re-attached one (page reload, second tab).
  const handleRunEvent = (event: DebateEvent) => {
    const type = (event as { type: string }).type;
    // Heartbeats are liveness only: keep them out of the transcript/event list.
    if (type === 'run.heartbeat') {
      const hb = event as unknown as HeartbeatEvent;
      setRunClock((c) => ({ ...c, lastHeartbeatAt: Date.now(), heartbeat: hb }));
      return;
    }
    // Run bookkeeping events update the clock, they are not part of the debate transcript.
    if (type === 'run.cancelling') {
      setRunClock((c) => ({ ...c, cancelling: true }));
      return;
    }
    if (type === 'run.attached' || type === 'run.finished' || type === 'run.resync') {
      if (type === 'run.finished') {
        const fin = event as unknown as { state?: string; cost_report?: CostReport | null };
        const state = fin.state;
        setRunClock((c) => ({
          ...c, endedAt: c.endedAt ?? Date.now(), cancelling: false, usagePartial: state !== undefined && state !== 'completed',
          costReport: fin.cost_report ?? c.costReport ?? null,
        }));
      }
      return;
    }
    setRunClock((c) => ({ ...c, lastEventAt: Date.now() }));
    setEvents((prev) => [...prev, event]);
  };
  const handleRunError = (err: unknown) => {
    const msg = err instanceof Error ? err.message : t('analyze.err.stream');
    setRunClock((c) => ({ ...c, streamError: msg }));
    setStreamError(msg);
  };
  const handleConnection = (state: RunConnection) => {
    setRunClock((c) => ({ ...c, connection: state, streamError: state === 'connected' ? null : c.streamError }));
    if (state === 'connected') setStreamError(null);
  };

  /** Re-attach to an engine-side run and replay it from the start (reload, second tab, finished run shown again). */
  const attachGenRef = useRef(0);
  const resumeFromEngine = async (st: RunStatusSnapshot, opts: { follow?: boolean } = {}) => {
    // Only the newest attach may touch the page: switching to the next batch ticker closes the old subscription and its
    // late events / its `finally` must not clobber the new one.
    const gen = ++attachGenRef.current;
    if (opts.follow) handleRef.current?.close();
    cancellingRef.current = !!st.cancelling;
    isStreamingRef.current = st.active;
    setEvents([]);
    setStreamError(null);
    setAssetClass(null);
    if (st.ticker) setTicker(st.ticker);
    if (st.trade_date) setDate(st.trade_date);
    setIsStreaming(st.active);
    setRunCtx({ provider: st.provider, model: st.model });
    const startedAt = st.started_at * 1000;
    setRunClock({
      ...EMPTY_CLOCK,
      startedAt,
      endedAt: st.ended_at ? st.ended_at * 1000 : null,
      lastEventAt: st.since_last_event_s === null ? null : Date.now() - st.since_last_event_s * 1000,
      resumed: st.active,
      cancelling: !!st.cancelling,
      usagePartial: !st.active && st.state !== 'completed',
      costReport: st.cost_report ?? null,
      connection: 'connected',
    });
    // A followed batch run is never remembered: a reload re-derives it from GET /batches (D71).
    if (!opts.follow) rememberRun({ runId: st.run_id, ticker: st.ticker ?? '' });
    try {
      const handle = await resumeRun(
        st.run_id,
        (event) => {
          if (gen !== attachGenRef.current) return;
          const t0 = (event as { type: string }).type;
          if (t0 === 'run.heartbeat') return handleRunEvent(event);
          // Replayed events must not make a long-quiet run look freshly active: only live ones touch the clock.
          const seq = (event as { seq?: number }).seq;
          if (typeof seq === 'number' && seq <= st.last_seq) {
            if (t0 === 'run.attached' || t0 === 'run.finished' || t0 === 'run.resync') return;
            setEvents((prev) => [...prev, event]);
            return;
          }
          handleRunEvent(event);
        },
        (err) => { if (gen === attachGenRef.current) handleRunError(err); },
        (state) => { if (gen === attachGenRef.current) handleConnection(state); },
        0,
      );
      if (gen !== attachGenRef.current) {
        handle.close();
        return;
      }
      handleRef.current = handle;
      await handle.done;
    } catch (err) {
      if (gen === attachGenRef.current) handleRunError(err);
    } finally {
      if (gen !== attachGenRef.current) return;
      setRunClock((c) => (c.startedAt && !c.endedAt ? { ...c, endedAt: Date.now() } : c));
      handleRef.current = null;
      isStreamingRef.current = false;
      setIsStreaming(false);
    }
  };

  // Batch analysis (D71). A batch runs in the engine one ticker at a time; every ticker is an ordinary run. When this tab
  // has no run of its own, Analyze FOLLOWS the batch: it shows the current ticker's live debate exactly like a single
  // analysis and moves to the next ticker by itself. A run this tab started (or remembers) always wins; then the batch
  // only shows a one-line notice. Following never remembers a run and never cancels anything on leave.
  const [activeBatch, setActiveBatch] = useState<BatchSnapshot | null>(null);
  const [followId, setFollowId] = useState<string | null>(null);
  const [followSnap, setFollowSnap] = useState<BatchSnapshot | null>(null);
  const [followEnded, setFollowEnded] = useState<BatchSnapshot | null>(null);
  const [followStopping, setFollowStopping] = useState(false);
  const [followStopFailed, setFollowStopFailed] = useState(false);
  const [followPollError, setFollowPollError] = useState(false);
  const followIdRef = useRef<string | null>(null);
  const followAttachedRef = useRef<string | null>(null);
  const followStopSentRef = useRef(false);
  const followRefreshRef = useRef<(() => void) | null>(null);
  const resumeFromEngineRef = useRef(resumeFromEngine);
  resumeFromEngineRef.current = resumeFromEngine;

  const startFollow = (batchId: string) => {
    if (followIdRef.current === batchId) return;
    followIdRef.current = batchId;
    followAttachedRef.current = null;
    followStopSentRef.current = false;
    setFollowEnded(null);
    setFollowSnap(null);
    setFollowStopping(false);
    setFollowStopFailed(false);
    setFollowPollError(false);
    setActiveBatch(null);
    setFollowId(batchId);
  };

  // One poll chain per followed batch: 2 s, backing off 2→5→10 s on errors, immediate on foreground. A stale response never
  // overwrites a newer one. Terminal state stops the chain and keeps the last ticker's content on screen.
  useEffect(() => {
    if (!followId) return;
    let dead = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let seq = 0;
    let applied = 0;
    let errors = 0;
    const BACKOFF = [2000, 5000, 10000];
    const schedule = (ms: number) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void poll(), ms);
    };
    const apply = async (snap: BatchSnapshot) => {
      setFollowSnap(snap);
      if (snap.cancel_requested) followStopSentRef.current = true;
      if (!snap.active) {
        followIdRef.current = null;
        setFollowId(null);
        setFollowSnap(null);
        setFollowEnded(snap);
        setFollowStopping(false);
        return;
      }
      const rid = snap.current_run?.run_id ?? null;
      if (rid && rid !== followAttachedRef.current) {
        followAttachedRef.current = rid;
        try {
          const st = await getRunStatus(rid);
          if (dead) return;
          if (st) void resumeFromEngineRef.current(st, { follow: true });
          else followAttachedRef.current = null;
        } catch (err) {
          followAttachedRef.current = null;
          throw err;
        }
      }
    };
    const poll = async () => {
      const mine = ++seq;
      try {
        const snap = await getBatch(followId);
        if (dead || mine < applied) return;
        applied = mine;
        errors = 0;
        setFollowPollError(false);
        await apply(snap);
        if (!dead && snap.active) schedule(2000);
      } catch {
        if (dead) return;
        setFollowPollError(true);
        schedule(BACKOFF[Math.min(errors, BACKOFF.length - 1)]);
        errors += 1;
      }
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        if (timer) clearTimeout(timer);
        void poll();
      }
    };
    followRefreshRef.current = () => void poll();
    document.addEventListener('visibilitychange', onVisible);
    void poll();
    return () => {
      dead = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
      followRefreshRef.current = null;
    };
  }, [followId]);

  // Leaving the page only unsubscribes from a followed run; the run and the batch keep going in the engine.
  useEffect(
    () => () => {
      if (followAttachedRef.current) handleRef.current?.close();
    },
    [],
  );

  // Stop while following cancels the BATCH (idempotent), never the run: sent once, a failed send can be retried, and
  // "stopping" shows only after the engine accepted it.
  const stopInFlightRef = useRef(false);
  const stopBatch = () => {
    const id = followIdRef.current;
    if (!id || followStopSentRef.current || stopInFlightRef.current) return;
    stopInFlightRef.current = true;
    setFollowStopFailed(false);
    void cancelBatch(id).then(
      (snap) => {
        stopInFlightRef.current = false;
        followStopSentRef.current = true;
        setFollowStopping(true);
        if (snap && followIdRef.current === id && snap.active) setFollowSnap(snap);
      },
      () => {
        stopInFlightRef.current = false;
        setFollowStopFailed(true);
      },
    );
  };
  const stopBatchRef = useRef(stopBatch);
  stopBatchRef.current = stopBatch;

  // Keep the one-line notice honest (a run of this tab is showing): re-check every 10 s and on foreground return.
  const batchShown = activeBatch !== null;
  useEffect(() => {
    if (!batchShown || followId) return;
    let dead = false;
    const check = async () => {
      try {
        const latest = (await listBatches(1))[0] ?? null;
        if (!dead) setActiveBatch(latest?.active ? latest : null);
      } catch {
        // keep the current notice until a read succeeds
      }
    };
    const id = setInterval(() => void check(), 10_000);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void check();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      dead = true;
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [batchShown, followId]);

  // Analyze stays mounted while hidden, so a batch started on the Watchlist is picked up when this page is shown again
  // (hash → #analyze) or the window returns to the foreground.
  useEffect(() => {
    const detect = async () => {
      if (followIdRef.current) return;
      try {
        const latest = (await listBatches(1))[0] ?? null;
        if (followIdRef.current) return;
        if (!latest?.active) return;
        if (isStreamingRef.current) setActiveBatch(latest);
        else startFollow(latest.batch_id);
      } catch {
        // engine not ready: nothing to follow
      }
    };
    const onHash = () => {
      if (window.location.hash.replace(/^#/, '') === 'analyze') void detect();
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible' && window.location.hash.replace(/^#/, '') === 'analyze') void detect();
    };
    window.addEventListener('hashchange', onHash);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('hashchange', onHash);
      document.removeEventListener('visibilitychange', onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // On open / reload: re-attach to the run this tab was showing, else to the one active run (second tab).
  const resumeTriedRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // Source of truth for the notice (D58 A9): the latest batch, only when it is active. A failure here must not block the resume.
      let batchActive = false;
      let latest: BatchSnapshot | null = null;
      try {
        latest = (await listBatches(1))[0] ?? null;
        batchActive = !!latest?.active;
        if (!cancelled) setActiveBatch(batchActive ? latest : null);
      } catch {
        // engine not ready / older engine without /batches: no notice
      }
      try {
        if (isStreamingRef.current || resumeTriedRef.current) return;
        resumeTriedRef.current = true;
        const last = recallRun();
        let st: RunStatusSnapshot | null = last ? await getRunStatus(last.runId) : null;
        if (!st && last) forgetRun();
        if (!st) {
          const { runs } = await getActiveRuns();
          st = runs.find((r) => !r.batch_id) ?? null;
        }
        // A batch run is never attached as "my" run (a remembered one is forgotten); it is followed through GET /batches.
        if (st && st.batch_id) {
          forgetRun();
          st = null;
        }
        if (cancelled || isStreamingRef.current) return;
        if (!st) {
          if (batchActive && latest) startFollow(latest.batch_id);
          return;
        }
        await resumeFromEngine(st);
      } catch {
        // No engine yet / nothing to resume: the normal empty page.
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onAnalyze = async () => {
    if (!engineReadyRef.current || isStreamingRef.current || batchBlockedRef.current) return;
    // A saved model the provider has retired is kept in the picker (labelled) but cannot start a run: say so, change nothing.
    const guardModel = activeModelRef.current;
    if (isRetiredModel(activeProviderRef.current, guardModel)) {
      setStreamError(t('analyze.err.retiredModel', { model: guardModel ?? '' }));
      return;
    }
    setEvents([]);
    setStreamError(null);
    setCopied(false);
    setFollowEnded(null);
    forgetRun();
    cancellingRef.current = false;
    // Reset asset class so the previous run's "crypto" badge doesn't bleed
    // into a new equity analysis (or vice versa) before the first
    // data.summary event arrives. dataProvider stays as-is — it's the
    // engine default until overridden.
    setAssetClass(null);
    isStreamingRef.current = true;
    setIsStreaming(true);
    setRunClock({ ...EMPTY_CLOCK, startedAt: Date.now() });

    // Snapshot the provider/auth/model at click-time through refs so a
    // Settings-tab state change racing with the multiple awaits inside
    // runAnalysis can't leave the request using a now-stale combo.
    const provider = activeProviderRef.current;
    const authKind = openaiAuthKindRef.current ?? 'api_key';
    const model =
      activeModelRef.current ??
      (provider ? getRecommendedModel(provider, authKind) : '');

    setRunCtx({ provider, model: model || null });
    try {
      const result = await runAnalysis(
        {
          ticker,
          trade_date: date,
          provider,
          openaiAuthKind: authKind,
          model,
          reportLanguage: locale,
          twFlow,
          twDexter,
        },
        {
          onEvent: handleRunEvent,
          onError: handleRunError,
          onConnection: handleConnection,
          // Open the existing CostGuard modal and await the user's choice.
          // Preserves the established UX where Cancel aborts and Override
          // re-tries the reservation with override=true (handled inside
          // runAnalysis).
          onCostBlocked: (block) =>
            new Promise<boolean>((resolve) => {
              costGuardResolverRef.current = resolve;
              setCostGuardBlocked({
                over_dimension: block.over_dimension,
                spend: block.spend,
                config: block.config,
                est_cost_usd: block.est_cost_usd,
              });
            }),
        },
      );

      if (result.kind === 'cancelled') {
        setStreamError(t('analyze.err.costCap'));
        return;
      }

      handleRef.current = result.handle;
      await result.handle.done;
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('analyze.err.streamFailed');
      setRunClock((c) => ({ ...c, streamError: msg }));
      setStreamError(msg);
    } finally {
      setRunClock((c) => (c.startedAt ? { ...c, endedAt: Date.now() } : c));
      handleRef.current = null;
      isStreamingRef.current = false;
      setIsStreaming(false);
    }
  };

  // Stop is an explicit cancel: the engine stops the analysis and settles its cost. (Leaving the page only unsubscribes.)
  // One entry point for every stop gesture (button, menu, shortcut, New analysis): idempotent here AND in the handle, and the UI
  // shows "cancelling" until the engine reports the end.
  const cancellingRef = useRef(false);
  const requestCancel = () => {
    const h = handleRef.current;
    if (!h || cancellingRef.current) return; // already asked: Stop + shortcut + menu + New analysis never ask twice
    cancellingRef.current = true;
    setRunClock((c) => (c.cancelling ? c : { ...c, cancelling: true }));
    void h.cancel().then((ok) => {
      if (ok === false) {
        cancellingRef.current = false;
        setRunClock((c) => ({ ...c, cancelling: false }));
      }
    });
  };
  // Button, menu and shortcut share one gesture: following a batch → stop the batch; otherwise → stop this run.
  const stopGesture = () => {
    if (followIdRef.current) stopBatchRef.current();
    else requestCancel();
  };
  const onStop = stopGesture;

  // Listen for the App menu "Stop streaming" command.
  useEffect(() => {
    const bridge = window.tradingAgentsLab;
    if (!bridge?.onMenuCommand) return;
    return bridge.onMenuCommand('menu:stop-stream', () => {
      stopGesture();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Page-level keyboard shortcuts:
  //   Cmd/Ctrl + Enter → run analysis
  //   Cmd/Ctrl + .     → stop streaming
  // (Cmd+, for Settings + Cmd+1..3 for nav are handled by the App menu.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;
      if (!meta) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        if (engineReadyRef.current && !isStreamingRef.current && !batchBlockedRef.current) onAnalyze();
      } else if (e.key === '.') {
        e.preventDefault();
        stopGesture();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, date]);

  const onCopyTranscript = async () => {
    if (!events.length) return;
    const md = buildTranscriptMarkdown(events, locale, runClock.costReport ?? null);
    try {
      await navigator.clipboard.writeText(md);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      setStreamError(
        t('analyze.err.copyFailed', {
          error: err instanceof Error ? err.message : t('status.unknown'),
        }),
      );
    }
  };

  // Full-page reading view: builds the standalone HTML document and hands it
  // to main, which writes it under userData/transcripts and opens the default
  // browser. Imported lazily — react-dom/server + the generator stay out of
  // the initial bundle for what is a post-run affordance.
  // The PDF button builds the identical document (events + current cost report) only when clicked.
  const buildReportHtml = useCallback(async () => {
    const { buildTranscriptHtml } = await import('../lib/transcript-html');
    return buildTranscriptHtml(events, locale, runClock.costReport ?? null);
  }, [events, locale, runClock.costReport]);

  const onOpenHtml = async () => {
    if (!events.length) return;
    try {
      const { html, suggestedName } = await buildReportHtml();
      await window.tradingAgentsLab.transcript.openHtml(html, suggestedName);
    } catch (err) {
      setStreamError(
        t('analyze.err.openHtmlFailed', {
          error: err instanceof Error ? err.message : t('status.unknown'),
        }),
      );
    }
  };

  // One analysis at a time: while a batch is active (followed here or only noticed) starting one is disabled.
  const followActive = followId !== null;
  const batchBlocked = followActive || activeBatch !== null;
  const batchBlockedRef = useRef(false);
  batchBlockedRef.current = batchBlocked;
  const busy = isStreaming || followActive;
  const buttonDisabled = engineStatus !== 'running' || isStreaming || batchBlocked;
  const followStopShown = followStopping || !!followSnap?.cancel_requested;
  const followTicker = followSnap?.current_run && followSnap.current_index !== null
    ? followSnap.items.find((i) => i.index === followSnap.current_index)?.ticker ?? ticker
    : null;
  // A run that ended without a result (cancelled / failed / interrupted) can still be exported: its usage report is the engine's
  // current partial one (same object the status panel shows), so no session.complete has to be invented.
  const transcriptReady =
    events.some((e) => e.type === 'session.complete') || (!isStreaming && events.length > 0 && !!runClock.usagePartial && !!runClock.costReport);

  const onCostGuardConfirm = useCallback(() => {
    setCostGuardBlocked(null);
    costGuardResolverRef.current?.(true);
    costGuardResolverRef.current = null;
  }, []);

  const onCostGuardCancel = useCallback(() => {
    setCostGuardBlocked(null);
    costGuardResolverRef.current?.(false);
    costGuardResolverRef.current = null;
  }, []);

  return (
    <div className={styles.page}>
      {costGuardBlocked && (
        <CostGuardModal
          overDimension={costGuardBlocked.over_dimension}
          spend={costGuardBlocked.spend}
          config={costGuardBlocked.config}
          estCostUsd={costGuardBlocked.est_cost_usd}
          onConfirm={onCostGuardConfirm}
          onCancel={onCostGuardCancel}
        />
      )}
      <header className={styles.pageHeader}>
        <div className={styles.pageHeaderTitleBlock}>
          <h1 className={styles.pageTitle}>{t('analyze.title')}</h1>
          <p className={styles.pageSubtitle}>{rich('analyze.subtitle')}</p>
        </div>
        <div className={styles.pageHeaderProvider}>
          <label className={styles.providerLabel} htmlFor="run-with">
            {t('analyze.runWith')}
          </label>
          <select
            id="run-with"
            className={styles.providerSelect}
            value={activeProvider ?? ''}
            onChange={(e) => onSelectProvider(e.target.value as LLMProvider | '')}
            disabled={busy || engineStatus !== 'running'}
            data-testid="provider-select"
          >
            {PROVIDER_PRIORITY.map((p) => {
              const configured = configuredProviders.has(p);
              const isOpenAIOAuth = p === 'openai' && openaiAuthKind === 'oauth';
              const label = configured
                ? isOpenAIOAuth
                  ? t('analyze.option.oauth', { provider: providerLabel(p, t) })
                  : providerLabel(p, t)
                : t('analyze.option.configure', { provider: providerLabel(p, t) });
              return (
                <option key={p} value={p} disabled={!configured}>
                  {label}
                </option>
              );
            })}
            {configuredProviders.size === 0 && (
              <option value="" disabled>
                {t('analyze.option.stub')}
              </option>
            )}
          </select>
          {activeProvider && availableModels.length > 0 && (
            <select
              className={styles.providerSelect}
              value={activeModel ?? ''}
              onChange={(e) => onSelectModel(e.target.value)}
              disabled={busy || engineStatus !== 'running'}
              aria-label={t('analyze.model.aria')}
              data-testid="model-select"
            >
              {availableModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                  {m.recommended ? t('analyze.model.recommended') : ''}
                  {m.note ? t('analyze.model.note', { note: modelNoteLabel(m.note, t) }) : ''}
                  {modelMarkerSuffix(m, t)}
                </option>
              ))}
            </select>
          )}
          {(manualProvider ||
            (activeProvider &&
              activeModel !== null &&
              activeModel !==
                (activeProvider === 'openai' && openaiAuthKind === 'oauth' && codexModels.models[0]
                  ? codexModels.models[0].id
                  : getRecommendedModel(activeProvider, openaiAuthKind)))) && (
            <button
              type="button"
              className={styles.providerReset}
              onClick={onResetOverrides}
              disabled={busy}
              title={t('analyze.resetTitle')}
              aria-label={t('analyze.resetTitle')}
            >
              {t('analyze.reset')}
            </button>
          )}
        </div>
      </header>

      {activeProvider === 'openai' && openaiAuthKind === 'oauth' && (
        // Make live-vs-fallback visible. A silent fallback looks exactly like
        // success until OpenAI's next model retirement breaks OAuth again.
        <p className={styles.helper} data-testid="codex-models-source">
          {codexModels.loading
            ? t('analyze.codex.checking')
            : codexModels.source === 'live'
              ? t('analyze.codex.live')
              : codexModels.source === 'cached' && !codexModels.error
                ? t('analyze.codex.cached')
                : t('analyze.codex.fallback', {
                    detail: codexModels.error
                      ? t('analyze.codex.fallbackDetail', {
                          error: describeEngineError(codexModels.error, i18n),
                        })
                      : '',
                  })}{' '}
          <button
            type="button"
            className={styles.providerReset}
            onClick={codexModels.refresh}
            disabled={codexModels.loading || isStreaming}
            data-testid="codex-models-refresh"
          >
            {t('analyze.codex.refresh')}
          </button>
        </p>
      )}

      {activeProvider === null && <ProviderSetupCallout />}

      <section className={styles.card}>
        <div className={styles.fieldRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="ticker">{t('analyze.ticker')}</label>
            <input
              id="ticker"
              className={styles.input}
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="NVDA"
              maxLength={8}
              disabled={busy}
              data-testid="ticker-input"
            />
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="date">{t('analyze.asOfDate')}</label>
            <input
              id="date"
              type="date"
              className={styles.input}
              value={date}
              max={new Date().toISOString().split('T')[0]}
              onChange={(e) => setDate(e.target.value)}
              disabled={busy}
              data-testid="date-input"
            />
          </div>
          <div className={styles.fieldButton}>
            {followActive || isStreaming ? (
              <button
                className={`${styles.button} ${styles.buttonStop}`}
                onClick={onStop}
                disabled={followActive ? followStopShown : !!runClock.cancelling}
                type="button"
                data-testid="stop-button"
              >
                {followActive ? (followStopShown ? t('batch.follow.stopping') : t('batch.follow.stop')) : t('analyze.stop')}
              </button>
            ) : (
              <button
                className={styles.button}
                disabled={buttonDisabled}
                onClick={onAnalyze}
                type="button"
                data-testid="analyze-button"
              >
                {t('analyze.run')}
              </button>
            )}
          </div>
        </div>
        {/^\d{4,6}[A-Z]?(\.TWO?)?$/i.test(ticker.trim()) && (
          <div className={styles.errorBanner} data-testid="taiwan-ticker-hint">
            <p style={{ margin: 0 }}>{t('analyze.taiwanHint')}</p>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
              <span>{t('analyze.twFlow.label')}</span>
              <select
                value={twFlow}
                onChange={(e) => setTwFlow(e.target.value as 'full' | 'simplified')}
                disabled={busy}
                data-testid="tw-flow-select"
              >
                <option value="full">{t('analyze.twFlow.full')}</option>
                <option value="simplified">{t('analyze.twFlow.simplified')}</option>
              </select>
            </label>
            <p style={{ margin: '6px 0 0', fontSize: 'var(--tal-text-sm)' }} data-testid="tw-flow-note">
              {twFlow === 'full' ? t('analyze.twFlow.fullNote') : t('analyze.twFlow.simplifiedNote')}
            </p>
            {twFlow === 'full' && (
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
                <input
                  type="checkbox"
                  checked={twDexter}
                  onChange={(e) => setTwDexter(e.target.checked)}
                  disabled={busy}
                  data-testid="tw-dexter-toggle"
                />
                <span>{t('analyze.twDexter.label')}</span>
              </label>
            )}
            {twFlow === 'full' && (
              <p style={{ margin: '4px 0 0', fontSize: 'var(--tal-text-sm)' }} data-testid="tw-dexter-note">
                {t('analyze.twDexter.note')}
              </p>
            )}
          </div>
        )}
        <div className={styles.cardFooter}>
          <p className={styles.helper}>
            {engineStatus === 'pending' && t('analyze.helper.pending')}
            {engineStatus === 'running' && !isStreaming && activeProvider && activeModel &&
              t('analyze.helper.live', {
                provider:
                  openaiAuthKind === 'oauth' && activeProvider === 'openai'
                    ? t('analyze.option.oauth', { provider: providerLabel(activeProvider, t) })
                    : providerLabel(activeProvider, t),
                model: activeModel,
                language: locale === 'zh-TW' ? '繁體中文' : 'English',
              })}
            {engineStatus === 'running' && !isStreaming && !activeProvider &&
              t('analyze.helper.stub')}
            {engineStatus === 'running' && isStreaming &&
              t('analyze.helper.streaming')}
            {engineStatus === 'error' &&
              t('analyze.helper.engineError', {
                error: engineError ? describeEngineError(engineError, i18n) : t('status.unknown'),
              })}
          </p>
          {transcriptReady && !isStreaming && (
            <>
              <button
                className={styles.transcriptButton}
                onClick={onCopyTranscript}
                type="button"
              >
                {copied ? t('analyze.copied') : t('analyze.copyTranscript')}
              </button>
              <button
                className={styles.transcriptButton}
                onClick={onOpenHtml}
                type="button"
                data-testid="open-html-transcript"
              >
                {t('analyze.openHtml')}
              </button>
              <ReportPdfButton className={styles.transcriptButton} build={buildReportHtml} testId="export-pdf" />
            </>
          )}
        </div>
        {streamError && (
          <p className={styles.errorBanner}>
            {t('analyze.streamError', { error: describeEngineError(streamError, i18n) })}
          </p>
        )}
        {followActive && (
          <div className={styles.batchBanner} role="status" data-testid="analyze-batch-follow">
            <span>
              {followSnap?.current_run && followSnap.current_index !== null
                ? t('batch.follow.banner', { current: followSnap.current_index + 1, total: followSnap.total, ticker: followTicker ?? '' })
                : t('batch.follow.preparing')}
            </span>
            {followStopShown && <span data-testid="analyze-batch-stopping"> {t('batch.follow.stopping')}</span>}{' '}
            <a href="#watchlist">{t('batch.follow.link')}</a>
            {followPollError && <p className={styles.batchNote} data-testid="analyze-batch-poll-error">{t('batch.follow.pollError')}</p>}
          </div>
        )}
        {followActive && followStopFailed && (
          <p className={styles.errorBanner} data-testid="analyze-batch-stop-failed">{t('batch.follow.stopFailed')}</p>
        )}
        {!followActive && followEnded && (
          <div className={styles.batchBanner} role="status" data-testid="analyze-batch-ended">
            <span>
              {t(`batch.follow.ended.${followEnded.state === 'queued' || followEnded.state === 'running' ? 'stopped' : followEnded.state}`, {
                done: followEnded.counts.completed,
                failed: followEnded.counts.failed,
                skipped: followEnded.counts.skipped,
              })}
            </span>{' '}
            <a href="#watchlist">{t('batch.follow.link')}</a>
            {followEnded.terminal_code && FOLLOW_TERMINAL_CODES.includes(followEnded.terminal_code) && (
              <p className={styles.batchNote} data-testid="analyze-batch-ended-reason">{t(`batch.terminal.${followEnded.terminal_code}`)}</p>
            )}
          </div>
        )}
        {!followActive && activeBatch && (
          <p role="status" data-testid="analyze-batch-notice" className={styles.batchBanner}>
            {activeBatch.current_index === null
              ? t('batch.analyzeNoticePreparing')
              : t('batch.analyzeNotice', { current: activeBatch.current_index + 1, total: activeBatch.total })}{' '}
            <a href="#watchlist">{t('batch.analyzeNoticeLink')}</a>
          </p>
        )}
        {batchBlocked && <p className={styles.helper} data-testid="analyze-batch-blocked">{t('batch.follow.blocked')}</p>}
      </section>

      {/* Status cards used to live here; lifted to the App-shell `<StatusStrip>`
          so the user can glance up from any page (founder feedback 2026-05-09).
          The Analyze page surfaces engine error inline below the streamError
          banner when needed; everything else is on the strip. */}

      {engineStatus === 'error' && engineError && (
        <div className={styles.engineErrorBanner}>
          {rich('analyze.engineError', { error: describeEngineError(engineError, i18n) })}
        </div>
      )}

      <RunStatus events={events} clock={runClock} streaming={isStreaming} provider={runCtx.provider} model={runCtx.model} />

      <DebateStream events={events} isStreaming={isStreaming} />

      <section className={styles.disclaimer}>{rich('analyze.disclaimer')}</section>
    </div>
  );
}

export default Analyze;
