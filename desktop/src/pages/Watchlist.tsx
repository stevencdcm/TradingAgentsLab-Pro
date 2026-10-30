import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './Watchlist.module.css';
import {
  addWatchlist,
  listWatchlist,
  removeWatchlist,
  searchTwSymbols,
  type TwSymbol,
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

  // Taiwan symbol/name suggestions (official TWSE/TPEx index). US tickers never touch this path.
  const [suggestions, setSuggestions] = useState<TwSymbol[]>([]);
  const [twState, setTwState] = useState<'idle' | 'loading' | 'ok' | 'error'>('idle');
  const [twNote, setTwNote] = useState<{ asOf: string | null; stale: boolean; error: string | null } | null>(null);
  const [twError, setTwError] = useState<string | null>(null);
  const [selected, setSelected] = useState<TwSymbol | null>(null);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [listOpen, setListOpen] = useState(false);

  useEffect(() => {
    const q = tickerInput.trim();
    // Taiwan queries start with a digit (code) or contain CJK (name); plain letters are US tickers.
    const looksTaiwan = q.length > 0 && (/^\d/.test(q) || /[^\u0000-\u007f]/.test(q));
    if (!looksTaiwan || (selected && selected.code === q)) {
      setSuggestions([]);
      setTwState('idle');
      return;
    }
    setTwState('loading');
    let cancelled = false;
    const timer = setTimeout(async () => {
      const res = await searchTwSymbols(q);
      if (cancelled) return;
      if (res.ok) {
        setSuggestions(res.results);
        setTwState('ok');
        setTwError(null);
        setTwNote({ asOf: res.as_of, stale: res.stale, error: res.refresh_error });
        setActiveIdx(res.results.length > 0 ? 0 : -1);
        setListOpen(true);
      } else {
        setSuggestions([]);
        setTwState('error');
        setTwError(res.error);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [tickerInput, selected]);

  const pick = useCallback((sym: TwSymbol) => {
    setSelected(sym);
    setTickerInput(sym.code);
    setListOpen(false);
    setSuggestions([]);
    setFormError(null);
  }, []);

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
      // A Taiwan name (non-ASCII) must be picked from the official suggestions; a typed code is
      // resolved against them too, so the market (上市/上櫃) always comes from the index.
      let pickedTw = selected && selected.code === cleaned ? selected : null;
      if (!pickedTw) {
        if (/[^\u0000-\u007f]/.test(cleaned)) {
          const exact = suggestions.filter((x) => x.name === tickerInput.trim());
          if (exact.length === 1) pickedTw = exact[0];
          else {
            setFormError(t('watchlist.tw.pick'));
            return;
          }
        } else if (/^\d/.test(cleaned)) {
          pickedTw = suggestions.find((x) => x.code === cleaned) ?? null;
        }
      }
      if (!pickedTw && cleaned.length > 8) {
        setFormError(t('watchlist.err.tickerLength'));
        return;
      }
      setBusy(true);
      setFormError(null);
      try {
        const newEntry = await addWatchlist({
          ticker: pickedTw ? pickedTw.code : cleaned,
          market: pickedTw?.market,
          note: noteInput.trim() || undefined,
        });
        setEntries((prev) => [newEntry, ...(prev ?? [])]);
        setTickerInput('');
        setSelected(null);
        setSuggestions([]);
        setNoteInput('');
        tickerInputRef.current?.focus();
      } catch (err) {
        setFormError(err instanceof Error ? err.message : t('watchlist.err.add'));
      } finally {
        setBusy(false);
      }
    },
    [tickerInput, noteInput, selected, suggestions, t],
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
              onChange={(e) => {
                setTickerInput(e.target.value.toUpperCase());
                setSelected(null);
                setListOpen(true);
              }}
              onKeyDown={(e) => {
                if (!listOpen || suggestions.length === 0) return;
                if (e.key === 'ArrowDown') {
                  e.preventDefault();
                  setActiveIdx((i) => (i + 1) % suggestions.length);
                } else if (e.key === 'ArrowUp') {
                  e.preventDefault();
                  setActiveIdx((i) => (i - 1 + suggestions.length) % suggestions.length);
                } else if (e.key === 'Enter' && activeIdx >= 0) {
                  e.preventDefault();
                  pick(suggestions[activeIdx]);
                } else if (e.key === 'Escape') {
                  setListOpen(false);
                }
              }}
              placeholder={t('watchlist.ticker.placeholder')}
              maxLength={20}
              disabled={busy}
              autoFocus
              autoComplete="off"
              role="combobox"
              aria-expanded={listOpen && suggestions.length > 0}
              aria-controls="watchlist-suggestions"
              aria-autocomplete="list"
              data-testid="watchlist-ticker-input"
            />
            {(twState !== 'idle' || suggestions.length > 0) && (
              <div className={styles.suggestBox} data-testid="watchlist-tw-box">
                {twState === 'loading' && <p className={styles.suggestNote}>{t('watchlist.tw.searching')}</p>}
                {twState === 'error' && (
                  <p className={styles.suggestError} role="alert" data-testid="watchlist-tw-error">
                    {t('watchlist.tw.error', { error: twError ?? '' })}
                  </p>
                )}
                {twState === 'ok' && suggestions.length === 0 && (
                  <p className={styles.suggestNote}>{t('watchlist.tw.none', { q: tickerInput.trim() })}</p>
                )}
                {listOpen && suggestions.length > 0 && (
                  <ul id="watchlist-suggestions" role="listbox" className={styles.suggestList} data-testid="watchlist-suggestions">
                    {suggestions.map((sym, i) => (
                      <li
                        key={`${sym.market}-${sym.code}`}
                        role="option"
                        aria-selected={i === activeIdx}
                        className={`${styles.suggestItem} ${i === activeIdx ? styles.suggestActive : ''}`}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          pick(sym);
                        }}
                        data-testid={`watchlist-suggestion-${sym.code}`}
                      >
                        <span className={styles.suggestCode}>{sym.code}</span>
                        <span className={styles.suggestName}>{sym.name}</span>
                        <span className={styles.badge}>{t(`watchlist.tw.${sym.market}` as const)}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {twState === 'ok' && twNote && (
                  <p className={styles.suggestNote}>
                    {twNote.stale
                      ? t('watchlist.tw.stale', { asOf: twNote.asOf ?? '—', error: twNote.error ?? '' })
                      : t('watchlist.tw.source', { asOf: twNote.asOf ?? '—' })}
                  </p>
                )}
              </div>
            )}
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
                  {entry.name && <span className={styles.rowName}>{entry.name}</span>}
                  {entry.market && <span className={styles.badge}>{t(`watchlist.tw.${entry.market}` as const)}</span>}
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
