/** Small shared dialogs: provider callout, cost guard, consent gate, error boundary. */
export const dialogs = {
  'providerCallout.title': 'Connect an AI model to run the real analysis',
  'providerCallout.text':
    'No LLM provider is set up yet, so the Diligence runs a built-in sample rather than a real multi-agent analysis. Add a provider to unlock the full thing. Paste an API key for OpenAI, Anthropic, OpenRouter, or Google Gemini.',
  'providerCallout.cta': 'Set up a provider',
  'costGuard.title.rate': 'Session rate cap reached',
  'costGuard.title.budget': 'Budget cap reached',
  'costGuard.dim.daily': 'Daily',
  'costGuard.dim.weekly': 'Weekly',
  'costGuard.dim.monthly': 'Monthly',
  'costGuard.dim.sessions': 'Sessions',
  'costGuard.cancel': 'Cancel',
  'costGuard.override': 'Override and continue',
  'costGuard.overrideWait': 'Override ({seconds})',
  'costGuard.body.rate':
    "You've used {used} of your {cap} sessions today. This session would push you over the limit.",
  'costGuard.body.budget':
    "You've spent {current} of your {cap} {dimension} cap. This session may cost up to {estimate} and will exceed the limit.",
  'costGuard.dimWord.daily': 'daily',
  'costGuard.dimWord.weekly': 'weekly',
  'costGuard.dimWord.monthly': 'monthly',
  'costGuard.hint': 'Override applies to this session only. Adjust caps in <b>Settings → Cost Guard</b> for permanent changes.',
  'consent.title': 'Before you begin',
  'consent.p1':
    '<b>Trading Agents Lab is an educational research tool, not financial advice.</b> It uses multi-agent LLM analysis to surface multiple perspectives on a ticker. The analysis can be wrong or incomplete. Any decision you make is your own.',
  'consent.p2':
    'This app is for personal, educational use. It is free and open source (AGPL-3.0) and is provided without warranty of any kind.',
  'consent.p3':
    '<b>Zero data collection.</b> Nothing about you leaves your machine. The only outbound calls the app initiates are an optional update check and the data and LLM providers you configure with your own keys.',
  'consent.listSep': ', ',
  'consent.listEnd': '.',
  'consent.fullTerms': 'Full terms:',
  'consent.terms': 'Terms',
  'consent.privacy': 'Privacy',
  'consent.disclaimer': 'Disclaimer',
  'consent.englishNote': 'The legal documents above are published in English; the English text is authoritative.',
  'consent.decline': 'Decline and Quit',
  'consent.agree': 'I Agree',
  'errorBoundary.badge': 'Something went wrong',
  'errorBoundary.title': 'The view hit an unexpected error',
  'errorBoundary.text':
    'Your data and settings are safe. Reload to get back to a working screen. If it keeps happening on the same page, note what you were doing so it can be reproduced.',
  'errorBoundary.reload': 'Reload',
} as const;
