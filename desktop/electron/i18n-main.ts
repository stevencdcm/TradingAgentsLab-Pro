/**
 * Main-process translation helper. Reads the persisted UI language (prefs) on
 * every call so a language switch applies to the next dialog / message
 * without a restart.
 */
import { translate, type Vars } from '../src/i18n/core';
import { catalogFor, type MessageKey } from '../src/i18n/messages';
import { getLocale } from './prefs';

export function tm(key: MessageKey, vars?: Vars): string {
  return translate(catalogFor(getLocale()), key, vars);
}
