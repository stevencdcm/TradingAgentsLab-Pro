# Traditional Chinese (zh-TW) interface

This fork adds a Traditional Chinese UI to the Lab Pro desktop app, on by default and switchable to English at any time.
The UI language is also the **report language**: in zh-TW mode the research itself (summary, analyst sections, risk discussion and final conclusion) is generated in Traditional Chinese (Taiwan usage). Provider names, tickers, numbers, source names / citations and model / API identifiers are kept as given.

## How it works

| Piece | Where |
|---|---|
| Pure functions (`translate`, plurals, `{name}` / `{n:n}` interpolation, rich-text split) | `desktop/src/i18n/core.ts` |
| Message dictionaries (English = key source, zh-TW typed against it) | `desktop/src/i18n/en/*.ts`, `desktop/src/i18n/zh-TW/*.ts`, merged in `messages.ts` |
| React `I18nProvider`, `useI18n()` / `useT()` | `desktop/src/i18n/context.tsx` |
| Persistence (`localStorage` key `tal.locale`, default `zh-TW`) | `desktop/src/i18n/storage.ts` |
| Native menu / dialogs / updater + OAuth messages (main process) | `desktop/electron/menu.ts`, `desktop/electron/i18n-main.ts`; renderer sends **only the locale code** over `i18n:set-locale`, main stores it in `preferences.json` |
| Engine / provider error mapping (original text always kept) | `desktop/src/lib/engine-errors.ts` |
| Exported transcripts (Markdown + HTML) follow the chosen locale | `desktop/src/lib/transcript*.ts` (`locale` argument, default `en`) |

Rules the code relies on:

- No DOM text replacement. Every visible string is a dictionary key.
- A missing key falls back to English, then to the key itself — never a half-translated mix.
- `zh-TW` is declared as `Record<keyof typeof en, string>`: a missing or extra key is a **compile error**.
- Unit tests (`tests/unit/i18n*.test.ts*`) also check placeholders and `<b>/<i>/<code>` tags match between locales, plural pairs exist, and no common Simplified-only characters leak into zh-TW text.
- Outside a provider (existing unit tests) the hook resolves to English, so English assertions keep working.

## Adding or changing a string

1. Add the key + English text to the relevant `en/<area>.ts`.
2. Add the zh-TW text to `zh-TW/<area>.ts` (TypeScript will not compile until you do).
3. Use it with `t('key', { vars })`, `rich('key')` for `<b>/<i>/<code>`, or `tp('key', n)` (needs `key.one` / `key.other`).

## Generated report language

| Step | Where |
|---|---|
| UI locale → request | `runAnalysis({ reportLanguage })` (Analyze + batch runner) → WebSocket start frame field `report_language` (`desktop/src/lib/run-analysis.ts`, `engine-client.ts`) |
| Validation (unknown ⇒ English) | `engine/report_language.py` `normalize_report_language` |
| Full LangGraph engine (used by every provider incl. OAuth / Anthropic) | `full_debate._apply_report_language` sets the upstream `output_language` key; the upstream library appends `get_language_instruction()` to every analyst / researcher / debater / manager / trader prompt |
| Free simplified engine (`live_debate`, used e.g. when `engine` is absent) | `free_engine_suffix` is appended to each agent's system prompt; the `STANCE=…`/`CONVICTION=…` decision lines stay in the fixed English/numeric form so parsing keeps working |
| Traceability | `session.start` events carry `report_language` |

English mode adds no prompt text (byte-identical to before). Already saved sessions are never rewritten. The Telegram bot path has no UI locale and stays English.

Verification levels (kept separate on purpose):

- **Static / wire level (automated, no real model):** prompt text per agent per provider config, English byte-identity, WS frame → engine kwargs, real local adapter → HTTP body against a fake OpenAI-compatible server, upstream `get_language_instruction()` with the config applied, and the renderer start frame. See `engine/tests/test_report_language.py` and `desktop/tests/unit/report-language.test.tsx`.
- **Real generation (needs a provider + key, run by the owner):** whether a given model actually answers fully in Traditional Chinese with the numbers / tickers / sources intact. Not covered by CI.

## Deliberately NOT translated

- The built-in stub-debate sample text (canned English, not a model output) and text of sessions saved before this change.
- The upstream prompts themselves (they stay English; only a language instruction is appended).
- Provider brand names, model IDs, ticker symbols, API-key field formats / placeholders (`sk-…`, `PKxxxx…`), URLs and legal-document text published on the website (the consent screen says the English documents are authoritative).
- Server error text that does not match a known pattern (shown as-is). Known errors are shown in Chinese **with the original text** underneath / in parentheses.
- Text from third-party libraries (pi-ai OAuth instructions in the sign-in flow).

## Known limits

- Whether a model follows the language instruction perfectly depends on the model; tool-call arguments and structured-output enum values stay in their fixed form.
- The Playwright e2e suite asserts English strings; its fixture pins the UI to English. It was not run in the PR environment.
- Building this fork yourself produces an app that is **not** signed/notarized by the original publisher and does not auto-update from the official feed.
