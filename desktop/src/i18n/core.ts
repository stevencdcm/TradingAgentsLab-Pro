/**
 * i18n core — pure functions only (no React, no Electron), so the renderer,
 * the Electron main process (native menu / dialogs) and unit tests all share
 * one implementation.
 *
 * Design:
 *  - English is the source of truth for message KEYS. Every locale module is
 *    typed `Record<keyof typeof en…, string>`, so a missing key in zh-TW is a
 *    compile error, not a runtime surprise.
 *  - Messages are plain strings with `{name}` placeholders. `{name:n}` formats
 *    a number with the locale's digit grouping. No DOM text replacement.
 *  - Plurals use sibling keys `<key>.one` / `<key>.other`, picked with
 *    `Intl.PluralRules` (zh-TW only ever selects `other`, but both keys exist
 *    so the type check stays uniform).
 *  - A missing key at runtime falls back to English, then to the key itself —
 *    never to a half-translated or mixed-script string.
 */

export type Locale = 'zh-TW' | 'en';

export const LOCALES: readonly Locale[] = ['zh-TW', 'en'];

/** Traditional Chinese is the default for this build; English is one click away. */
export const DEFAULT_LOCALE: Locale = 'zh-TW';

export type Vars = Record<string, string | number>;

export type Messages = Record<string, string>;

export function isLocale(value: unknown): value is Locale {
  return value === 'zh-TW' || value === 'en';
}

/** BCP-47 tag for `<html lang>` and Intl APIs. */
export function htmlLang(locale: Locale): string {
  return locale === 'zh-TW' ? 'zh-Hant-TW' : 'en';
}

const PLACEHOLDER = /\{([A-Za-z0-9_]+)(?::(n))?\}/g;

/** Substitute `{name}` / `{name:n}` placeholders. Unknown names are left intact. */
export function interpolate(template: string, vars: Vars | undefined, locale: Locale): string {
  if (!vars) return template;
  return template.replace(PLACEHOLDER, (whole, name: string, fmt?: string) => {
    if (!(name in vars)) return whole;
    const value = vars[name];
    if (fmt === 'n' && typeof value === 'number') {
      return new Intl.NumberFormat(htmlLang(locale)).format(value);
    }
    return String(value);
  });
}

/** Placeholder names used by a template, sorted + de-duplicated (for tests). */
export function placeholders(template: string): string[] {
  const names = new Set<string>();
  for (const m of template.matchAll(PLACEHOLDER)) names.add(m[1]);
  return [...names].sort();
}

export interface Catalog {
  /** Messages for the active locale. */
  active: Messages;
  /** English fallback. */
  fallback: Messages;
  locale: Locale;
}

export function translate(catalog: Catalog, key: string, vars?: Vars): string {
  const template = catalog.active[key] ?? catalog.fallback[key] ?? key;
  return interpolate(template, vars, catalog.locale);
}

/** Plural-aware lookup: picks `<key>.one` / `<key>.other` by `count`. */
export function translatePlural(
  catalog: Catalog,
  key: string,
  count: number,
  vars?: Vars,
): string {
  const category = new Intl.PluralRules(htmlLang(catalog.locale)).select(count);
  const chosen =
    catalog.active[`${key}.${category}`] !== undefined || catalog.fallback[`${key}.${category}`] !== undefined
      ? `${key}.${category}`
      : `${key}.other`;
  return translate(catalog, chosen, { count, ...vars });
}

export function hasKey(catalog: Catalog, key: string): boolean {
  return catalog.active[key] !== undefined || catalog.fallback[key] !== undefined;
}

export type RichTag = 'b' | 'i' | 'code';

export interface RichPart {
  tag: RichTag | null;
  text: string;
}

/**
 * Split a message on `<b>…</b>`, `<i>…</i>` and `<code>…</code>` markers so
 * React can render them as <strong>/<em>/<code>. No nesting, no attributes —
 * messages are our own strings, never user or server content (variables are
 * interpolated AFTER splitting, so they can never inject markup).
 */
export function splitRich(text: string): RichPart[] {
  const out: RichPart[] = [];
  const re = /<(b|i|code)>(.*?)<\/\1>/gs;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push({ tag: null, text: text.slice(last, idx) });
    out.push({ tag: m[1] as RichTag, text: m[2] });
    last = idx + m[0].length;
  }
  if (last < text.length) out.push({ tag: null, text: text.slice(last) });
  return out;
}
