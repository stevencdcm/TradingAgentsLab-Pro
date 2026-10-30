import {
  app,
  BrowserWindow,
  Menu,
  MenuItemConstructorOptions,
  shell,
} from 'electron';
import { translate, type Locale, type Vars } from '../src/i18n/core';
import { catalogFor, type MessageKey } from '../src/i18n/messages';
import { getLocale } from './prefs';

/** Routes the renderer hash router knows about. */
type Route = 'analyze' | 'watchlist' | 'history' | 'settings';

const SITE_URL = 'https://tradingagentslab.ai';
const SUPPORT_EMAIL = 'support@tradingagentslab.ai';

function send(win: BrowserWindow | null, channel: string, ...args: unknown[]): void {
  if (!win || win.isDestroyed()) return;
  win.webContents.send(channel, ...args);
}

function navTo(win: BrowserWindow | null, route: Route): void {
  send(win, 'menu:navigate', route);
}

function buildMenu(getWin: () => BrowserWindow | null, locale: Locale): Menu {
  const isMac = process.platform === 'darwin';
  const catalog = catalogFor(locale);
  const t = (key: MessageKey, vars?: Vars): string => translate(catalog, key, vars);

  const fileMenu: MenuItemConstructorOptions = {
    label: t('menu.file'),
    submenu: [
      {
        label: t('menu.newAnalysis'),
        accelerator: 'CmdOrCtrl+N',
        click: () => {
          navTo(getWin(), 'analyze');
          send(getWin(), 'menu:new-analysis');
        },
      },
      {
        label: t('menu.stopStreaming'),
        accelerator: 'CmdOrCtrl+.',
        click: () => send(getWin(), 'menu:stop-stream'),
      },
      { type: 'separator' },
      isMac
        ? { role: 'close', label: t('menu.close') }
        : { role: 'quit', label: t('menu.quit') },
    ],
  };

  const navMenu: MenuItemConstructorOptions = {
    label: t('menu.go'),
    submenu: [
      {
        label: t('menu.analyze'),
        accelerator: 'CmdOrCtrl+1',
        click: () => navTo(getWin(), 'analyze'),
      },
      {
        label: t('menu.watchlist'),
        accelerator: 'CmdOrCtrl+2',
        click: () => navTo(getWin(), 'watchlist'),
      },
      {
        label: t('menu.history'),
        accelerator: 'CmdOrCtrl+3',
        click: () => navTo(getWin(), 'history'),
      },
      { type: 'separator' },
      {
        label: t('menu.settings'),
        accelerator: 'CmdOrCtrl+,',
        click: () => navTo(getWin(), 'settings'),
      },
    ],
  };

  const editMenu: MenuItemConstructorOptions = {
    label: t('menu.edit'),
    submenu: [
      { role: 'undo', label: t('menu.undo') },
      { role: 'redo', label: t('menu.redo') },
      { type: 'separator' },
      { role: 'cut', label: t('menu.cut') },
      { role: 'copy', label: t('menu.copy') },
      { role: 'paste', label: t('menu.paste') },
      { role: 'selectAll', label: t('menu.selectAll') },
    ],
  };

  const viewMenu: MenuItemConstructorOptions = {
    label: t('menu.view'),
    submenu: [
      { role: 'reload', label: t('menu.reload') },
      { role: 'forceReload', label: t('menu.forceReload') },
      { role: 'toggleDevTools', label: t('menu.toggleDevTools') },
      { type: 'separator' },
      { role: 'resetZoom', label: t('menu.resetZoom') },
      { role: 'zoomIn', label: t('menu.zoomIn') },
      { role: 'zoomOut', label: t('menu.zoomOut') },
      { type: 'separator' },
      { role: 'togglefullscreen', label: t('menu.toggleFullScreen') },
    ],
  };

  const windowMenu: MenuItemConstructorOptions = {
    label: t('menu.window'),
    submenu: isMac
      ? [
          { role: 'minimize', label: t('menu.minimize') },
          { role: 'zoom', label: t('menu.zoom') },
          { type: 'separator' },
          { role: 'front', label: t('menu.front') },
        ]
      : [
          { role: 'minimize', label: t('menu.minimize') },
          { role: 'close', label: t('menu.close') },
        ],
  };

  const helpMenu: MenuItemConstructorOptions = {
    label: t('menu.help'),
    submenu: [
      {
        label: t('menu.checkUpdates'),
        // Opens Settings, where the Updates tab manages app updates. (The old
        // item pinged the upstream open-source repo, which is not meaningful
        // for this product.)
        click: () => navTo(getWin(), 'settings'),
      },
      { type: 'separator' },
      {
        label: t('menu.visitSite'),
        click: () => shell.openExternal(SITE_URL),
      },
      {
        label: t('menu.contactSupport'),
        click: () => shell.openExternal(`mailto:${SUPPORT_EMAIL}`),
      },
      { type: 'separator' },
      {
        label: t('menu.openSettings'),
        click: () => navTo(getWin(), 'settings'),
      },
    ],
  };

  const macAppMenu: MenuItemConstructorOptions = {
    label: app.name,
    submenu: [
      { role: 'about', label: t('menu.about', { app: app.name }) },
      { type: 'separator' },
      {
        label: t('menu.settingsEllipsis'),
        accelerator: 'Cmd+,',
        click: () => navTo(getWin(), 'settings'),
      },
      { type: 'separator' },
      { role: 'services', label: t('menu.services') },
      { type: 'separator' },
      { role: 'hide', label: t('menu.hide', { app: app.name }) },
      { role: 'hideOthers', label: t('menu.hideOthers') },
      { role: 'unhide', label: t('menu.unhide') },
      { type: 'separator' },
      { role: 'quit', label: t('menu.quitApp', { app: app.name }) },
    ],
  };

  const template: MenuItemConstructorOptions[] = isMac
    ? [macAppMenu, fileMenu, editMenu, navMenu, viewMenu, windowMenu, helpMenu]
    : [fileMenu, editMenu, navMenu, viewMenu, windowMenu, helpMenu];

  return Menu.buildFromTemplate(template);
}

let currentGetWin: (() => BrowserWindow | null) | null = null;

export function registerAppMenu(getWin: () => BrowserWindow | null): void {
  currentGetWin = getWin;
  Menu.setApplicationMenu(buildMenu(getWin, getLocale()));
}

/** Rebuild the native menu in `locale` (called over IPC when the user switches language). */
export function rebuildAppMenu(locale: Locale): void {
  if (!currentGetWin) return;
  Menu.setApplicationMenu(buildMenu(currentGetWin, locale));
}
