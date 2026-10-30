import { useI18n } from '../i18n';
import styles from './LearnAI.module.css';

/**
 * Learn AI — an in-app page (not a raw external link) introducing Clawdemy,
 * RBJ Global's free AI education platform, with a single deliberate button to
 * open it. Permanent in-app spot for the educational mission + the family.
 *
 * Content is drawn from clawdemy.org and rbjglobal.com (factual, no claims
 * invented here). The "Open Clawdemy" link opens the browser via main.ts'
 * setWindowOpenHandler (http(s) only).
 */
const CLAWDEMY_URL = 'https://clawdemy.org';

export default function LearnAI() {
  const { t, rich } = useI18n();
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{t('learn.title')}</h1>
      <p className={styles.lede}>{rich('learn.lede')}</p>

      <section className={styles.card}>
        <h2 className={styles.h2}>{t('learn.what.title')}</h2>
        <p>{rich('learn.what.body')}</p>
      </section>

      <section className={styles.card}>
        <h2 className={styles.h2}>{t('learn.read.title')}</h2>
        <p>{t('learn.read.body')}</p>
      </section>

      <section className={styles.card}>
        <h2 className={styles.h2}>{t('learn.free.title')}</h2>
        <p>{t('learn.free.body')}</p>
      </section>

      <section className={styles.card}>
        <h2 className={styles.h2}>{t('learn.who.title')}</h2>
        <p>{t('learn.who.body')}</p>
        <p className={styles.connect}>
          {t('learn.connect')}{' '}
          <a
            href="https://www.linkedin.com/in/junaidsiddiqi/"
            target="_blank"
            rel="noreferrer"
          >
            {t('learn.linkedin')}
          </a>
          {t('learn.sentenceEnd')}
        </p>
      </section>

      <div className={styles.ctaRow}>
        <a
          className={styles.cta}
          href={CLAWDEMY_URL}
          target="_blank"
          rel="noreferrer"
        >
          {t('learn.cta')}
        </a>
        <span className={styles.ctaHint}>{t('learn.ctaHint')}</span>
      </div>

      <p className={styles.footnote}>{t('learn.footnote')}</p>
    </div>
  );
}
