import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import styles from './ConsentGate.module.css';

/**
 * First-launch consent gate (Phase 7c.3). Blocks the app until the user accepts
 * the educational-use / not-financial-advice agreement. No account, no signup:
 * acceptance is recorded locally (main-process `consent` bridge) and the user
 * is re-prompted only when the agreement version bumps. Decline quits the app.
 *
 * Wrap the app shell: <ConsentGate>{appContent}</ConsentGate>. Children render
 * only once consent for the current version is on record.
 */
type Status = 'loading' | 'needed' | 'ok';

export default function ConsentGate({ children }: { children: React.ReactNode }) {
  const { t, rich } = useI18n();
  const [status, setStatus] = useState<Status>('loading');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    window.tradingAgentsLab.consent
      .get()
      .then((s) => {
        if (cancelled) return;
        setStatus(s.acceptedVersion === s.requiredVersion ? 'ok' : 'needed');
      })
      .catch(() => {
        // Fail safe: if we can't read consent state, show the gate rather
        // than letting someone through unprompted.
        if (!cancelled) setStatus('needed');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Brief: avoids flashing either the app or the gate before we know the state.
  if (status === 'loading') return null;
  if (status === 'ok') return <>{children}</>;

  const onAgree = async () => {
    setBusy(true);
    try {
      await window.tradingAgentsLab.consent.accept();
      setStatus('ok');
    } catch {
      setBusy(false); // let them try again
    }
  };

  const onDecline = () => {
    setBusy(true);
    void window.tradingAgentsLab.consent.decline(); // main quits the app
  };

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="consent-title">
      <div className={styles.card}>
        <h1 id="consent-title" className={styles.title}>{t('consent.title')}</h1>
        <div className={styles.body}>
          <p>{rich('consent.p1')}</p>
          <p>{t('consent.p2')}</p>
          <p>{rich('consent.p3')}</p>
          <p className={styles.links}>
            {t('consent.fullTerms')}{' '}
            <a href="https://tradingagentslab.ai/legal/terms" target="_blank" rel="noreferrer">{t('consent.terms')}</a>{t('consent.listSep')}
            <a href="https://tradingagentslab.ai/legal/privacy" target="_blank" rel="noreferrer">{t('consent.privacy')}</a>{t('consent.listSep')}
            <a href="https://tradingagentslab.ai/legal/disclaimer" target="_blank" rel="noreferrer">{t('consent.disclaimer')}</a>{t('consent.listEnd')}
            <br />
            {t('consent.englishNote')}
          </p>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.decline}
            onClick={onDecline}
            disabled={busy}
          >
            {t('consent.decline')}
          </button>
          <button
            type="button"
            className={styles.agree}
            onClick={onAgree}
            disabled={busy}
          >
            {t('consent.agree')}
          </button>
        </div>
      </div>
    </div>
  );
}
