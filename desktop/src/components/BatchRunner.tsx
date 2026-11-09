/**
 * Multi-ticker batch runner (D53, over the engine-side batches of D56/D58).
 *
 * The queue lives in the ENGINE, not in this page: "Run" posts the ticked tickers once (`POST /batches`) and the engine
 * analyses them one after another with no browser connected. This component only (1) starts a batch, (2) reads progress
 * back, and (3) stops it on an explicit click. Closing the page, switching tabs or a phone discarding the tab never
 * cancels anything; on mount the latest batch is restored from the server, even when the watchlist is empty.
 *
 * Polling rules: every poll carries an increasing sequence number and a response older than the one already applied is
 * dropped; errors back off 2 → 5 → 10 s while a "retrying" line is shown; returning to the foreground re-reads at once;
 * unmounting aborts in-flight reads and timers but never calls cancel.
 */

import { isRetiredModel, resolveSelectedModel } from '../lib/model-catalog';
import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './BatchRunner.module.css';
import { useI18n } from '../i18n';
import { describeEngineError } from '../lib/engine-errors';
import {
  BatchApiError,
  PROVIDER_PRIORITY,
  PROVIDER_SECRET_KEY,
  cancelBatch,
  createBatch,
  getBatch,
  listBatches,
  type BatchItem,
  type BatchSnapshot,
  type LLMProvider,
} from '../lib/engine-client';
import { listSecrets } from '../lib/secrets';
import { isReview, normalizeStance, stanceLabel, stanceLean } from '../lib/stance';
import { getOpenAIOAuthStatus } from '../lib/oauth';
import { buildRunStart } from '../lib/run-analysis';
import { setPendingSession } from '../lib/handoff';

const SELECTED_PROVIDER_STORAGE_KEY = 'tal:analyze:selected-provider';
/** localStorage: id of the finished batch the user cleared. Only ever hides a batch that has ended. */
const HIDDEN_BATCH_STORAGE_KEY = 'tal:batch:hidden';

const POLL_MS = 2000;
const BACKOFF_MS = [2000, 5000, 10000];
const STUCK_AFTER_S = 120;
const TERMINAL_CODES: string[] = ['invalid_credential', 'user_cancelled', 'cost_guard_blocked', 'oauth_expired', 'oauth_refresh_failed', 'storage_unavailable', 'server_restart'];

interface BatchRunnerProps {
  /** The tickers the user ticked, in list order. Read once when Run is clicked; later changes never touch a running batch. */
  tickers: string[];
  /** True while a batch is active on the SERVER (or being started), so the caller can lock the selection. */
  onRunningChange?: (running: boolean) => void;
  /** The watchlist has no entries: render nothing unless there is a batch to show. */
  watchlistEmpty?: boolean;
}

interface View {
  snap: BatchSnapshot;
  /** Local time the snapshot was received; live clocks extrapolate from it between polls. */
  at: number;
}

function readHidden(): string | null {
  try {
    return localStorage.getItem(HIDDEN_BATCH_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeHidden(id: string): void {
  try {
    localStorage.setItem(HIDDEN_BATCH_STORAGE_KEY, id);
  } catch {
    // storage unavailable: the batch simply stays visible
  }
}

function newBatchKey(): string {
  const c = globalThis.crypto;
  return typeof c?.randomUUID === 'function' ? c.randomUUID() : `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

function BatchRunner({ tickers, onRunningChange, watchlistEmpty = false }: BatchRunnerProps) {
  const i18n = useI18n();
  const { t, locale } = i18n;
  const [view, setView] = useState<View | null>(null);
  const [pollError, setPollError] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startMsg, setStartMsg] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);
  const [stopSentId, setStopSentId] = useState<string | null>(null);
  const [stopSending, setStopSending] = useState(false);
  const [stopFailed, setStopFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const aliveRef = useRef(true);
  const seqRef = useRef(0);
  const appliedRef = useRef(0);
  const viewRef = useRef<View | null>(null);
  const settledRef = useRef(false);
  const failRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ctrlsRef = useRef<Set<AbortController>>(new Set());
  const startingRef = useRef(false);
  const stopSendingRef = useRef(false);
  const keyRef = useRef<{ sig: string; key: string } | null>(null);
  const onRunningChangeRef = useRef(onRunningChange);
  onRunningChangeRef.current = onRunningChange;

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  /** Apply a snapshot unless a newer one was already applied. `seq` is taken when the request was ISSUED (polls) or when the
   * authoritative write returned (start / stop), so a slow older poll can never overwrite a newer state. */
  const apply = useCallback((seq: number, snap: BatchSnapshot | null) => {
    if (seq < appliedRef.current) return false;
    appliedRef.current = seq;
    const hidden = readHidden();
    const next: View | null = snap && !(hidden && snap.batch_id === hidden && !snap.active) ? { snap, at: Date.now() } : null;
    viewRef.current = next;
    setView(next);
    return true;
  }, []);

  const shouldPoll = useCallback(() => {
    const v = viewRef.current;
    if (v) return v.snap.active;
    return !settledRef.current;
  }, []);

  /** `latest`: read GET /batches?limit=1 even when a batch is already shown (foreground return: a newer batch may exist elsewhere). */
  const pollOnce = useCallback(async (latest = false) => {
    clearTimer();
    if (!aliveRef.current) return;
    const seq = ++seqRef.current;
    const ctrl = new AbortController();
    ctrlsRef.current.add(ctrl);
    const id = latest ? undefined : viewRef.current?.snap.batch_id;
    try {
      const snap = id ? await getBatch(id, ctrl.signal) : ((await listBatches(1, ctrl.signal))[0] ?? null);
      if (!aliveRef.current) return;
      failRef.current = 0;
      settledRef.current = true;
      setPollError(false);
      apply(seq, snap);
    } catch (err) {
      if (!aliveRef.current || ctrl.signal.aborted) return;
      if (err instanceof BatchApiError && err.status === 404 && id) {
        // The batch is gone (retention): nothing left to follow.
        failRef.current = 0;
        settledRef.current = true;
        setPollError(false);
        apply(seq, null);
      } else {
        failRef.current += 1;
        setPollError(true);
      }
    } finally {
      ctrlsRef.current.delete(ctrl);
    }
    scheduleRef.current();
  }, [apply, clearTimer]);

  const scheduleRef = useRef<() => void>(() => {});
  scheduleRef.current = () => {
    clearTimer();
    if (!aliveRef.current || !shouldPoll()) return;
    const delay = failRef.current > 0 ? BACKOFF_MS[Math.min(failRef.current - 1, BACKOFF_MS.length - 1)] : POLL_MS;
    timerRef.current = setTimeout(() => void pollOnce(), delay);
  };

  // Restore on mount + foreground refresh. Unmount stops reads and timers; it NEVER cancels the batch.
  useEffect(() => {
    aliveRef.current = true;
    void pollOnce();
    // Always re-read the latest batch on return, even from an empty screen or a finished batch (another tab/device may have started one).
    const onVisible = () => {
      if (document.visibilityState === 'visible') void pollOnce(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    const ctrls = ctrlsRef.current;
    return () => {
      aliveRef.current = false;
      document.removeEventListener('visibilitychange', onVisible);
      clearTimer();
      for (const c of ctrls) c.abort();
      ctrls.clear();
    };
  }, [pollOnce, clearTimer]);

  const active = !!view?.snap.active;
  useEffect(() => {
    onRunningChangeRef.current?.(active || starting);
  }, [active, starting]);

  // 1 Hz tick for the live clocks, only while a batch is active.
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);

  const onStop = useCallback(async () => {
    const id = viewRef.current?.snap.batch_id;
    if (!id || stopSendingRef.current || stopSentId === id) return;
    stopSendingRef.current = true;
    setStopSending(true);
    setStopFailed(false);
    try {
      const snap = await cancelBatch(id);
      if (!aliveRef.current) return;
      setStopSentId(id);
      apply(++seqRef.current, snap);
      scheduleRef.current();
    } catch {
      // Network error / 5xx: the button comes back and the (idempotent) cancel can be sent again.
      if (aliveRef.current) setStopFailed(true);
    } finally {
      stopSendingRef.current = false;
      if (aliveRef.current) setStopSending(false);
    }
  }, [apply, stopSentId]);

  const onClear = useCallback(() => {
    const v = viewRef.current;
    if (!v || v.snap.active) return;
    writeHidden(v.snap.batch_id);
    viewRef.current = null;
    setView(null);
    setStartMsg(null);
    setStopFailed(false);
  }, []);

  const onRun = useCallback(async () => {
    const queue = Array.from(new Set(tickers));
    if (queue.length === 0 || startingRef.current || viewRef.current?.snap.active) return;
    startingRef.current = true;
    setStarting(true);
    setStartMsg(null);
    try {
      // Resolve the active provider once at start: the user's intent at click time governs the whole batch.
      const provider = await resolveActiveProvider();
      if (!provider.provider) {
        setStartMsg({ kind: 'error', text: t('batch.err.noProvider') });
        return;
      }
      // A saved model the provider has retired would fail every ticker: stop before starting and say so.
      if (isRetiredModel(provider.provider, provider.model)) {
        setStartMsg({ kind: 'error', text: t('batch.err.retiredModel', { model: provider.model }) });
        return;
      }
      const tradeDate = new Date().toISOString().split('T')[0];
      const parts = await buildRunStart({
        trade_date: tradeDate,
        provider: provider.provider,
        openaiAuthKind: provider.openaiAuthKind ?? 'api_key',
        model: provider.model,
        reportLanguage: locale,
      });
      if (!parts.template.provider_config) {
        // No usable credentials: a batch never runs the demo engine.
        setStartMsg({ kind: 'error', text: t('batch.err.noProvider') });
        return;
      }
      // Same click retried (network error / 5xx) reuses the key so the engine attaches to the batch it already made.
      const sig = `${queue.join(',')}|${tradeDate}`;
      if (!keyRef.current || keyRef.current.sig !== sig) keyRef.current = { sig, key: newBatchKey() };
      const snap = await createBatch({ tickers: queue, client_batch_key: keyRef.current.key, start: parts.template });
      keyRef.current = null;
      if (!aliveRef.current) return;
      settledRef.current = true;
      failRef.current = 0;
      setPollError(false);
      setStopSentId(null);
      setStopFailed(false);
      apply(++seqRef.current, snap);
      scheduleRef.current();
    } catch (err) {
      if (!aliveRef.current) return;
      if (err instanceof BatchApiError) {
        const code = err.code;
        if (err.status === 409 && code === 'batch_active') {
          // Someone (this or another tab) already runs a batch: show it instead of starting a second one.
          try {
            const existing = err.batchId ? await getBatch(err.batchId) : (await listBatches(1))[0];
            if (existing && aliveRef.current) {
              settledRef.current = true;
              apply(++seqRef.current, existing);
              scheduleRef.current();
            }
          } catch {
            void pollOnce();
          }
          setStartMsg({ kind: 'info', text: t('batch.err.active') });
        } else if (err.status === 409 && code === 'idempotency_conflict') {
          keyRef.current = null;
          setStartMsg({ kind: 'error', text: t('batch.err.conflict') });
        } else if (code === 'no_provider') {
          keyRef.current = null;
          setStartMsg({ kind: 'error', text: t('batch.err.noProvider') });
        } else if (code === 'invalid_batch' || code === 'secret_ref_rejected') {
          keyRef.current = null;
          setStartMsg({ kind: 'error', text: t('batch.err.invalid') });
        } else if (err.status === 503 || code === 'storage_unavailable') {
          setStartMsg({ kind: 'error', text: t('batch.err.storage') });
        } else {
          setStartMsg({ kind: 'error', text: t('batch.err.startFailed', { error: describeEngineError(err.message, i18n) }) });
        }
      } else {
        const message = err instanceof Error ? err.message : String(err);
        setStartMsg({ kind: 'error', text: t('batch.err.startFailed', { error: describeEngineError(message, i18n) }) });
      }
    } finally {
      startingRef.current = false;
      if (aliveRef.current) setStarting(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickers, t, locale, apply, pollOnce]);

  const snap = view?.snap ?? null;
  if (!snap && watchlistEmpty) return null;

  const stopping = !!snap && snap.active && (snap.cancel_requested || stopSentId === snap.batch_id);
  const items = snap?.items ?? [];

  return (
    <div className={styles.batchCard} data-testid="batch-runner">
      <div className={styles.batchHeader}>
        <h2 className={styles.batchTitle}>{t('batch.title')}</h2>
        <div className={styles.batchStatus} data-testid="batch-status" aria-live="polite">
          {statusLine(snap, stopping, starting, tickers.length, i18n)}
        </div>
        <div className={styles.batchActions}>
          {snap?.active ? (
            <button
              type="button"
              className={styles.stopButton}
              onClick={() => void onStop()}
              disabled={stopping || stopSending}
              data-testid="batch-stop-button"
            >
              {stopping ? t('batch.stopping') : t('batch.stop')}
            </button>
          ) : (
            <>
              {snap && (
                <button type="button" className={styles.clearButton} onClick={onClear} data-testid="batch-clear-button">
                  {t('batch.clear')}
                </button>
              )}
              <button
                type="button"
                className={styles.runButton}
                onClick={() => void onRun()}
                disabled={tickers.length === 0 || starting}
                data-testid="batch-run-button"
              >
                {starting ? t('batch.starting') : t('batch.runSelected', { count: tickers.length })}
              </button>
            </>
          )}
        </div>
      </div>

      {pollError && (
        <p className={styles.pollError} role="status" data-testid="batch-poll-error">
          {t('batch.pollError')}
        </p>
      )}
      {stopFailed && snap?.active && (
        <p className={styles.summaryError} role="alert" data-testid="batch-stop-error">
          {t('batch.stopFailed')}
        </p>
      )}
      {startMsg && (
        <p className={startMsg.kind === 'error' ? styles.summaryError : styles.infoLine} role={startMsg.kind === 'error' ? 'alert' : 'status'} data-testid="batch-start-msg">
          {startMsg.text}
        </p>
      )}

      {snap && (
        <>
          <ProgressBlock snap={snap} i18n={i18n} />
          {snap.active && <CurrentCard snap={snap} at={view!.at} now={now} stopping={stopping} i18n={i18n} />}
          {snap.active && <EtaLine snap={snap} i18n={i18n} />}

          <ul className={styles.progressList}>
            {items.map((row) => (
              <ItemRow key={`${row.index}-${row.ticker}`} row={row} current={snap.active && snap.current_index === row.index} elapsedLive={snap.active && snap.current_index === row.index ? currentElapsed(snap, view!.at, now) : null} i18n={i18n} />
            ))}
          </ul>

          {!snap.active && <SummaryBlock snap={snap} i18n={i18n} />}

          <p className={styles.hint} data-testid="batch-hint">
            {t('batch.hint.serverSide')}
          </p>
        </>
      )}
    </div>
  );
}

type Msgs = ReturnType<typeof useI18n>;

function statusLine(snap: BatchSnapshot | null, stopping: boolean, starting: boolean, selected: number, i18n: Msgs): string {
  const { t, tp } = i18n;
  if (!snap) {
    if (starting) return t('batch.starting');
    return selected === 0 ? t('batch.noneSelected') : tp('batch.ready', selected);
  }
  if (snap.active) {
    if (stopping) return t('batch.stopping');
    if (snap.current_index === null) return t('batch.preparing');
    return t('batch.running', { current: snap.current_index + 1, total: snap.total });
  }
  switch (snap.state) {
    case 'cancelled':
      return t('batch.cancelled');
    case 'stopped':
      return t('batch.stopped');
    case 'interrupted':
      return t('batch.interrupted');
    default:
      return t('batch.complete');
  }
}

function fmtDuration(sec: number, i18n: Msgs): string {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return i18n.t('batch.secondsShort', { n: s });
  const m = Math.floor(s / 60);
  if (m < 60) return i18n.t('batch.minutesShort', { m, s: s % 60 });
  return i18n.t('batch.hoursShort', { h: Math.floor(m / 60), m: m % 60 });
}

function currentElapsed(snap: BatchSnapshot, at: number, now: number): number | null {
  const run = snap.current_run;
  if (!run) return null;
  return run.elapsed_s + Math.max(0, (now - at) / 1000);
}

function ProgressBlock({ snap, i18n }: { snap: BatchSnapshot; i18n: Msgs }) {
  const { t } = i18n;
  const c = snap.counts;
  const n = (k: keyof BatchSnapshot['counts']) => c?.[k] ?? 0;
  const left = n('pending') + n('waiting_slot') + n('running');
  const finished = Math.max(0, snap.total - left);
  const pct = snap.total > 0 ? Math.round((finished / snap.total) * 100) : 0;
  const stoppedN = n('cancelled') + n('interrupted');
  return (
    <div className={styles.progressBlock} data-testid="batch-progress">
      <div
        className={styles.bar}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={snap.total}
        aria-valuenow={finished}
        aria-label={t('batch.progress', { done: finished, total: snap.total })}
      >
        <div className={styles.barFill} style={{ width: `${pct}%` }} data-testid="batch-bar-fill" />
      </div>
      <div className={styles.counts} data-testid="batch-counts">
        <span>{t('batch.progress', { done: finished, total: snap.total })}</span>
        <span>
          {t('batch.counts', { done: n('completed'), failed: n('failed'), skipped: n('skipped'), left })}
          {stoppedN > 0 ? t('batch.counts.stopped', { n: stoppedN }) : ''}
        </span>
      </div>
    </div>
  );
}

function phaseLabel(phase: string | null, i18n: Msgs): string {
  const known = ['starting', 'waiting_slot', 'data', 'evidence', 'debate', 'analysts', 'researchers', 'trader', 'risk', 'done'];
  if (phase && known.includes(phase)) return i18n.t(`batch.phase.${phase}` as 'batch.phase.data');
  return i18n.t('batch.phase.unknown');
}

function CurrentCard({ snap, at, now, stopping, i18n }: { snap: BatchSnapshot; at: number; now: number; stopping: boolean; i18n: Msgs }) {
  const { t } = i18n;
  if (snap.current_index === null) {
    return (
      <div className={styles.currentCard} data-testid="batch-current" data-state="preparing">
        <span className={styles.currentTicker}>{t('batch.current.preparing')}</span>
      </div>
    );
  }
  const item = snap.items.find((i) => i.index === snap.current_index);
  const run = snap.current_run;
  const waiting = item?.state === 'waiting_slot' || run?.phase === 'waiting_slot';
  const elapsed = currentElapsed(snap, at, now);
  const since = run && run.since_last_event_s !== null ? run.since_last_event_s + Math.max(0, (now - at) / 1000) : null;
  const stuck = !waiting && run?.since_last_event_s != null && since !== null && since > STUCK_AFTER_S;
  return (
    <div className={styles.currentCard} data-testid="batch-current" data-state={waiting ? 'waiting' : 'running'}>
      <div className={styles.currentHead}>
        <span className={styles.currentTicker}>{t('batch.current.title', { ticker: item?.ticker ?? '' })}</span>
        {run && <span className={styles.currentPhase} data-testid="batch-current-phase">{phaseLabel(run.phase, i18n)}</span>}
      </div>
      {waiting ? (
        <p className={styles.currentLine} data-testid="batch-current-waiting">{t('batch.current.waitingSlot')}</p>
      ) : (
        run && (
          <p className={styles.currentLine}>
            <span data-testid="batch-current-steps">{t('batch.current.steps', { n: run.steps_done })}</span>
            {elapsed !== null && <span data-testid="batch-current-elapsed">{t('batch.current.elapsed', { time: fmtDuration(elapsed, i18n) })}</span>}
            {since !== null && <span data-testid="batch-current-since">{t('batch.current.lastUpdate', { n: Math.round(since) })}</span>}
          </p>
        )
      )}
      {stuck && <p className={styles.stuck} role="status" data-testid="batch-current-stuck">{t('batch.current.stuck')}</p>}
      {(stopping || run?.cancelling) && <p className={styles.currentLine}>{t('batch.current.cancelling')}</p>}
    </div>
  );
}

function EtaLine({ snap, i18n }: { snap: BatchSnapshot; i18n: Msgs }) {
  const done = snap.items.filter((i) => i.state === 'completed' && typeof i.elapsed_s === 'number');
  if (done.length === 0) return null;
  const left = (snap.counts?.pending ?? 0) + (snap.counts?.waiting_slot ?? 0) + (snap.counts?.running ?? 0);
  if (left === 0) return null;
  const avg = done.reduce((s, i) => s + (i.elapsed_s ?? 0), 0) / done.length;
  return (
    <p className={styles.eta} data-testid="batch-eta">
      {i18n.t('batch.eta', { time: fmtDuration(avg * left, i18n) })}
    </p>
  );
}

function ItemRow({ row, current, elapsedLive, i18n }: { row: BatchItem; current: boolean; elapsedLive: number | null; i18n: Msgs }) {
  const { t } = i18n;
  const failed = row.state === 'failed';
  const stance = row.stance ? normalizeStance(row.stance) : null;
  const elapsed = elapsedLive ?? row.elapsed_s;
  const costText =
    row.cost_known === false && (row.state === 'completed' || row.state === 'failed' || row.cost_partial)
      ? t('batch.cost.unknown')
      : typeof row.estimated_cost_usd === 'number' && row.estimated_cost_usd > 0
        ? `${formatUsdShort(row.estimated_cost_usd)}${row.cost_partial ? ` (${t('batch.cost.partial')})` : ''}`
        : '';
  return (
    <li
      className={`${styles.progressRow} ${current ? styles.progressRow_current : ''} ${failed ? styles.progressRow_failed : ''}`}
      data-testid={`batch-row-${row.ticker}`}
      data-state={row.state}
    >
      <span className={styles.progressTicker}>{row.ticker}</span>
      <span className={`${styles.progressState} ${styles[`state_${row.state}`] ?? ''}`} data-testid={`batch-row-state-${row.ticker}`}>
        {failed
          ? t('batch.row.failed', { error: row.error || row.error_code ? describeEngineError(row.error ?? row.error_code ?? '', i18n) : t('status.unknown') })
          : t(`batch.state.${row.state}` as 'batch.state.pending')}
      </span>
      <span className={`${styles.progressDecision} ${stance ? styles[`progressDecision_${stanceLean(stance)}`] : ''}`}>
        {stance
          ? `${stanceLabel(stance, t)}${typeof row.conviction === 'number' && !isReview(stance) ? ` ${Math.round(row.conviction * 100)}%` : ''}`
          : ''}
      </span>
      <span className={styles.progressMeta}>{costText}</span>
      <span className={styles.progressMeta}>{typeof elapsed === 'number' ? fmtDuration(elapsed, i18n) : ''}</span>
      <span className={styles.progressLink}>
        {row.state === 'completed' && row.session_id && (
          <a href="#history" data-session-id={row.session_id} data-testid={`batch-view-${row.ticker}`} onClick={() => setPendingSession(row.session_id!)}>
            {t('batch.row.view')}
          </a>
        )}
      </span>
    </li>
  );
}

function SummaryBlock({ snap, i18n }: { snap: BatchSnapshot; i18n: Msgs }) {
  const { t } = i18n;
  let bullish = 0;
  let bearish = 0;
  let neutral = 0;
  let review = 0;
  for (const r of snap.items) {
    if (r.state !== 'completed' || !r.stance) continue;
    if (isReview(r.stance)) review += 1;
    else {
      const lean = stanceLean(r.stance);
      if (lean === 'bullish') bullish += 1;
      else if (lean === 'bearish') bearish += 1;
      else neutral += 1;
    }
  }
  const completed = snap.counts?.completed ?? 0;
  const failed = snap.counts?.failed ?? 0;
  const cost = snap.cost;
  const hasCost = !!cost && (cost.known_usd > 0 || cost.unknown_items > 0 || cost.partial);
  const costMarked = !!cost && (cost.partial || cost.unknown_items > 0);
  const reason = snap.terminal_code && TERMINAL_CODES.includes(snap.terminal_code) ? t(`batch.terminal.${snap.terminal_code}` as 'batch.terminal.user_cancelled') : null;
  return (
    <div data-testid="batch-summary">
      <div className={styles.summaryBar}>
        <span>{t('batch.summary.complete', { done: completed, total: snap.total })}</span>
        {bullish > 0 && <span>{t('batch.summary.bullish', { n: bullish })}</span>}
        {bearish > 0 && <span>{t('batch.summary.bearish', { n: bearish })}</span>}
        {review > 0 && <span>{t('batch.summary.review', { n: review })}</span>}
        {neutral > 0 && <span>{t('batch.summary.neutral', { n: neutral })}</span>}
        {failed > 0 && <span>{t('batch.summary.failed', { n: failed })}</span>}
        {hasCost && (
          <span data-testid="batch-summary-cost">
            {cost.known_usd > 0 || !costMarked
              ? t('batch.summary.cost', { cost: formatUsdShort(cost.known_usd) })
              : t('batch.cost.unknown')}
            {costMarked && cost.known_usd > 0 ? t('batch.summary.costPartial') : ''}
          </span>
        )}
      </div>
      {reason && <p className={styles.terminalReason} data-testid="batch-terminal-reason">{reason}</p>}
      {snap.error && <p className={styles.summaryError} data-testid="batch-error">{describeEngineError(snap.error, i18n)}</p>}
    </div>
  );
}

function formatUsdShort(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(4)}`;
}

interface ActiveProvider {
  provider: LLMProvider | null;
  openaiAuthKind?: 'oauth' | 'api_key';
  model: string;
}

/**
 * Mirrors the Analyze page's active-provider resolution: respect the
 * user's manual dropdown choice (localStorage) when its credentials
 * still exist, otherwise fall back to the priority order's first
 * configured provider. OAuth wins over API key on the OpenAI path.
 */
export async function resolveActiveProvider(): Promise<ActiveProvider> {
  const [secrets, oauth] = await Promise.all([
    listSecrets().catch(() => []),
    getOpenAIOAuthStatus().catch(() => ({ connected: false } as const)),
  ]);
  const stored = new Set(secrets.map((s) => s.key));
  const configured = new Set<LLMProvider>();
  for (const p of PROVIDER_PRIORITY) {
    const hasKey = stored.has(PROVIDER_SECRET_KEY[p]);
    if (p === 'openai' && (oauth.connected || hasKey)) configured.add(p);
    else if (p === 'local' && stored.has(PROVIDER_SECRET_KEY.local) && stored.has('local:model')) configured.add(p);
    else if (p !== 'openai' && p !== 'local' && hasKey) configured.add(p);
  }
  if (configured.size === 0) {
    return { provider: null, model: '' };
  }
  let manual: LLMProvider | null = null;
  try {
    const raw = localStorage.getItem(SELECTED_PROVIDER_STORAGE_KEY);
    if (raw && (PROVIDER_PRIORITY as readonly string[]).includes(raw)) {
      manual = raw as LLMProvider;
    }
  } catch {
    // ignore
  }
  const provider =
    (manual && configured.has(manual) ? manual : null) ??
    PROVIDER_PRIORITY.find((p) => configured.has(p)) ??
    null;
  const openaiAuthKind: 'oauth' | 'api_key' =
    oauth.connected ? 'oauth' : 'api_key';
  // The user's saved model for this provider (shared with the Analyze picker and its refreshed catalog), else the recommendation.
  const model = provider ? resolveSelectedModel(provider, openaiAuthKind) : '';
  return { provider, openaiAuthKind, model };
}

export default BatchRunner;
