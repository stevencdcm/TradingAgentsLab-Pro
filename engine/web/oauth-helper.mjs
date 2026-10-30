// OpenAI (ChatGPT / Codex) OAuth helper for the local web version.
//
// Reuses the same MIT-licensed `@earendil-works/pi-ai` flow the desktop app uses
// (PKCE + localhost:1455 callback). It never talks to the browser page: it speaks
// JSON lines on stdout to the Python server, which stores the tokens in the macOS
// Keychain. Tokens are only ever printed on the final {type:"done"} line, to the
// parent process.
//
//   node oauth-helper.mjs login           # interactive login; reads {type:"prompt-response"} on stdin
//   node oauth-helper.mjs refresh         # stdin: {"refresh":"..."}  -> stdout: {type:"done",credentials}
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.resolve(here, '..', '..', 'desktop');
// The package's `exports` map only allows ESM `import` of the subpath, so import the
// built file directly from the desktop app's node_modules.
const piPath = path.join(desktop, 'node_modules', '@earendil-works', 'pi-ai', 'dist', 'oauth.js');
const { loginOpenAICodex, refreshOpenAICodexToken } = await import(pathToFileURL(piPath).href);

const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');

function planType(access) {
  try {
    const p = access.split('.');
    if (p.length !== 3) return undefined;
    let b = p[1].replace(/-/g, '+').replace(/_/g, '/');
    while (b.length % 4) b += '=';
    const claim = JSON.parse(Buffer.from(b, 'base64').toString('utf8'))['https://api.openai.com/auth'];
    return typeof claim?.chatgpt_plan_type === 'string' ? claim.chatgpt_plan_type : undefined;
  } catch { return undefined; }
}

function pack(c, fallbackEmail) {
  const accountId = typeof c.accountId === 'string' ? c.accountId : undefined;
  const email = typeof c.email === 'string' ? c.email : accountId ? `account ${accountId.slice(0, 8)}…` : fallbackEmail;
  return { access: c.access, refresh: c.refresh, expires: c.expires, email, accountId, planType: planType(c.access) };
}

const mode = process.argv[2];
try {
  if (mode === 'refresh') {
    const rl = readline.createInterface({ input: process.stdin });
    let line = '';
    for await (const l of rl) { line = l; break; }
    const { refresh, email } = JSON.parse(line);
    out({ type: 'done', credentials: pack(await refreshOpenAICodexToken(refresh), email) });
  } else if (mode === 'login') {
    const rl = readline.createInterface({ input: process.stdin });
    let pending = null;
    rl.on('line', (l) => {
      try {
        const m = JSON.parse(l);
        if (m.type === 'prompt-response' && pending) { const r = pending; pending = null; r(String(m.value ?? '')); }
      } catch { /* ignore */ }
    });
    const creds = await loginOpenAICodex({
      onAuth: ({ url, instructions }) => out({ type: 'auth', url, instructions: instructions ?? null }),
      onPrompt: (prompt) => { out({ type: 'prompt', message: prompt.message, placeholder: prompt.placeholder ?? null }); return new Promise((r) => { pending = r; }); },
      onProgress: (message) => out({ type: 'progress', message }),
      onManualCodeInput: () => new Promise((r) => {
        setTimeout(() => { out({ type: 'prompt', message: 'Browser callback not received. Paste the code from the OpenAI page here:', placeholder: 'paste code' }); pending = r; }, 20000);
      }),
    });
    out({ type: 'done', credentials: pack(creds) });
    process.exit(0);
  } else {
    out({ type: 'error', message: 'unknown mode' });
    process.exit(2);
  }
} catch (err) {
  out({ type: 'error', message: err instanceof Error ? err.message : String(err) });
  process.exit(1);
}
