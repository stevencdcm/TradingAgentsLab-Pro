import { applyTheme, loadTheme, type ThemeChoice } from './lib/theme';
import proIcon from './assets/pro-icon.png';
import { useCallback, useEffect, useState } from 'react';
import Analyze from './pages/Analyze';
import Settings from './pages/Settings';
import History from './pages/History';
import Watchlist from './pages/Watchlist';
import LearnAI from './pages/LearnAI';
import StatusStrip from './components/StatusStrip';
import { UpstreamCheckModal } from './components/UpstreamCheckModal';
import { checkUpstream, type UpstreamCheckResult } from './lib/upstream';
import { useI18n, LOCALES, type Locale } from './i18n';
import styles from './App.module.css';

const LOCALE_NAMES: Record<Locale, string> = { 'zh-TW': '繁體中文', en: 'English' };

type Route = 'analyze' | 'watchlist' | 'history' | 'learn' | 'settings';

const ROUTES: Route[] = ['analyze', 'watchlist', 'history', 'learn', 'settings'];

function parseHash(hash: string): Route {
  const cleaned = hash.replace(/^#/, '') as Route;
  return ROUTES.includes(cleaned) ? cleaned : 'analyze';
}

function App() {
  const { t, locale, setLocale } = useI18n();
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  const [newAnalysisTick, setNewAnalysisTick] = useState(0);
  const [theme, setTheme] = useState<ThemeChoice>(() => loadTheme());
  useEffect(() => applyTheme(theme), []); // apply the saved choice once on start
  const [appMenuOpen, setAppMenuOpen] = useState(false);
  /** Upstream-check modal state. null = closed; "checking" = in flight;
   * UpstreamCheckResult once the IPC round-trip returns. */
  const [upstreamModal, setUpstreamModal] = useState<
    'checking' | UpstreamCheckResult | null
  >(null);

  const runUpstreamCheck = useCallback(async () => {
    setUpstreamModal('checking');
    try {
      const result = await checkUpstream();
      setUpstreamModal(result);
    } catch (err) {
      setUpstreamModal({
        status: 'error',
        latestTag: '',
        upstreamHead: '',
        ourHead: '',
        behindCount: 0,
        aheadCount: 0,
        behindCommits: [],
        checkedAt: new Date().toISOString(),
        error: err instanceof Error ? err.message : String(err),
        compareUrl: 'https://github.com/TauricResearch/TradingAgents',
      });
    }
  }, []);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // App menu accelerators come in via IPC.
  useEffect(() => {
    const bridge = window.tradingAgentsLab;
    if (!bridge?.onMenuCommand) return;
    const unsubNav = bridge.onMenuCommand('menu:navigate', (...args: unknown[]) => {
      const target = args[0];
      if (typeof target === 'string' && ROUTES.includes(target as Route)) {
        window.location.hash = `#${target}`;
      }
    });
    const unsubNew = bridge.onMenuCommand('menu:new-analysis', () => {
      window.location.hash = '#analyze';
      setNewAnalysisTick((n) => n + 1);
    });
    const unsubUpstream = bridge.onMenuCommand('menu:check-upstream', () => {
      void runUpstreamCheck();
    });
    return () => {
      unsubNav();
      unsubNew();
      unsubUpstream();
    };
  }, [runUpstreamCheck]);

  // Expose the trigger globally so Settings → About can call it without
  // prop-drilling. Slightly hacky vs proper context but cheap and contained.
  useEffect(() => {
    (window as unknown as { __talCheckUpstream?: () => void }).__talCheckUpstream =
      () => void runUpstreamCheck();
    return () => {
      delete (window as unknown as { __talCheckUpstream?: () => void }).__talCheckUpstream;
    };
  }, [runUpstreamCheck]);

  // Close the app menu on Escape or click outside.
  useEffect(() => {
    if (!appMenuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setAppMenuOpen(false);
    };
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(`.${styles.appMenuWrapper}`)) {
        setAppMenuOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('mousedown', onClick);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('mousedown', onClick);
    };
  }, [appMenuOpen]);

  const navItem = (target: Route, label: string) => {
    const active = route === target;
    return (
      <a
        className={`${styles.navItem} ${active ? styles.navItemActive : ''}`}
        href={`#${target}`}
        data-testid={`nav-${target}`}
        data-active={active ? 'true' : 'false'}
      >
        <span className={styles.navItemDot} />
        {label}
      </a>
    );
  };

  return (
    <div className={styles.shell}>
      <header className={`${styles.titleBar} app-drag-region`}>
        <div className={styles.titleBarContent}>
          <span className={styles.brand}>
            <img className={styles.brandLogo} src={proIcon} alt="" />
            <span className={styles.brandText}>Trading Agents Lab</span>
            <span className={styles.brandPro}>Pro</span>
          </span>
          <div className={styles.titleBarRight}>
            <span className={styles.connectionPill}>{t('app.connection.standalone')}</span>
            <select
              className={`${styles.langSelect} app-no-drag`}
              value={theme}
              onChange={(e) => {
                const next = e.target.value as ThemeChoice;
                setTheme(next);
                applyTheme(next);
              }}
              title={t('app.theme.title')}
              aria-label={t('app.theme.label')}
              data-testid="theme-select"
            >
              <option value="system">{t('app.theme.system')}</option>
              <option value="light">{t('app.theme.light')}</option>
              <option value="dark">{t('app.theme.dark')}</option>
            </select>
            <select
              className={`${styles.langSelect} app-no-drag`}
              value={locale}
              onChange={(e) => setLocale(e.target.value as Locale)}
              title={t('app.language.title')}
              aria-label={t('app.language.label')}
              data-testid="language-select"
            >
              {LOCALES.map((l) => (
                <option key={l} value={l}>
                  {LOCALE_NAMES[l]}
                </option>
              ))}
            </select>
            <div className={`${styles.appMenuWrapper} app-no-drag`}>
              <button
                type="button"
                className={`${styles.titleBarButton} ${appMenuOpen ? styles.titleBarButtonActive : ''}`}
                onClick={() => setAppMenuOpen((open) => !open)}
                title={t('app.menu.title')}
                aria-label={t('app.menu.aria')}
                aria-haspopup="menu"
                aria-expanded={appMenuOpen}
              >
                ⏻
              </button>
              {appMenuOpen && (
                <div className={styles.appMenu} role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    className={styles.appMenuItem}
                    onClick={() => {
                      setAppMenuOpen(false);
                      window.tradingAgentsLab?.restart?.();
                    }}
                  >
                    <span className={styles.appMenuIcon}>↻</span>
                    <span className={styles.appMenuLabel}>{t('app.menu.restart')}</span>
                    <span className={styles.appMenuHint}>{t('app.menu.restartHint')}</span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className={`${styles.appMenuItem} ${styles.appMenuItemDanger}`}
                    onClick={() => {
                      setAppMenuOpen(false);
                      window.tradingAgentsLab?.shutdown?.();
                    }}
                  >
                    <span className={styles.appMenuIcon}>⏻</span>
                    <span className={styles.appMenuLabel}>{t('app.menu.shutdown')}</span>
                    <span className={styles.appMenuHint}>{t('app.menu.shutdownHint')}</span>
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </header>

      <div className={`${styles.statusStripRow} app-no-drag`}>
        <StatusStrip />
      </div>

      <nav className={`${styles.sidebar} app-no-drag`}>
        {navItem('analyze', t('nav.analyze'))}
        {navItem('watchlist', t('nav.watchlist'))}
        {navItem('history', t('nav.history'))}
        {navItem('learn', t('nav.learn'))}
        <div className={styles.navSpacer} />
        {navItem('settings', t('nav.settings'))}
      </nav>

      <main className={`${styles.main} app-no-drag`}>
        {/* Analyze stays mounted (hide-don't-unmount) so an in-flight
            debate's WebSocket survives navigation. Founder ask 2026-05-17:
            "user may have to start the analysis and then go to their
            watchlist or history and then come back to analysis." Pre-fix,
            Analyze unmount closed the WS and engine aborted mid-stream.

            The other three pages keep their original mount/unmount
            behaviour so their on-mount fetches re-run on each visit. */}
        <div style={{ display: route === 'analyze' ? 'contents' : 'none' }}>
          <Analyze resetSignal={newAnalysisTick} />
        </div>
        {route === 'watchlist' && <Watchlist />}
        {route === 'history' && <History />}
        {route === 'learn' && <LearnAI />}
        {route === 'settings' && <Settings />}
      </main>

      {upstreamModal !== null && (
        <UpstreamCheckModal
          state={upstreamModal}
          onDismiss={() => setUpstreamModal(null)}
        />
      )}

      <footer className={styles.footer}>
        <span>{t('footer.version', { version: __APP_VERSION__ })}</span>
        <span className={styles.footerRight}>{t('footer.disclaimer')}</span>
      </footer>
    </div>
  );
}

export default App;
