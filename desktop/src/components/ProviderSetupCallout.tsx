import { useT } from '../i18n';
import styles from './ProviderSetupCallout.module.css';

/**
 * Shown on Analyze when no LLM provider is configured (activeProvider === null).
 * New users otherwise have no idea the app is running a built-in sample instead
 * of the real multi-agent analysis, and no signpost to Settings. This is an
 * informational callout (not a blocking wizard); it auto-hides once a provider
 * is added. The button deep-links to Settings (LLM Providers is the default tab).
 */
export default function ProviderSetupCallout() {
  const t = useT();
  return (
    <div className={styles.callout} role="status">
      <div className={styles.body}>
        <h2 className={styles.title}>{t('providerCallout.title')}</h2>
        <p className={styles.text}>{t('providerCallout.text')}</p>
      </div>
      <button
        type="button"
        className={styles.cta}
        onClick={() => {
          window.location.hash = '#settings';
        }}
        data-testid="setup-provider-cta"
      >
        {t('providerCallout.cta')}
      </button>
    </div>
  );
}
