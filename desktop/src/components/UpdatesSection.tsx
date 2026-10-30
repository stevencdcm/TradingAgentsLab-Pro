import { useCallback, useEffect, useState } from 'react';
import { useI18n, type I18n } from '../i18n';
import styles from './UpdatesSection.module.css';

/**
 * Auto-update controls (Phase 7c.4): a toggle for the automatic check (default
 * on), a manual "Check for updates" button, and a live status line. The actual
 * update flow only works in a packaged build (and once releases are published);
 * in dev `supported` is false and the control explains that.
 */

// Local shapes, structurally compatible with the (module-scoped) bridge types
// in vite-env.d.ts. Components can't name those directly, so we mirror them.
interface UpdatesState {
  autoUpdate: boolean;
  currentVersion: string;
  supported: boolean;
}
interface UpdateStatus {
  state:
    | 'idle'
    | 'checking'
    | 'available'
    | 'not-available'
    | 'downloading'
    | 'downloaded'
    | 'error'
    | 'dev';
  version?: string;
  percent?: number;
  message?: string;
}

function statusLabel(status: UpdateStatus | null, supported: boolean, t: I18n['t']): string {
  if (!supported) return t('updates.status.devBuild');
  switch (status?.state) {
    case 'checking':
      return t('updates.status.checking');
    case 'available':
      return status.version
        ? t('updates.status.availableVersion', { version: status.version })
        : t('updates.status.available');
    case 'downloading':
      return t('updates.status.downloading', { percent: status.percent ?? 0 });
    case 'downloaded':
      return status.version
        ? t('updates.status.downloadedVersion', { version: status.version })
        : t('updates.status.downloaded');
    case 'not-available':
      return t('updates.status.latest');
    case 'error':
      // status.message is the updater's own text (English); shown verbatim.
      return status.message
        ? t('updates.status.errorDetail', { message: status.message })
        : t('updates.status.error');
    case 'dev':
      return t('updates.status.devBuild');
    default:
      return '';
  }
}

export default function UpdatesSection() {
  const { t } = useI18n();
  const [state, setState] = useState<UpdatesState | null>(null);
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    window.tradingAgentsLab.updates
      .getState()
      .then((s) => {
        if (!cancelled) setState(s);
      })
      .catch(() => {
        /* leave state null; toggle stays at the default-checked display */
      });
    const unsub = window.tradingAgentsLab.updates.onStatus((s) => setStatus(s));
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  const onToggle = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const enabled = e.target.checked;
      await window.tradingAgentsLab.updates.setAuto(enabled);
      setState((prev) => (prev ? { ...prev, autoUpdate: enabled } : prev));
    },
    [],
  );

  const onCheck = useCallback(async () => {
    setBusy(true);
    try {
      await window.tradingAgentsLab.updates.check();
    } finally {
      setBusy(false);
    }
  }, []);

  const onInstall = useCallback(() => {
    // Explicit, reliable install. The in-app Restart / Shut down do NOT apply an
    // update; this calls quitAndInstall in main.
    void window.tradingAgentsLab.updates.install();
  }, []);

  const supported = state?.supported ?? false;
  const downloaded = status?.state === 'downloaded';
  const label = statusLabel(status, supported, t);

  return (
    <div className={styles.section}>
      <label className={styles.toggleRow}>
        <input
          type="checkbox"
          checked={state?.autoUpdate ?? true}
          onChange={onToggle}
          disabled={!state}
        />
        <span>{t('updates.auto')}</span>
      </label>
      <p className={styles.hint}>
        {t('updates.hint')}
      </p>
      <div className={styles.actions}>
        {downloaded ? (
          <button type="button" onClick={onInstall} className={styles.installBtn}>
            {status?.version
              ? t('updates.installVersion', { version: status.version })
              : t('updates.install')}
          </button>
        ) : (
          <button
            type="button"
            onClick={onCheck}
            disabled={busy || !supported}
            className={styles.checkBtn}
          >
            {busy ? t('updates.checking') : t('updates.check')}
          </button>
        )}
        {label && <span className={styles.status}>{label}</span>}
      </div>
    </div>
  );
}
