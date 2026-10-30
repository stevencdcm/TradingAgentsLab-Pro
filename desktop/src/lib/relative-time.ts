import type { I18n } from '../i18n';

/** "just now / 5m ago / 3h ago / 2d ago", then a locale date. Empty for unparseable input unless `fallback` given. */
export function formatRelativeTime(iso: string, i18n: I18n, fallback = ''): string {
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return fallback;
  const diffSec = Math.round((Date.now() - ts) / 1000);
  const { t } = i18n;
  if (diffSec < 60) return t('debate.time.justNow');
  if (diffSec < 3600) return t('debate.time.minutesAgo', { n: Math.round(diffSec / 60) });
  if (diffSec < 86400) return t('debate.time.hoursAgo', { n: Math.round(diffSec / 3600) });
  if (diffSec < 86400 * 7) return t('debate.time.daysAgo', { n: Math.round(diffSec / 86400) });
  return i18n.formatDateTime(iso, { dateStyle: 'medium' });
}
