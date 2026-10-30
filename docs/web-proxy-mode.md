# Trusted workbench reverse-proxy mode (S12)

Default behaviour is unchanged: loopback only, one-time launch nonce, `Host`/`Origin` = 127.0.0.1/localhost.
Proxy mode is **off** unless `TAL_WEB_PROXY_MODE=1`. It never replaces the local rules; a request that is not a
valid proxy hop is judged by the local rules (so a forged `X-TAL-*` header from anywhere gets 403).

## Environment
| variable | meaning |
|---|---|
| `TAL_WEB_PROXY_MODE=1` | enable |
| `TAL_WEB_PUBLIC_ORIGIN` | exact external origin, e.g. `https://m3.tailnet.example:47900` (scheme+host+port) |
| `TAL_WEB_BASE_PATH` | e.g. `/lab-pro` (empty = site root, e.g. a dedicated Tailnet HTTPS port) |
| `TAL_WEB_PROXY_SECRET_FILE` | file (0600) with a >=32-char random secret shared with the proxy (never in env/URLs) |
| `TAL_WEB_OWNER` | comma list of allowed owner identities (case-insensitive), e.g. the Tailscale login |

## What the proxy must do
1. Authenticate the real user (Tailscale identity) itself.
2. **Strip every client-supplied `X-TAL-*` header**, then add `X-TAL-Proxy-Auth: <secret>` and `X-TAL-Owner: <login>`.
3. Preserve `Host` (= public host) and `Origin`; strip the base path (`/lab-pro/x` -> `/x`) and forward to `127.0.0.1:8765`;
   redirect `/lab-pro` -> `/lab-pro/`; forward `Set-Cookie` unchanged (no Path/Domain rewrite).
4. Proxy WebSocket upgrades on `/lab-pro/stream` (keep `Origin`, `Cookie`; idle timeout >= 60 min: local models run 35+ min).

## What LabPro enforces on a hop
loopback peer + secret (constant-time) + owner allow-list + `Host == public host` + `Origin == public origin`
(required for POST/PUT/DELETE and WebSocket) + session cookie for state-changing requests and sockets.
Cookie: `tal_web; HttpOnly; SameSite=Strict; Secure (https); Path=<base>`; no Domain.
WebSocket authenticates by hop + owner + cookie: **no token in any URL**. The page uses `<base href>` and
same-origin relative URLs (`apiBase()`/`wsBase()` derive from `document.baseURI`).

`tools/dev/fake-workbench-proxy.py` is a test stand-in implementing the contract above.
