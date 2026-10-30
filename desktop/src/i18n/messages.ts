import { translate, type Catalog, type Locale, type Vars } from './core';
import { shell } from './en/shell';
import { shellZhTW } from './zh-TW/shell';
import { native } from './en/native';
import { analyze } from './en/analyze';
import { debate } from './en/debate';
import { errors } from './en/errors';
import { dialogs } from './en/dialogs';
import { pages } from './en/pages';
import { batch } from './en/batch';
import { history } from './en/history';
import { settingsA } from './en/settingsA';
import { settingsB } from './en/settingsB';
import { settingsC } from './en/settingsC';
import { exportMsgs } from './en/export';
import { modelNotes } from './en/modelNotes';
import { modelNotesZhTW } from './zh-TW/modelNotes';
import { exportZhTW } from './zh-TW/export';
import { settingsCZhTW } from './zh-TW/settingsC';
import { settingsBZhTW } from './zh-TW/settingsB';
import { settingsAZhTW } from './zh-TW/settingsA';
import { historyZhTW } from './zh-TW/history';
import { batchZhTW } from './zh-TW/batch';
import { pagesZhTW } from './zh-TW/pages';
import { dialogsZhTW } from './zh-TW/dialogs';
import { errorsZhTW } from './zh-TW/errors';
import { debateZhTW } from './zh-TW/debate';
import { analyzeZhTW } from './zh-TW/analyze';
import { nativeZhTW } from './zh-TW/native';

/**
 * English is the key source. Each area is its own module; zh-TW modules are
 * typed `Record<keyof typeof <en module>, string>` so a missing or extra key
 * is a compile error.
 */
export const en = {
  ...shell,
  ...native,
  ...analyze,
  ...debate,
  ...errors,
  ...dialogs,
  ...pages,
  ...batch,
  ...history,
  ...settingsA,
  ...settingsB,
  ...settingsC,
  ...exportMsgs,
  ...modelNotes,
} as const;

export type MessageKey = keyof typeof en;

export const zhTW: Record<MessageKey, string> = {
  ...shellZhTW,
  ...nativeZhTW,
  ...analyzeZhTW,
  ...debateZhTW,
  ...errorsZhTW,
  ...dialogsZhTW,
  ...pagesZhTW,
  ...batchZhTW,
  ...historyZhTW,
  ...settingsAZhTW,
  ...settingsBZhTW,
  ...settingsCZhTW,
  ...exportZhTW,
  ...modelNotesZhTW,
};

const CATALOGS: Record<Locale, Record<string, string>> = {
  en: en as unknown as Record<string, string>,
  'zh-TW': zhTW,
};

export function catalogFor(locale: Locale): Catalog {
  return { active: CATALOGS[locale], fallback: CATALOGS.en, locale };
}

/** Non-React translator for pure modules (transcript export, tests). */
export function makeTranslator(locale: Locale): (key: MessageKey, vars?: Vars) => string {
  const catalog = catalogFor(locale);
  return (key, vars) => translate(catalog, key, vars);
}
