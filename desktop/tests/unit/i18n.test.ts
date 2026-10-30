import { describe, expect, it } from 'vitest';
import {
  DEFAULT_LOCALE,
  LOCALES,
  interpolate,
  isLocale,
  placeholders,
  splitRich,
  translate,
  translatePlural,
  type Catalog,
} from '../../src/i18n/core';
import { LOCALE_STORAGE_KEY, readStoredLocale, writeStoredLocale } from '../../src/i18n/storage';
import { catalogFor, en, makeTranslator, zhTW } from '../../src/i18n/messages';
import { localizeEngineError } from '../../src/lib/engine-errors';

const enKeys = Object.keys(en).sort();

describe('dictionary integrity', () => {
  it('zh-TW has exactly the same keys as English (no missing, no extra)', () => {
    expect(Object.keys(zhTW).sort()).toEqual(enKeys);
  });

  it('no empty messages in either locale', () => {
    for (const k of enKeys) {
      expect((en as Record<string, string>)[k].trim(), `en ${k}`).not.toBe('');
      expect((zhTW as Record<string, string>)[k].trim(), `zh-TW ${k}`).not.toBe('');
    }
  });

  it('interpolation placeholders match between locales for every key', () => {
    const bad: string[] = [];
    for (const k of enKeys) {
      const a = placeholders((en as Record<string, string>)[k]);
      const b = placeholders((zhTW as Record<string, string>)[k]);
      if (a.join(',') !== b.join(',')) bad.push(`${k}: en{${a}} vs zh{${b}}`);
    }
    expect(bad).toEqual([]);
  });

  it('rich-text tags (<b>, <i>, <code>) are balanced identically in both locales', () => {
    const tagCount = (s: string) =>
      ['b', 'i', 'code'].map((t) => (s.match(new RegExp(`<${t}>`, 'g')) ?? []).length + ':' + (s.match(new RegExp(`</${t}>`, 'g')) ?? []).length);
    const bad: string[] = [];
    for (const k of enKeys) {
      const a = tagCount((en as Record<string, string>)[k]).join('|');
      const b = tagCount((zhTW as Record<string, string>)[k]).join('|');
      if (a !== b) bad.push(k);
    }
    expect(bad).toEqual([]);
  });

  it('plural keys come in .one/.other pairs', () => {
    for (const k of enKeys.filter((x) => x.endsWith('.one'))) {
      expect(enKeys).toContain(k.replace(/\.one$/, '.other'));
    }
  });

  it('zh-TW text contains no common Simplified-only characters (no silent mixing)', () => {
    // Forms that exist only in Simplified Chinese (their Traditional counterparts differ).
    const simplifiedOnly = '设帐户网络输项确误应数据资载关闭历时开记联认证钥规则为这个们来发务实现过还进选择测试说语处统计单击点连线动态问题';
    const offenders: string[] = [];
    for (const [k, v] of Object.entries(zhTW as Record<string, string>)) {
      for (const ch of simplifiedOnly) {
        if (v.includes(ch)) offenders.push(`${k}: ${ch}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('protected terms survive translation: provider brands, ticker and model-ish literals', () => {
    const t = makeTranslator('zh-TW');
    expect(t('settings.row.openai.note')).toContain('GPT-5');
    expect(t('settings.row.xai.note')).toContain('console.x.ai');
    expect(t('settings.tab.llm.desc')).toContain('OpenAI');
    expect(t('settings.tab.llm.desc')).toContain('Ollama');
    expect(t('footer.version', { version: '1.1.5' })).toBe('Trading Agents Lab Pro v1.1.5 · AGPL-3.0');
  });
});

describe('core behaviour', () => {
  const cat: Catalog = {
    locale: 'zh-TW',
    active: { a: '你好 {name}', 'n.one': '{count} 個', 'n.other': '{count} 個', big: '{v:n} 筆' },
    fallback: { a: 'Hello {name}', only: 'English only', 'n.one': '{count} item', 'n.other': '{count} items' },
  };

  it('defaults to Traditional Chinese and lists both locales', () => {
    expect(DEFAULT_LOCALE).toBe('zh-TW');
    expect(LOCALES).toEqual(['zh-TW', 'en']);
    expect(isLocale('en')).toBe(true);
    expect(isLocale('zh-CN')).toBe(false);
  });

  it('interpolates, leaves unknown placeholders intact, and formats numbers', () => {
    expect(translate(cat, 'a', { name: 'Amy' })).toBe('你好 Amy');
    expect(interpolate('x {missing} y', { other: 1 }, 'en')).toBe('x {missing} y');
    expect(translate(cat, 'big', { v: 1234567 })).toBe('1,234,567 筆');
  });

  it('falls back to English for a missing key, then to the key itself', () => {
    expect(translate(cat, 'only')).toBe('English only');
    expect(translate(cat, 'nope')).toBe('nope');
  });

  it('selects plural forms per locale', () => {
    const enCat: Catalog = { ...cat, locale: 'en', active: cat.fallback };
    expect(translatePlural(enCat, 'n', 1)).toBe('1 item');
    expect(translatePlural(enCat, 'n', 3)).toBe('3 items');
    expect(translatePlural(cat, 'n', 3)).toBe('3 個');
  });

  it('splitRich only recognises b/i/code and never markup from variables', () => {
    expect(splitRich('a <b>bold</b> and <code>x</code> <hex>')).toEqual([
      { tag: null, text: 'a ' },
      { tag: 'b', text: 'bold' },
      { tag: null, text: ' and ' },
      { tag: 'code', text: 'x' },
      { tag: null, text: ' <hex>' },
    ]);
  });
});

describe('locale persistence', () => {
  const fake = () => {
    const m = new Map<string, string>();
    return {
      getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
      setItem: (k: string, v: string) => void m.set(k, v),
      raw: m,
    };
  };

  it('defaults to zh-TW when nothing is stored or the value is invalid', () => {
    const s = fake();
    expect(readStoredLocale(s)).toBe('zh-TW');
    s.setItem(LOCALE_STORAGE_KEY, 'fr');
    expect(readStoredLocale(s)).toBe('zh-TW');
  });

  it('round-trips a chosen locale', () => {
    const s = fake();
    writeStoredLocale('en', s);
    expect(s.raw.get(LOCALE_STORAGE_KEY)).toBe('en');
    expect(readStoredLocale(s)).toBe('en');
  });

  it('never throws when storage is unavailable', () => {
    const boom = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readStoredLocale(boom)).toBe('zh-TW');
    expect(() => writeStoredLocale('en', boom)).not.toThrow();
  });
});

describe('native menu + dialog strings (main process)', () => {
  it('translates menu labels and keeps the app name variable', () => {
    const zh = makeTranslator('zh-TW');
    const enT = makeTranslator('en');
    expect(zh('menu.file')).toBe('檔案');
    expect(enT('menu.file')).toBe('File');
    expect(zh('menu.quitApp', { app: 'Trading Agents Lab Pro' })).toContain('Trading Agents Lab Pro');
    expect(zh('dialog.shutdown.title')).toContain('Trading Agents Lab Pro');
  });

  it('catalogFor exposes English as fallback', () => {
    expect(catalogFor('zh-TW').fallback).toBe(catalogFor('en').active);
  });
});

describe('engine error mapping keeps the original text', () => {
  const zh = { t: makeTranslator('zh-TW'), locale: 'zh-TW' as const };

  it('maps the Pro local-auth rejection', () => {
    const raw = 'Pro engine requires an API key or OpenAI OAuth; local auth is not supported for the full-graph analysis.';
    const r = localizeEngineError(raw, zh);
    expect(r.localized).toBe(true);
    expect(r.original).toBe(raw);
    expect(r.text).toContain('local');
  });

  it('maps provider exception class names and HTTP failures', () => {
    expect(localizeEngineError('AuthenticationError: bad key', zh).localized).toBe(true);
    expect(localizeEngineError('listSessions failed: 500 Internal Server Error', zh).text).toContain('500');
  });

  it('passes unknown errors through unchanged', () => {
    const r = localizeEngineError('some brand new failure', zh);
    expect(r).toEqual({ text: 'some brand new failure', original: 'some brand new failure', localized: false });
  });
});
