// @vitest-environment happy-dom
// Model catalog UI: Settings "更新模型清單" panel (manual refresh, loading, error + retry, no key) and the Analyze picker
// (shows provider-reported models, keeps a saved legacy choice, never changes the default on its own).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const fetchModelCatalog = vi.fn();
vi.mock('../../src/lib/engine-client', async () => {
  const actual = await vi.importActual<any>('../../src/lib/engine-client');
  return {
    ...actual,
    fetchModelCatalog: (...a: unknown[]) => fetchModelCatalog(...a),
    getHealth: vi.fn(async () => ({ ok: true, version: 't', uptime_seconds: 1, engine_state: 'ready', data_provider: 'yfinance', live_supported: true, providers: [] })),
    getLocalRuntimes: vi.fn(async () => ({ runtimes: [] })),
    listLocalModels: vi.fn(async () => ({ models: [] })),
    getRunStatus: vi.fn(async () => null),
    getActiveRuns: vi.fn(async () => ({ runs: [], recent: [] })),
  };
});
vi.mock('../../src/lib/secrets', async () => {
  const actual = await vi.importActual<any>('../../src/lib/secrets');
  return {
    ...actual,
    listSecrets: vi.fn(async () => [{ key: 'llm:anthropic', hint: '…1234', updatedAt: '2026-10-31' }]),
    getSecret: vi.fn(async () => 'sk-ant-secret-value-123'),
    onSecretsChanged: () => () => {},
  };
});
vi.mock('../../src/lib/oauth', async () => {
  const actual = await vi.importActual<any>('../../src/lib/oauth');
  return { ...actual, getOpenAIOAuthStatus: vi.fn(async () => ({ connected: false })) };
});

import Analyze from '../../src/pages/Analyze';
import ModelCatalogPanel from '../../src/components/ModelCatalogPanel';
import { I18nProvider } from '../../src/i18n';
import { _resetModelCatalogForTests } from '../../src/lib/model-catalog';

function stubStorage(seed: Record<string, string> = {}) {
  const m = new Map<string, string>(Object.entries(seed));
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null), setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k), clear: () => m.clear(),
  });
  return m;
}
const liveModels = (...ids: string[]) => ids.map((id) => ({ id, label: id, lifecycle: 'active', priced: id !== 'claude-opus-9-9' }));

beforeEach(() => {
  fetchModelCatalog.mockReset();
  _resetModelCatalogForTests();
  sessionStorage.clear();
  (window as any).tradingAgentsLab = { platform: 'web', getEngineHandshake: async () => ({ port: 8765, proxy: true }), consent: { get: async () => ({}) } };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Settings: 更新模型清單', () => {
  const panel = (keys = ['llm:anthropic']) => render(
    <I18nProvider initialLocale="zh-TW"><ModelCatalogPanel savedSecretKeys={new Set(keys)} /></I18nProvider>,
  );

  it('does not fetch on mount; the button fetches once, shows progress then the update time and count', async () => {
    stubStorage();
    let release: (v: unknown) => void = () => {};
    fetchModelCatalog.mockImplementation(() => new Promise((r) => { release = r; }));
    panel();
    expect(fetchModelCatalog).not.toHaveBeenCalled();
    const btn = screen.getByTestId('model-catalog-refresh-anthropic') as HTMLButtonElement;
    expect(btn.textContent).toBe('更新模型清單');
    await act(async () => { fireEvent.click(btn); });
    expect(btn.disabled).toBe(true);                                   // loading: no double refresh
    expect(screen.getByTestId('model-catalog-anthropic').textContent).toMatch(/更新中/);
    await act(async () => { release({ ok: true, source: 'live', fetched_at: 1_790_000_000, models: liveModels('claude-opus-5-5', 'claude-haiku-5-5') }); });
    await waitFor(() => expect(screen.getByTestId('model-catalog-anthropic').textContent).toMatch(/供應商列出 2 個/));
    expect(fetchModelCatalog).toHaveBeenCalledTimes(1);
    expect(fetchModelCatalog.mock.calls[0][1]).toBe(true);
  });

  it('failure shows a credential-free reason and a Retry button; the retry works', async () => {
    stubStorage();
    fetchModelCatalog.mockResolvedValueOnce({ ok: false, source: 'unavailable', models: [], error: { code: 'auth', message: 'rejected' } });
    panel();
    await act(async () => { fireEvent.click(screen.getByTestId('model-catalog-refresh-anthropic')); });
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/拒絕了已儲存的金鑰/));
    expect(document.body.textContent).not.toContain('sk-ant-secret');
    const retry = screen.getByTestId('model-catalog-refresh-anthropic');
    expect(retry.textContent).toBe('重試');
    fetchModelCatalog.mockResolvedValueOnce({ ok: true, source: 'live', fetched_at: 1_790_000_100, models: liveModels('claude-opus-5-5') });
    await act(async () => { fireEvent.click(retry); });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('a provider without a saved key has a disabled button and says why', () => {
    stubStorage();
    panel([]);
    expect((screen.getByTestId('model-catalog-refresh-anthropic') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('model-catalog-anthropic').textContent).toMatch(/先在上方儲存金鑰/);
  });
});

describe('Analyze model picker uses the shared catalog', () => {
  const mount = () => render(<I18nProvider initialLocale="zh-TW"><Analyze /></I18nProvider>);
  const options = async () => {
    const sel = (await screen.findByTestId('model-select')) as HTMLSelectElement;
    return { sel, ids: Array.from(sel.options).map((o) => o.value), texts: Array.from(sel.options).map((o) => o.textContent ?? '') };
  };

  it('offline (nothing refreshed): built-in list incl. the 5.5 family, default unchanged, nothing fetched', async () => {
    stubStorage();
    mount();
    const { sel, ids } = await options();
    for (const id of ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1']) expect(ids).toContain(id);
    expect(sel.value).toBe('claude-sonnet-4-6');
    expect(fetchModelCatalog).not.toHaveBeenCalled();
  });

  it('after a refresh the provider-reported models appear, marked; a saved legacy choice stays selected', async () => {
    stubStorage({
      'tal:model-catalog:v1:anthropic': JSON.stringify({ fetchedAt: 1_790_000_000, credStamp: '2026-10-31|…1234', models: liveModels('claude-opus-5-6', 'claude-opus-9-9', 'claude-opus-5-5') }),
      'tal:analyze:selected-model:anthropic': 'claude-opus-4-7',   // legacy id in neither list
    });
    mount();
    const { sel, ids, texts } = await options();
    expect(ids).toContain('claude-opus-5-6');
    expect(sel.value).toBe('claude-opus-4-7');                   // saved choice kept, not swapped to the recommendation
    expect(texts[ids.indexOf('claude-opus-4-7')]).toMatch(/已儲存的選擇/);
    expect(texts[ids.indexOf('claude-opus-5-6')]).toMatch(/供應商清單/);
    expect(texts[ids.indexOf('claude-opus-9-9')]).toMatch(/價格未知/);   // listed != priced
    expect(ids.filter((i) => i === 'claude-opus-5-5')).toHaveLength(1);
  });

  it('a Settings refresh updates the open picker (shared store) without touching the saved choice', async () => {
    stubStorage({ 'tal:analyze:selected-model:anthropic': 'claude-haiku-4-5' });
    mount();
    const before = await options();
    expect(before.ids).not.toContain('claude-opus-5-6');
    fetchModelCatalog.mockResolvedValue({ ok: true, source: 'live', fetched_at: 1_790_000_000, models: liveModels('claude-opus-5-6') });
    const { refreshCatalog } = await import('../../src/lib/model-catalog');
    await act(async () => { await refreshCatalog('anthropic'); });
    const after = await options();
    expect(after.ids).toContain('claude-opus-5-6');
    expect(after.sel.value).toBe('claude-haiku-4-5');
  });
});


describe('Batch uses the same selected model as Analyze', () => {
  it('runs the saved Analyze choice (a refreshed, newly listed model), not the built-in recommendation', async () => {
    stubStorage({
      'tal:model-catalog:v1:anthropic': JSON.stringify({ fetchedAt: 1, credStamp: '2026-10-31|…1234', models: liveModels('claude-opus-5-6') }),
      'tal:analyze:selected-model:anthropic': 'claude-opus-5-6',
    });
    const { resolveActiveProvider } = await import('../../src/components/BatchRunner');
    expect(await resolveActiveProvider()).toMatchObject({ provider: 'anthropic', model: 'claude-opus-5-6' });
  });
  it('with nothing saved it still uses the recommendation, and never writes the selection', async () => {
    const store = stubStorage();
    const { resolveActiveProvider } = await import('../../src/components/BatchRunner');
    expect(await resolveActiveProvider()).toMatchObject({ provider: 'anthropic', model: 'claude-sonnet-4-6' });
    expect(store.has('tal:analyze:selected-model:anthropic')).toBe(false);
  });
});
