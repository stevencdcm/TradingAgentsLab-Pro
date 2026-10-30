import { useI18n } from '../i18n';
import { formatCostReport, type CostReport } from '../lib/cost-report';
import styles from './CostReportPanel.module.css';

interface Props {
  report: CostReport;
}

/** Token usage + official-API price estimate for a finished run, with per-agent and Dexter lines. Not an invoice. */
export default function CostReportPanel({ report }: Props) {
  const i18n = useI18n();
  const { t } = i18n;
  const f = formatCostReport(report, t, i18n.formatNumber);
  return (
    <section className={styles.panel} data-testid="cost-report">
      <h4 className={styles.title}>{f.title}</h4>
      <p className={styles.line}>{f.billingLine}</p>
      {f.totalLines.map((l) => (
        <p key={l} className={styles.total} data-testid="cost-report-total">
          {l}
        </p>
      ))}
      <p className={report.totals.complete ? styles.ok : styles.warn} data-testid="cost-report-status">
        {f.statusLine}
      </p>
      {f.reasons.length > 0 ? (
        <ul className={styles.reasons}>
          {f.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      {f.basisLine ? <p className={styles.warn}>{f.basisLine}</p> : null}
      {f.rows.length > 0 ? (
        <details className={styles.details}>
          <summary>{t('costReport.detail')}</summary>
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>{t('costReport.col.agent')}</th>
                  <th>{t('costReport.col.model')}</th>
                  <th>{t('costReport.col.input')}</th>
                  <th>{t('costReport.col.cached')}</th>
                  <th>{t('costReport.col.write')}</th>
                  <th>{t('costReport.col.output')}</th>
                  <th>{t('costReport.col.reasoning')}</th>
                  <th>{t('costReport.col.usd')}</th>
                </tr>
              </thead>
              <tbody>
                {f.rows.map((row, i) => (
                  <tr key={`${row.agent}-${row.model}-${i}`}>
                    <td>{row.agent}</td>
                    <td>{row.model}</td>
                    <td>{row.input}</td>
                    <td>{row.cached}</td>
                    <td>{row.write}</td>
                    <td>{row.output}</td>
                    <td>{row.reasoning}</td>
                    <td>{row.usd}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
      <p className={styles.source}>{f.sourceLine}</p>
      {f.notes.map((n) => (
        <p key={n} className={styles.source}>
          {n}
        </p>
      ))}
    </section>
  );
}
