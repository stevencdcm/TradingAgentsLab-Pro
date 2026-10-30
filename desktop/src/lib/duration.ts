/** "1 min 25 s" / "1 h 02 min" for a whole-run duration. null/negative/NaN = unknown (never guessed). */
type T = (key: any, vars?: Record<string, string | number>) => string;

export function formatRunDuration(seconds: number | null | undefined, t: T): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return t('duration.unknown');
  const total = Math.round(seconds);
  if (total < 60) return t('duration.badge', { v: t('duration.s', { s: total }) });
  const m = Math.floor(total / 60);
  if (m < 60) return t('duration.badge', { v: t('duration.ms', { m, s: total % 60 }) });
  return t('duration.badge', { v: t('duration.hm', { h: Math.floor(m / 60), m: m % 60 }) });
}
