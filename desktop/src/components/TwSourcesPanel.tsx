import { useCallback, useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { listTwSources, testTwSource, type TwSource } from '../lib/engine-client';
import styles from './TwSourcesPanel.module.css';

/** Taiwan data sources: what each provides, the free-tier limits, and what the engine has ACTUALLY observed
 * (a source is only "connected" after a real successful call; wired-but-untested is shown as such). */
export default function TwSourcesPanel({ tokenSaved }: { tokenSaved: boolean }) {
  const { t, formatDateTime } = useI18n();
  const [sources, setSources] = useState<TwSource[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      setSources(await listTwSources());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, tokenSaved]);

  const test = async (id: string) => {
    setBusy(id);
    const r = await testTwSource(id);
    setResults((prev) => ({
      ...prev,
      [id]: r.ok
        ? `${t('twSources.testOk')}${r.ms !== undefined ? ` · ${r.ms} ms` : ''}${r.rows !== undefined ? ` · ${r.rows}` : ''}${r.note ? ` · ${r.note}` : ''}`
        : `${t('twSources.testFail')}: ${r.error ?? r.status ?? '?'}${r.http_status ? ` (HTTP ${r.http_status})` : ''}`,
    }));
    setBusy(null);
    void load();
  };

  return (
    <section className={styles.panel} data-testid="tw-sources">
      <h3 className={styles.title}>{t('twSources.title')}</h3>
      <p className={styles.intro}>{t('twSources.intro')}</p>
      {error && <p className={styles.error} role="alert">{t('twSources.loadError', { error })}</p>}
      {sources?.map((s) => (
        <article key={s.id} className={styles.card} data-testid={`tw-source-${s.id}`} data-state={s.state}>
          <header className={styles.head}>
            <h4 className={styles.name}>{s.name}</h4>
            <span className={`${styles.state} ${styles[`s_${s.state}`] ?? ''}`} data-testid={`tw-source-state-${s.id}`}>
              {t(`twSources.state.${s.state}` as const)}
            </span>
          </header>
          <dl className={styles.facts}>
            <div><dt>{t('twSources.provides')}</dt><dd>{s.free_scope}</dd></div>
            <div><dt>{t('twSources.usedBy')}</dt><dd>{s.used_by.length ? s.used_by.join('、') : t('twSources.notUsed')}</dd></div>
            <div><dt>{t('twSources.token')}</dt><dd>{s.token_requirement}{s.token_configured !== null && ` · ${s.token_configured ? t('twSources.tokenSaved') : t('twSources.tokenNone')}`}</dd></div>
            <div><dt>{t('twSources.limits')}</dt><dd>{s.limits}</dd></div>
            <div><dt>{t('twSources.cadence')}</dt><dd>{s.update_cadence === 'unknown' ? t('twSources.unknownCadence') : s.update_cadence}</dd></div>
            <div><dt>{t('twSources.license')}</dt><dd>{s.license_notes}</dd></div>
            <div><dt>{t('twSources.verified')}</dt><dd>{s.verified_at} · {s.verification_scope}</dd></div>
            <div>
              <dt>{t('twSources.lastSuccess')}</dt>
              <dd data-testid={`tw-source-last-${s.id}`}>
                {s.last_ok_at ? formatDateTime(s.last_ok_at, { dateStyle: 'medium', timeStyle: 'short' }) : t('twSources.neverObserved')}
                {s.last_error && s.state === 'failed' ? ` · ${t('twSources.lastError')}: ${s.last_error}` : ''}
              </dd>
            </div>
          </dl>
          <div className={styles.links}>
            {s.official_docs.map((u) => (
              <a key={u} href={u} target="_blank" rel="noopener noreferrer">{u.replace(/^https?:\/\//, '').slice(0, 60)}</a>
            ))}
          </div>
          {s.implementation_status === 'wired' && (
            <div className={styles.actions}>
              <button type="button" className={styles.testButton} disabled={busy !== null} onClick={() => void test(s.id)} data-testid={`tw-source-test-${s.id}`}>
                {busy === s.id ? t('twSources.testing') : t('twSources.test')}
              </button>
              {results[s.id] && <span className={styles.result} role="status">{results[s.id]}</span>}
            </div>
          )}
        </article>
      ))}
    </section>
  );
}
