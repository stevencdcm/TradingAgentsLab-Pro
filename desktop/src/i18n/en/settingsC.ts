/** Settings: Telegram bot panel. */
export const settingsC = {
  'tg.err.token': 'Bot token is required. Paste the token from BotFather.',
  'tg.err.allowlist':
    'Allowlist is empty. Add at least one numeric Telegram user ID, one per line. Message /start to your bot to discover yours.',
  'tg.err.cap': 'Daily cap must be a non-negative dollar amount.',
  'tg.err.noCreds': 'No {label} credentials configured.{hint}',
  'tg.hint.openai':
    ' Either sign in with OpenAI OAuth in the LLM Providers tab, or paste an API key in the OpenAI API-key fallback row.',
  'tg.hint.other': ' Set one in the LLM Providers tab, or pick a different provider above.',
  'tg.title': '<b>Telegram Bot (bidirectional)</b>',
  'tg.intro':
    'Message the bot a ticker like <code>NVDA</code> from your phone and get a Diligence run back. The bot polls Telegram outbound, so nothing is exposed to the internet from your machine. Allowlist is required; the bot drops messages from anyone else silently. Per-chat daily spend cap prevents token-drain abuse if your bot token leaks.',
  'tg.edu': '<i>Educational and research purposes only. The bot returns the same committee assessment the desktop app shows, with the same disclaimers.</i>',
  'tg.pending.title.one': 'Pending approval',
  'tg.pending.title.other': 'Pending approvals',
  'tg.pending.hint':
    'Someone messaged <code>/start</code> to the bot. Approve to add them to the allowlist and let them trigger Diligence runs; the bot will DM them on approval. Deny silently drops the request. Entries expire automatically after 30 minutes.',
  'tg.pending.noName': '(no name)',
  'tg.pending.chat': 'chat {id}',
  'tg.approve': 'Approve',
  'tg.deny': 'Deny',
  'tg.token': 'Bot Token',
  'tg.tokenStored': ' (stored, leave blank to keep)',
  'tg.tokenPlaceholder': 'paste from BotFather',
  'tg.provider': 'LLM provider for bot-triggered debates',
  'tg.providerHint.pre':
    'Uses the credential stored for this provider in the LLM Providers tab. For OpenAI, OAuth (Codex) is preferred when connected; an API key in the OpenAI fallback row is used otherwise. Default model: ',
  'tg.providerHint.openai': '{apiModel} (API key) or {oauthModel} (OAuth)',
  'tg.providerHint.post': '. Local runtimes are not yet supported for the bot.',
  'tg.allowlist': 'Allowlist (numeric chat IDs, one per line)',
  'tg.cap': 'Daily cap per chat (USD)',
  'tg.stop': 'Stop bot',
  'tg.stopping': 'Stopping…',
  'tg.polling': 'polling',
  'tg.start': 'Save & Start',
  'tg.starting': 'Starting…',
  'tg.stopped': 'stopped',
  'tg.lastError': 'last error: {error}',
  'tg.spend': '<b>Today\'s spend (UTC):</b>',
  'tg.spendEntry': 'chat {id}: {cost}',
} as const;
