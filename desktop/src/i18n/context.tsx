import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import {
  DEFAULT_LOCALE,
  htmlLang,
  interpolate,
  splitRich,
  translate,
  translatePlural,
  type Catalog,
  type Locale,
  type Vars,
} from './core';
import { catalogFor, type MessageKey } from './messages';
import { readStoredLocale, writeStoredLocale } from './storage';

export interface I18n {
  locale: Locale;
  setLocale: (next: Locale) => void;
  t: (key: MessageKey, vars?: Vars) => string;
  /** Like `t`, but renders `<b>`/`<i>`/`<code>` runs as <strong>/<em>/<code>. */
  rich: (key: MessageKey, vars?: Vars, codeClassName?: string) => ReactNode;
  /** Plural-aware: `key` must have `.one` and `.other` siblings. */
  tp: (key: string, count: number, vars?: Vars) => string;
  /** Lookup by a runtime string (e.g. a server error code). Undefined if unknown. */
  tryT: (key: string, vars?: Vars) => string | undefined;
  formatNumber: (n: number, opts?: Intl.NumberFormatOptions) => string;
  formatDateTime: (value: string | number | Date, opts?: Intl.DateTimeFormatOptions) => string;
}

function build(locale: Locale, setLocale: (l: Locale) => void): I18n {
  const catalog: Catalog = catalogFor(locale);
  const tag = htmlLang(locale);
  return {
    locale,
    setLocale,
    t: (key, vars) => translate(catalog, key, vars),
    rich: (key, vars, codeClassName) =>
      splitRich(catalog.active[key] ?? catalog.fallback[key] ?? key).map((part, i) => {
        const text = interpolate(part.text, vars, locale);
        if (part.tag === 'b') return <strong key={i}>{text}</strong>;
        if (part.tag === 'i') return <em key={i}>{text}</em>;
        if (part.tag === 'code') return <code key={i} className={codeClassName}>{text}</code>;
        return <Fragment key={i}>{text}</Fragment>;
      }),
    tp: (key, count, vars) => translatePlural(catalog, key, count, vars),
    tryT: (key, vars) =>
      catalog.active[key] !== undefined || catalog.fallback[key] !== undefined
        ? translate(catalog, key, vars)
        : undefined,
    formatNumber: (n, opts) => new Intl.NumberFormat(tag, opts).format(n),
    formatDateTime: (value, opts) => {
      const d = value instanceof Date ? value : new Date(value);
      if (Number.isNaN(d.getTime())) return String(value);
      return new Intl.DateTimeFormat(tag, opts ?? { dateStyle: 'medium', timeStyle: 'short' }).format(d);
    },
  };
}

/**
 * Outside a provider (unit tests, isolated components) the hook resolves to
 * English so existing component tests keep asserting the source strings. The
 * real app always mounts <I18nProvider> at the root (main.tsx).
 */
const FALLBACK: I18n = build('en', () => undefined);

export const I18nContext = createContext<I18n>(FALLBACK);

export function I18nProvider({
  children,
  initialLocale,
}: {
  children: ReactNode;
  /** Test hook; production reads the persisted preference. */
  initialLocale?: Locale;
}) {
  const [locale, setLocaleState] = useState<Locale>(() => initialLocale ?? readStoredLocale());

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    writeStoredLocale(next);
  }, []);

  // <html lang> follows the locale; the native menu (main process) is told
  // over IPC. The payload is only the locale code — no secrets.
  useEffect(() => {
    document.documentElement.lang = htmlLang(locale);
    try {
      void window.tradingAgentsLab?.setLocale?.(locale);
    } catch {
      // Browser preview / tests: no bridge.
    }
  }, [locale]);

  const value = useMemo(() => build(locale, setLocale), [locale, setLocale]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  return useContext(I18nContext);
}

/** Shorthand for components that only need `t`. */
export function useT(): I18n['t'] {
  return useContext(I18nContext).t;
}

export { DEFAULT_LOCALE };
