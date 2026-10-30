# Local OpenAI-compatible servers (oMLX, vLLM, LM Studio, Ollama)

Settings → LLM Providers → Local LLM accepts any OpenAI-compatible server, including ones that **require an API key** (for example oMLX).

## Configure oMLX

1. Settings → Local LLM → *Manual entry*.
2. *Use oMLX defaults* fills `http://127.0.0.1:8000/v1` and `Q3.8 Flash Next 原版` — the model id oMLX actually serves (it maps to Qwen3.8-Flash-Next-oQ4e-mtp, context 65,536). Use *List models* to confirm the id on your server.
3. Paste the server's API key into *API key (optional)*. It is stored only through Electron `safeStorage` (OS keychain), under its own entry (`local:api-key`), is never rendered back, and is never written to logs. Leave the field blank to keep a stored key.
4. *List models* (GET `{base}/models` with the key) and *Test connection* (one 1-token completion) check the server and key. HTTP 401 is reported as "enter the API key it requires".
5. *Save*. *Clear* removes URL, model and key.

The app never changes the server's own settings or authentication.

## How runs use it

| Engine | Mapping |
|---|---|
| Full LangGraph engine (what the desktop app runs) | the `local` provider maps onto the upstream library's generic `openai_compatible` provider: `backend_url` = your base URL, key injected into `OPENAI_COMPATIBLE_API_KEY` only while the graph is constructed (then restored), structured output bound **without** a forced `tool_choice` (many servers reject the object form) |
| Free simplified engine | `LocalLLMAdapter` sends `Authorization: Bearer <key>` (or the `local` sentinel for keyless runtimes) |

Cost is treated as $0 for local runs.

## What is and is not verified

- Automated (fake OpenAI-compatible server that enforces a Bearer key): auth header on every call, endpoint path, model id, absence of an object-form `tool_choice`, full graph (one market analyst, 1 debate round, 1 risk round) running to `session.complete`, key not left in the process environment, key not echoed by `/llm/local-models`.
- **Not** automated: a real model's behaviour (tool-calling quality, latency, whether it follows the report-language instruction). That needs a real run against the actual server.
