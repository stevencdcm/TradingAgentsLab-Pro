import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import styles from './Settings.module.css';
import UpdatesSection from '../components/UpdatesSection';
import { useI18n, type I18n, type MessageKey } from '../i18n';
import { describeEngineError } from '../lib/engine-errors';
import { formatRelativeTime } from '../lib/relative-time';
import { STANCES, stanceLabel } from '../lib/stance';
import {
  deleteSecret,
  getAvailability,
  getSecret,
  listSecrets,
  onSecretsRecovered,
  setSecret,
  type CorruptionRecovery,
  type SecretListing,
  type SecretsAvailability,
} from '../lib/secrets';
import {
  disconnectOpenAIOAuth,
  getOpenAICredentialsForRequest,
  getOpenAIOAuthStatus,
  onOAuthProgress,
  onOAuthPrompt,
  startOpenAIOAuthLogin,
  submitOAuthPromptResponse,
  type OAuthStatus,
} from '../lib/oauth';
import {
  getCostGuardState,
  updateCostGuardConfig,
  type CostGuardConfig,
  type SpendState,
} from '../lib/cost-guard';
import {
  approveTelegramChat,
  denyTelegramChat,
  getLocalRuntimes,
  listLocalModels,
  testLocalConnection,
  LOCAL_API_KEY_SECRET_KEY,
  getTelegramBotStatus,
  LOCAL_MODEL_SECRET_KEY,
  OPENAI_CODEX_DEFAULT_MODEL,
  PROVIDER_SECRET_KEY,
  refreshTelegramBotCredentials,
  startTelegramBot,
  stopTelegramBot,
  testLLMConnection,
  type LLMProvider,
  type LLMTestResult,
  type LocalRuntime,
  type TelegramBotStatus,
  type TelegramPendingApproval,
} from '../lib/engine-client';
import {
  loadLocalApiKey,
  loadLocalConfig,
  saveLocalApiKey,
  saveLocalConfig,
  type LocalLLMConfig,
} from '../lib/local-llm';
import {
  loadWebhooks,
  saveWebhooks,
  newWebhookId,
  newWebhookFilter,
  type WebhookConfig,
  type WebhookKind,
} from '../lib/webhooks';
import {
  ALL_ANALYSTS,
  ANALYSTS_NEEDING_DATA_KEY,
  EFFORT_LEVELS,
  NEWS_SOURCES,
  loadProConfig,
  saveProConfig,
  type AnalystId,
  type EffortLevel,
  type NewsSource,
  type ProConfigState,
} from '../lib/pro-config';
// OpenAI OAuth is hidden in Pro v1 — see lib/feature-flags.ts for the why
// and everything the flag gates (this row + provider resolution).
import { OAUTH_ENABLED } from '../lib/feature-flags';

type Tab =
  | 'llm'
  | 'data'
  | 'analysis'
  | 'webhooks'
  | 'channels'
  | 'costguard'
  | 'updates'
  | 'about';

interface TabDef {
  id: Tab;
  labelKey: MessageKey;
  descKey: MessageKey;
}

const TABS: TabDef[] = [
  { id: 'llm', labelKey: 'settings.tab.llm', descKey: 'settings.tab.llm.desc' },
  { id: 'data', labelKey: 'settings.tab.data', descKey: 'settings.tab.data.desc' },
  { id: 'analysis', labelKey: 'settings.tab.analysis', descKey: 'settings.tab.analysis.desc' },
  { id: 'webhooks', labelKey: 'settings.tab.webhooks', descKey: 'settings.tab.webhooks.desc' },
  { id: 'channels', labelKey: 'settings.tab.channels', descKey: 'settings.tab.channels.desc' },
  { id: 'costguard', labelKey: 'settings.tab.costguard', descKey: 'settings.tab.costguard.desc' },
  { id: 'updates', labelKey: 'settings.tab.updates', descKey: 'settings.tab.updates.desc' },
  { id: 'about', labelKey: 'settings.tab.about', descKey: 'settings.tab.about.desc' },
];

interface SecretRow {
  /** Stable key in `secrets.json` — e.g. "llm:openai", "data:alpaca". */
  secretKey: string;
  /** Human label for the row (provider brand; not translated). */
  name: string;
  /** Translated label (overrides `name`) for rows whose name is a descriptor. */
  nameKey?: MessageKey;
  /** Sub-line explaining the provider (i18n key). */
  noteKey: MessageKey;
  /** Pill content key (no functional meaning yet). */
  pillKey: MessageKey;
  pillVariant: 'default' | 'planned' | 'optional';
  /** Input affordance — `password` (default) or `text` for non-secret URLs. */
  fieldType?: 'password' | 'text';
  /** Placeholder for the input (API-key formats are not translated). */
  placeholder?: string;
  /** Translated placeholder (overrides `placeholder`). */
  placeholderKey?: MessageKey;
  /** When set, a "Test" button appears once the key is stored. Clicking
   * runs a 1-token completion against the live provider to validate the
   * credential. Only meaningful for LLM rows. */
  testProvider?: Exclude<LLMProvider, 'local'>;
}

const LLM_PROVIDERS: SecretRow[] = [
  {
    secretKey: 'llm:openai',
    name: 'OpenAI',
    noteKey: 'settings.row.openai.note',
    pillKey: 'settings.pill.apiKey',
    pillVariant: 'default',
    placeholder: 'sk-…',
    testProvider: 'openai',
  },
  {
    secretKey: 'llm:anthropic',
    name: 'Anthropic',
    noteKey: 'settings.row.anthropic.note',
    pillKey: 'settings.pill.apiKeyOnly',
    pillVariant: 'default',
    placeholder: 'sk-ant-…',
    testProvider: 'anthropic',
  },
  {
    secretKey: 'llm:openrouter',
    name: 'OpenRouter',
    noteKey: 'settings.row.openrouter.note',
    pillKey: 'settings.pill.compatible',
    pillVariant: 'default',
    placeholder: 'sk-or-…',
    testProvider: 'openrouter',
  },
  {
    secretKey: 'llm:gemini',
    name: 'Google Gemini',
    noteKey: 'settings.row.gemini.note',
    pillKey: 'settings.pill.compatible',
    pillVariant: 'default',
    placeholder: 'AIza…',
    testProvider: 'gemini',
  },
  {
    secretKey: 'llm:xai',
    name: 'xAI Grok',
    noteKey: 'settings.row.xai.note',
    pillKey: 'settings.pill.apiKeyOnly',
    pillVariant: 'default',
    placeholder: 'xai-…',
    testProvider: 'xai',
  },
  {
    secretKey: 'llm:minimax',
    name: 'MiniMax',
    noteKey: 'settings.row.minimax.note',
    pillKey: 'settings.pill.apiKeyOnly',
    pillVariant: 'default',
    placeholder: 'eyJ… (JWT)',
    testProvider: 'minimax',
  },
];

// Alpaca auth requires TWO values — Key ID (sent as APCA-API-KEY-ID) and
// Secret Key (sent as APCA-API-SECRET-KEY). Both are minted together on the
// Alpaca dashboard; the Secret is shown only once at generation time.
//
// Per project positioning (locked 2026-05-09 — see CLAUDE.md §3 + memory):
// TradingAgentsLab is an analysis tool, not an execution platform. Alpaca
// is integrated for HIGH-QUALITY MARKET DATA only — not order execution.
// The engine's URL constants point to data.alpaca.markets and (read-only)
// paper-api.alpaca.markets. Live keys (api.alpaca.markets) have nowhere
// to go in our code: pasting a live key here will store it but the system
// will error out at request time. Defense-in-depth via endpoint constants,
// not a guard flag.
const DATA_PROVIDERS: SecretRow[] = [
  {
    secretKey: 'data:alpaca-key-id',
    name: 'Alpaca Markets: Key ID',
    nameKey: 'settings.row.alpacaKeyId.name',
    noteKey: 'settings.row.alpacaKeyId.note',
    pillKey: 'settings.pill.keyId',
    pillVariant: 'default',
    placeholder: 'PKxxxxxxxxxxxxxxxx',
  },
  {
    secretKey: 'data:alpaca-secret',
    name: 'Alpaca Markets: Secret',
    nameKey: 'settings.row.alpacaSecret.name',
    noteKey: 'settings.row.alpacaSecret.note',
    pillKey: 'settings.pill.secret',
    pillVariant: 'default',
    placeholder: 'data secret key',
    placeholderKey: 'settings.row.alpacaSecret.placeholder',
  },
  {
    secretKey: 'data:alpha-vantage',
    name: 'Alpha Vantage: API key',
    nameKey: 'settings.row.alphaVantage.name',
    noteKey: 'settings.row.alphaVantage.note',
    pillKey: 'settings.pill.analysts',
    pillVariant: 'default',
    placeholder: 'Alpha Vantage API key',
    placeholderKey: 'settings.row.alphaVantage.placeholder',
  },
];

function Settings() {
  const { t, rich } = useI18n();
  const [active, setActive] = useState<Tab>('llm');
  const [availability, setAvailability] = useState<SecretsAvailability | null>(null);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [listings, setListings] = useState<SecretListing[]>([]);
  const [refreshTick, setRefreshTick] = useState(0);
  // null in the common case. Populated either from the initial `availability`
  // call (recovery happened before this page mounted) or from the
  // `secrets:recovered` IPC (recovery happened while we were watching).
  const [recovery, setRecovery] = useState<CorruptionRecovery | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAvailability()
      .then((info) => {
        if (cancelled) return;
        setAvailability(info);
        if (info.corruptionRecovery) setRecovery(info.corruptionRecovery);
        if (!info.available) {
          setAvailabilityError(t('settings.err.encryptionUnavailable'));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setAvailabilityError(
            err instanceof Error ? err.message : t('settings.err.encryptionCheck'),
          );
        }
      });
    const unsubscribe = onSecretsRecovered((info) => {
      if (!cancelled) setRecovery(info);
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once; `t` only shapes error text
  }, []);

  useEffect(() => {
    if (!availability?.available) return;
    let cancelled = false;
    listSecrets()
      .then((rows) => {
        if (!cancelled) setListings(rows);
      })
      .catch(() => {
        if (!cancelled) setListings([]);
      });
    return () => {
      cancelled = true;
    };
  }, [availability, refreshTick]);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  const tab = TABS.find((x) => x.id === active) ?? TABS[0];

  const listingByKey = useMemo(() => {
    const map = new Map<string, SecretListing>();
    for (const l of listings) map.set(l.key, l);
    return map;
  }, [listings]);

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>{t('settings.title')}</h1>
        <p className={styles.pageSubtitle}>{t('settings.subtitle')}</p>
      </header>

      {availabilityError && (
        <div className={styles.availabilityBanner}>
          {rich('settings.banner.offline', { error: availabilityError })}
        </div>
      )}

      {recovery && (
        <div className={styles.availabilityBanner}>
          {rich('settings.banner.recovered', { path: recovery.backupPath })}
        </div>
      )}

      <div className={styles.layout}>
        <nav className={styles.tabs} aria-label={t('settings.sectionsAria')}>
          {TABS.map((tb) => (
            <button
              key={tb.id}
              className={`${styles.tab} ${active === tb.id ? styles.tabActive : ''}`}
              onClick={() => setActive(tb.id)}
              type="button"
            >
              {t(tb.labelKey)}
            </button>
          ))}
        </nav>

        <section className={styles.panel}>
          <header className={styles.panelHeader}>
            <h2 className={styles.panelTitle}>{t(tab.labelKey)}</h2>
            <p className={styles.panelDescription}>{t(tab.descKey)}</p>
          </header>

          {active === 'llm' && (
            <>
              {OAUTH_ENABLED && <OpenAIOAuthRow disabled={!availability?.available} />}
              <SecretRowList
                rows={LLM_PROVIDERS}
                listingByKey={listingByKey}
                disabled={!availability?.available}
                onChange={refresh}
              />
              <LocalLLMRow
                disabled={!availability?.available}
                onChange={refresh}
              />
            </>
          )}
          {active === 'data' && (
            <>
              <YfinanceRow />
              <SecretRowList
                rows={DATA_PROVIDERS}
                listingByKey={listingByKey}
                disabled={!availability?.available}
                onChange={refresh}
              />
              <AlphaVantageSignupCallout
                hasKey={listingByKey.has('data:alpha-vantage')}
                onSaved={refresh}
              />
            </>
          )}
          {active === 'analysis' && <ProAnalysisTab />}
          {active === 'webhooks' && <WebhooksTab availability={availability} />}
          {active === 'channels' && <ChannelsTab />}
          {active === 'costguard' && <CostGuardTab />}
          {active === 'updates' && (
            <div className={styles.formCard}>
              <UpdatesSection />
            </div>
          )}
          {active === 'about' && (
            <AboutTab availability={availability} secretsCount={listings.length} />
          )}
        </section>
      </div>
    </div>
  );
}

interface SecretRowListProps {
  rows: SecretRow[];
  listingByKey: Map<string, SecretListing>;
  disabled: boolean;
  onChange: () => void;
}

function SecretRowList({ rows, listingByKey, disabled, onChange }: SecretRowListProps) {
  return (
    <ul className={styles.list}>
      {rows.map((row) => (
        <SecretRowItem
          key={row.secretKey}
          row={row}
          listing={listingByKey.get(row.secretKey) ?? null}
          disabled={disabled}
          onChange={onChange}
        />
      ))}
    </ul>
  );
}

interface SecretRowItemProps {
  row: SecretRow;
  listing: SecretListing | null;
  disabled: boolean;
  onChange: () => void;
}

function SecretRowItem({ row, listing, disabled, onChange }: SecretRowItemProps) {
  const i18n = useI18n();
  const { t } = i18n;
  const rowName = row.nameKey ? t(row.nameKey) : row.name;
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Test-connection state. `null` = not tested this mount; otherwise the
   * most recent result is rendered inline. Re-running replaces. */
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LLMTestResult | null>(null);

  const onTest = async () => {
    if (!row.testProvider || !listing) return;
    setTesting(true);
    setTestResult(null);
    try {
      const apiKey = await getSecret(row.secretKey);
      if (!apiKey) {
        setTestResult({ ok: false, error: t('settings.row.noStoredKey') });
        return;
      }
      const result = await testLLMConnection({
        provider: row.testProvider,
        apiKey,
      });
      setTestResult(result);
    } catch (err) {
      setTestResult({
        ok: false,
        error: err instanceof Error ? err.message : t('settings.row.testFailed'),
      });
    } finally {
      setTesting(false);
    }
  };

  const onSave = async () => {
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      await setSecret(row.secretKey, value);
      setValue('');
      setEditing(false);
      onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('settings.row.saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async () => {
    if (!listing) return;
    if (!confirm(t('settings.row.confirmDelete', { name: rowName }))) return;
    setBusy(true);
    setError(null);
    try {
      await deleteSecret(row.secretKey);
      onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('settings.row.deleteFailed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className={styles.row}>
      <div className={styles.rowMain}>
        <div className={styles.rowName}>{rowName}</div>
        <div className={styles.rowNote}>{t(row.noteKey)}</div>
        {listing && (
          <div className={styles.rowMeta}>
            {t('settings.row.storedPre', {
              kind: row.fieldType === 'text' ? t('settings.row.kind.value') : t('settings.row.kind.key'),
            })}
            <span className={styles.hint}>{listing.hint}</span>
            {t('settings.row.storedPost', {
              when: formatRelativeTime(listing.updatedAt, i18n, listing.updatedAt),
            })}
          </div>
        )}
        {testResult && (
          <div
            className={styles.rowMeta}
            style={{
              color: testResult.ok
                ? 'var(--tal-buy, #4caf50)'
                : 'var(--tal-sell, #d64545)',
            }}
          >
            {testResult.ok
              ? t('settings.row.testOk', {
                  ms: testResult.ms ? t('settings.row.testMs', { ms: testResult.ms }) : '',
                  model: testResult.model ? t('settings.row.testModel', { model: testResult.model }) : '',
                })
              : t('settings.row.testFail', {
                  error: testResult.error
                    ? describeEngineError(testResult.error, i18n)
                    : t('settings.row.testFailedShort'),
                })}
          </div>
        )}
        {editing && (
          <div className={styles.editor}>
            <input
              type={row.fieldType === 'text' ? 'text' : 'password'}
              className={styles.input}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={row.placeholderKey ? t(row.placeholderKey) : (row.placeholder ?? '')}
              autoFocus
              disabled={busy}
            />
            <div className={styles.editorActions}>
              <button
                className={styles.actionPrimary}
                onClick={onSave}
                disabled={busy || !value}
                type="button"
              >
                {busy ? t('settings.row.saving') : listing ? t('settings.row.replace') : t('settings.row.save')}
              </button>
              <button
                className={styles.actionGhost}
                onClick={() => {
                  setEditing(false);
                  setValue('');
                  setError(null);
                }}
                disabled={busy}
                type="button"
              >
                {t('settings.row.cancel')}
              </button>
            </div>
            {error && <div className={styles.editorError}>{error}</div>}
          </div>
        )}
      </div>
      <div className={styles.rowAside}>
        <span className={`${styles.pill} ${styles[`pill_${pillState(row, listing, t).variant}`]}`}>
          {pillState(row, listing, t).label}
        </span>
        {!editing && (
          <>
            {row.testProvider && listing && (
              <button
                className={styles.rowAction}
                onClick={() => void onTest()}
                disabled={disabled || busy || testing}
                type="button"
              >
                {testing ? t('settings.row.testing') : t('settings.row.test')}
              </button>
            )}
            <button
              className={styles.rowAction}
              onClick={() => setEditing(true)}
              disabled={disabled || busy}
              type="button"
            >
              {listing ? t('settings.row.replace') : t('settings.row.configure')}
            </button>
            {listing && (
              <button
                className={styles.rowActionDanger}
                onClick={onDelete}
                disabled={disabled || busy}
                type="button"
              >
                {t('settings.row.delete')}
              </button>
            )}
          </>
        )}
      </div>
    </li>
  );
}

function YfinanceRow() {
  const t = useI18n().t;
  return (
    <div className={styles.staticRow}>
      <div className={styles.rowMain}>
        <div className={styles.rowName}>{t('settings.yfinance.name')}</div>
        <div className={styles.rowNote}>{t('settings.yfinance.note')}</div>
      </div>
      <div className={styles.rowAside}>
        <span className={`${styles.pill} ${styles.pill_success}`}>{t('settings.yfinance.pill')}</span>
      </div>
    </div>
  );
}

interface LocalLLMRowProps {
  disabled: boolean;
  onChange: () => void;
}

/**
 * Local LLM row — auto-detects running Ollama / LM Studio / llama.cpp
 * runtimes on localhost, and saves the user's (base_url, model) choice
 * as a pair of safeStorage entries.
 *
 * Detection runs once on mount + on demand via the Refresh button.
 * Empty detection is a normal state (user hasn't installed a runtime
 * yet); the manual-entry fallback covers anyone with a custom OpenAI-
 * compatible server on a non-standard port.
 *
 * No "Connect" button — once a runtime is detected, picking a model from
 * the dropdown saves the (URL, model) pair atomically. Cost is $0 by
 * design (the engine treats local sessions the same as OAuth for
 * CostGuard purposes).
 */
function LocalLLMRow({ disabled, onChange }: LocalLLMRowProps) {
  const i18n = useI18n();
  const { t, tp, rich } = i18n;
  const [runtimes, setRuntimes] = useState<LocalRuntime[] | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [saved, setSaved] = useState<LocalLLMConfig | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savingFor, setSavingFor] = useState<string | null>(null); // base_url being saved
  /** Manual entry form state (used when nothing detected or user wants
   * a non-standard runtime). */
  const [manualOpen, setManualOpen] = useState(false);
  const [manualUrl, setManualUrl] = useState('http://localhost:11434/v1');
  const [manualModel, setManualModel] = useState('');
  /** Optional key for authenticated local servers (oMLX). Held only in this form
   * until saved to safeStorage; never rendered back. */
  const [manualKey, setManualKey] = useState('');
  const [hasStoredKey, setHasStoredKey] = useState(false);
  const [serverModels, setServerModels] = useState<string[]>([]);
  const [localBusy, setLocalBusy] = useState<'list' | 'test' | null>(null);
  const [localTest, setLocalTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [listNote, setListNote] = useState<string | null>(null);

  const probe = useCallback(async () => {
    setProbing(true);
    setProbeError(null);
    try {
      const detected = await getLocalRuntimes();
      setRuntimes(detected);
    } catch (err) {
      // /llm/local-runtimes returns empty arrays cleanly; a thrown error
      // here means the engine is unreachable or returned a non-200. Show
      // it so the user knows detection isn't running silently broken.
      setProbeError(err instanceof Error ? err.message : t('settings.local.err.probe'));
      setRuntimes([]);
    } finally {
      setProbing(false);
    }
  }, []);

  useEffect(() => {
    void probe();
  }, [probe]);

  // Load the saved (base_url, model) pair so the UI can highlight what's
  // currently active. Re-runs on every Settings refresh so saves from
  // sibling rows don't leave stale state.
  useEffect(() => {
    let cancelled = false;
    void loadLocalApiKey().then((k) => {
      if (!cancelled) setHasStoredKey(Boolean(k));
    }).catch(() => undefined);
    void loadLocalConfig().then((cfg) => {
      if (!cancelled) setSaved(cfg);
    });
    return () => {
      cancelled = true;
    };
  }, [savingFor]);

  const onPickModel = useCallback(
    async (baseUrl: string, model: string): Promise<boolean> => {
      if (disabled) return false;
      setSavingFor(baseUrl);
      setSaveError(null);
      try {
        await saveLocalConfig({ base_url: baseUrl, model });
        setSaved({ base_url: baseUrl, model });
        onChange();
        return true;
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('settings.local.err.save'));
        return false;
      } finally {
        setSavingFor(null);
      }
    },
    [disabled, onChange],
  );

  const onSaveManual = useCallback(async () => {
    const url = manualUrl.trim();
    const model = manualModel.trim();
    if (!url || !model) {
      setSaveError(t('settings.local.err.required'));
      return;
    }
    // Use onPickModel's return value, not the saveError state: state read
    // right after the await is the previous render's value (stale closure),
    // so the form would close on failure / stay open on success.
    const ok = await onPickModel(url, model);
    if (ok) {
      // Blank key field = keep whatever is stored; a typed key replaces it.
      if (manualKey.trim()) {
        try {
          await saveLocalApiKey(manualKey);
          setHasStoredKey(true);
        } catch (err) {
          setSaveError(err instanceof Error ? err.message : t('settings.local.err.save'));
          return;
        }
      }
      setManualKey('');
      setManualOpen(false);
    }
  }, [manualUrl, manualModel, manualKey, onPickModel, t]);

  /** The key to use for List/Test: what is typed, else what is stored. */
  const effectiveKey = useCallback(async (): Promise<string | null> => {
    return manualKey.trim() || (await loadLocalApiKey());
  }, [manualKey]);

  const onListModels = useCallback(async () => {
    setLocalBusy('list');
    setListNote(null);
    setSaveError(null);
    try {
      const res = await listLocalModels({ baseUrl: manualUrl.trim(), apiKey: await effectiveKey() });
      if (res.ok) {
        setServerModels(res.models);
        setListNote(tp('settings.local.listed', res.models.length));
        if (res.models.length > 0 && !manualModel.trim()) setManualModel(res.models[0]);
      } else {
        setSaveError(
          res.status === 401
            ? t('settings.local.err.unauthorized')
            : t('settings.local.err.list', { error: res.error }),
        );
      }
    } finally {
      setLocalBusy(null);
    }
  }, [manualUrl, manualModel, effectiveKey, t, tp]);

  const onTestLocal = useCallback(async () => {
    setLocalBusy('test');
    setLocalTest(null);
    try {
      const res = await testLocalConnection({
        baseUrl: manualUrl.trim(),
        model: manualModel.trim() || 'default',
        apiKey: await effectiveKey(),
      });
      setLocalTest(
        res.ok
          ? { ok: true, text: t('settings.local.testOk', { ms: res.ms ? t('settings.local.testMs', { ms: res.ms }) : '' }) }
          : { ok: false, text: t('settings.local.testFail', { error: describeEngineError(res.error ?? '', i18n) }) },
      );
    } finally {
      setLocalBusy(null);
    }
  }, [manualUrl, manualModel, effectiveKey, t, i18n]);

  const onClear = useCallback(async () => {
    if (!saved) return;
    if (!confirm(t('settings.local.confirmClear'))) return;
    try {
      await Promise.all([
        deleteSecret(PROVIDER_SECRET_KEY.local),
        deleteSecret(LOCAL_MODEL_SECRET_KEY),
        deleteSecret(LOCAL_API_KEY_SECRET_KEY),
      ]);
      setSaved(null);
      setHasStoredKey(false);
      onChange();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : t('settings.local.err.clear'));
    }
  }, [saved, onChange]);

  return (
    <div className={styles.row}>
      <div className={styles.rowMain}>
        <div className={styles.rowName}>{t('settings.local.name')}</div>
        <div className={styles.rowNote}>{t('settings.local.note')}</div>

        {saved && (
          <div className={styles.rowMeta}>
            {rich('settings.local.active', { model: saved.model, url: saved.base_url })}
            {hasStoredKey && <span className={styles.hint}> · {t('settings.local.apiKeyBadge')}</span>}
          </div>
        )}

        {probing && !runtimes && (
          <div className={styles.rowMeta}>{t('settings.local.probing')}</div>
        )}

        {probeError && (
          <div className={styles.editorError} role="alert">
            {t('settings.local.detectError', { error: probeError })}
          </div>
        )}

        {runtimes && runtimes.length === 0 && (
          <div className={styles.rowMeta}>
            {t('settings.local.none.pre')}<a
              href="https://ollama.com"
              target="_blank"
              rel="noopener noreferrer"
            >Ollama</a>{t('settings.local.none.mid')}<a
              href="https://lmstudio.ai"
              target="_blank"
              rel="noopener noreferrer"
            >LM Studio</a>{t('settings.local.none.post')}
          </div>
        )}

        {runtimes && runtimes.length > 0 && (
          <ul className={styles.list} style={{ marginTop: 8 }}>
            {runtimes.map((rt) => (
              <li key={rt.base_url} className={styles.staticRow}>
                <div className={styles.rowMain}>
                  <div className={styles.rowName}>
                    {rt.runtime}
                    {saved?.base_url === rt.base_url && (
                      <span
                        className={`${styles.pill} ${styles.pill_success}`}
                        style={{ marginLeft: 8 }}
                      >
                        {t('settings.local.connected')}
                      </span>
                    )}
                  </div>
                  <div className={styles.rowNote}>
                    <code>{rt.base_url}</code> · {tp('settings.local.models', rt.models.length)}
                  </div>
                  <div style={{ marginTop: 6 }}>
                    <label style={{ fontSize: 12, marginRight: 6 }}>
                      {t('settings.local.modelLabel')}
                    </label>
                    <select
                      disabled={disabled || savingFor === rt.base_url}
                      value={
                        saved?.base_url === rt.base_url ? saved.model : ''
                      }
                      onChange={(e) => {
                        if (e.target.value) {
                          void onPickModel(rt.base_url, e.target.value);
                        }
                      }}
                    >
                      <option value="">{t('settings.local.pickModel')}</option>
                      {rt.models.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}

        {manualOpen && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--tal-bg-overlay)', borderRadius: 6 }}>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t('settings.local.baseUrl')}
              </label>
              <input
                type="text"
                value={manualUrl}
                onChange={(e) => setManualUrl(e.target.value)}
                placeholder="http://localhost:11434/v1"
                style={{ width: '100%' }}
              />
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t('settings.local.model')}
              </label>
              <input
                type="text"
                value={manualModel}
                onChange={(e) => setManualModel(e.target.value)}
                placeholder="llama3.2:latest"
                list="local-model-options"
                style={{ width: '100%' }}
              />
              <datalist id="local-model-options">
                {serverModels.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t('settings.local.apiKey')}
              </label>
              <input
                type="password"
                value={manualKey}
                onChange={(e) => setManualKey(e.target.value)}
                placeholder={hasStoredKey ? t('settings.local.apiKeyStored') : t('settings.local.apiKeyPlaceholder')}
                autoComplete="off"
                spellCheck={false}
                style={{ width: '100%' }}
                data-testid="local-api-key-input"
              />
            </div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
              <button
                type="button"
                onClick={() => {
                  setManualUrl('http://127.0.0.1:8000/v1');
                  setManualModel('Qwen3.8-Flash-Next-oQ4e-mtp');
                }}
                disabled={disabled}
              >
                {t('settings.local.omlx')}
              </button>
              <button type="button" onClick={() => void onListModels()} disabled={disabled || localBusy !== null}>
                {localBusy === 'list' ? t('settings.local.listing') : t('settings.local.listModels')}
              </button>
              <button type="button" onClick={() => void onTestLocal()} disabled={disabled || localBusy !== null}>
                {localBusy === 'test' ? t('settings.local.testing') : t('settings.local.test')}
              </button>
            </div>
            {listNote && <div className={styles.rowMeta}>{listNote}</div>}
            {localTest && (
              <div className={styles.rowMeta} style={{ color: localTest.ok ? 'var(--tal-buy, #4caf50)' : 'var(--tal-sell, #d64545)' }}>
                {localTest.text}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                onClick={onSaveManual}
                disabled={disabled}
              >
                {t('settings.local.save')}
              </button>
              <button
                type="button"
                onClick={() => setManualOpen(false)}
                disabled={disabled}
              >
                {t('settings.local.cancel')}
              </button>
            </div>
          </div>
        )}

        {saveError && (
          <div className={styles.editorError} role="alert">
            {saveError}
          </div>
        )}
      </div>

      <div className={styles.rowAside} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <button
          type="button"
          onClick={() => void probe()}
          disabled={disabled || probing}
        >
          {probing ? t('settings.local.probingBtn') : t('settings.local.refresh')}
        </button>
        {!manualOpen && (
          <button
            type="button"
            onClick={() => setManualOpen(true)}
            disabled={disabled}
          >
            {t('settings.local.manual')}
          </button>
        )}
        {saved && (
          <button
            type="button"
            onClick={onClear}
            disabled={disabled}
          >
            {t('settings.local.clear')}
          </button>
        )}
      </div>
    </div>
  );
}

interface OpenAIOAuthRowProps {
  disabled: boolean;
}

function OpenAIOAuthRow({ disabled }: OpenAIOAuthRowProps) {
  const { t, rich } = useI18n();
  const [status, setStatus] = useState<OAuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [prompt, setPrompt] = useState<{ message: string; placeholder?: string } | null>(null);
  const [pasteValue, setPasteValue] = useState('');
  const [error, setError] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const next = await getOpenAIOAuthStatus();
      setStatus(next);
    } catch (err) {
      setStatus({ connected: false });
      setError(err instanceof Error ? err.message : t('oauth.row.err.status'));
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    const offProgress = onOAuthProgress((event) => {
      setProgress(event.message);
    });
    const offPrompt = onOAuthPrompt((event) => {
      setPrompt({ message: event.message, placeholder: event.placeholder });
    });
    return () => {
      offProgress();
      offPrompt();
    };
  }, []);

  const onConnect = useCallback(async () => {
    setBusy(true);
    setError(null);
    setProgress(t('oauth.row.starting'));
    setPrompt(null);
    try {
      const result = await startOpenAIOAuthLogin();
      if (result.success) {
        setProgress(
          result.email
            ? t('oauth.row.connectedAs', { email: result.email })
            : t('oauth.row.connectedShort'),
        );
      } else {
        setError(result.error ?? t('oauth.row.loginFailed'));
        setProgress(null);
      }
      await refreshStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('oauth.row.err.start'));
      setProgress(null);
    } finally {
      setBusy(false);
      setPrompt(null);
      setPasteValue('');
    }
  }, [refreshStatus]);

  const onDisconnect = useCallback(async () => {
    if (!confirm(t('oauth.row.confirmDisconnect'))) return;
    setBusy(true);
    setError(null);
    try {
      await disconnectOpenAIOAuth();
      setProgress(null);
      await refreshStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('oauth.row.err.disconnect'));
    } finally {
      setBusy(false);
    }
  }, [refreshStatus]);

  const onSubmitPaste = useCallback(() => {
    const value = pasteValue.trim();
    if (!value) return;
    submitOAuthPromptResponse(value);
    setPrompt(null);
    setPasteValue('');
  }, [pasteValue]);

  const connected = status?.connected ?? false;

  return (
    <div className={styles.row}>
      <div className={styles.rowMain}>
        <div className={styles.rowName}>{t('oauth.row.name')}</div>
        <div className={styles.rowNote}>{rich('oauth.row.note')}</div>
        {connected && (
          <div className={styles.rowMeta}>
            {t('oauth.row.connected', {
              email: status?.email ? t('oauth.row.asEmail', { email: status.email }) : '',
              plan: status?.planType ? t('oauth.row.plan', { plan: status.planType }) : '',
              refresh: status?.needsRefresh ? t('oauth.row.refreshNext') : '',
            })}
          </div>
        )}
        {connected && status?.isFreeTier && (
          <div className={styles.editorError} role="alert">
            {t('oauth.row.freeTier')}
          </div>
        )}
        {progress && !error && (
          <div className={styles.rowMeta}>{progress}</div>
        )}
        {prompt && (
          <div className={styles.editor}>
            <div className={styles.label}>{prompt.message}</div>
            <input
              className={styles.input}
              value={pasteValue}
              onChange={(e) => setPasteValue(e.target.value)}
              placeholder={prompt.placeholder ?? t('oauth.row.pasteHere')}
              autoFocus
              disabled={busy && !prompt}
            />
            <div className={styles.editorActions}>
              <button
                className={styles.actionPrimary}
                onClick={onSubmitPaste}
                disabled={!pasteValue.trim()}
                type="button"
              >
                {t('oauth.row.submit')}
              </button>
            </div>
          </div>
        )}
        {error && <div className={styles.editorError}>{error}</div>}
      </div>
      <div className={styles.rowAside}>
        <span
          className={`${styles.pill} ${
            connected ? styles.pill_success : styles.pill_optional
          }`}
        >
          {connected ? t('oauth.row.pillConnected') : t('oauth.row.pillRecommended')}
        </span>
        {connected ? (
          <button
            className={styles.rowActionDanger}
            onClick={() => void onDisconnect()}
            disabled={disabled || busy}
            type="button"
          >
            {t('oauth.row.disconnect')}
          </button>
        ) : (
          <button
            className={styles.rowAction}
            onClick={() => void onConnect()}
            disabled={disabled || busy}
            type="button"
          >
            {busy ? t('oauth.row.connecting') : t('oauth.row.connect')}
          </button>
        )}
      </div>
    </div>
  );
}

interface AboutTabProps {
  availability: SecretsAvailability | null;
  secretsCount: number;
}

function AboutTab({ availability, secretsCount }: AboutTabProps) {
  const { t, tp, rich } = useI18n();
  return (
    <div className={styles.formCard}>
      <p className={styles.aboutIntro}>{rich('about.intro')}</p>
      <dl className={styles.aboutList}>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.version')}</dt>
          <dd className={styles.aboutValue}>{__APP_VERSION__}</dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.madeBy')}</dt>
          <dd className={styles.aboutValue}>
            RBJ Global LLC
            <div className={styles.aboutHint}>{t('about.madeByHint')}</div>
          </dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.license')}</dt>
          <dd className={styles.aboutValue}>{t('about.licenseValue')}</dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.posture')}</dt>
          <dd className={styles.aboutValue}>{t('about.postureValue')}</dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.legal')}</dt>
          <dd className={styles.aboutValue}>
            <a
              className={styles.link}
              href="https://tradingagentslab.ai/legal/disclaimer/"
              target="_blank"
              rel="noreferrer"
            >
              {t('about.legal.disclaimer')}
            </a>{' '}
            ·{' '}
            <a
              className={styles.link}
              href="https://tradingagentslab.ai/legal/terms/"
              target="_blank"
              rel="noreferrer"
            >
              {t('about.legal.terms')}
            </a>{' '}
            ·{' '}
            <a
              className={styles.link}
              href="https://tradingagentslab.ai/legal/privacy/"
              target="_blank"
              rel="noreferrer"
            >
              {t('about.legal.privacy')}
            </a>
            <div className={styles.aboutHint}>{t('about.legalHint')}</div>
          </dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.encryption')}</dt>
          <dd className={styles.aboutValue}>
            {availability?.available
              ? t('about.encryptionOn')
              : t('about.encryptionOff')}
          </dd>
        </div>
        <div className={styles.aboutRow}>
          <dt className={styles.aboutKey}>{t('about.secretsFile')}</dt>
          <dd className={styles.aboutValue}>
            <code className={styles.code}>
              {availability?.filePath ?? t('about.unavailable')}
            </code>
            <div className={styles.aboutHint}>
              {secretsCount === 0
                ? t('about.secretsEmpty')
                : tp('about.secretsCount', secretsCount)}
            </div>
          </dd>
        </div>
      </dl>
      <p className={styles.aboutAttribution}>{t('about.attribution')}</p>
    </div>
  );
}

/**
 * Pill state per row, applying the green-Connected convention from
 * commit `bdc1716` ("Green = wired and working right now") universally
 * to every SecretRowItem — not just OAuth + yfinance.
 *
 * Rules:
 * - listing != null AND pillVariant = 'planned' → "Stored · Inert" in the
 *   neutral optional style. Communicates "we have the key" without the
 *   green-confirming-active feel that would mislead on Restricted rows
 *   (e.g. Alpaca Live, where the engine refuses to place real-money
 *   orders even with keys configured).
 * - listing != null otherwise → "Connected ✓" in green pill_success.
 * - listing == null → static label/variant from the row definition.
 */
function pillState(
  row: SecretRow,
  listing: SecretListing | null,
  t: I18n['t'],
): { label: string; variant: 'default' | 'planned' | 'optional' | 'success' } {
  if (!listing) {
    return { label: t(row.pillKey), variant: row.pillVariant };
  }
  if (row.pillVariant === 'planned') {
    return { label: t('settings.pill.storedInert'), variant: 'optional' };
  }
  return { label: t('settings.pill.connected'), variant: 'success' };
}

// ---- Webhooks tab ---------------------------------------------------------
//
// Single-key safeStorage backed. The full config list is serialized to one
// JSON blob (`webhooks:configs`) so a refresh round-trips in two IPC calls
// instead of one-per-webhook. URLs + secrets are sensitive (Telegram URLs
// embed bot tokens) — the OS keychain is the right home.

interface WebhooksTabProps {
  availability: SecretsAvailability | null;
}

function WebhooksTab({ availability }: WebhooksTabProps) {
  const { t, rich } = useI18n();
  const [webhooks, setWebhooksState] = useState<WebhookConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<WebhookConfig | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setWebhooksState(await loadWebhooks());
    } catch {
      setWebhooksState([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onPersist = useCallback(
    async (next: WebhookConfig[]) => {
      try {
        await saveWebhooks(next);
        setWebhooksState(next);
        setSaveError(null);
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('webhooks.err.save'));
      }
    },
    [t],
  );

  const onAddNew = useCallback(() => {
    setEditing({
      id: newWebhookId(),
      name: '',
      url: '',
      kind: 'telegram',
      filter: newWebhookFilter(),
    });
  }, []);

  const onSave = useCallback(
    async (cfg: WebhookConfig) => {
      const trimmed: WebhookConfig = {
        ...cfg,
        name: cfg.name.trim() || t('webhooks.untitled'),
        url: cfg.url.trim(),
        secret: cfg.secret?.trim() || undefined,
        telegram_chat_id: cfg.telegram_chat_id?.trim() || undefined,
      };
      if (!trimmed.url.startsWith('https://') && !trimmed.url.startsWith('http://')) {
        setSaveError(t('webhooks.err.url'));
        return;
      }
      if (trimmed.kind === 'telegram') {
        const token = extractTelegramToken(trimmed.url);
        if (!token) {
          setSaveError(t('webhooks.err.token'));
          return;
        }
        if (!trimmed.telegram_chat_id) {
          setSaveError(t('webhooks.err.chatId'));
          return;
        }
      }
      const existing = webhooks.findIndex((w) => w.id === trimmed.id);
      const next =
        existing >= 0
          ? webhooks.map((w) => (w.id === trimmed.id ? trimmed : w))
          : [...webhooks, trimmed];
      await onPersist(next);
      setEditing(null);
    },
    [webhooks, onPersist, t],
  );

  const onDelete = useCallback(
    async (id: string) => {
      if (!confirm(t('webhooks.confirmDelete'))) {
        return;
      }
      await onPersist(webhooks.filter((w) => w.id !== id));
    },
    [webhooks, onPersist, t],
  );

  if (availability && !availability.available) {
    return (
      <div className={styles.placeholder}>
        {t('webhooks.needsEncryption')}
      </div>
    );
  }

  return (
    <div>
      <div className={styles.webhooksToolbar}>
        <button
          type="button"
          className={styles.actionPrimary}
          onClick={onAddNew}
          disabled={loading}
          data-testid="webhooks-add-button"
        >
          {t('webhooks.add')}
        </button>
      </div>

      {saveError && <p className={styles.formError}>{saveError}</p>}

      {loading && <p className={styles.hint}>{t('webhooks.loading')}</p>}

      {!loading && webhooks.length === 0 && !editing && (
        <div className={styles.phaseGuard}>
          {rich('webhooks.empty')}
          <br />
          <br />
          {t('webhooks.emptyNote')}
        </div>
      )}

      {webhooks.length > 0 && (
        <ul className={styles.list} data-testid="webhooks-list">
          {webhooks.map((w) => (
            <li
              key={w.id}
              className={styles.row}
              data-testid={`webhook-row-${w.id}`}
            >
              <div className={styles.rowMain}>
                <div className={styles.rowName}>
                  {w.name}{' '}
                  <span className={styles.pill}>{t(`webhooks.kind.${w.kind}` as const)}</span>
                  {w.filter.stances.length > 0 && (
                    <span className={styles.pill}>
                      {w.filter.stances.map((st) => stanceLabel(st, t)).join(' / ')}
                    </span>
                  )}
                  {w.filter.min_conviction > 0 && (
                    <span className={styles.pill}>
                      ≥ {Math.round(w.filter.min_conviction * 100)}%
                    </span>
                  )}
                </div>
                <div className={styles.rowNote}>
                  {hostFromUrl(w.url) || t('webhooks.invalidUrl')}
                </div>
              </div>
              <div className={styles.rowAside}>
                <button
                  type="button"
                  className={styles.rowAction}
                  onClick={() => setEditing({ ...w })}
                >
                  {t('webhooks.edit')}
                </button>
                <button
                  type="button"
                  className={styles.rowActionDanger}
                  onClick={() => void onDelete(w.id)}
                >
                  {t('webhooks.delete')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {editing && (
        <WebhookEditor
          config={editing}
          onCancel={() => setEditing(null)}
          onSave={onSave}
        />
      )}
    </div>
  );
}

// ---- Channels tab ---------------------------------------------------------
//
// Two-way integrations where the user reaches into Trading Agents Lab from
// outside the desktop app. Today this is just the Telegram bot. Future
// candidates (Discord, Slack, iMessage Shortcuts, etc.) would each get
// their own panel here. Kept separate from Webhooks because the mental
// model is different: webhooks are outbound HTTP push on session.complete;
// channels are inbound triggers that drive a new debate.

function ChannelsTab() {
  const t = useI18n().t;
  return (
    <div>
      <div className={styles.channelsHero}>
        <span className={styles.channelsHeroIcon} aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor">
            <path d="M21.94 4.3 18.6 20.02c-.25 1.11-.91 1.38-1.85.86l-5.1-3.76-2.46 2.37c-.27.27-.5.5-1.03.5l.37-5.2 9.47-8.56c.41-.37-.09-.57-.64-.2L5.66 13.2l-5.03-1.57c-1.1-.34-1.12-1.1.23-1.62l19.66-7.58c.91-.34 1.71.2 1.42 1.87Z" />
          </svg>
        </span>
        <div className={styles.channelsHeroText}>
          <span className={styles.channelsHeroTitle}>Telegram</span>
          <span className={styles.channelsHeroSubtitle}>
            {t('channels.telegram.subtitle')}
          </span>
        </div>
      </div>
      <TelegramBotPanel />
    </div>
  );
}

/** Show just the hostname in the row preview so the bot token (Telegram /
 * Discord URLs) never appears in the UI even briefly. The Editor shows
 * the full URL in the password field for editing. */
function hostFromUrl(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}

/** Telegram URLs embed the bot token. Settings UI asks for the bare token
 * (closer to how BotFather presents it) and these helpers materialise the
 * full Bot API URL on the way to storage and back. */
const TELEGRAM_URL_PREFIX = 'https://api.telegram.org/bot';
const TELEGRAM_URL_SUFFIX = '/sendMessage';

function extractTelegramToken(url: string): string {
  if (!url.startsWith(TELEGRAM_URL_PREFIX)) return '';
  const rest = url.slice(TELEGRAM_URL_PREFIX.length);
  const slash = rest.indexOf('/');
  return slash >= 0 ? rest.slice(0, slash) : rest;
}

function buildTelegramUrl(token: string): string {
  return `${TELEGRAM_URL_PREFIX}${token.trim()}${TELEGRAM_URL_SUFFIX}`;
}

interface WebhookEditorProps {
  config: WebhookConfig;
  onCancel: () => void;
  onSave: (cfg: WebhookConfig) => void;
}

function WebhookEditor({ config, onCancel, onSave }: WebhookEditorProps) {
  const { t, rich } = useI18n();
  const [draft, setDraft] = useState<WebhookConfig>(config);

  const setKind = (kind: WebhookKind) => {
    setDraft((d) => ({
      ...d,
      kind,
      // Reset Telegram-specific fields when switching away.
      telegram_chat_id: kind === 'telegram' ? d.telegram_chat_id : undefined,
      // HMAC only meaningful for generic.
      secret: kind === 'generic' ? d.secret : undefined,
    }));
  };

  return (
    <div className={styles.editor} data-testid="webhook-editor">
      <div className={styles.field}>
        <label className={styles.label}>{t('webhooks.editor.name')}</label>
        <input
          className={styles.input}
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder={t('webhooks.editor.namePlaceholder')}
          data-testid="webhook-name-input"
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label}>{t('webhooks.editor.kind')}</label>
        <select
          className={styles.input}
          value={draft.kind}
          onChange={(e) => setKind(e.target.value as WebhookKind)}
          data-testid="webhook-kind-select"
        >
          {(['telegram', 'slack', 'discord', 'generic'] as WebhookKind[]).map((k) => (
            <option key={k} value={k}>
              {t(`webhooks.kind.${k}` as const)}
            </option>
          ))}
        </select>
        <p className={styles.hint}>{t(`webhooks.hint.${draft.kind}` as const)}</p>
      </div>

      {draft.kind === 'telegram' ? (
        <>
          <div className={styles.field}>
            <label className={styles.label}>{t('webhooks.editor.botToken')}</label>
            <input
              type="password"
              className={styles.input}
              value={extractTelegramToken(draft.url)}
              onChange={(e) =>
                setDraft({ ...draft, url: buildTelegramUrl(e.target.value) })
              }
              placeholder="123456789:ABCdef-GhIJklmnOPqrsTUVwxyz"
              data-testid="webhook-url-input"
            />
            <p className={styles.hint}>{rich('webhooks.editor.botTokenHint', undefined, styles.code)}</p>
          </div>
          <div className={styles.field}>
            <label className={styles.label}>{t('webhooks.editor.chatId')}</label>
            <input
              className={styles.input}
              value={draft.telegram_chat_id ?? ''}
              onChange={(e) =>
                setDraft({ ...draft, telegram_chat_id: e.target.value })
              }
              placeholder={t('webhooks.editor.chatIdPlaceholder')}
              data-testid="webhook-chat-id-input"
            />
            <p className={styles.hint}>{rich('webhooks.editor.chatIdHint', undefined, styles.code)}</p>
          </div>
        </>
      ) : (
        <div className={styles.field}>
          <label className={styles.label}>{t('webhooks.editor.url')}</label>
          <input
            type="password"
            className={styles.input}
            value={draft.url}
            onChange={(e) => setDraft({ ...draft, url: e.target.value })}
            placeholder={
              draft.kind === 'slack'
                ? 'https://hooks.slack.com/services/...'
                : draft.kind === 'discord'
                  ? 'https://discord.com/api/webhooks/<id>/<token>'
                  : 'https://your-receiver.example.com/hook'
            }
            data-testid="webhook-url-input"
          />
        </div>
      )}

      {draft.kind === 'generic' && (
        <div className={styles.field}>
          <label className={styles.label}>{t('webhooks.editor.secret')}</label>
          <input
            type="password"
            className={styles.input}
            value={draft.secret ?? ''}
            onChange={(e) => setDraft({ ...draft, secret: e.target.value })}
            placeholder={t('webhooks.editor.secretPlaceholder')}
          />
          <p className={styles.hint}>{rich('webhooks.editor.secretHint', undefined, styles.code)}</p>
        </div>
      )}

      <div className={styles.field}>
        <label className={styles.label}>{t('webhooks.editor.stances')}</label>
        <div className={styles.webhooksCheckboxes}>
          {STANCES.map((s) => {
            const checked = draft.filter.stances.includes(s);
            return (
              <label key={s} className={styles.webhooksCheckboxLabel}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      filter: {
                        ...draft.filter,
                        stances: e.target.checked
                          ? [...draft.filter.stances, s]
                          : draft.filter.stances.filter((x) => x !== s),
                      },
                    })
                  }
                />
                {stanceLabel(s, t)}
              </label>
            );
          })}
        </div>
        <p className={styles.hint}>{t('webhooks.editor.stancesHint')}</p>
      </div>

      <div className={styles.field}>
        <label className={styles.label}>
          {t('webhooks.editor.minConviction', { pct: Math.round(draft.filter.min_conviction * 100) })}
        </label>
        <input
          type="range"
          min={0}
          max={100}
          step={5}
          value={Math.round(draft.filter.min_conviction * 100)}
          onChange={(e) =>
            setDraft({
              ...draft,
              filter: { ...draft.filter, min_conviction: Number(e.target.value) / 100 },
            })
          }
        />
      </div>

      <div className={styles.editorActions}>
        <button
          type="button"
          className={styles.actionGhost}
          onClick={onCancel}
        >
          {t('webhooks.editor.cancel')}
        </button>
        <button
          type="button"
          className={styles.actionPrimary}
          onClick={() => onSave(draft)}
          disabled={!draft.url.trim()}
          data-testid="webhook-save-button"
        >
          {t('webhooks.editor.save')}
        </button>
      </div>
    </div>
  );
}

// ---- Cost Guard tab --------------------------------------------------------

function AlphaVantageSignupCallout({
  hasKey,
  onSaved,
}: {
  hasKey: boolean;
  onSaved: () => void;
}) {
  const { t, rich } = useI18n();
  // In-app free-key signup: open Alpha Vantage's real page in an isolated
  // window, capture the issued key, and save it, so the user never has to
  // leave the app (and never reaches the competitor promo AV shows below the
  // key). Degrades gracefully: if capture misses, the user pastes the key into
  // the row above.
  const [status, setStatus] = useState<
    'idle' | 'opening' | 'saved' | 'cancelled' | 'error'
  >('idle');

  // Once a key is stored the row above already shows CONNECTED, so this
  // "get one free" prompt is not just redundant, it is confusing (it reads as
  // "no key yet" beside a connected key). Hide it entirely while a key exists;
  // it comes back on its own if the user deletes the key (the parent re-renders
  // with hasKey=false).
  if (hasKey) return null;

  const onGetKey = async () => {
    const bridge = window.tradingAgentsLab;
    if (!bridge?.avSignup) return;
    setStatus('opening');
    try {
      const key = await bridge.avSignup.getFreeKey();
      if (!key) {
        setStatus('cancelled');
        return;
      }
      await bridge.secrets.set('data:alpha-vantage', key);
      setStatus('saved');
      onSaved();
    } catch {
      setStatus('error');
    }
  };

  return (
    <div className={styles.avCallout}>
      <div className={styles.avCalloutText}>
        {rich('av.title')}
        <div className={styles.formHint}>{t('av.hint')}</div>
        {status === 'saved' && (
          <p className={styles.formSuccess}>{t('av.saved')}</p>
        )}
        {status === 'cancelled' && (
          <p className={styles.formHint}>{t('av.cancelled')}</p>
        )}
        {status === 'error' && (
          <p className={styles.formError}>
            {t('av.error')}
          </p>
        )}
      </div>
      <button
        type="button"
        className={styles.formSave}
        onClick={() => void onGetKey()}
        disabled={status === 'opening'}
      >
        {status === 'opening' ? t('av.opening') : t('av.get')}
      </button>
    </div>
  );
}

function ProAnalysisTab() {
  const { t } = useI18n();
  // Local mirror of the persisted Pro run-config. Writes go straight to
  // localStorage (no engine round-trip), so we persist on each change and
  // reflect it immediately. `tokenCapDraft` is kept as a separate string so
  // the field can be cleared/retyped without fighting a numeric state.
  const [state, setState] = useState<ProConfigState>(() => loadProConfig());
  const [tokenCapDraft, setTokenCapDraft] = useState<string>(
    state.tokenCap != null ? String(state.tokenCap) : '',
  );

  const persist = (next: ProConfigState) => {
    setState(next);
    saveProConfig(next);
  };

  const toggleAnalyst = (id: AnalystId) => {
    const raw = state.selectedAnalysts.includes(id)
      ? state.selectedAnalysts.filter((a) => a !== id)
      : [...state.selectedAnalysts, id];
    // Keep canonical order and never allow an empty set (an analyst-less run
    // is invalid); fall back to market-only.
    const ordered = ALL_ANALYSTS.filter((a) => raw.includes(a));
    persist({ ...state, selectedAnalysts: ordered.length > 0 ? ordered : ['market'] });
  };

  const commitTokenCap = () => {
    const trimmed = tokenCapDraft.trim();
    const parsed = trimmed === '' ? null : Math.max(0, Math.floor(Number(trimmed)));
    const cap = parsed && parsed > 0 ? parsed : null;
    setTokenCapDraft(cap != null ? String(cap) : '');
    persist({ ...state, tokenCap: cap });
  };

  return (
    <div className={styles.formCard}>
      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('analysis.analysts')}</legend>
        <p className={styles.formHint}>{t('analysis.analystsHint')}</p>
        {ALL_ANALYSTS.map((id) => (
          <label key={id} className={styles.toggleRow}>
            <input
              type="checkbox"
              checked={state.selectedAnalysts.includes(id)}
              onChange={() => toggleAnalyst(id)}
            />
            <span>
              <strong>{t(`analyst.${id}` as const)}</strong>
              {ANALYSTS_NEEDING_DATA_KEY.includes(id) && (
                <div className={styles.formHint}>{t('analysis.requiresKey')}</div>
              )}
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('analysis.newsSource')}</legend>
        <p className={styles.formHint}>{t('analysis.newsSourceHint')}</p>
        <div className={styles.costInputRow}>
          <label className={styles.costInputLabel}>
            {t('analysis.source')}
            <select
              className={styles.costInputField}
              value={state.newsSource}
              onChange={(e) =>
                persist({ ...state, newsSource: e.target.value as NewsSource })
              }
            >
              {NEWS_SOURCES.map((s) => (
                <option key={s} value={s}>
                  {t(`newsSource.${s}` as const)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </fieldset>

      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('analysis.depth')}</legend>
        <p className={styles.formHint}>{t('analysis.depthHint')}</p>
        <div className={styles.costInputRow}>
          <label className={styles.costInputLabel}>
            {t('analysis.researchRounds')}
            <select
              className={styles.costInputField}
              value={state.maxDebateRounds}
              onChange={(e) => persist({ ...state, maxDebateRounds: Number(e.target.value) })}
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className={styles.costInputRow}>
          <label className={styles.costInputLabel}>
            {t('analysis.riskRounds')}
            <select
              className={styles.costInputField}
              value={state.maxRiskRounds}
              onChange={(e) => persist({ ...state, maxRiskRounds: Number(e.target.value) })}
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>
      </fieldset>

      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('analysis.effort')}</legend>
        <div className={styles.costInputRow}>
          <label className={styles.costInputLabel}>
            {t('analysis.effortLabel')}
            <select
              className={styles.costInputField}
              value={state.effort ?? ''}
              onChange={(e) =>
                persist({ ...state, effort: (e.target.value || null) as EffortLevel | null })
              }
            >
              <option value="">{t('analysis.effortDefault')}</option>
              {EFFORT_LEVELS.map((lvl) => (
                <option key={lvl} value={lvl}>
                  {t(`analysis.effort.${lvl}` as const)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className={styles.formHint}>{t('analysis.effortHint')}</p>
      </fieldset>

      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('analysis.tokenCap')}</legend>
        <div className={styles.costInputRow}>
          <label className={styles.costInputLabel}>
            {t('analysis.maxTokens')}
            <input
              type="number"
              inputMode="numeric"
              min={0}
              step={1000}
              className={styles.costInputField}
              placeholder={t('analysis.noCap')}
              value={tokenCapDraft}
              onChange={(e) => setTokenCapDraft(e.target.value)}
              onBlur={commitTokenCap}
            />
          </label>
        </div>
        <p className={styles.formHint}>{t('analysis.tokenCapHint')}</p>
      </fieldset>

      <p className={styles.formHint}>{t('analysis.autosave')}</p>
    </div>
  );
}

function CostGuardTab() {
  const { t } = useI18n();
  const [config, setConfig] = useState<CostGuardConfig | null>(null);
  const [spend, setSpend] = useState<SpendState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  // Form draft state — separate from `config` so the user can edit without
  // clobbering displayed-current values until they hit Save.
  const [draftEnabled, setDraftEnabled] = useState(true);
  const [draftDaily, setDraftDaily] = useState('');
  const [draftWeekly, setDraftWeekly] = useState('');
  const [draftMonthly, setDraftMonthly] = useState('');
  const [draftSessionsPerDay, setDraftSessionsPerDay] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const state = await getCostGuardState();
      setConfig(state.config);
      setSpend(state.spend);
      setDraftEnabled(state.config.enabled);
      setDraftDaily(String(state.config.cap_daily_usd));
      setDraftWeekly(String(state.config.cap_weekly_usd));
      setDraftMonthly(String(state.config.cap_monthly_usd));
      setDraftSessionsPerDay(String(state.config.cap_sessions_per_day));
    } catch (err) {
      setError(err instanceof Error ? err.message : t('cg.err.load'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onSave = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const updated = await updateCostGuardConfig({
        enabled: draftEnabled,
        cap_daily_usd: parseNonNegFloat(draftDaily, config?.cap_daily_usd ?? 0),
        cap_weekly_usd: parseNonNegFloat(draftWeekly, config?.cap_weekly_usd ?? 0),
        cap_monthly_usd: parseNonNegFloat(draftMonthly, config?.cap_monthly_usd ?? 0),
        cap_sessions_per_day: parseNonNegInt(
          draftSessionsPerDay,
          config?.cap_sessions_per_day ?? 0,
        ),
      });
      setConfig(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      // Refresh spend snapshot in case time crossed a window boundary.
      const state = await getCostGuardState();
      setSpend(state.spend);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('cg.err.save'));
    } finally {
      setSaving(false);
    }
  };

  if (loading || !config || !spend) {
    return (
      <div className={styles.formCard}>
        <p className={styles.formHint}>{error ?? t('cg.loading')}</p>
      </div>
    );
  }

  return (
    <div className={styles.formCard}>
      <div className={styles.formGroup}>
        <label className={styles.toggleRow}>
          <input
            type="checkbox"
            checked={draftEnabled}
            onChange={(e) => setDraftEnabled(e.target.checked)}
          />
          <span>
            <strong>{t('cg.enabled')}</strong>
            <div className={styles.formHint}>{t('cg.enabledHint')}</div>
          </span>
        </label>
      </div>

      <fieldset className={styles.formGroup} disabled={!draftEnabled}>
        <legend className={styles.formLegend}>{t('cg.apiCaps')}</legend>
        <p className={styles.formHint}>{t('cg.apiCapsHint')}</p>
        <CostInputRow
          label={t('cg.daily')}
          value={draftDaily}
          onChange={setDraftDaily}
          step={0.25}
          placeholder="1.00"
        />
        <CostInputRow
          label={t('cg.weekly')}
          value={draftWeekly}
          onChange={setDraftWeekly}
          step={1}
          placeholder="5.00"
        />
        <CostInputRow
          label={t('cg.monthly')}
          value={draftMonthly}
          onChange={setDraftMonthly}
          step={5}
          placeholder="15.00"
        />
      </fieldset>

      <fieldset className={styles.formGroup} disabled={!draftEnabled}>
        <legend className={styles.formLegend}>{t('cg.rateCap')}</legend>
        <p className={styles.formHint}>{t('cg.rateCapHint')}</p>
        <CostInputRow
          label={t('cg.perDay')}
          value={draftSessionsPerDay}
          onChange={setDraftSessionsPerDay}
          step={1}
          placeholder={t('cg.disabledPlaceholder')}
          asInteger
        />
      </fieldset>

      <fieldset className={styles.formGroup}>
        <legend className={styles.formLegend}>{t('cg.currentSpend')}</legend>
        <SpendBar
          label={t('cg.daily')}
          current={spend.daily_usd}
          cap={config.cap_daily_usd}
        />
        <SpendBar
          label={t('cg.weekly')}
          current={spend.weekly_usd}
          cap={config.cap_weekly_usd}
        />
        <SpendBar
          label={t('cg.monthly')}
          current={spend.monthly_usd}
          cap={config.cap_monthly_usd}
        />
        {config.cap_sessions_per_day > 0 && (
          <SpendBar
            label={t('cg.sessionsToday')}
            current={spend.sessions_today}
            cap={config.cap_sessions_per_day}
            isCount
          />
        )}
        <button
          type="button"
          className={styles.refreshLink}
          onClick={() => void refresh()}
        >
          {t('cg.refresh')}
        </button>
      </fieldset>

      {error && <p className={styles.formError}>{error}</p>}
      {saved && <p className={styles.formSuccess}>{t('cg.saved')}</p>}

      <div className={styles.formActions}>
        <button
          type="button"
          className={styles.formSave}
          onClick={() => void onSave()}
          disabled={saving}
        >
          {saving ? t('cg.saving') : t('cg.save')}
        </button>
      </div>
    </div>
  );
}

interface CostInputRowProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  step?: number;
  placeholder?: string;
  asInteger?: boolean;
}

function CostInputRow({
  label,
  value,
  onChange,
  step = 0.25,
  placeholder,
  asInteger,
}: CostInputRowProps) {
  return (
    <div className={styles.costInputRow}>
      <label className={styles.costInputLabel}>
        {label}
        <input
          type="number"
          inputMode="decimal"
          min={0}
          step={asInteger ? 1 : step}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={styles.costInputField}
        />
      </label>
    </div>
  );
}

interface SpendBarProps {
  label: string;
  current: number;
  cap: number;
  isCount?: boolean;
}

function SpendBar({ label, current, cap, isCount }: SpendBarProps) {
  const { t } = useI18n();
  if (cap <= 0) {
    return (
      <div className={styles.spendRow}>
        <span className={styles.spendLabel}>{label}</span>
        <span className={styles.spendValueDisabled}>
          {t('cg.capDisabled', { value: isCount ? current : formatUsdShort(current) })}
        </span>
      </div>
    );
  }
  const pct = Math.min(100, (current / cap) * 100);
  const tone = pct >= 100 ? 'over' : pct >= 80 ? 'warn' : 'ok';
  return (
    <div className={styles.spendRow}>
      <span className={styles.spendLabel}>{label}</span>
      <div className={styles.spendProgress}>
        <div
          className={`${styles.spendBar} ${styles[`spendBar_${tone}`] ?? ''}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={styles.spendValue}>
        {isCount
          ? `${current} / ${cap}`
          : `${formatUsdShort(current)} / ${formatUsdShort(cap)}`}
      </span>
    </div>
  );
}

function parseNonNegFloat(raw: string, fallback: number): number {
  const n = parseFloat(raw);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return n;
}

function parseNonNegInt(raw: string, fallback: number): number {
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return n;
}

function formatUsdShort(value: number): string {
  if (value >= 100) return `$${value.toFixed(0)}`;
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(4)}`;
}

// ---- Telegram bot (bidirectional, Phase 8c) -------------------------------
//
// Sits at the bottom of the Webhooks tab because the user mental model for
// anything Telegram-related lives there. Webhooks above this panel are
// OUTBOUND only (engine pushes session.complete to Telegram). The bot
// below is BIDIRECTIONAL: it lets a user message the bot from Telegram
// to trigger a fresh Diligence run. Same bot token can power both.
//
// Persistence:
// - Bot token in OS keychain (safeStorage) under `telegram:bot-token`
// - Allowlist + cap + selected provider in localStorage (non-secret config)
// - Provider API key looked up in safeStorage from PROVIDER_SECRET_KEY at
//   start time, matched to the user's provider dropdown choice
//
// Engine side persists per-chat daily spend across restarts so the cap is
// resistant to app-reboot evasion; see engine/telegram_bot.py for details.

const TELEGRAM_BOT_TOKEN_KEY = 'telegram:bot-token';
const TELEGRAM_ALLOWLIST_LS_KEY = 'tal.telegram.allowlist';
const TELEGRAM_CAP_LS_KEY = 'tal.telegram.daily_cap';
const TELEGRAM_PROVIDER_LS_KEY = 'tal.telegram.provider';

// Supports all 6 API-key providers plus OpenAI OAuth (Codex). OAuth access
// tokens are short-lived, so when the bot is on OpenAI OAuth the panel kicks
// off a periodic refresh that pushes fresh tokens to the engine via
// /telegram/refresh-credentials. xAI and MiniMax are API-key only and ride
// the generic path (same as anthropic/openrouter/gemini), so they need no
// OAuth refresh. Local LLM is still excluded because the base_url + dynamic
// model picker plumbing isn't here yet.
type BotProvider = 'openai' | 'anthropic' | 'openrouter' | 'gemini' | 'xai' | 'minimax';
const BOT_PROVIDERS: { value: BotProvider; label: string }[] = [
  { value: 'openai',     label: 'OpenAI' },
  { value: 'anthropic',  label: 'Anthropic' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'gemini',     label: 'Google Gemini' },
  { value: 'xai',        label: 'xAI Grok' },
  { value: 'minimax',    label: 'MiniMax' },
];
const BOT_PROVIDER_DEFAULT_MODEL: Record<BotProvider, string> = {
  openai:     'gpt-4o-mini',
  anthropic:  'claude-haiku-4-5',
  openrouter: 'openai/gpt-4o-mini',
  gemini:     'gemini-2.0-flash',
  // Fast/cheap defaults, matching the Analyze picker + engine _DEFAULT_MODELS.
  xai:        'grok-4.3',
  minimax:    'MiniMax-M2.7-highspeed',
};

function parseAllowlist(text: string): number[] {
  return text
    .split(/[\s,\n]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => parseInt(s, 10))
    .filter((n) => Number.isFinite(n) && n > 0);
}

/**
 * Assemble the provider_config to ship to /telegram/start (or push via
 * /telegram/refresh-credentials). For OpenAI, prefer OAuth if connected
 * because the founder's intent on this product is "use the ChatGPT plan
 * I already pay for"; an API key in the fallback slot is the API-key
 * path. For everything else, pull the API key from safeStorage.
 *
 * Returns `null` if there's no usable credential for the chosen provider
 * (renderer surfaces this as the "configure a key" error). Returns
 * `{ kind: 'oauth' }` when OAuth was used, so the caller can decide
 * whether to start the periodic refresh interval; an `api_key` result
 * has stable creds and needs no refresh.
 */
async function buildBotProviderConfig(provider: BotProvider): Promise<
  | { kind: 'api_key'; config: Record<string, unknown> }
  | { kind: 'oauth'; config: Record<string, unknown> }
  | null
> {
  const model = BOT_PROVIDER_DEFAULT_MODEL[provider];

  if (provider === 'openai') {
    // OAuth preferred. `getOpenAICredentialsForRequest` refreshes silently
    // if the cached token is within 60s of expiry; on full failure it
    // returns null and we fall through to the API-key path.
    try {
      const creds = await getOpenAICredentialsForRequest();
      if (creds) {
        return {
          kind: 'oauth',
          config: {
            provider: 'openai',
            auth: {
              type: 'oauth',
              access: creds.access,
              refresh: creds.refresh,
              expires: creds.expires,
              account_id: creds.accountId,
            },
            // OpenAI Codex backend uses a different default model than the
            // API path (Codex rejects gpt-4o-mini etc.). Mirror the Analyze
            // page's OAuth default.
            model: OPENAI_CODEX_DEFAULT_MODEL,
            max_tokens: 400,
          },
        };
      }
    } catch {
      // Fall through to API-key path. The OAuth bridge may be unavailable
      // (preload not loaded) or the user may not have connected OAuth.
    }
  }

  const apiKey = await getSecret(PROVIDER_SECRET_KEY[provider]);
  if (!apiKey) {
    return null;
  }
  return {
    kind: 'api_key',
    config: {
      provider,
      auth: { type: 'api_key', api_key: apiKey },
      model,
      max_tokens: 400,
    },
  };
}

// Refresh OAuth tokens this often. OpenAI access tokens are typically 1hr;
// 45 minutes leaves comfortable headroom but doesn't churn unnecessarily.
const OAUTH_REFRESH_INTERVAL_MS = 45 * 60 * 1000;

function TelegramBotPanel() {
  const { t, tp, rich } = useI18n();
  const [token, setToken] = useState('');
  const [hasStoredToken, setHasStoredToken] = useState(false);
  const [allowlistText, setAllowlistText] = useState('');
  const [capText, setCapText] = useState('5.00');
  const [botProvider, setBotProvider] = useState<BotProvider>('openai');
  const [status, setStatus] = useState<TelegramBotStatus | null>(null);
  const [busy, setBusy] = useState<'starting' | 'stopping' | null>(null);
  const [error, setError] = useState<string | null>(null);

  // OAuth refresh interval. Only armed when the bot is started with
  // OpenAI OAuth credentials; cleared on Stop or panel unmount. Holds
  // the interval handle so we can call clearInterval on stop / restart.
  const oauthRefreshRef = useRef<number | null>(null);

  const clearOAuthRefresh = useCallback(() => {
    if (oauthRefreshRef.current !== null) {
      window.clearInterval(oauthRefreshRef.current);
      oauthRefreshRef.current = null;
    }
  }, []);

  const startOAuthRefresh = useCallback((provider: BotProvider) => {
    // Refresh interval is only meaningful when OAuth is the auth path.
    // OAuth is OpenAI-only today; if more providers grow OAuth the gate
    // moves here.
    if (provider !== 'openai') return;
    clearOAuthRefresh();
    oauthRefreshRef.current = window.setInterval(async () => {
      try {
        const built = await buildBotProviderConfig(provider);
        if (built === null || built.kind !== 'oauth') {
          // OAuth no longer available (user disconnected, etc.). The
          // last-pushed token will eventually expire and the next debate
          // will fail with a friendly error. Stop refreshing.
          clearOAuthRefresh();
          return;
        }
        const refreshed = await refreshTelegramBotCredentials(built.config);
        if (refreshed === null) {
          // Engine says bot isn't running. Stop refreshing.
          clearOAuthRefresh();
        }
      } catch {
        // Network blips and other transient errors are fine; the next
        // tick will retry. Don't surface to the UI; it would be noise.
      }
    }, OAUTH_REFRESH_INTERVAL_MS);
  }, [clearOAuthRefresh]);

  // Clear the interval on unmount so we don't leak when the user
  // navigates away from Settings.
  useEffect(() => () => clearOAuthRefresh(), [clearOAuthRefresh]);

  const refresh = useCallback(async () => {
    try {
      const s = await getTelegramBotStatus();
      setStatus(s);
    } catch {
      setStatus(null);
    }
  }, []);

  // Initial load: pull persisted config + current bot status.
  useEffect(() => {
    void (async () => {
      try {
        const existing = await getSecret(TELEGRAM_BOT_TOKEN_KEY);
        setHasStoredToken(Boolean(existing));
      } catch {
        setHasStoredToken(false);
      }
      try {
        const al = localStorage.getItem(TELEGRAM_ALLOWLIST_LS_KEY);
        if (al) setAllowlistText(al);
      } catch {
        // localStorage failures are non-fatal for this read.
      }
      try {
        const cap = localStorage.getItem(TELEGRAM_CAP_LS_KEY);
        if (cap) setCapText(cap);
      } catch {
        // non-fatal
      }
      try {
        const p = localStorage.getItem(TELEGRAM_PROVIDER_LS_KEY);
        if (p && BOT_PROVIDERS.some((bp) => bp.value === p)) {
          setBotProvider(p as BotProvider);
        }
      } catch {
        // non-fatal
      }
      void refresh();
    })();
  }, [refresh]);

  // Poll status every 4s while the panel is mounted so the spend counters
  // and polling pill stay current as messages arrive from Telegram.
  useEffect(() => {
    const id = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const onStart = useCallback(async () => {
    setError(null);
    const effectiveToken = token.trim() || (hasStoredToken
      ? (await getSecret(TELEGRAM_BOT_TOKEN_KEY)) ?? ''
      : '');
    if (!effectiveToken) {
      setError(t('tg.err.token'));
      return;
    }
    const allowlist = parseAllowlist(allowlistText);
    if (allowlist.length === 0) {
      setError(t('tg.err.allowlist'));
      return;
    }
    const cap = parseFloat(capText);
    if (!Number.isFinite(cap) || cap < 0) {
      setError(t('tg.err.cap'));
      return;
    }
    // v1.2: build provider_config with OAuth-awareness for OpenAI. For
    // other providers and the OpenAI API-key fallback, this resolves to
    // the same API-key shape v1.1 used.
    const built = await buildBotProviderConfig(botProvider);
    if (built === null) {
      const label =
        BOT_PROVIDERS.find((p) => p.value === botProvider)?.label ?? botProvider;
      const oauthHint =
        botProvider === 'openai' ? t('tg.hint.openai') : t('tg.hint.other');
      setError(t('tg.err.noCreds', { label, hint: oauthHint }));
      return;
    }
    setBusy('starting');
    try {
      // Persist before starting so a restart picks up the same config.
      if (token.trim()) {
        await setSecret(TELEGRAM_BOT_TOKEN_KEY, token.trim());
        setHasStoredToken(true);
        setToken('');
      }
      try {
        localStorage.setItem(TELEGRAM_ALLOWLIST_LS_KEY, allowlistText.trim());
        localStorage.setItem(TELEGRAM_CAP_LS_KEY, capText.trim());
        localStorage.setItem(TELEGRAM_PROVIDER_LS_KEY, botProvider);
      } catch {
        // non-fatal; engine state remains the source of truth
      }
      const s = await startTelegramBot({
        token: effectiveToken,
        allowlist,
        daily_cap_usd: cap,
        provider_config: built.config,
      });
      setStatus(s);
      // Arm the refresh loop after the bot is confirmed running. The
      // first refresh fires OAUTH_REFRESH_INTERVAL_MS after start, which
      // is well within the typical 1hr OAuth lifetime.
      if (built.kind === 'oauth') {
        startOAuthRefresh(botProvider);
      } else {
        // Switched away from OAuth (or chose a non-OAuth provider on
        // restart). Make sure any prior refresh loop is gone.
        clearOAuthRefresh();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }, [token, hasStoredToken, allowlistText, capText, botProvider, startOAuthRefresh, clearOAuthRefresh]);

  const onStop = useCallback(async () => {
    setBusy('stopping');
    setError(null);
    try {
      const s = await stopTelegramBot();
      setStatus(s);
      clearOAuthRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }, [clearOAuthRefresh]);

  const polling = status?.polling ?? false;
  const spendEntries = Object.entries(status?.daily_spend_usd ?? {});
  const pending: TelegramPendingApproval[] = status?.pending_approvals ?? [];

  const onApprove = useCallback(async (chatId: number) => {
    setError(null);
    try {
      const s = await approveTelegramChat(chatId);
      setStatus(s);
      // Reflect the new allowlist member in the textarea so the user sees
      // the change without having to reload.
      setAllowlistText((prev) => {
        const lines = parseAllowlist(prev);
        if (lines.includes(chatId)) return prev;
        const next = [...lines, chatId].join('\n');
        try {
          localStorage.setItem(TELEGRAM_ALLOWLIST_LS_KEY, next);
        } catch {
          // non-fatal
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const onDeny = useCallback(async (chatId: number) => {
    setError(null);
    try {
      const s = await denyTelegramChat(chatId);
      setStatus(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  return (
    <div className={styles.phaseGuard} style={{ marginTop: 32 }}>
      {rich('tg.title')}
      <br />
      <br />
      {rich('tg.intro')}
      <br />
      <br />
      {rich('tg.edu')}

      {pending.length > 0 && (
        <div
          style={{
            marginTop: 16,
            padding: 12,
            border: '1px solid var(--color-accent)',
            borderRadius: 4,
          }}
          data-testid="telegram-pending-list"
        >
          <strong>{tp('tg.pending.title', pending.length)}</strong>
          <p className={styles.hint} style={{ marginTop: 4 }}>
            {rich('tg.pending.hint')}
          </p>
          <ul className={styles.list} style={{ marginTop: 10 }}>
            {pending.map((p) => (
              <li
                key={p.chat_id}
                className={styles.row}
                data-testid={`telegram-pending-${p.chat_id}`}
              >
                <div className={styles.rowMain}>
                  <div className={styles.rowName}>
                    {p.first_name || t('tg.pending.noName')}{' '}
                    {p.username && (
                      <span className={styles.pill}>@{p.username}</span>
                    )}
                    <span className={styles.pill}>{t('tg.pending.chat', { id: p.chat_id })}</span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button
                    type="button"
                    className={styles.actionPrimary}
                    onClick={() => void onApprove(p.chat_id)}
                    data-testid={`telegram-approve-${p.chat_id}`}
                  >
                    {t('tg.approve')}
                  </button>
                  <button
                    type="button"
                    className={styles.actionSecondary}
                    onClick={() => void onDeny(p.chat_id)}
                    data-testid={`telegram-deny-${p.chat_id}`}
                  >
                    {t('tg.deny')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div style={{ marginTop: 16, display: 'grid', gap: 12 }}>
        <label style={{ display: 'block' }}>
          <span className={styles.fieldLabel}>
            {t('tg.token')}{hasStoredToken ? t('tg.tokenStored') : ''}
          </span>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={hasStoredToken ? '••••••••' : t('tg.tokenPlaceholder')}
            className={styles.input}
            spellCheck={false}
            autoComplete="off"
            data-testid="telegram-bot-token"
          />
        </label>

        <label style={{ display: 'block' }}>
          <span className={styles.fieldLabel}>
            {t('tg.provider')}
          </span>
          <select
            value={botProvider}
            onChange={(e) => setBotProvider(e.target.value as BotProvider)}
            className={styles.input}
            style={{ maxWidth: 240 }}
            data-testid="telegram-bot-provider"
          >
            {BOT_PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <span className={styles.hint} style={{ marginTop: 4, display: 'block' }}>
            {t('tg.providerHint.pre')}
            {botProvider === 'openai'
              ? t('tg.providerHint.openai', {
                  apiModel: BOT_PROVIDER_DEFAULT_MODEL.openai,
                  oauthModel: OPENAI_CODEX_DEFAULT_MODEL,
                })
              : BOT_PROVIDER_DEFAULT_MODEL[botProvider]}
            {t('tg.providerHint.post')}
          </span>
        </label>

        <label style={{ display: 'block' }}>
          <span className={styles.fieldLabel}>
            {t('tg.allowlist')}
          </span>
          <textarea
            value={allowlistText}
            onChange={(e) => setAllowlistText(e.target.value)}
            placeholder={'12345678\n23456789'}
            className={styles.input}
            rows={3}
            spellCheck={false}
            data-testid="telegram-bot-allowlist"
          />
        </label>

        <label style={{ display: 'block' }}>
          <span className={styles.fieldLabel}>
            {t('tg.cap')}
          </span>
          <input
            type="text"
            inputMode="decimal"
            value={capText}
            onChange={(e) => setCapText(e.target.value)}
            className={styles.input}
            style={{ maxWidth: 120 }}
            data-testid="telegram-bot-cap"
          />
        </label>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {polling ? (
            <>
              <button
                type="button"
                className={styles.actionSecondary}
                onClick={onStop}
                disabled={busy !== null}
                data-testid="telegram-bot-stop"
              >
                {busy === 'stopping' ? t('tg.stopping') : t('tg.stop')}
              </button>
              <span className={styles.pill} data-testid="telegram-bot-status">
                {t('tg.polling')}
              </span>
            </>
          ) : (
            <>
              <button
                type="button"
                className={styles.actionPrimary}
                onClick={onStart}
                disabled={busy !== null}
                data-testid="telegram-bot-start"
              >
                {busy === 'starting' ? t('tg.starting') : t('tg.start')}
              </button>
              <span className={styles.pill} data-testid="telegram-bot-status">
                {t('tg.stopped')}
              </span>
            </>
          )}
          {status?.last_error && (
            <span className={styles.formError}>
              {t('tg.lastError', { error: status.last_error })}
            </span>
          )}
        </div>

        {error && <p className={styles.formError}>{error}</p>}

        {spendEntries.length > 0 && (
          <div className={styles.hint} style={{ marginTop: 8 }}>
            {rich('tg.spend')}{' '}
            {spendEntries
              .map(
                ([cid, usd]) =>
                  t('tg.spendEntry', { id: cid, cost: formatUsdShort(Number(usd)) }),
              )
              .join(' · ')}
          </div>
        )}
      </div>
    </div>
  );
}

export default Settings;
