// @vitest-environment happy-dom
//
// Component-level i18n checks: default locale, switching, persistence, <html lang>,
// and that protected tokens (tickers, provider brands) are not translated.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { I18nProvider, useI18n } from '../../src/i18n';
import ProviderSetupCallout from '../../src/components/ProviderSetupCallout';
import { CostGuardModal } from '../../src/components/CostGuardModal';
import DebateStream from '../../src/components/DebateStream';
import { buildTranscriptMarkdown } from '../../src/lib/transcript';
import { buildTranscriptHtml } from '../../src/lib/transcript-html';
import type { DebateEvent } from '../../src/lib/engine-client';

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
    raw: m,
  };
}

let store: ReturnType<typeof memoryStorage>;
beforeEach(() => {
  store = memoryStorage();
  vi.stubGlobal('localStorage', store);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Switcher() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div>
      <span data-testid="loc">{locale}</span>
      <span data-testid="nav">{t('nav.analyze')}</span>
      <button data-testid="to-en" onClick={() => setLocale('en')} />
      <button data-testid="to-zh" onClick={() => setLocale('zh-TW')} />
    </div>
  );
}

describe('I18nProvider', () => {
  it('defaults to Traditional Chinese, sets <html lang>, and persists a switch', () => {
    render(
      <I18nProvider>
        <Switcher />
      </I18nProvider>,
    );
    expect(screen.getByTestId('loc').textContent).toBe('zh-TW');
    expect(screen.getByTestId('nav').textContent).toBe('分析');
    expect(document.documentElement.lang).toBe('zh-Hant-TW');

    act(() => screen.getByTestId('to-en').click());
    expect(screen.getByTestId('nav').textContent).toBe('Analyze');
    expect(document.documentElement.lang).toBe('en');
    expect(store.raw.get('tal.locale')).toBe('en');
  });

  it('restores the saved locale on the next mount', () => {
    store.setItem('tal.locale', 'en');
    render(
      <I18nProvider>
        <Switcher />
      </I18nProvider>,
    );
    expect(screen.getByTestId('loc').textContent).toBe('en');
  });

  it('tells the main process only the locale code (no other data)', () => {
    const setLocale = vi.fn().mockResolvedValue('zh-TW');
    (window as unknown as { tradingAgentsLab: unknown }).tradingAgentsLab = { setLocale };
    render(
      <I18nProvider>
        <Switcher />
      </I18nProvider>,
    );
    expect(setLocale).toHaveBeenCalledWith('zh-TW');
    act(() => screen.getByTestId('to-en').click());
    expect(setLocale).toHaveBeenLastCalledWith('en');
    delete (window as unknown as { tradingAgentsLab?: unknown }).tradingAgentsLab;
  });

  it('hook without a provider resolves to English (existing component tests stay valid)', () => {
    render(<Switcher />);
    expect(screen.getByTestId('nav').textContent).toBe('Analyze');
  });
});

describe('translated components', () => {
  it('ProviderSetupCallout renders in zh-TW and keeps provider brand names', () => {
    render(
      <I18nProvider initialLocale="zh-TW">
        <ProviderSetupCallout />
      </I18nProvider>,
    );
    expect(screen.getByText('連接 AI 模型以執行真實分析')).toBeTruthy();
    expect(screen.getByText(/OpenAI、Anthropic、OpenRouter 或 Google Gemini/)).toBeTruthy();
    expect(screen.getByTestId('setup-provider-cta').textContent).toBe('設定供應商');
  });

  it('CostGuardModal interpolates dollar amounts and the dimension word', () => {
    render(
      <I18nProvider initialLocale="zh-TW">
        <CostGuardModal
          overDimension="daily"
          spend={{ daily_usd: 1.5, weekly_usd: 2, monthly_usd: 3, sessions_today: 1 } as never}
          config={{ cap_daily_usd: 1, cap_weekly_usd: 5, cap_monthly_usd: 15, cap_sessions_per_day: 0, enabled: true } as never}
          estCostUsd={0.2}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />
      </I18nProvider>,
    );
    expect(screen.getByText('已達預算上限')).toBeTruthy();
    expect(screen.getByText(/\$1\.50.*每日上限為 \$1\.00/)).toBeTruthy();
  });

  const EVENTS: DebateEvent[] = [
    { type: 'session.start', ticker: 'NVDA', trade_date: '2026-06-16' } as DebateEvent,
    { type: 'graph.plan', analysts: ['market'], max_debate_rounds: 1, max_risk_rounds: 1 } as DebateEvent,
    { type: 'agent.message', agent: 'technical_analyst', phase: 'analysts', content: 'English model text' } as DebateEvent,
    {
      type: 'session.complete',
      ticker: 'NVDA',
      trade_date: '2026-06-16',
      live: true,
      provider: 'openai',
      model: 'gpt-6-sol',
      input_tokens: 208994,
      output_tokens: 6196,
      estimated_cost_usd: 0,
      decision: { stance: 'neutral', conviction: 0.5, bull_strength: 50, bear_strength: 50, risk_level: 'moderate', reasoning: 'steady' },
    } as DebateEvent,
  ];

  it('DebateStream localises roles/progress/decision but never the ticker, model id or model text', () => {
    render(
      <I18nProvider initialLocale="zh-TW">
        <DebateStream events={EVENTS} isStreaming={false} />
      </I18nProvider>,
    );
    expect(screen.getByText('NVDA')).toBeTruthy();
    expect(screen.getByText('技術分析師')).toBeTruthy();
    expect(screen.getByText('English model text')).toBeTruthy();
    expect(screen.getByTestId('decision-stance').textContent).toBe('中立');
    expect(screen.getByText(/gpt-6-sol/)).toBeTruthy();
    expect(screen.getByText(/輸入 208,994 · 輸出 6,196 tokens/)).toBeTruthy();
  });

  it('transcript exports follow the chosen locale and default to English', () => {
    const zh = buildTranscriptMarkdown(EVENTS, 'zh-TW');
    expect(zh).toContain('委員會評估');
    expect(zh).toContain('技術分析師');
    expect(zh).toContain('English model text');
    const en = buildTranscriptMarkdown(EVENTS);
    expect(en).toContain('Committee Assessment');
    const html = buildTranscriptHtml(EVENTS, 'zh-TW').html;
    expect(html).toContain('lang="zh-Hant-TW"');
    expect(html).toContain('委員會評估');
    expect(html).toContain('<strong>僅供教育與研究用途。</strong>');
    expect(buildTranscriptHtml(EVENTS).html).toContain('<html lang="en">');
  });
});
