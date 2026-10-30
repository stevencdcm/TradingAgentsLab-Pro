import { formatRunDuration } from '../lib/duration';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import styles from './History.module.css';
import DebateStream from '../components/DebateStream';
import {
  deleteSession,
  getSession,
  listSessions,
  type SessionDetail,
  type SessionSummary,
} from '../lib/engine-client';
import { isReview, stanceLabel, stanceLean } from '../lib/stance';
import { buildTranscriptMarkdown } from '../lib/transcript';
import { useI18n } from '../i18n';
import { formatRelativeTime } from '../lib/relative-time';
import { describeEngineError } from '../lib/engine-errors';

type View = 'list' | 'detail';

/** Sort modes for the History list. Default 'recent' matches the engine's
 * created_at DESC ordering — we apply the sort client-side so a future
 * switch (e.g. cost DESC) doesn't require a round-trip and the user gets
 * instant feedback. */
type SortMode = 'recent' | 'cost' | 'ticker';

const SORT_STORAGE_KEY = 'tal:history:sort';



// decision_action holds the stance string for new rows and BUY/SELL/HOLD
// for rows persisted before the stance relabel — stanceLean normalizes both.
function stancePillClass(value: string): string {
  if (isReview(value)) return styles.pill_review;
  const lean = stanceLean(value);
  if (lean === 'bullish') return styles.pill_bullish;
  if (lean === 'bearish') return styles.pill_bearish;
  return styles.pill_neutral;
}

function History() {
  const i18n = useI18n();
  const { t, rich, locale } = i18n;
  const formatRelative = (iso: string) => formatRelativeTime(iso, i18n, iso);
  const [view, setView] = useState<View>('list');
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>(() => {
    try {
      const saved = localStorage.getItem(SORT_STORAGE_KEY);
      if (saved === 'recent' || saved === 'cost' || saved === 'ticker') {
        return saved;
      }
    } catch {
      // localStorage offline — fall through to default
    }
    return 'recent';
  });
  // Generation counter — guards against rapid row clicks where an earlier
  // (slower) fetch could land after a later one and stomp the UI.
  const detailGenRef = useRef(0);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const rows = await listSessions({ limit: 50 });
      setSessions(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('history.err.load'));
      setSessions([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onOpen = useCallback(async (id: string) => {
    const gen = ++detailGenRef.current;
    setActiveId(id);
    setView('detail');
    setDetail(null);
    setCopied(false);
    setError(null);
    try {
      const d = await getSession(id);
      // Only commit the result if no later click has superseded this one.
      if (gen === detailGenRef.current) {
        setDetail(d);
      }
    } catch (err) {
      if (gen === detailGenRef.current) {
        setError(err instanceof Error ? err.message : t('history.err.loadSession'));
      }
    }
  }, []);

  const onBack = useCallback(() => {
    setView('list');
    setActiveId(null);
    setDetail(null);
    setError(null);
  }, []);

  const onDelete = useCallback(
    async (id: string) => {
      if (!confirm(t('history.confirmDelete'))) return;
      setBusy(true);
      setError(null);
      try {
        await deleteSession(id);
        if (activeId === id) {
          onBack();
        }
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : t('history.err.delete'));
      } finally {
        setBusy(false);
      }
    },
    [activeId, onBack, refresh, t],
  );

  const onCopyTranscript = useCallback(async () => {
    if (!detail) return;
    const md = buildTranscriptMarkdown(detail.events, locale);
    try {
      await navigator.clipboard.writeText(md);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      setError(
        t('analyze.err.copyFailed', {
          error: err instanceof Error ? err.message : t('status.unknown'),
        }),
      );
    }
  }, [detail, locale, t]);

  // Same full-page HTML view as Analyze — lazily imported for the same
  // bundle-size reason (react-dom/server rides along with the generator).
  const onOpenHtml = useCallback(async () => {
    if (!detail) return;
    try {
      const { buildTranscriptHtml } = await import('../lib/transcript-html');
      const { html, suggestedName } = buildTranscriptHtml(detail.events, locale);
      await window.tradingAgentsLab.transcript.openHtml(html, suggestedName);
    } catch (err) {
      setError(
        t('analyze.err.openHtmlFailed', {
          error: err instanceof Error ? err.message : t('status.unknown'),
        }),
      );
    }
  }, [detail, locale, t]);

  const stats = useMemo(() => {
    if (!sessions) return null;
    const total = sessions.length;
    const live = sessions.filter((s) => s.live).length;
    const totalCost = sessions.reduce(
      (acc, s) => acc + (s.estimated_cost_usd ?? 0),
      0,
    );
    return { total, live, totalCost };
  }, [sessions]);

  // Sorted view of the engine's list. Engine returns created_at DESC by
  // default which matches 'recent'; the other two modes re-sort the array
  // in place of a re-fetch. Tiebreaker is always created_at DESC so two
  // free runs at the same cost don't shuffle on every render.
  const sortedSessions = useMemo(() => {
    if (!sessions) return null;
    if (sortMode === 'recent') return sessions;
    const arr = [...sessions];
    if (sortMode === 'cost') {
      arr.sort((a, b) => {
        const ac = a.estimated_cost_usd ?? 0;
        const bc = b.estimated_cost_usd ?? 0;
        if (bc !== ac) return bc - ac;
        return b.created_at.localeCompare(a.created_at);
      });
    } else if (sortMode === 'ticker') {
      arr.sort((a, b) => {
        const cmp = a.ticker.localeCompare(b.ticker);
        if (cmp !== 0) return cmp;
        return b.created_at.localeCompare(a.created_at);
      });
    }
    return arr;
  }, [sessions, sortMode]);

  const onSortChange = useCallback((mode: SortMode) => {
    setSortMode(mode);
    try {
      localStorage.setItem(SORT_STORAGE_KEY, mode);
    } catch {
      // ignore — sort still applies for the current session
    }
  }, []);

  if (view === 'detail') {
    return (
      <div className={styles.page} data-testid="history-page">
        <header className={styles.detailHeader}>
          <button className={styles.backButton} onClick={onBack} type="button">
            {t('history.back')}
          </button>
          {detail && (
            <div className={styles.detailToolbar}>
              <button
                className={styles.toolbarButton}
                onClick={onCopyTranscript}
                disabled={busy}
                type="button"
              >
                {copied ? t('history.copied') : t('history.copyTranscript')}
              </button>
              <button
                className={styles.toolbarButton}
                onClick={onOpenHtml}
                disabled={busy}
                type="button"
                data-testid="history-open-html"
              >
                {t('history.openHtml')}
              </button>
              <button
                className={`${styles.toolbarButton} ${styles.toolbarDanger}`}
                onClick={() => void onDelete(detail.id)}
                disabled={busy}
                type="button"
              >
                {t('history.delete')}
              </button>
            </div>
          )}
        </header>

        {error && <div className={styles.errorBanner}>{describeEngineError(error, i18n)}</div>}

        {!detail && !error && (
          <div className={styles.placeholder}>{t('history.loadingSession')}</div>
        )}

        {detail && (
          <>
            <section className={styles.detailSummary}>
              <div className={styles.detailTickerBlock}>
                <span className={styles.detailTicker}>{detail.ticker}</span>
                <span className={styles.detailDate}>{detail.trade_date}</span>
              </div>
              <div className={styles.detailMetaBlock}>
                <span className={styles.detailMetaItem}>
                  {t('history.saved', { when: formatRelative(detail.created_at) })}
                </span>
                {detail.live ? (
                  <span className={styles.detailMetaPill}>
                    {t('history.live', {
                      provider: detail.provider ?? 'openai',
                      model: detail.model ?? '?',
                    })}
                    {' · '}
                    <span data-testid="history-detail-duration">{formatRunDuration(detail.elapsed_s, t)}</span>
                  </span>
                ) : (
                  <span className={styles.detailMetaPillNeutral}>{t('history.stub')}</span>
                )}
                {detail.estimated_cost_usd !== null && (
                  <span className={styles.detailMetaItem}>
                    {t('history.estCost', { cost: `$${detail.estimated_cost_usd.toFixed(4)}` })}
                  </span>
                )}
              </div>
            </section>

            <DebateStream events={detail.events} isStreaming={false} />
          </>
        )}
      </div>
    );
  }

  return (
    <div className={styles.page} data-testid="history-page">
      <header className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>{t('history.title')}</h1>
        <p className={styles.pageSubtitle}>{rich('history.subtitle', undefined, styles.code)}</p>
      </header>

      {stats && stats.total > 0 && (
        <div className={styles.statBar}>
          <div className={styles.statItem}>
            <span className={styles.statLabel}>{t('history.stat.sessions')}</span>
            <span className={styles.statValue}>{stats.total}</span>
          </div>
          <div className={styles.statItem}>
            <span className={styles.statLabel}>{t('history.stat.live')}</span>
            <span className={styles.statValue}>{stats.live}</span>
          </div>
          <div className={styles.statItem}>
            <span className={styles.statLabel}>{t('history.stat.stub')}</span>
            <span className={styles.statValue}>{stats.total - stats.live}</span>
          </div>
          {stats.totalCost > 0 && (
            <div className={styles.statItem}>
              <span className={styles.statLabel}>{t('history.stat.cost')}</span>
              <span className={styles.statValue}>
                ${stats.totalCost.toFixed(4)}
              </span>
            </div>
          )}
        </div>
      )}

      {error && <div className={styles.errorBanner}>{describeEngineError(error, i18n)}</div>}

      {sessions === null && !error && (
        <div className={styles.placeholder}>{t('history.loading')}</div>
      )}

      {sessions && sessions.length === 0 && !error && (
        <div className={styles.empty}>
          <h2 className={styles.emptyTitle}>{t('history.emptyTitle')}</h2>
          <p className={styles.emptyBody}>
            {t('history.emptyBody.pre')}
            <a href="#analyze">{t('history.emptyBody.link')}</a>
            {t('history.emptyBody.post')}
          </p>
        </div>
      )}

      {sortedSessions && sortedSessions.length > 0 && (
        <div className={styles.toolbar}>
          <label className={styles.toolbarLabel} htmlFor="history-sort">
            {t('history.sortBy')}
          </label>
          <select
            id="history-sort"
            className={styles.toolbarSelect}
            value={sortMode}
            onChange={(e) => onSortChange(e.target.value as SortMode)}
          >
            {(['recent', 'cost', 'ticker'] as SortMode[]).map((m) => (
              <option key={m} value={m}>
                {t(`history.sort.${m}` as const)}
              </option>
            ))}
          </select>
        </div>
      )}

      {sortedSessions && sortedSessions.length > 0 && (
        <ul className={styles.list}>
          {sortedSessions.map((s) => (
            <li key={s.id} className={styles.row}>
              <button
                className={styles.rowMain}
                onClick={() => void onOpen(s.id)}
                type="button"
              >
                <div className={styles.rowHeadline}>
                  <span className={styles.rowTicker}>{s.ticker}</span>
                  <span className={`${styles.actionPill} ${stancePillClass(s.decision_action)}`}>
                    {stanceLabel(s.decision_action, t)}
                    {!isReview(s.decision_action) && (
                      <span className={styles.actionConfidence}>
                        {Math.round(s.decision_confidence * 100)}%
                      </span>
                    )}
                  </span>
                  {s.live && (
                    <span className={styles.livePill}>
                      {t('history.live', {
                        provider: s.provider ?? 'openai',
                        model: s.model ?? '?',
                      })}
                      {' · '}
                      <span data-testid={`history-duration-${s.id}`}>{formatRunDuration(s.elapsed_s, t)}</span>
                    </span>
                  )}
                </div>
                <div className={styles.rowMeta}>
                  <span>{s.trade_date}</span>
                  <span className={styles.rowSep}>·</span>
                  <span>{formatRelative(s.created_at)}</span>
                  {s.estimated_cost_usd !== null ? (
                    <>
                      <span className={styles.rowSep}>·</span>
                      <span>{t('history.estCost', { cost: `$${s.estimated_cost_usd.toFixed(4)}` })}</span>
                    </>
                  ) : s.live ? (
                    <>
                      <span className={styles.rowSep}>·</span>
                      <span>{t('history.costUnknown')}</span>
                    </>
                  ) : null}
                </div>
                <div className={styles.rowReasoning}>{s.decision_reasoning}</div>
              </button>
              <button
                className={styles.rowDelete}
                onClick={() => void onDelete(s.id)}
                disabled={busy}
                type="button"
                aria-label={t('history.deleteAria', { ticker: s.ticker, date: s.trade_date })}
              >
                {t('history.delete')}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default History;
