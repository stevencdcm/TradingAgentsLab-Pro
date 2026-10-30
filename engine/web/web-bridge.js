// Browser implementation of the Electron preload bridge (window.tradingAgentsLab)
// for the local web version. Every call goes to the same-origin /web/* API on
// 127.0.0.1; nothing here stores a secret (the server keeps them in the macOS
// Keychain and hands the page opaque @@secret:name@@ references).
(function () {
  const post = (path, body) =>
    fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  const getJson = async (path) => (await fetch(path)).json();
  const noop = () => () => {};
  const checked = async (res) => {
    const data = await res.json();
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  };
  let handshake = null;
  window.tradingAgentsLab = {
    version: 'web',
    platform: 'web',
    getEngineHandshake: async () => (handshake ??= await getJson('/web/handshake')),
    onEngineExited: noop,
    consent: {
      get: () => getJson('/web/consent'),
      accept: async () => (await post('/web/consent/accept')).json(),
      decline: async () => { document.body.textContent = 'You declined the agreement. Close this tab.'; },
    },
    updates: {
      getState: async () => ({ autoUpdate: false, currentVersion: 'web', supported: false }),
      setAuto: async (v) => v,
      check: async () => ({ ok: false, reason: 'dev' }),
      install: async () => {},
      onStatus: noop,
    },
    secrets: {
      availability: () => getJson('/web/secrets/availability'),
      set: async (key, value) => checked(await post('/web/secrets/set', { key, value })),
      get: async (key) => checked(await post('/web/secrets/get', { key })),
      list: () => getJson('/web/secrets/list'),
      delete: async (key) => checked(await post('/web/secrets/delete', { key })),
      onRecovered: noop,
    },
    avSignup: { getFreeKey: async () => null },
    transcript: {
      openHtml: async (html, baseName) => {
        const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
        window.open(url, '_blank', 'noopener');
        return baseName;
      },
    },
    oauth: {
      openaiStart: async () => ({ success: false, error: 'OpenAI OAuth sign-in is not available in the web version yet.' }),
      openaiStatus: async () => ({ connected: false }),
      openaiDisconnect: async () => true,
      openaiPromptResponse: () => {},
      openaiCredentials: async () => null,
      onProgress: noop,
      onPrompt: noop,
    },
    onMenuCommand: noop,
    checkUpstream: async () => ({ status: 'error', latestTag: '', upstreamHead: '', ourHead: '', behindCount: 0, aheadCount: 0, behindCommits: [], checkedAt: new Date().toISOString(), error: 'Not available in the web version.', compareUrl: 'https://github.com/TauricResearch/TradingAgents' }),
    setLocale: async (l) => l,
    shutdown: async () => { await post('/web/shutdown'); document.body.textContent = 'Server stopped. You can close this tab.'; },
    restart: async () => { location.reload(); },
  };
})();
