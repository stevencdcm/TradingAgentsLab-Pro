# Traditional Chinese (zh-TW) interface

This fork adds a Traditional Chinese UI to the Lab Pro desktop app, on by default and switchable to English at any time.
It is a **UI** translation only: model output, upstream agent prompts and provider names are left in their original language.

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

## Deliberately NOT translated

- Model-generated research text, agent messages and the stub-debate sample text (those come from the engine / LLM).
- Upstream TradingAgents prompts and the engine's Python source.
- Provider brand names, model IDs, ticker symbols, API-key field formats / placeholders (`sk-…`, `PKxxxx…`), URLs and legal-document text published on the website (the consent screen says the English documents are authoritative).
- Server error text that does not match a known pattern (shown as-is). Known errors are shown in Chinese **with the original text** underneath / in parentheses.
- Text from third-party libraries (pi-ai OAuth instructions in the sign-in flow).

## Known limits

- Analyst/risk agents answer in English unless their prompts are changed; changing the output language is a separate piece of work (prompts live in the vendored upstream package).
- The Playwright e2e suite asserts English strings; its fixture pins the UI to English. It was not run in the PR environment.
- Building this fork yourself produces an app that is **not** signed/notarized by the original publisher and does not auto-update from the official feed.
