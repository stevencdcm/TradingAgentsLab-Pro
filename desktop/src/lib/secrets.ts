export interface SecretListing {
  key: string;
  hint: string;
  updatedAt: string;
}

export interface SecretEntry {
  hint: string;
  updatedAt: string;
  cipher: string;
}

export interface CorruptionRecovery {
  backupPath: string;
  recoveredAt: string;
}

export interface SecretsAvailability {
  available: boolean;
  filePath: string;
  corruptionRecovery: CorruptionRecovery | null;
}

function bridge() {
  if (!window.tradingAgentsLab?.secrets) {
    throw new Error('secrets bridge not available: preload not loaded');
  }
  return window.tradingAgentsLab.secrets;
}

export function getAvailability(): Promise<SecretsAvailability> {
  return bridge().availability();
}

/** Same-window notification that a stored secret changed, so pages that cache availability
 * (provider dropdown, status strip) re-read it instead of waiting for a remount or reload. */
const SECRETS_CHANGED = 'tal:secrets-changed';

function notifySecretsChanged(): void {
  try {
    window.dispatchEvent(new Event(SECRETS_CHANGED));
  } catch {
    /* non-DOM environment */
  }
}

export function onSecretsChanged(handler: () => void): () => void {
  window.addEventListener(SECRETS_CHANGED, handler);
  return () => window.removeEventListener(SECRETS_CHANGED, handler);
}

export async function setSecret(key: string, value: string): Promise<SecretEntry> {
  const entry = await bridge().set(key, value);
  notifySecretsChanged();
  return entry;
}

export function getSecret(key: string): Promise<string | null> {
  return bridge().get(key);
}

export function listSecrets(): Promise<SecretListing[]> {
  return bridge().list();
}

export async function deleteSecret(key: string): Promise<boolean> {
  const ok = await bridge().delete(key);
  notifySecretsChanged();
  return ok;
}

export function onSecretsRecovered(
  handler: (info: CorruptionRecovery) => void,
): () => void {
  return bridge().onRecovered(handler);
}
