/**
 * Upstream check result modal — shown after the user clicks Help → Check
 * for Updates or Settings → About → Check for updates.
 *
 * Three states:
 * - "checking" — spinner while the IPC round-trip is in flight
 * - "ok"       — green checkmark + "fully caught up" + dismiss
 * - "behind"   — list of upstream commits + link to GitHub compare view
 * - "error"    — surface the underlying git error (no upstream remote, etc.)
 *
 * Does NOT auto-merge. Surfaces what's available; merging is a deliberate
 * step the maintainer takes from the terminal because upstream changes can
 * touch agent prompts / decision parser / role definitions wrapped by
 * engine/live_debate.py.
 */

import { useEffect } from 'react';

import type { UpstreamCheckResult } from '../lib/upstream';
import { useI18n } from '../i18n';
import { describeEngineError } from '../lib/engine-errors';
import styles from './UpstreamCheckModal.module.css';

export interface UpstreamCheckModalProps {
  /** "checking" while the check is in flight; a result object once it returns. */
  state: 'checking' | UpstreamCheckResult;
  onDismiss: () => void;
}

export function UpstreamCheckModal({ state, onDismiss }: UpstreamCheckModalProps): JSX.Element {
  const i18n = useI18n();
  const { t, tp, rich, formatDateTime } = i18n;
  // Esc dismisses. Always allowed — this is informational, not a gate.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onDismiss();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onDismiss]);

  if (state === 'checking') {
    return (
      <div className={styles.scrim} role="dialog" aria-modal="true">
        <div className={styles.modal}>
          <h2 className={styles.title}>{t('upstream.checking.title')}</h2>
          <p className={styles.body}>{t('upstream.checking.body')}</p>
          <div className={styles.spinner} />
        </div>
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className={styles.scrim} role="dialog" aria-modal="true">
        <div className={styles.modal}>
          <h2 className={`${styles.title} ${styles.titleError}`}>{t('upstream.error.title')}</h2>
          <p className={styles.body}>{describeEngineError(state.error ?? '', i18n)}</p>
          <div className={styles.buttons}>
            <button type="button" className={styles.dismiss} onClick={onDismiss} autoFocus>
              {t('upstream.close')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (state.status === 'ok') {
    return (
      <div className={styles.scrim} role="dialog" aria-modal="true">
        <div className={styles.modal}>
          <h2 className={`${styles.title} ${styles.titleOk}`}>{t('upstream.ok.title')}</h2>
          <p className={styles.body}>{rich('upstream.ok.body', undefined, styles.code)}</p>
          <dl className={styles.facts}>
            <div className={styles.factRow}>
              <dt>{t('upstream.fact.latestTag')}</dt>
              <dd>{state.latestTag || t('upstream.fact.none')}</dd>
            </div>
            <div className={styles.factRow}>
              <dt>{t('upstream.fact.upstreamHead')}</dt>
              <dd><code className={styles.code}>{state.upstreamHead}</code></dd>
            </div>
            <div className={styles.factRow}>
              <dt>{t('upstream.fact.ourHead')}</dt>
              <dd><code className={styles.code}>{state.ourHead}</code></dd>
            </div>
            <div className={styles.factRow}>
              <dt>{t('upstream.fact.ahead')}</dt>
              <dd>{t('upstream.fact.commits', { count: state.aheadCount })}</dd>
            </div>
          </dl>
          <p className={styles.hint}>
            {t('upstream.checkedAt', {
              time: t('upstream.at', {
                time: formatDateTime(state.checkedAt, { timeStyle: 'medium' }),
              }),
            })}
          </p>
          <div className={styles.buttons}>
            <button type="button" className={styles.dismiss} onClick={onDismiss} autoFocus>
              {t('upstream.ok')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // status === 'behind'
  return (
    <div className={styles.scrim} role="dialog" aria-modal="true">
      <div className={styles.modal}>
        <h2 className={`${styles.title} ${styles.titleWarn}`}>
          {tp('upstream.behind.title', state.behindCount)}
        </h2>
        <p className={styles.body}>
          {rich(
            state.behindCount === 1 ? 'upstream.behind.body.one' : 'upstream.behind.body.other',
            { count: state.behindCount, tag: state.latestTag || t('upstream.fact.none') },
            styles.code,
          )}
        </p>
        <ul className={styles.commitList}>
          {state.behindCommits.slice(0, 12).map((line, idx) => (
            <li key={idx} className={styles.commitItem}>
              <code className={styles.code}>{line}</code>
            </li>
          ))}
          {state.behindCommits.length > 12 && (
            <li className={styles.commitItem}>
              <em>{t('upstream.behind.more', { count: state.behindCommits.length - 12 })}</em>
            </li>
          )}
        </ul>
        <p className={styles.hint}>{t('upstream.behind.hint')}</p>
        <pre className={styles.codeBlock}>
{`git fetch upstream
git merge upstream/main
bash tools/dev-smoke.sh`}
        </pre>
        <div className={styles.buttons}>
          <button
            type="button"
            className={styles.dismiss}
            onClick={onDismiss}
          >
            {t('upstream.close')}
          </button>
          <button
            type="button"
            className={styles.primary}
            onClick={() => {
              window.open(state.compareUrl, '_blank', 'noopener,noreferrer');
            }}
            autoFocus
          >
            {t('upstream.behind.view')}
          </button>
        </div>
      </div>
    </div>
  );
}


