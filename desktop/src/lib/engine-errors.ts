/**
 * Map recognisable engine / provider error strings to localised text.
 *
 * The engine emits plain strings (no error codes), so we match the stable
 * prefixes / exception class names it produces. The ORIGINAL text is never
 * altered or discarded: callers get `text` (localised) and `original`, and
 * the UI shows the original alongside so support can still search for it.
 * Unrecognised errors come back unchanged (`localized === false`).
 */
import type { I18n } from '../i18n';

export interface LocalizedError {
  text: string;
  original: string;
  localized: boolean;
}

const KNOWN_EXCEPTIONS = [
  'AuthenticationError',
  'PermissionDeniedError',
  'RateLimitError',
  'NotFoundError',
  'BadRequestError',
  'APIConnectionError',
  'APITimeoutError',
  'TokenCapExceeded',
] as const;

export function localizeEngineError(raw: string, i18n: Pick<I18n, 't' | 'locale'>): LocalizedError {
  const original = String(raw ?? '');
  const { t } = i18n;
  const done = (text: string): LocalizedError => ({ text, original, localized: true });

  let m = /^Pro engine requires an API key or OpenAI OAuth; (\S+) auth is not supported/.exec(original);
  if (m) return done(t('err.proAuthUnsupported', { auth: m[1] }));
  m = /^Invalid Pro configuration: (.*)$/s.exec(original);
  if (m) return done(t('err.proConfigInvalid', { detail: m[1] }));
  m = /^Pro engine unavailable: .*\((\w+)\)\.?$/s.exec(original);
  if (m) return done(t('err.proUnavailable', { type: m[1] }));
  if (original === 'cost_guard_blocked') return done(t('err.costGuardBlocked'));
  if (original === 'invalid provider_config') return done(t('err.invalidProviderConfig'));
  if (original.startsWith('OAuth credentials cannot be tested here')) return done(t('err.oauthNotTestable'));
  if (original === 'model discovery is only for OpenAI OAuth') return done(t('err.modelDiscoveryOauthOnly'));

  m = /^(\w+) failed: (\d{3})(?: (.*))?$/s.exec(original);
  if (m) {
    return done(t('err.httpFailed', { op: m[1], status: m[2], text: m[3] ? ` ${m[3]}` : '' }));
  }
  m = /^engine request (\S+) timed out after (\d+)s$/.exec(original);
  if (m) return done(t('err.requestTimeout', { path: m[1], seconds: m[2] }));
  m = /^\/health failed: (\d{3})/.exec(original);
  if (m) return done(t('err.healthFailed', { status: m[1] }));
  m = /^Invalid ticker: (.*)$/s.exec(original);
  if (m) return done(t('err.invalidTicker', { ticker: m[1] }));
  m = /^(.*) is already on the watchlist$/s.exec(original);
  if (m) return done(t('err.alreadyOnWatchlist', { ticker: m[1] }));
  m = /^Test timed out after (\d+)s$/.exec(original);
  if (m) return done(t('err.testTimeout', { seconds: m[1] }));
  m = /^stream closed with code (\d+)(?:: (.*))?$/s.exec(original);
  if (m) return done(t('err.streamClosed', { code: m[1], reason: m[2] ? `: ${m[2]}` : '' }));
  if (original === 'not signed in to ChatGPT') return done(t('err.notSignedIn'));
  if (original === 'model catalog unavailable') return done(t('err.catalogUnavailable'));
  m = /^Fetch from upstream failed: (.*)$/s.exec(original);
  if (m) return done(t('err.upstreamFetch', { detail: m[1] }));
  m = /^Git comparison failed: (.*)$/s.exec(original);
  if (m) return done(t('err.gitCompare', { detail: m[1] }));
  m = /^(engine|oauth|secrets|upstream-check) bridge not available: preload not loaded$/.exec(original);
  if (m) return done(t('err.bridgeMissing', { bridge: m[1] }));

  for (const name of KNOWN_EXCEPTIONS) {
    if (original.startsWith(`${name}:`) || original === name) {
      return done(t(`err.${name}` as const));
    }
  }
  return { text: original, original, localized: false };
}

/** "localised (原文：…)" in one string, for places that only have room for text. */
export function describeEngineError(raw: string, i18n: Pick<I18n, 't' | 'locale'>): string {
  const e = localizeEngineError(raw, i18n);
  if (!e.localized) return e.text;
  return `${e.text} (${i18n.t('err.original', { text: e.original })})`;
}
