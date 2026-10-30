import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './Watchlist.module.css';
import {
  addWatchlist,
  listWatchlist,
  removeWatchlist,
  type WatchlistEntry,
} from '../lib/engine-client';
import { setPendingTicker } from '../lib/handoff';
import BatchRunner from '../components/BatchRunner';
import { useI18n } from '../i18n';
import { formatRelativeTime } from '../lib/relative-time';
import { describeEngineError } from '../lib/engine-errors';

function Watchlist() {
  const i18n = useI18n();
  const { t, rich } = i18n;
  const [entries, setEntries] = useState<WatchlistEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [tickerInput, setTickerInput] = useState('');
  const [noteInput, setNoteInput] = useState('');
  const tickerInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const rows = await listWatchlist();
      setEntries(rows);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : t('watchlist.err.load'));
      setEntries([]);
    }
  }, [t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onAdd = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const cleaned = tickerInput.trim().toUpperCase();
      if (!cleaned) {
        setFormError(t('watchlist.err.enterTicker'));
        return;
      }
      if (cleaned.length > 8) {
        setFormError(t('watchlist.err.tickerLength'));
        return;
      }
      setBusy(true);
      setFormError(null);
      try {
        const newEntry = await addWatchlist({
          ticker: cleaned,
          note: noteInput.trim() || undefined,
        });
        setEntries((prev) => [newEntry, ...(prev ?? [])]);
        setTickerInput('');
        setNoteInput('');
        tickerInputRef.current?.focus();
      } catch (err) {
        setFormError(err instanceof Error ? err.message : t('watchlist.err.add'));
      } finally {
        setBusy(false);
      }
    },
    [tickerInput, noteInput, t],
  );

  const onRemove = useCallback(
    async (ticker: string) => {
      if (!confirm(t('watchlist.confirmRemove', { ticker }))) return;
      setBusy(true);
      try {
        await removeWatchlist(ticker);
        setEntries((prev) => (prev ?? []).filter((e) => e.ticker !== ticker));
      } catch (err) {
        setLoadError(err instanceof Error ? err.message : t('watchlist.err.remove'));
      } finally {
        setBusy(false);
      }
    },
    [t],
  );

  const onAnalyze = useCallback((ticker: string) => {
    setPendingTicker(ticker);
    window.location.hash = '#analyze';
  }, []);

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>{t('watchlist.title')}</h1>
        <p className={styles.pageSubtitle}>{rich('watchlist.subtitle')}</p>
      </header>

      <form className={styles.addCard} onSubmit={onAdd}>
        <div className={styles.fieldRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="watchlist-ticker">{t('watchlist.ticker')}</label>
            <input
              id="watchlist-ticker"
              ref={tickerInputRef}
              className={styles.input}
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value.toUpperCase())}
              placeholder="NVDA"
              maxLength={8}
              disabled={busy}
              autoFocus
              data-testid="watchlist-ticker-input"
            />
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="watchlist-note">
              {t('watchlist.note')} <span className={styles.optional}>{t('watchlist.optional')}</span>
            </label>
            <input
              id="watchlist-note"
              className={styles.input}
              value={noteInput}
              onChange={(e) => setNoteInput(e.target.value)}
              placeholder={t('watchlist.notePlaceholder')}
              maxLength={200}
              disabled={busy}
            />
          </div>
          <div className={styles.fieldButton}>
            <button
              type="submit"
              className={styles.addButton}
              disabled={busy || !tickerInput.trim()}
              data-testid="watchlist-add-button"
            >
              {busy ? t('watchlist.adding') : t('watchlist.add')}
            </button>
          </div>
        </div>
        {formError && <p className={styles.formError}>{describeEngineError(formError, i18n)}</p>}
      </form>

      {loadError && <div className={styles.errorBanner}>{describeEngineError(loadError, i18n)}</div>}

      {entries && entries.length > 0 && (
        <BatchRunner tickers={entries.map((e) => e.ticker)} />
      )}

      {entries === null && !loadError && (
        <div className={styles.placeholder}>{t('watchlist.loading')}</div>
      )}

      {entries && entries.length === 0 && !loadError && (
        <div className={styles.empty}>
          <h2 className={styles.emptyTitle}>{t('watchlist.emptyTitle')}</h2>
          <p className={styles.emptyBody}>
            {rich('watchlist.emptyBody', undefined, styles.code)}
          </p>
        </div>
      )}

      {entries && entries.length > 0 && (
        <ul className={styles.list}>
          {entries.map((entry) => (
            <li
              key={entry.ticker}
              className={styles.row}
              data-testid={`watchlist-row-${entry.ticker}`}
            >
              <div className={styles.rowMain}>
                <div className={styles.rowHeadline}>
                  <span className={styles.rowTicker}>{entry.ticker}</span>
                  <span className={styles.rowAdded}>
                    {t('watchlist.added', {
                      when: formatRelativeTime(entry.added_at, i18n, entry.added_at),
                    })}
                  </span>
                </div>
                {entry.note && <p className={styles.rowNote}>{entry.note}</p>}
              </div>
              <div className={styles.rowActions}>
                <button
                  className={styles.actionPrimary}
                  onClick={() => onAnalyze(entry.ticker)}
                  disabled={busy}
                  type="button"
                >
                  {t('watchlist.analyze')}
                </button>
                <button
                  className={styles.actionRemove}
                  onClick={() => void onRemove(entry.ticker)}
                  disabled={busy}
                  type="button"
                  aria-label={t('watchlist.removeAria', { ticker: entry.ticker })}
                >
                  {t('watchlist.remove')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default Watchlist;
