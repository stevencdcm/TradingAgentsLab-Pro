/**
 * Where the engine API lives.
 *  - Desktop (Electron): http://127.0.0.1:<port> as handed out by the main process.
 *  - Web: the SAME origin and base path the page was loaded from (document.baseURI), so the app works
 *    unchanged on http://127.0.0.1:8765/ and behind the workbench reverse proxy at https://host/lab-pro/.
 * Never a hardcoded host: the trusted-proxy mode must not need loopback URLs in the browser.
 */
export function apiBase(port: number): string {
  if (window.tradingAgentsLab?.platform === 'web') {
    return new URL('.', document.baseURI).href.replace(/\/$/, '');
  }
  return `http://127.0.0.1:${port}`;
}

export function wsBase(port: number): string {
  return apiBase(port).replace(/^http/, 'ws');
}

/** `Authorization` header for the engine API; empty behind the trusted proxy, where the session cookie authenticates and no token exists. */
export function authHeader(token: string | undefined): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
