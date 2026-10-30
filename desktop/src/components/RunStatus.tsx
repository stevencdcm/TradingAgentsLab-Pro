import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import type { DebateEvent } from '../lib/engine-client';
import { deriveRunStatus, type RunClock } from '../lib/run-status';
import { agentLabelFor } from '../lib/transcript';
import { derivePhaseTotals } from './DebateStream';
import styles from './RunStatus.module.css';

interface Props {
  events: DebateEvent[];
  clock: RunClock;
  streaming: boolean;
  provider: string | null;
  model: string | null;
}

function fmt(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

/** Live "is it running?" panel. Re-renders once a second while streaming; everything shown comes from
 * real engine events or the engine's own heartbeat (see lib/run-status.ts for what each one means). */
export default function RunStatus({ events, clock, streaming, provider, model }: Props) {
  const i18n = useI18n();
  const { t } = i18n;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!streaming) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [streaming]);
  if (clock.startedAt === 0) return null;

  const total = derivePhaseTotals(events).totalAgents;
  const v = deriveRunStatus(events, clock, streaming ? now : (clock.endedAt ?? now), streaming, {
    provider,
    model,
    totalSteps: events.some((e) => e.type === 'graph.plan') ? total : null,
  });
  // No provider configured and no engine heartbeat: this is the canned demo run, no model is being called.
  const demo = !provider && !clock.heartbeat && (v.state === 'waiting_model' || v.state === 'connecting');
  const live = streaming && v.state !== 'completed';
  const note =
    v.quiet === 'no_heartbeat'
      ? t('runStatus.note.noHeartbeat', { age: fmt(v.heartbeatAgeMs ?? now - clock.startedAt) })
      : v.quiet === 'quiet'
        ? t('runStatus.note.quiet', { age: fmt(v.sinceEventMs ?? now - clock.startedAt) })
        : v.state === 'waiting_model' && !demo
          ? t('runStatus.note.waiting')
          : v.state === 'disconnected'
            ? t('runStatus.note.disconnected', { error: clock.streamError ?? '' })
            : null;

  return (
    <section className={styles.panel} data-testid="run-status" data-state={v.state} data-quiet={v.quiet} aria-live="polite">
      <div className={styles.head}>
        <span className={styles.title}>{t('runStatus.title')}</span>
        <span className={`${styles.state} ${styles[`s_${v.state}`] ?? ''}`}>
          {live && v.quiet === 'none' && <span className={styles.dot} aria-hidden="true" />}
          {demo ? t('runStatus.state.demo') : t(`runStatus.state.${v.state}` as const)}
        </span>
      </div>
      <dl className={styles.grid}>
        <div>
          <dt>{t('runStatus.model')}</dt>
          <dd data-testid="run-status-model">
            {[v.provider, v.model].filter(Boolean).join(' / ') || '—'}
            {' · '}
            {v.route ? t(`runStatus.route.${v.route}` as const) : t('runStatus.routeUnknown')}
          </dd>
        </div>
        <div>
          <dt>{t('runStatus.elapsed')}</dt>
          <dd data-testid="run-status-elapsed">{fmt(v.elapsedMs)}</dd>
        </div>
        <div>
          <dt>{t('runStatus.sinceEvent')}</dt>
          <dd data-testid="run-status-since">{v.sinceEventMs === null ? t('runStatus.sinceEventNone') : fmt(v.sinceEventMs)}</dd>
        </div>
        <div>
          <dt>{t('runStatus.lastDone')}</dt>
          <dd>
            {v.activeAgent
              ? `${t('runStatus.activeAgent')}: ${agentLabelFor(v.activeAgent, t)}`
              : v.lastCompletedAgent
                ? agentLabelFor(v.lastCompletedAgent, t)
                : '—'}
            {' · '}
            {v.stepsTotal ? t('runStatus.steps', { done: v.stepsDone, total: v.stepsTotal }) : t('runStatus.stepsOpen', { done: v.stepsDone })}
          </dd>
        </div>
      </dl>
      {clock.cancelling && (
        <p className={`${styles.note} ${styles.noteWarn}`} data-testid="run-status-cancelling">{t('runStatus.cancelling')}</p>
      )}
      {clock.usagePartial && !clock.cancelling && (
        <p className={styles.note} data-testid="run-status-usage-partial">{t('runStatus.usagePartial')}</p>
      )}
      {(clock.connection === 'reconnecting' || clock.connection === 'offline' || clock.resumed) && (
        <p className={`${styles.note} ${clock.connection === 'connected' || !clock.connection ? '' : styles.noteWarn}`} data-testid="run-status-connection" data-connection={clock.connection ?? 'connected'}>
          {clock.connection === 'reconnecting'
            ? t('runStatus.conn.reconnecting')
            : clock.connection === 'offline'
              ? t('runStatus.conn.offline')
              : t('runStatus.conn.resumed')}
        </p>
      )}
      <p className={styles.line} data-testid="run-status-heartbeat">
        {streaming
          ? v.heartbeatAgeMs === null
            ? t('runStatus.engineNoHeartbeat')
            : t('runStatus.engineAlive', { age: fmt(v.heartbeatAgeMs) })
          : null}
      </p>
      <p className={styles.line} data-testid="run-status-tokens">
        {v.tokens
          ? t('runStatus.tokens', { input: i18n.formatNumber(v.tokens.input), output: i18n.formatNumber(v.tokens.output) })
          : t('runStatus.tokensUnknown')}
      </p>
      {note && (
        <p className={`${styles.note} ${v.quiet !== 'none' || v.state === 'disconnected' ? styles.noteWarn : ''}`} data-testid="run-status-note">
          {note}
        </p>
      )}
    </section>
  );
}
