import { useI18n } from '../i18n';
import { PROVIDER_LABEL, PROVIDER_SECRET_KEY, type LLMProvider } from '../lib/engine-client';
import { CATALOG_PROVIDERS, useModelCatalog } from '../lib/model-catalog';
import styles from './ModelCatalogPanel.module.css';

/** Providers listed in this order; Anthropic first (the one whose hardcoded list went stale). */
const ORDER: LLMProvider[] = ['anthropic', 'openai', 'gemini', 'xai', 'openrouter'];

const KNOWN_ERRORS = new Set(['auth', 'rate_limited', 'network', 'timeout', 'no_credential']);

interface Props {
  /** Secret names that currently hold a saved key (Settings' listing). */
  savedSecretKeys: ReadonlySet<string>;
  disabled?: boolean;
}

/** Settings → LLM providers → "更新模型清單": manual, one click per provider, discovery only (no inference, no billing). */
export default function ModelCatalogPanel({ savedSecretKeys, disabled }: Props) {
  const { t } = useI18n();
  return (
    <section className={styles.panel} aria-labelledby="model-catalog-title" data-testid="model-catalog-panel">
      <h3 className={styles.title} id="model-catalog-title">{t('modelCatalog.title')}</h3>
      <p className={styles.hint}>{t('modelCatalog.intro')}</p>
      <ul className={styles.list}>
        {ORDER.filter((p) => CATALOG_PROVIDERS.has(p)).map((p) => (
          <CatalogRow key={p} provider={p} hasKey={savedSecretKeys.has(PROVIDER_SECRET_KEY[p])} disabled={!!disabled} />
        ))}
      </ul>
      <p className={styles.hint}>{t('modelCatalog.unsupported')}</p>
      <p className={styles.hint}>{t('modelCatalog.caveat')}</p>
    </section>
  );
}

function CatalogRow({ provider, hasKey, disabled }: { provider: LLMProvider; hasKey: boolean; disabled: boolean }) {
  const i18n = useI18n();
  const { t } = i18n;
  const cat = useModelCatalog(provider);
  const loading = cat.status === 'loading';
  const when = cat.fetchedAt ? new Date(cat.fetchedAt * 1000).toLocaleString(i18n.locale) : '';
  const failed = cat.error !== null;
  const errText = cat.error
    ? KNOWN_ERRORS.has(cat.error.code)
      ? t(`modelCatalog.error.${cat.error.code}` as 'modelCatalog.error.auth')
      : t('modelCatalog.error.generic', { code: cat.error.code })
    : '';
  const idBase = `model-catalog-${provider}`;
  return (
    <li className={styles.item} data-testid={`model-catalog-${provider}`}>
      <div className={styles.main}>
        <span className={styles.name}>{PROVIDER_LABEL[provider]}</span>
        <span className={styles.status} id={`${idBase}-status`} role="status" aria-live="polite">
          {!hasKey
            ? t('modelCatalog.noKey')
            : loading
              ? t('modelCatalog.refreshing')
              : cat.fetchedAt === null
                ? t('modelCatalog.never')
                : cat.stale
                  ? t('modelCatalog.stale', { time: when })
                  : t('modelCatalog.updated', { time: when, count: cat.models.length })}
        </span>
        {failed && !loading && <span className={styles.error} role="alert">{errText}</span>}
      </div>
      <button
        type="button"
        className={styles.action}
        onClick={cat.refresh}
        disabled={disabled || !hasKey || loading}
        aria-describedby={`${idBase}-status`}
        data-testid={`model-catalog-refresh-${provider}`}
      >
        {loading ? t('modelCatalog.refreshing') : failed ? t('modelCatalog.retry') : t('modelCatalog.refresh')}
      </button>
    </li>
  );
}
