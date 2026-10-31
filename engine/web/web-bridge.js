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
    getEngineHandshake: async () => (handshake ??= await getJson('web/handshake')),
    onEngineExited: noop,
    consent: {
      get: () => getJson('web/consent'),
      accept: async () => (await post('web/consent/accept')).json(),
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
      availability: () => getJson('web/secrets/availability'),
      set: async (key, value) => checked(await post('web/secrets/set', { key, value })),
      get: async (key) => checked(await post('web/secrets/get', { key })),
      list: () => getJson('web/secrets/list'),
      delete: async (key) => checked(await post('web/secrets/delete', { key })),
      onRecovered: noop,
    },
    avSignup: { getFreeKey: async () => null },
    transcript: {
      openHtml: async (html, baseName) => {
        const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
        window.open(url, '_blank', 'noopener');
        return baseName;
      },
      // On-demand PDF: the engine renders the same report HTML with the machine's headless Chrome; we download the bytes.
      savePdf: async (html, baseName, signal) => {
        const res = await fetch('web/report-pdf', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
          body: JSON.stringify({ html, name: baseName }),
        });
        if (!res.ok) {
          let d = {};
          try { d = await res.json(); } catch { /* non-JSON error page */ }
          const err = new Error((d && d.error) || `HTTP ${res.status}`);
          err.code = (d && d.code) || `http_${res.status}`;
          throw err;
        }
        const blob = await res.blob();
        const name = `${String(baseName || 'report').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '').slice(0, 64) || 'report'}.pdf`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = name; a.rel = 'noopener';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        return name;
      },
    },
    oauth: (() => {
      const progress = new Set(); const prompt = new Set(); let timer = null;
      const pump = async () => {
        try {
          const d = await getJson('web/oauth/events');
          for (const ev of d.events) {
            if (ev.type === 'progress') progress.forEach((h) => h({ message: ev.message }));
            if (ev.type === 'prompt') prompt.forEach((h) => h({ message: ev.message, placeholder: ev.placeholder ?? undefined }));
          }
          return d;
        } catch { return { state: 'error', error: 'bridge unreachable', events: [] }; }
      };
      return {
        openaiStart: async () => {
          const s = await (await post('web/oauth/start')).json();
          if (!s.success) return s;
          for (;;) {
            await new Promise((r) => setTimeout(r, 1000));
            const d = await pump();
            if (d.state === 'done') { const st = await getJson('web/oauth/status'); return { success: true, email: st.email }; }
            if (d.state === 'error') return { success: false, error: d.error || 'Login failed.' };
          }
        },
        openaiStatus: () => getJson('web/oauth/status'),
        openaiDisconnect: async () => (await post('web/oauth/disconnect')).json(),
        openaiPromptResponse: (value) => { void post('web/oauth/prompt', { value }); },
        openaiCredentials: () => getJson('web/oauth/credentials'),
        onProgress: (h) => { progress.add(h); return () => progress.delete(h); },
        onPrompt: (h) => { prompt.add(h); return () => prompt.delete(h); },
      };
    })(),
    onMenuCommand: noop,
    checkUpstream: async () => ({ status: 'error', latestTag: '', upstreamHead: '', ourHead: '', behindCount: 0, aheadCount: 0, behindCommits: [], checkedAt: new Date().toISOString(), error: 'Not available in the web version.', compareUrl: 'https://github.com/TauricResearch/TradingAgents' }),
    setLocale: async (l) => l,
    shutdown: async () => { await post('web/shutdown'); document.body.textContent = 'Server stopped. You can close this tab.'; },
    restart: async () => { location.reload(); },
  };
})();
