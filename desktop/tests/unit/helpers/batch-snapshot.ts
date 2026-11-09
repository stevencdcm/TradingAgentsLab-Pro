// Test fixtures for the server-side batch API (D56 BatchSnapshot). Pure data, no engine.
import type { BatchItem, BatchItemState, BatchSnapshot } from '../../../src/lib/engine-client';

export function item(index: number, ticker: string, state: BatchItemState, extra: Partial<BatchItem> = {}): BatchItem {
  return {
    index, ticker, state, run_id: null, session_id: null, stance: null, conviction: null, estimated_cost_usd: null, cost_known: null,
    cost_partial: false, elapsed_s: null, started_at: null, ended_at: null, error_code: null, error: null, ...extra,
  };
}

export function snapshot(items: BatchItem[], extra: Partial<BatchSnapshot> = {}): BatchSnapshot {
  const counts: BatchSnapshot['counts'] = { pending: 0, waiting_slot: 0, running: 0, completed: 0, failed: 0, cancelled: 0, skipped: 0, interrupted: 0 };
  for (const i of items) counts[i.state] += 1;
  const cur = items.find((i) => i.state === 'running' || i.state === 'waiting_slot');
  const active = counts.pending + counts.waiting_slot + counts.running > 0;
  return {
    batch_id: 'b_1', state: active ? 'running' : 'completed', active, cancel_requested: false, terminal_code: null, error: null,
    provider: 'anthropic', model: 'claude-sonnet-4-6', auth_kind: 'api_key', engine: 'pro', trade_date: '2026-11-09', report_language: 'zh-TW',
    created_at: 1_000, started_at: 1_001, ended_at: active ? null : 2_000, total: items.length, counts,
    current_index: cur ? cur.index : null,
    cost: { known_usd: 0, unknown_items: 0, partial: false },
    items,
    current_run: cur
      ? { run_id: 'run_1', phase: 'debate', steps_done: 3, elapsed_s: 42, since_last_event_s: 5, last_event_type: 'agent.message', cancelling: false }
      : null,
    ...extra,
  };
}
