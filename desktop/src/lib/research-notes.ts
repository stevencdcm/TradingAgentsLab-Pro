/** Taiwan research disclosures shared by the screen, History replay and both exports. */
import type { DebateEvent, DexterResultEvent, EvidencePackEvent, SessionCompleteEvent } from './engine-client';

type T = (key: any, vars?: Record<string, string | number>) => string;

export interface ResearchInfo {
  evidence: EvidencePackEvent | null;
  dexter: DexterResultEvent | null;
  complete: SessionCompleteEvent | null;
}

/** `run.notice` messages the engine emitted (e.g. social analyst disabled for Taiwan, data purpose). */
export function findNotices(events: DebateEvent[]): string[] {
  return events
    .filter((e) => (e as { type: string }).type === 'run.notice')
    .map((e) => String((e as unknown as { message?: string }).message ?? ''))
    .filter(Boolean);
}

export function findResearch(events: DebateEvent[]): ResearchInfo {
  const evidence = events.find((e) => e.type === 'evidence.pack') as EvidencePackEvent | undefined;
  const dexter = events.find((e) => e.type === 'dexter.result') as DexterResultEvent | undefined;
  const complete = events.find((e) => e.type === 'session.complete') as SessionCompleteEvent | undefined;
  return { evidence: evidence ?? null, dexter: dexter ?? null, complete: complete ?? null };
}

/** Notes that must travel with every Taiwan result; empty for non-Taiwan runs. */
export function researchNotes(info: ResearchInfo, t: T, fmt: (n: number) => string = String): string[] {
  const c = info.complete;
  if (!c || c.market !== 'TW') return [];
  const notes: string[] = [];
  if (c.engine === 'simplified') notes.push(t('evidence.engineSimplified'));
  if (info.dexter) notes.push(t('evidence.notNumberChecked'));
  const u = c.usage_breakdown;
  if (u) {
    const d = (u.debate?.input_tokens ?? 0) + (u.debate?.output_tokens ?? 0);
    const x = (u.dexter?.input_tokens ?? 0) + (u.dexter?.output_tokens ?? 0);
    notes.push(t('evidence.usage', { debate: fmt(d), dexter: fmt(x) }));
  }
  if (c.usage_complete === false) notes.push(t('evidence.usageIncomplete'));
  if (c.cost_complete === false) notes.push(t('evidence.costIncomplete'));
  if (info.dexter?.summary_dropped) notes.push(t('evidence.summaryDropped'));
  return notes;
}

/** Fixed limitations block the engine wrote for a Taiwan full-graph run (not model text). */
export function findLimitations(info: ResearchInfo): Array<{ code: string; text: string }> {
  return info.complete?.limitations ?? [];
}

export interface SnapshotView {
  /** 'present' = manifest saved; 'missing' = a Taiwan full-graph report written before snapshots existed (never reconstructed); 'failed' = the run could not save one. */
  state: 'present' | 'missing' | 'failed';
  id?: string;
  calls?: number;
  sources: Array<{ id: string; ok: number; notOk: number }>;
  gaps: string[];
}

/** What the Taiwan run actually fed its analysts: the snapshot manifest, an explicit "none" for older reports, or null for non-Taiwan/other flows. */
export function findSnapshot(info: ResearchInfo): SnapshotView | null {
  const c = info.complete;
  if (!c || c.market !== 'TW' || (c as { tw_flow?: string }).tw_flow !== 'full') return null;
  const m = c.evidence_snapshot;
  if (!m) return { state: 'missing', sources: [], gaps: [] };
  if (m.status && m.status !== 'ok') return { state: 'failed', sources: [], gaps: [] };
  const sources = Object.entries(m.coverage?.sources ?? {}).map(([id, v]) => ({ id, ok: v.ok, notOk: v.not_ok }));
  return { state: 'present', id: m.snapshot_id, calls: m.calls, sources, gaps: m.coverage?.gaps ?? [] };
}

/** Markdown for the snapshot/coverage section (empty for non-Taiwan-full reports). */
export function snapshotMarkdown(info: ResearchInfo, t: T): string[] {
  const v = findSnapshot(info);
  if (!v) return [];
  if (v.state !== 'present') return [`## ${t('snapshot.title')}`, '', `- ${t(v.state === 'missing' ? 'snapshot.missing' : 'snapshot.failed')}`, ''];
  return [`## ${t('snapshot.title')}`, '', `- ${t('snapshot.id', { id: String(v.id).slice(0, 16), n: String(v.calls ?? 0) })}`,
    ...v.sources.map((s) => `- ${s.id}: ${s.ok} ok / ${s.notOk} ${t('snapshot.notOk')}`), ...v.gaps.map((g) => `- ${t('snapshot.gap')}: ${g}`), ''];
}

/** Markdown for the fixed "data limitations" section; empty when the run carried none. */
export function limitationsMarkdown(info: ResearchInfo, t: T): string[] {
  const lim = findLimitations(info);
  if (!lim.length) return [];
  return [`## ${t('limits.title')}`, '', ...lim.map((x) => `- ${x.text}`), ''];
}

/** Export section: research disclosures followed by the fixed limitations block. */
export function researchMarkdown(info: ResearchInfo, t: T, fmt: (n: number) => string = String, notices: string[] = []): string[] {
  return [...researchMarkdownCore(info, t, fmt, notices), ...limitationsMarkdown(info, t), ...snapshotMarkdown(info, t)];
}

/** Markdown section for the export: snapshot, gaps, Dexter status and every finding with its verdict. */
function researchMarkdownCore(info: ResearchInfo, t: T, fmt: (n: number) => string = String, notices: string[] = []): string[] {
  if (notices.length && !info.evidence && !info.dexter) return [...notices.map((n) => `> ${n}`), ''];
  if (!info.evidence && !info.dexter) return [];
  const lines: string[] = [];
  const ev = info.evidence;
  if (ev) {
    lines.push(`## ${t('evidence.title')}`, '');
    lines.push(`- ${t('evidence.snapshot', { id: ev.snapshot_id.slice(0, 12), asOf: ev.as_of.slice(0, 10) })} (${ev.security_id})`);
    if (ev.unknowns.length) {
      lines.push(`- **${t('evidence.unknownsHeading')}**`);
      for (const u of ev.unknowns) lines.push(`  - ${u}`);
    }
    lines.push('');
  }
  const dx = info.dexter;
  if (dx) {
    lines.push(`## ${t('dexter.title')}`, '');
    const bits = [dx.ran === false ? t('dexter.notRun') : '', t(`dexter.status.${dx.status}` as const), dx.reason ?? ''];
    if (dx.usage?.measured) bits.push(t('dexter.tokens', { n: fmt(dx.usage.total_tokens ?? 0) }));
    lines.push(bits.filter(Boolean).join(' · '), '');
    if (dx.findings.length === 0) lines.push(`_${t('dexter.noFindings')}_`, '');
    for (const f of dx.findings) {
      const cites = f.citations
        .map((c) => `[${c.kind} ${c.period}${c.entity ?? c.field ? ` ${c.entity ?? c.field}` : ''}]`)
        .join(' ');
      const flags = [
        f.verification === 'unverified' ? `${t('dexter.unverified')}${f.reason ? `: ${f.reason}` : ''}` : '',
        f.verification === 'verified' && f.point_in_time === 'unknown' ? t('dexter.pitUnknown') : '',
      ].filter(Boolean);
      lines.push(`- ${f.verification === 'verified' ? '✓' : '✗'} ${f.claim} ${cites}${flags.length ? ` — _${flags.join(' · ')}_` : ''}`);
    }
    lines.push('');
  }
  const notes = researchNotes(info, t, fmt);
  if (notes.length) {
    for (const n of notes) lines.push(`> ${n}`);
    lines.push('');
  }
  return lines;
}
