/**
 * "Export PDF" for a finished (or partial) report. Strictly on demand: nothing is built, imported or sent until the
 * user clicks. The caller supplies `build()` (the same lazy HTML builder the "open HTML" action uses, so content, cost
 * report, evidence and disclaimers are identical); the bridge then has the local engine render and download the PDF.
 * No model is called and no token is spent. Hidden where the bridge has no PDF support (the Electron preload).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import styles from './ReportPdfButton.module.css';
import { useI18n } from '../i18n';

type Phase = 'idle' | 'working' | 'done' | 'error';

interface Props {
  build: () => Promise<{ html: string; suggestedName: string }>;
  className?: string;
  disabled?: boolean;
  testId?: string;
}

const ERR_KEYS = {
  pdf_busy: 'pdf.err.busy',
  pdf_unavailable: 'pdf.err.unavailable',
  pdf_timeout: 'pdf.err.timeout',
  pdf_too_large: 'pdf.err.tooLarge',
} as const;

export function pdfSupported(): boolean {
  return typeof window !== 'undefined' && typeof window.tradingAgentsLab?.transcript?.savePdf === 'function';
}

function ReportPdfButton({ build, className, disabled, testId = 'export-pdf' }: Props) {
  const { t } = useI18n();
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const onClick = useCallback(async () => {
    if (busyRef.current) return; // double-click / double-tap guard: one export at a time
    busyRef.current = true;
    const ac = new AbortController();
    abortRef.current = ac;
    setPhase('working');
    setError(null);
    try {
      const { html, suggestedName } = await build();
      if (ac.signal.aborted) return;
      await window.tradingAgentsLab.transcript.savePdf!(html, suggestedName, ac.signal);
      if (aliveRef.current && !ac.signal.aborted) setPhase('done');
    } catch (err) {
      if (!aliveRef.current) return;
      if (ac.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
        setPhase('idle');
        return;
      }
      const code = (err as { code?: string }).code;
      const key = code && code in ERR_KEYS ? ERR_KEYS[code as keyof typeof ERR_KEYS] : null;
      setError(key ? t(key) : t('pdf.err.generic', { error: err instanceof Error ? err.message : t('status.unknown') }));
      setPhase('error');
    } finally {
      busyRef.current = false;
      abortRef.current = null;
    }
  }, [build, t]);

  const onCancel = useCallback(() => abortRef.current?.abort(), []);

  if (!pdfSupported()) return null;
  const working = phase === 'working';
  return (
    <>
      <button
        className={className}
        onClick={() => void onClick()}
        disabled={disabled || working}
        aria-busy={working}
        type="button"
        data-testid={testId}
      >
        {working ? t('pdf.working') : phase === 'error' ? t('pdf.retry') : t('pdf.button')}
      </button>
      {working && (
        <button className={`${className ?? ''} ${styles.cancel}`} onClick={onCancel} type="button" data-testid={`${testId}-cancel`}>
          {t('pdf.cancel')}
        </button>
      )}
      <span className={styles.status} role={phase === 'error' ? 'alert' : 'status'} data-testid={`${testId}-status`}>
        {phase === 'done' ? t('pdf.done') : phase === 'error' ? error : ''}
      </span>
    </>
  );
}

export default ReportPdfButton;
