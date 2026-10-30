import { formatRunDuration } from '../lib/duration';
import { findNotices, findResearch, researchNotes } from '../lib/research-notes';
import { useEffect, useRef, useState } from 'react';
import type {
  DebateEvent,
  AnalyzeDecision,
  QuoteSummary,
  Headline,
  NewsHeadlinesEvent,
} from '../lib/engine-client';
import Markdown from './Markdown';
import { isReview, normalizeStance, riskLabel, stanceLabel, stanceLean } from '../lib/stance';
import { useI18n, type I18n } from '../i18n';
import { localizeEngineError } from '../lib/engine-errors';
import styles from './DebateStream.module.css';

type Tr = I18n['t'];

interface DebateStreamProps {
  events: DebateEvent[];
  isStreaming: boolean;
}

/**
 * Free-engine fallback roster — matches engine/live_debate.py `_AGENTS`
 * (fixed single-pass debate: 4 analysts, 3 researchers, 1 trader, 4 risk).
 * Used ONLY when the stream carries no `graph.plan` event, i.e. the free
 * simplified engine. The Pro full-graph engine emits `graph.plan` and its
 * per-phase totals are derived from that instead (analyst selection is
 * user-chosen and the debate loops run multiple rounds).
 */
const AGENTS_PER_PHASE: Record<string, number> = {
  analysts: 4,
  researchers: 3,
  trader: 1,
  risk: 4,
};

const PHASE_ORDER: string[] = ['analysts', 'researchers', 'trader', 'risk'];

/** Wire agent name -> phase, for attributing `agent.activity` (which carries
 * no phase field) to the right phase so a tool-looping analyst can light up
 * the Analysts phase before its first finished message lands. Covers the full
 * Pro roster; agents not present here simply don't move the active marker. */
const AGENT_PHASE: Record<string, string> = {
  technical_analyst: 'analysts',
  sentiment_analyst: 'analysts',
  news_analyst: 'analysts',
  fundamental_analyst: 'analysts',
  bull_researcher: 'researchers',
  bear_researcher: 'researchers',
  research_manager: 'researchers',
  trader: 'trader',
  risk_aggressive: 'risk',
  risk_conservative: 'risk',
  risk_neutral: 'risk',
  portfolio_manager: 'risk',
};

interface PhaseProgress {
  phase: string;
  done: number;
  total: number;
  state: 'pending' | 'active' | 'done';
}

/**
 * Per-phase totals for the run. When a `graph.plan` event is present (Pro
 * full-graph engine) totals are derived from it: the analysts total is the
 * count of selected analysts, and the researcher/risk loops run
 * `2*max_debate_rounds+1` (bull+bear per round, then the research manager) and
 * `3*max_risk_rounds+1` (aggressive+conservative+neutral per round, then the
 * portfolio manager). With no plan (free engine) we fall back to the fixed
 * 4/3/1/4 roster. Verified against a live rounds=1 Pro run (researchers 3,
 * risk 4) and unit-tested for rounds=2 (researchers 5, risk 7).
 */
export function derivePhaseTotals(events: DebateEvent[]): {
  totals: Record<string, number>;
  totalAgents: number;
} {
  const plan = events.find((e) => e.type === 'graph.plan');
  // WS events are unchecked `as DebateEvent`, so a malformed/version-skewed
  // graph.plan (missing rounds, non-array analysts) would otherwise yield NaN
  // totals ("0/NaN"). Validate the fields; degrade to the fixed roster if bad.
  if (
    plan &&
    plan.type === 'graph.plan' &&
    Array.isArray(plan.analysts) &&
    Number.isFinite(plan.max_debate_rounds) &&
    Number.isFinite(plan.max_risk_rounds)
  ) {
    const totals: Record<string, number> = {
      analysts: plan.analysts.length,
      researchers: 2 * plan.max_debate_rounds + 1,
      trader: 1,
      risk: 3 * plan.max_risk_rounds + 1,
    };
    const totalAgents = PHASE_ORDER.reduce((sum, p) => sum + (totals[p] ?? 0), 0);
    return { totals, totalAgents };
  }
  const totalAgents = Object.values(AGENTS_PER_PHASE).reduce((a, b) => a + b, 0);
  return { totals: AGENTS_PER_PHASE, totalAgents };
}

/**
 * Walk the event stream and compute per-phase + aggregate progress.
 *
 * - `done` count = agent.message events seen in that phase (multi-round Pro
 *   runs accumulate naturally; each finished turn is one message).
 * - `total` per phase comes from `derivePhaseTotals` (plan-aware).
 * - A phase is `done` once `done >= total`.
 * - The most recently active phase (latest agent.message, phase.transition
 *   target, or agent.activity attributed via AGENT_PHASE) is marked `active`;
 *   everything else pending. Walking forward and keeping the last wins ensures
 *   a stalled phase doesn't keep showing "active" after the stream moved on.
 */
export function computeProgress(events: DebateEvent[]): {
  phases: PhaseProgress[];
  totalDone: number;
  totalAgents: number;
} {
  const { totals, totalAgents } = derivePhaseTotals(events);
  const counts: Record<string, number> = {};
  let lastActivePhase: string | null = null;

  for (const ev of events) {
    if (ev.type === 'agent.message') {
      counts[ev.phase] = (counts[ev.phase] ?? 0) + 1;
      lastActivePhase = ev.phase;
    } else if (ev.type === 'phase.transition') {
      lastActivePhase = ev.to;
    } else if (ev.type === 'agent.activity') {
      const phase = AGENT_PHASE[ev.agent];
      if (phase) lastActivePhase = phase;
    }
  }

  const phases: PhaseProgress[] = PHASE_ORDER.map((phase) => {
    const done = counts[phase] ?? 0;
    const total = totals[phase] ?? 0;
    let state: 'pending' | 'active' | 'done' = 'pending';
    // `done >= total` also marks a zero-total phase done (nothing to do), e.g.
    // a Pro plan with zero selected analysts — it should not sit 'pending'.
    if (done >= total) state = 'done';
    else if (lastActivePhase === phase || done > 0) state = 'active';
    return { phase, done, total, state };
  });

  const totalDone = phases.reduce((sum, p) => sum + p.done, 0);
  return { phases, totalDone, totalAgents };
}

/** Format elapsed milliseconds for the live clock. Compact: "12s",
 * "1m 24s", "1h 02m". Long-form (1h+) is for slow local models. */
function formatElapsed(ms: number, t: Tr): string {
  if (ms < 0) return t('debate.elapsed.s', { s: 0 });
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return t('debate.elapsed.s', { s: totalSec });
  const totalMin = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (totalMin < 60) {
    return t('debate.elapsed.ms', { m: totalMin, s: String(sec).padStart(2, '0') });
  }
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return t('debate.elapsed.hm', { h: hr, m: String(min).padStart(2, '0') });
}

function findSummary(events: DebateEvent[]): QuoteSummary | null {
  const ev = events.find((e) => e.type === 'data.summary');
  if (ev && ev.type === 'data.summary') {
    const { type: _t, ...summary } = ev;
    return summary as QuoteSummary;
  }
  return null;
}

function findHeadlines(events: DebateEvent[]): NewsHeadlinesEvent | null {
  const ev = events.find((e) => e.type === 'news.headlines');
  return ev && ev.type === 'news.headlines' ? ev : null;
}

function formatRelativeTime(iso: string, i18n: I18n): string {
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return '';
  const diffSec = Math.round((Date.now() - ts) / 1000);
  const { t } = i18n;
  if (diffSec < 60) return t('debate.time.justNow');
  if (diffSec < 3600) return t('debate.time.minutesAgo', { n: Math.round(diffSec / 60) });
  if (diffSec < 86400) return t('debate.time.hoursAgo', { n: Math.round(diffSec / 3600) });
  if (diffSec < 86400 * 7) return t('debate.time.daysAgo', { n: Math.round(diffSec / 86400) });
  return i18n.formatDateTime(iso, { dateStyle: 'medium' });
}

function formatVolume(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${Math.round(n)}`;
}

interface PhaseGroup {
  phase: string;
  messages: Array<{ agent: string; content: string; round?: number }>;
}

function groupByPhase(events: DebateEvent[]): PhaseGroup[] {
  const groups: PhaseGroup[] = [];
  for (const ev of events) {
    if (ev.type !== 'agent.message') continue;
    // `round` is Pro-only (researcher/risk loops); undefined on the free
    // engine and on single-turn phases, in which case no label renders.
    const msg = { agent: ev.agent, content: ev.content, round: ev.round };
    const last = groups[groups.length - 1];
    if (last && last.phase === ev.phase) {
      last.messages.push(msg);
    } else {
      groups.push({ phase: ev.phase, messages: [msg] });
    }
  }
  return groups;
}

function findStart(events: DebateEvent[]): { ticker: string; trade_date: string } | null {
  const start = events.find((e) => e.type === 'session.start');
  if (start && start.type === 'session.start') {
    return { ticker: start.ticker, trade_date: start.trade_date };
  }
  return null;
}

function findDecision(events: DebateEvent[]): AnalyzeDecision | null {
  const complete = events.find((e) => e.type === 'session.complete');
  if (complete && complete.type === 'session.complete') {
    return complete.decision;
  }
  return null;
}

interface WebhookReport {
  results: import('../lib/webhooks').WebhookResult[];
}

function findWebhookReport(events: DebateEvent[]): WebhookReport | null {
  const evt = events.find((e) => e.type === 'webhook.report');
  if (evt && evt.type === 'webhook.report') {
    return { results: evt.results };
  }
  return null;
}

interface DecisionMeta {
  live: boolean;
  provider?: string;
  model?: string;
  inputTokens?: number;
  outputTokens?: number;
  estimatedCostUsd?: number | null;
  elapsedS?: number | null;
}

function findDecisionMeta(events: DebateEvent[]): DecisionMeta {
  const complete = events.find((e) => e.type === 'session.complete');
  if (complete && complete.type === 'session.complete') {
    return {
      live: complete.live === true,
      provider: complete.provider,
      model: complete.model,
      inputTokens: complete.input_tokens,
      outputTokens: complete.output_tokens,
      estimatedCostUsd: complete.estimated_cost_usd,
      elapsedS: complete.elapsed_s ?? null,
    };
  }
  return { live: false };
}

/** Pro-only terminal states. A run can stop without a session.complete:
 * `run.token_cap` (budget stop, expected) or `run.error` (node failure).
 * The WS still closes cleanly on these, so DebateStream detects the event
 * to render a stopped/error notice in place of the decision card rather than
 * ending the transcript silently. Kind distinguishes the two so the copy and
 * styling read as "stopped" vs "failed". */
interface TerminalState {
  kind: 'token_cap' | 'error';
  /** token_cap: cap + used; error: the raw engine error text. */
  cap?: number | null;
  used?: number;
  error?: string;
}

function findTerminal(events: DebateEvent[]): TerminalState | null {
  for (const ev of events) {
    if (ev.type === 'run.token_cap') {
      return { kind: 'token_cap', cap: ev.cap, used: ev.used };
    }
    if (ev.type === 'run.error') {
      return { kind: 'error', error: ev.error };
    }
  }
  return null;
}

/** Known wire phase ids (anything else renders its raw id). */
const KNOWN_PHASES = ['analysts', 'researchers', 'trader', 'risk'] as const;
type KnownPhase = (typeof KNOWN_PHASES)[number];
const isKnownPhase = (p: string): p is KnownPhase => (KNOWN_PHASES as readonly string[]).includes(p);

const KNOWN_AGENTS = [
  'technical_analyst',
  'fundamental_analyst',
  'news_analyst',
  'sentiment_analyst',
  'bull_researcher',
  'bear_researcher',
  'research_manager',
  'trader',
  'risk_aggressive',
  'risk_conservative',
  'risk_neutral',
  'portfolio_manager',
] as const;
type KnownAgent = (typeof KNOWN_AGENTS)[number];
const isKnownAgent = (a: string): a is KnownAgent => (KNOWN_AGENTS as readonly string[]).includes(a);

/* Present-tense narration of what the active phase is doing, for the live
   status banner. Each agent turn takes 20-60s of silent LLM work, so this plus
   the always-moving shimmer bar and ticking clock reassures the user that the
   run is progressing rather than stuck. */

// Founder-measured real full-graph runs (2026-07-06, TSLA, all four
// analysts): ~11m30s on Anthropic API; OAuth/OpenAI in the same band.
// Varies with provider, model split, debate rounds, and analyst count,
// so the copy quotes a range rather than a point estimate.
function DebateStream({ events, isStreaming }: DebateStreamProps) {
  const i18n = useI18n();
  const { t, tp } = i18n;
  const start = findStart(events);
  const summary = findSummary(events);
  const news = findHeadlines(events);
  const evidencePack = events.find((e) => e.type === 'evidence.pack') as
    | Extract<DebateEvent, { type: 'evidence.pack' }>
    | undefined;
  const dexter = events.find((e) => e.type === 'dexter.result') as
    | Extract<DebateEvent, { type: 'dexter.result' }>
    | undefined;
  const notices = findNotices(events);
  const researchNotesList = researchNotes(findResearch(events), t, i18n.formatNumber);
  const groups = groupByPhase(events);
  const decision = findDecision(events);
  const meta = findDecisionMeta(events);
  const progress = computeProgress(events);
  const webhookReport = findWebhookReport(events);
  // Only surface a terminal notice when the run stopped WITHOUT a decision;
  // a completed run that also happened to emit a late cost event should show
  // its decision card, not a stop notice.
  const terminal = decision ? null : findTerminal(events);

  // Live elapsed clock — captured on first event arrival, frozen when
  // session.complete lands. For History replay (events present from the
  // start, !isStreaming), we never start the clock — duration was a
  // server-side concern that didn't make it onto the wire and faking
  // "0s" would be worse than not showing it.
  const startedAtRef = useRef<number | null>(null);
  const endedAtRef = useRef<number | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    if (events.length === 0) {
      // A new/cleared run. This component stays mounted across runs (it just
      // renders null when empty), so the refs survive — reset them or the
      // next run would reuse the previous run's start/end and the clock would
      // never tick (endedAt already set) or show a bogus elapsed.
      startedAtRef.current = null;
      endedAtRef.current = null;
      return;
    }
    if (startedAtRef.current === null && isStreaming) {
      startedAtRef.current = Date.now();
    }
    if (decision !== null && endedAtRef.current === null) {
      endedAtRef.current = Date.now();
    }
  }, [events.length, isStreaming, decision]);

  // Tick the clock while a stream is in flight. 500ms gives a smooth
  // second-by-second feel without burning render budget. Stopped as
  // soon as the decision lands (endedAt is set).
  useEffect(() => {
    if (!isStreaming) return;
    if (endedAtRef.current !== null) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [isStreaming]);

  if (events.length === 0) {
    return null;
  }

  // Compute the elapsed value for render. While the stream is live we
  // sample `now`; once the decision lands we freeze on endedAt.
  const elapsedMs =
    startedAtRef.current !== null
      ? (endedAtRef.current ?? now) - startedAtRef.current
      : null;

  // Live-status inputs. A full run is 8-15 min with 20-60s of silent LLM work
  // between agent turns, so we drive continuous motion (spinner + shimmer bar +
  // ticking clock) and a present-tense narration of the active phase so the run
  // never reads as stuck. Only while genuinely streaming toward a decision.
  const streaming = isStreaming && decision === null && terminal === null;
  const activePhase = progress.phases.find((p) => p.state === 'active')?.phase ?? null;
  const usingTools = events[events.length - 1]?.type === 'agent.activity';
  const liveNarration = usingTools
    ? t('debate.narration.tools')
    : activePhase
      ? isKnownPhase(activePhase)
        ? t(`debate.phaseActivity.${activePhase}` as const)
        : t('debate.narration.working')
      : t('debate.narration.starting');

  return (
    <section className={styles.stream}>
      {start && (
        <header className={styles.header}>
          <div className={styles.headerLabel}>{t('debate.session')}</div>
          <div className={styles.headerTitle}>
            <span className={styles.ticker}>{start.ticker}</span>
            <span className={styles.headerSep}>·</span>
            <span className={styles.tradeDate}>{start.trade_date}</span>
          </div>
          {isStreaming && (
            <div className={styles.streamingBadge}>
              <span className={styles.streamingDot} />
              {t('debate.badge')}
            </div>
          )}
        </header>
      )}

      {evidencePack && (
        <div className={styles.news} data-testid="evidence-pack">
          <div className={styles.newsHeader}>
            <span className={styles.newsLabel}>{t('evidence.title')}</span>
            <span className={styles.newsSource}>
              {t('evidence.snapshot', { id: evidencePack.snapshot_id.slice(0, 12), asOf: evidencePack.as_of.slice(0, 10) })}
            </span>
          </div>
          <ul className={styles.newsList}>
            {evidencePack.unknowns.map((u, i) => (
              <li key={i} className={styles.newsItem}>
                <span className={styles.newsMeta}>{t('evidence.unknown')} {u}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {dexter && (
        <div className={styles.news} data-testid="dexter-result" data-status={dexter.status}>
          <div className={styles.newsHeader}>
            <span className={styles.newsLabel}>{t('dexter.title')}</span>
            <span className={styles.newsSource}>
              {t(`dexter.status.${dexter.status}` as const)}
              {dexter.reason ? ` · ${dexter.reason}` : ''}
              {dexter.usage?.measured
                ? ` · ${t('dexter.tokens', { n: i18n.formatNumber(dexter.usage.total_tokens ?? 0) })}`
                : ''}
            </span>
          </div>
          <ul className={styles.newsList}>
            {dexter.findings.map((f) => (
              <li key={f.id} className={styles.newsItem} data-verification={f.verification}>
                <span className={styles.newsTitle}>
                  {f.verification === 'verified' ? '✓' : '✗'} {f.claim}
                </span>
                <span className={styles.newsMeta}>
                  {f.citations.map((c) => `[${c.kind} ${c.period}]`).join(' ')}
                  {f.verification === 'unverified' && f.reason ? ` · ${t('dexter.unverified')}: ${f.reason}` : ''}
                  {f.verification === 'verified' && f.point_in_time === 'unknown' ? ` · ${t('dexter.pitUnknown')}` : ''}
                </span>
              </li>
            ))}
            {dexter.findings.length === 0 && (
              <li className={styles.newsItem}>
                <span className={styles.newsMeta}>{t('dexter.noFindings')}</span>
              </li>
            )}
          </ul>
        </div>
      )}

      {notices.length > 0 && (
        <div className={styles.news} data-testid="run-notices">
          <ul className={styles.newsList}>
            {notices.map((n, i) => (
              <li key={i} className={styles.newsItem}>
                <span className={styles.newsMeta}>{n}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {researchNotesList.length > 0 && (
        <div className={styles.news} data-testid="research-notes">
          <ul className={styles.newsList}>
            {researchNotesList.map((n, i) => (
              <li key={i} className={styles.newsItem}>
                <span className={styles.newsMeta}>{n}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary && (
        <div className={styles.summaryStrip}>
          <div className={styles.summaryItem}>
            <span className={styles.summaryLabel}>{t('debate.summary.lastClose')}</span>
            <span className={styles.summaryValue}>{summary.last_close.toFixed(2)}</span>
          </div>
          <div className={styles.summaryItem}>
            <span className={styles.summaryLabel}>{t('debate.summary.periodChange')}</span>
            <span
              className={
                summary.period_change_pct >= 0
                  ? styles.summaryValuePositive
                  : styles.summaryValueNegative
              }
            >
              {summary.period_change_pct >= 0 ? '+' : ''}
              {summary.period_change_pct.toFixed(2)}%
            </span>
          </div>
          <div className={styles.summaryItem}>
            <span className={styles.summaryLabel}>{t('debate.summary.range')}</span>
            <span className={styles.summaryValue}>
              {t('debate.summary.rangeValue', {
                low: summary.period_low.toFixed(2),
                high: summary.period_high.toFixed(2),
              })}
            </span>
          </div>
          <div className={styles.summaryItem}>
            <span className={styles.summaryLabel}>{t('debate.summary.avgVolume')}</span>
            <span className={styles.summaryValue}>{formatVolume(summary.avg_volume)}</span>
          </div>
          <div className={styles.summarySource}>
            {t('debate.summary.source', {
              sessions: summary.sessions,
              source: summary.source,
              asOf: summary.as_of,
            })}
          </div>
        </div>
      )}

      {/* Progress strip — visible from the moment session.start lands.
          During the live stream it surfaces the 4-phase structure of the
          debate (Analysts → Researchers → Trader → Risk), telegraphs
          which agents are done / which phase is currently running, and
          ticks a live elapsed clock. After the decision lands, it
          freezes on the final state so the user can see "12 of 12
          agents · 1m 24s" alongside the conclusion. */}
      <div className={styles.progress} aria-label={t('debate.progress.aria')}>
        {streaming && (
          <div className={styles.liveStatus} aria-live="polite">
            <div className={styles.liveStatusRow}>
              <span className={styles.liveSpinner} aria-hidden="true" />
              <span className={styles.liveNarration}>{liveNarration}</span>
              <span className={styles.liveTiming}>
                {elapsedMs !== null && (
                  <span className={styles.liveElapsed}>{formatElapsed(elapsedMs, t)}</span>
                )}
                <span className={styles.liveHint}>{t('debate.typicalDuration')}</span>
              </span>
            </div>
            <div className={styles.liveBar} aria-hidden="true">
              <span className={styles.liveBarShimmer} />
            </div>
          </div>
        )}
        <div className={styles.progressPhases}>
          {progress.phases.map((p) => (
            <div
              key={p.phase}
              className={`${styles.progressPhase} ${
                styles[`progressPhase_${p.state}`] ?? ''
              }`}
              aria-current={p.state === 'active' ? 'step' : undefined}
            >
              <span className={styles.progressPhaseMark}>
                {p.state === 'done' ? '✓' : p.state === 'active' ? '●' : '○'}
              </span>
              <span className={styles.progressPhaseLabel}>
                {isKnownPhase(p.phase) ? t(`debate.phase.${p.phase}` as const) : p.phase}
              </span>
              <span className={styles.progressPhaseCount}>
                {p.done}/{p.total}
              </span>
            </div>
          ))}
        </div>
        <div className={styles.progressFooter}>
          <span>
            {t('debate.footer.agents', {
              done: progress.totalDone,
              total: progress.totalAgents,
            })}
          </span>
          {elapsedMs !== null && (
            <>
              <span className={styles.progressSep}>·</span>
              <span>
                {endedAtRef.current !== null
                  ? t('debate.footer.complete', { elapsed: formatElapsed(elapsedMs, t) })
                  : t('debate.footer.inProgress', { elapsed: formatElapsed(elapsedMs, t) })}
              </span>
            </>
          )}
        </div>
      </div>

      {news && news.headlines.length > 0 && (
        <div className={styles.news}>
          <div className={styles.newsHeader}>
            <span className={styles.newsLabel}>{t('debate.news')}</span>
            <span className={styles.newsSource}>
              {tp('debate.news.count', news.headlines.length, { source: news.source })}
            </span>
          </div>
          <ul className={styles.newsList}>
            {news.headlines.map((h: Headline, idx: number) => (
              <li key={`${h.url || h.title}-${idx}`} className={styles.newsItem}>
                {h.url ? (
                  <a
                    className={styles.newsTitle}
                    href={h.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {h.title}
                  </a>
                ) : (
                  <span className={styles.newsTitle}>{h.title}</span>
                )}
                <span className={styles.newsMeta}>
                  {[h.publisher, h.pub_date && formatRelativeTime(h.pub_date, i18n)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {groups.map((group, groupIdx) => (
        <div
          key={`${group.phase}-${groupIdx}`}
          className={`${styles.phase} ${styles[`phase_${group.phase}`] ?? ''}`}
        >
          <div className={styles.phaseHeader}>
            <span className={styles.phaseLabel}>
              {isKnownPhase(group.phase) ? t(`debate.phase.${group.phase}` as const) : group.phase}
            </span>
            <span className={styles.phaseDescription}>
              {isKnownPhase(group.phase) ? t(`debate.phaseDesc.${group.phase}` as const) : ''}
            </span>
          </div>
          <div className={styles.messages}>
            {group.messages.map((msg, idx) => (
              <article key={`${msg.agent}-${idx}`} className={styles.message}>
                <div className={styles.messageAgent} title={msg.agent}>
                  {isKnownAgent(msg.agent) ? t(`agent.${msg.agent}` as const) : msg.agent}
                  {msg.round != null && msg.round > 0 && (
                    <span className={styles.messageRound}>
                      {t('debate.round', { round: msg.round })}
                    </span>
                  )}
                </div>
                <div className={styles.messageContent}>
                  <Markdown>{msg.content}</Markdown>
                </div>
              </article>
            ))}
          </div>
        </div>
      ))}

      {decision && (
        <div
          className={`${styles.decision} ${styles[`decision_${stanceLean(decision.stance)}`] ?? ''}`}
          data-testid="decision-card"
          data-stance={normalizeStance(decision.stance)}
        >
          <div className={styles.decisionLabel}>
            {t('debate.decision.label')}
            {meta.live && (
              <span className={styles.decisionLiveBadge}>
                {t('debate.decision.live', {
                  provider: meta.provider ?? 'openai',
                  model: meta.model ?? '?',
                })}
                {' · '}
                <span data-testid="decision-duration">{formatRunDuration(meta.elapsedS, t)}</span>
              </span>
            )}
          </div>
          <div className={styles.decisionAction} data-testid="decision-stance">
            {stanceLabel(decision.stance, t)}
          </div>
          {isReview(decision.stance) && (
            <div className={styles.decisionConfidence} data-testid="decision-review-note">
              {t('debate.decision.reviewNote')}
            </div>
          )}
          {!isReview(decision.stance) && (
            <div className={styles.decisionConfidence}>
              {t('debate.decision.conviction')}{' '}
              <span className={styles.decisionConfidenceValue}>
                {(decision.conviction * 100).toFixed(0)}%
              </span>
            </div>
          )}
          {decision.bull_strength != null && decision.bear_strength != null && (
            <div className={styles.decisionStrengths}>
              <div className={styles.strengthRow}>
                <span className={styles.strengthName}>{t('debate.decision.bull')}</span>
                <div className={styles.strengthTrack}>
                  <div
                    className={styles.strengthFillBull}
                    style={{ width: `${decision.bull_strength}%` }}
                  />
                </div>
                <span className={styles.strengthValue}>
                  {decision.bull_strength}/100
                </span>
              </div>
              <div className={styles.strengthRow}>
                <span className={styles.strengthName}>{t('debate.decision.bear')}</span>
                <div className={styles.strengthTrack}>
                  <div
                    className={styles.strengthFillBear}
                    style={{ width: `${decision.bear_strength}%` }}
                  />
                </div>
                <span className={styles.strengthValue}>
                  {decision.bear_strength}/100
                </span>
              </div>
              {decision.risk_level != null && (
                <div className={styles.riskChip} data-risk={String(decision.risk_level)}>
                  {t('debate.decision.risk', { risk: riskLabel(decision.risk_level, t) })}
                </div>
              )}
            </div>
          )}
          <div className={styles.decisionReasoning}>
            {(decision.price_target != null || decision.time_horizon) && (
              <div className={styles.decisionTargets}>
                {decision.price_target != null && (
                  <span className={styles.decisionTarget}>
                    <span className={styles.decisionTargetLabel}>{t('debate.decision.scenario')}</span>
                    <span className={styles.decisionTargetValue}>
                      {decision.price_target.toFixed(2)}
                    </span>
                  </span>
                )}
                {decision.time_horizon && (
                  <span className={styles.decisionTarget}>
                    <span className={styles.decisionTargetLabel}>{t('debate.decision.horizon')}</span>
                    <span className={styles.decisionTargetValue}>
                      {decision.time_horizon}
                    </span>
                  </span>
                )}
              </div>
            )}
            <Markdown>{decision.reasoning}</Markdown>
          </div>
          <div className={styles.decisionOwnership}>
            {t('debate.decision.ownership')}
          </div>
          {meta.live && (meta.estimatedCostUsd !== undefined || meta.inputTokens !== undefined) && (
            <div className={styles.decisionUsage}>
              {meta.inputTokens !== undefined && meta.outputTokens !== undefined && (
                <span>
                  {t('debate.decision.tokens', {
                    input: i18n.formatNumber(meta.inputTokens),
                    output: i18n.formatNumber(meta.outputTokens),
                  })}
                </span>
              )}
              {meta.estimatedCostUsd === null && <span>{t('debate.decision.costUnknown')}</span>}
              {typeof meta.estimatedCostUsd === 'number' && (
                <span>
                  {t('debate.decision.cost', { cost: `$${meta.estimatedCostUsd.toFixed(4)}` })}
                </span>
              )}
            </div>
          )}
          <div className={styles.decisionDisclaimer}>
            {t('debate.decision.disclaimer')}
          </div>
        </div>
      )}

      {terminal && (
        <div
          className={`${styles.decision} ${
            terminal.kind === 'token_cap' ? styles.terminalStop : styles.terminalError
          }`}
          data-testid="terminal-notice"
          data-terminal={terminal.kind}
        >
          <div className={styles.decisionLabel}>
            {terminal.kind === 'token_cap' ? t('debate.terminal.stopped') : t('debate.terminal.failed')}
          </div>
          <div className={styles.decisionReasoning}>
            {terminal.kind === 'token_cap' ? (
              terminal.cap != null ? (
                t('debate.terminal.capWithCap', {
                  cap: i18n.formatNumber(terminal.cap),
                  used: i18n.formatNumber(terminal.used ?? 0),
                })
              ) : (
                t('debate.terminal.cap', { used: i18n.formatNumber(terminal.used ?? 0) })
              )
            ) : (
              (() => {
                const e = localizeEngineError(terminal.error ?? '', i18n);
                return e.localized ? (
                  <>
                    {e.text}
                    <br />
                    <small>{t('err.original', { text: e.original })}</small>
                  </>
                ) : (
                  e.text
                );
              })()
            )}
          </div>
          <div className={styles.decisionDisclaimer}>
            {terminal.kind === 'token_cap'
              ? t('debate.terminal.capHint')
              : t('debate.terminal.errorHint')}
          </div>
        </div>
      )}

      {webhookReport && webhookReport.results.length > 0 && (
        <div className={styles.webhooks} data-testid="webhook-report">
          <div className={styles.webhooksHeader}>
            <span className={styles.webhooksLabel}>{t('debate.webhooks')}</span>
            <span className={styles.webhooksSummary}>
              {(() => {
                const fired = webhookReport.results.filter((r) => r.status === 'fired').length;
                const filtered = webhookReport.results.filter((r) => r.status === 'filtered').length;
                const failed = webhookReport.results.filter((r) => r.status === 'failed').length;
                const parts: string[] = [];
                if (fired) parts.push(t('debate.webhooks.fired', { n: fired }));
                if (filtered) parts.push(t('debate.webhooks.filtered', { n: filtered }));
                if (failed) parts.push(t('debate.webhooks.failed', { n: failed }));
                return parts.join(' · ') || t('debate.webhooks.none');
              })()}
            </span>
          </div>
          <ul className={styles.webhooksList}>
            {webhookReport.results.map((r) => (
              <li
                key={r.id}
                className={`${styles.webhookItem} ${
                  styles[`webhookItem_${r.status}`] ?? ''
                }`}
              >
                <span className={styles.webhookStatus}>
                  {r.status === 'fired' ? '✓' : r.status === 'filtered' ? '○' : '✗'}
                </span>
                <span className={styles.webhookName}>{r.name}</span>
                <span className={styles.webhookDetail}>
                  {r.status === 'filtered' && t('debate.webhooks.filterMiss')}
                  {r.status === 'fired' &&
                    r.http_status &&
                    t('debate.webhooks.http', { status: r.http_status })}
                  {r.status === 'failed' &&
                    (r.error ||
                      (r.http_status
                        ? t('debate.webhooks.http', { status: r.http_status })
                        : t('debate.webhooks.failedShort')))}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export default DebateStream;
