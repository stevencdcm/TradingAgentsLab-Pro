import type { I18n } from '../i18n';
import type { MessageKey } from '../i18n/messages';

/** Registry note text (English, from engine-client / the Codex catalog) -> message key. Unknown notes pass through. */
const NOTE_KEYS: Record<string, MessageKey> = {
  'Balanced': 'modelNote.balanced',
  'Balanced, cost-effective': 'modelNote.balancedCost',
  'Cheapest': 'modelNote.cheapest',
  'Current flagship, fastest': 'modelNote.currentFlagship',
  'Fast and focused, lighter reasoning': 'modelNote.fastFocused',
  'Flagship speed (~100 TPS)': 'modelNote.flagshipSpeed',
  'Flagship, 204K context': 'modelNote.flagship204k',
  'Latest frontier, 1M context': 'modelNote.latestFrontier',
  'Latest release': 'modelNote.latestRelease',
  'Most capable': 'modelNote.mostCapable',
  'Most capable, slower per turn': 'modelNote.mostCapableSlow',
  'Most cost-efficient (GA)': 'modelNote.mostCostEfficient',
  'Multimodal flagship': 'modelNote.multimodal',
  'Previous frontier': 'modelNote.previousFrontier',
  'Previous-gen flagship': 'modelNote.previousGen',
  'Reasoning-optimized': 'modelNote.reasoningOpt',
  'Speed-optimized': 'modelNote.speedOpt',
  'Strong all-round model, a good default for the debate': 'modelNote.goodDefault',
} as const;

export function modelNoteLabel(note: string, t: I18n['t']): string {
  const key = NOTE_KEYS[note];
  return key ? t(key) : note;
}
