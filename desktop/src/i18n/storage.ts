import { DEFAULT_LOCALE, isLocale, type Locale } from './core';

/** Renderer-only persistence (kept out of core.ts so the Electron main
 * process, which has no DOM lib, can import core without type errors). */
export const LOCALE_STORAGE_KEY = 'tal.locale';

/** Read the saved locale; fall back to the default. Never throws. */
export function readStoredLocale(storage?: Pick<Storage, 'getItem'> | null): Locale {
  try {
    const s = storage === undefined ? globalThis.localStorage : storage;
    const raw = s?.getItem(LOCALE_STORAGE_KEY);
    return isLocale(raw) ? raw : DEFAULT_LOCALE;
  } catch {
    return DEFAULT_LOCALE;
  }
}

export function writeStoredLocale(
  locale: Locale,
  storage?: Pick<Storage, 'setItem'> | null,
): void {
  try {
    const s = storage === undefined ? globalThis.localStorage : storage;
    s?.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Best-effort: a failed write only means the choice is not remembered.
  }
}
