// Renderer-side mirror of engine/stance.py: the stance vocabulary, human
// labels, and the mapping of legacy BUY/SELL/HOLD values (sessions persisted
// before the stance relabel) into the new model. Keep the two in sync.
import type { Stance } from './engine-client';
import type { MessageKey } from '../i18n/messages';

/** Optional translator; omitted => English (keeps pure callers and tests unchanged). */
type Tr = (key: MessageKey) => string;

export const STANCES: Stance[] = [
  'bullish',
  'moderately_bullish',
  'neutral',
  'moderately_bearish',
  'bearish',
];

const DISPLAY: Record<Stance, string> = {
  bullish: 'Bullish',
  moderately_bullish: 'Moderately bullish',
  neutral: 'Neutral',
  moderately_bearish: 'Moderately bearish',
  bearish: 'Bearish',
};

const LEGACY: Record<string, Stance> = {
  BUY: 'bullish',
  SELL: 'bearish',
  HOLD: 'neutral',
};

/** Coerce a stored or wire value (including legacy BUY/SELL/HOLD) to a
 * canonical stance. Unknown values read as neutral. */
export function normalizeStance(value: unknown): Stance {
  const raw = String(value ?? '').trim();
  if ((STANCES as string[]).includes(raw.toLowerCase())) {
    return raw.toLowerCase() as Stance;
  }
  return LEGACY[raw.toUpperCase()] ?? 'neutral';
}

/** Human-readable label ("Moderately bullish") for any stance or legacy
 * action value. */
export function stanceLabel(value: unknown, t?: Tr): string {
  const stance = normalizeStance(value);
  return t ? t(`stance.${stance}` as MessageKey) : DISPLAY[stance];
}

/** Bucket for styling and summary counts: which way the stance leans. */
export function stanceLean(value: unknown): 'bullish' | 'neutral' | 'bearish' {
  const s = normalizeStance(value);
  if (s === 'bullish' || s === 'moderately_bullish') return 'bullish';
  if (s === 'bearish' || s === 'moderately_bearish') return 'bearish';
  return 'neutral';
}

/** Risk level label with a neutral fallback. */
export function riskLabel(value: unknown, t?: Tr): string {
  const raw = String(value ?? '').trim().toLowerCase();
  const level = raw === 'low' ? 'low' : raw === 'elevated' ? 'elevated' : 'moderate';
  if (t) return t(`risk.${level}` as MessageKey);
  return level === 'low' ? 'Low' : level === 'elevated' ? 'Elevated' : 'Moderate';
}
