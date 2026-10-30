/**
 * Remembers which engine-side run this tab was showing, so a page reload re-attaches to it instead of losing it.
 * sessionStorage on purpose: per tab, gone when the tab closes. Holds only an id and a ticker, never a secret.
 */
const KEY = 'tal:last-run';

export interface RememberedRun {
  runId: string;
  ticker: string;
}

export function rememberRun(run: RememberedRun): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(run));
  } catch {
    // storage unavailable: resume still works through /runs/active
  }
}

export function recallRun(): RememberedRun | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<RememberedRun>;
    return typeof v.runId === 'string' && v.runId ? { runId: v.runId, ticker: String(v.ticker ?? '') } : null;
  } catch {
    return null;
  }
}

export function forgetRun(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
