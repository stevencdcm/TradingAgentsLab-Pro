/**
 * Override modal for CostGuard.
 *
 * Shown when a debate would exceed a configured cap and the user can opt
 * to override for this single session. Anti-tamper: the Override button
 * is disabled for 3 seconds after the modal opens (countdown visible),
 * preventing rage-click bypass. No "remember this for the day" option —
 * each over-cap session needs a fresh click.
 *
 * Cancel is always enabled. Pressing Escape cancels.
 */

import { useEffect, useState } from 'react';

import type { CostGuardConfig, SpendState } from '../lib/cost-guard';
import { useI18n, type I18n } from '../i18n';
import styles from './CostGuardModal.module.css';

const COUNTDOWN_SECONDS = 3;

export type OverDimension = 'daily' | 'weekly' | 'monthly' | 'rate' | 'unknown_price';

export interface CostGuardModalProps {
  /** Which cap fired. */
  overDimension: OverDimension;
  /** Current spend state at the time the cap was hit. */
  spend: SpendState;
  /** Active config — used to format cap labels. */
  config: CostGuardConfig;
  /** Worst-case estimated cost of the requested session. */
  estCostUsd: number;
  /** Called when the user clicks "Override and continue" (after countdown). */
  onConfirm: () => void;
  /** Called when the user clicks "Cancel" or presses Escape. */
  onCancel: () => void;
}

export function CostGuardModal({
  overDimension,
  spend,
  config,
  estCostUsd,
  onConfirm,
  onCancel,
}: CostGuardModalProps): JSX.Element {
  const i18n = useI18n();
  const { t: tr, rich } = i18n;
  const [secondsLeft, setSecondsLeft] = useState(COUNTDOWN_SECONDS);

  useEffect(() => {
    if (secondsLeft <= 0) return;
    const timer = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [secondsLeft]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const overrideReady = secondsLeft <= 0;

  return (
    <div className={styles.scrim} role="dialog" aria-modal="true" aria-labelledby="cg-modal-title">
      <div className={styles.modal}>
        <h2 id="cg-modal-title" className={styles.title}>
          {overDimension === 'rate' ? tr('costGuard.title.rate') : overDimension === 'unknown_price' ? tr('costGuard.title.unknownPrice') : tr('costGuard.title.budget')}
        </h2>

        <p className={styles.body}>{describePrimary(overDimension, spend, config, estCostUsd, tr)}</p>

        <div className={styles.capList}>
          <CapRow
            label={tr('costGuard.dim.daily')}
            current={spend.daily_usd}
            cap={config.cap_daily_usd}
            highlight={overDimension === 'daily'}
          />
          <CapRow
            label={tr('costGuard.dim.weekly')}
            current={spend.weekly_usd}
            cap={config.cap_weekly_usd}
            highlight={overDimension === 'weekly'}
          />
          <CapRow
            label={tr('costGuard.dim.monthly')}
            current={spend.monthly_usd}
            cap={config.cap_monthly_usd}
            highlight={overDimension === 'monthly'}
          />
          {config.cap_sessions_per_day > 0 && (
            <SessionCapRow
              label={tr('costGuard.dim.sessions')}
              count={spend.sessions_today}
              cap={config.cap_sessions_per_day}
              highlight={overDimension === 'rate'}
            />
          )}
        </div>

        <div className={styles.buttons}>
          <button
            type="button"
            className={styles.cancel}
            onClick={onCancel}
            autoFocus
          >
            {tr('costGuard.cancel')}
          </button>
          <button
            type="button"
            className={styles.override}
            onClick={onConfirm}
            disabled={!overrideReady}
            aria-disabled={!overrideReady}
          >
            {overrideReady
              ? tr('costGuard.override')
              : tr('costGuard.overrideWait', { seconds: secondsLeft })}
          </button>
        </div>

        <p className={styles.hint}>{rich('costGuard.hint')}</p>
      </div>
    </div>
  );
}

// ---- Helpers ----------------------------------------------------------------

function describePrimary(
  dim: OverDimension,
  spend: SpendState,
  config: CostGuardConfig,
  est: number,
  t: I18n['t'],
): string {
  if (dim === 'unknown_price') return t('costGuard.body.unknownPrice');
  if (dim === 'rate') {
    return t('costGuard.body.rate', {
      used: spend.sessions_today,
      cap: config.cap_sessions_per_day,
    });
  }
  const current =
    dim === 'daily' ? spend.daily_usd : dim === 'weekly' ? spend.weekly_usd : spend.monthly_usd;
  const cap =
    dim === 'daily'
      ? config.cap_daily_usd
      : dim === 'weekly'
        ? config.cap_weekly_usd
        : config.cap_monthly_usd;
  return t('costGuard.body.budget', {
    current: formatUsd(current),
    cap: formatUsd(cap),
    dimension: t(`costGuard.dimWord.${dim}` as const),
    estimate: formatUsd(est),
  });
}

function CapRow({
  label,
  current,
  cap,
  highlight,
}: {
  label: string;
  current: number;
  cap: number;
  highlight: boolean;
}): JSX.Element {
  if (cap <= 0) return <></>; // disabled cap — don't show
  const pct = Math.min(100, (current / cap) * 100);
  const overPct = pct >= 100;
  return (
    <div className={`${styles.capRow} ${highlight ? styles.capRowHighlight : ''}`}>
      <span className={styles.capLabel}>{label}</span>
      <div className={styles.capProgress}>
        <div
          className={`${styles.capBar} ${overPct ? styles.capBarOver : pct >= 80 ? styles.capBarWarn : ''}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={styles.capValue}>
        {formatUsd(current)} / {formatUsd(cap)}
      </span>
    </div>
  );
}

function SessionCapRow({
  label,
  count,
  cap,
  highlight,
}: {
  label: string;
  count: number;
  cap: number;
  highlight: boolean;
}): JSX.Element {
  const pct = Math.min(100, (count / cap) * 100);
  const overPct = pct >= 100;
  return (
    <div className={`${styles.capRow} ${highlight ? styles.capRowHighlight : ''}`}>
      <span className={styles.capLabel}>{label}</span>
      <div className={styles.capProgress}>
        <div
          className={`${styles.capBar} ${overPct ? styles.capBarOver : pct >= 80 ? styles.capBarWarn : ''}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={styles.capValue}>
        {count} / {cap}
      </span>
    </div>
  );
}

function formatUsd(value: number): string {
  if (value >= 100) return `$${value.toFixed(0)}`;
  if (value >= 10) return `$${value.toFixed(2)}`;
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(4)}`;
}
