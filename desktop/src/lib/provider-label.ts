import { PROVIDER_LABEL, type LLMProvider } from './engine-client';
import type { I18n } from '../i18n';

/** Provider brand names are never translated; only the generic "Local LLM" label is. */
export function providerLabel(provider: LLMProvider, t: I18n['t']): string {
  return provider === 'local' ? t('provider.local') : PROVIDER_LABEL[provider];
}
