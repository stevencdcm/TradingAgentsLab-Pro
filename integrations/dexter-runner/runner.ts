// Dexter runner: drives the REAL Dexter agent loop (Agent.create / agent.run) in a locked-down,
// per-job sandbox and prints ONE JSON result line on stdout. No server, no daemon.
//   bun run runner.ts   (request JSON on stdin; env DEXTER_ROOT = Dexter checkout, pinned ec1b64c)
import { copyFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { classify, MAX_ITER_MESSAGE, parseAnswerJson, summaryIsGrounded, SYSTEM_PROMPT, validateRequest, verifyFindings, type EvidencePack } from './lib.ts';

const out = (o: unknown) => process.stdout.write(JSON.stringify(o) + '\n');

async function main() {
  const startedAt = new Date().toISOString();
  const t0 = Date.now();
  const rawIn = await new Response(Bun.stdin.stream()).text();
  let parsed: unknown;
  try { parsed = JSON.parse(rawIn.split('\n')[0]); } catch { out(fail(null, 'bad_request', 'stdin is not JSON', startedAt, t0)); return; }
  const v = validateRequest(parsed);
  if (!v.ok) { out(fail(null, 'bad_request', v.error, startedAt, t0)); return; }
  const { req, limits } = v;

  const base = (extra: Record<string, unknown>) => ({
    schema: 'her30.dexter.result/1', job_id: req.job_id, attempt_id: req.attempt_id,
    started_at: startedAt, ended_at: new Date().toISOString(), elapsed_ms: Date.now() - t0,
    model: req.model, findings: [], unknowns: [], answer_markdown: '', tool_calls: [],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0, reasoning_tokens: null, cached_tokens: null, measured: false, provider_reported: false },
    ...extra,
  });

  // No Financial-Datasets key / US market: the native Dexter finance tools are not bound at all,
  // and the evidence-only path has no pack for a US ticker => unsupported (not a silent web search).
  if (!existsSync(req.evidence_pack_path)) {
    out(base({ status: 'unsupported', reason: 'no_data_source', unknowns: ['no evidence pack for this security'] }));
    return;
  }
  let pack: EvidencePack;
  try { pack = JSON.parse(readFileSync(req.evidence_pack_path, 'utf-8')); } catch { out(base({ status: 'failed', reason: 'bad_evidence_pack' })); return; }
  if (pack.schema !== 'her30.evidence/1' || !Array.isArray(pack.items) || pack.items.length === 0) {
    out(base({ status: 'unsupported', reason: 'no_data_source', unknowns: ['evidence pack is empty or has the wrong schema'] }));
    return;
  }

  // Per-job working directory: the ONLY file inside is the pack. Dexter's `.dexter` state dir and
  // read_file sandbox root are both relative to cwd, so nothing outside this directory is reachable.
  const jobDir = path.resolve(process.env.TAL_DEXTER_JOB_DIR ?? '', '');
  if (!process.env.TAL_DEXTER_JOB_DIR) { out(base({ status: 'failed', reason: 'no_job_dir' })); return; }
  mkdirSync(path.join(jobDir, 'evidence'), { recursive: true });
  copyFileSync(req.evidence_pack_path, path.join(jobDir, 'evidence', 'pack.json'));
  process.chdir(jobDir);

  const root = process.env.DEXTER_ROOT;
  if (!root) { out(base({ status: 'failed', reason: 'no_dexter_root' })); return; }
  const { Agent } = await import(path.join(root, 'src/agent/agent.ts'));

  const ac = new AbortController();
  let aborted: 'timeout' | 'canceled' | 'tool_limit' | 'token_limit' | null = null;
  const timer = setTimeout(() => { aborted = 'timeout'; ac.abort(); }, limits.wall_seconds * 1000);
  process.on('SIGTERM', () => { aborted = 'canceled'; ac.abort(); });

  const toolCalls: Array<{ tool: string; ok: boolean; ms: number }> = [];
  const denied: string[] = [];
  let packRead = false;
  let done: { answer: string; iterations: number; tokenUsage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number } } | null = null;
  let errorMsg: string | null = null;

  try {
    const agent = await Agent.create({
      model: req.model.name,
      modelProvider: req.model.provider,
      maxIterations: limits.max_iterations,
      signal: ac.signal,
      channel: 'api',                 // drops CLI-only tools (ask_user_question, bash)
      memoryEnabled: false,
      toolAllowlist: ['read_file'],   // the ONLY tool that can be bound
      systemPromptOverride: SYSTEM_PROMPT(req.report_language),
      requestToolApproval: async (r: { tool: string }) => { denied.push(r.tool); return 'deny'; },
    });
    const starts = new Map<string, number>();
    for await (const ev of agent.run(`Security: ${req.security_id}\nAs of: ${req.as_of}\nTask: ${req.question}`)) {
      if (ev.type === 'tool_start') {
        starts.set(ev.tool, Date.now());
        if (toolCalls.length + 1 > limits.max_tool_calls) { aborted = 'tool_limit'; ac.abort(); break; }
      } else if (ev.type === 'tool_end') {
        toolCalls.push({ tool: ev.tool, ok: true, ms: Date.now() - (starts.get(ev.tool) ?? Date.now()) });
        const a = JSON.stringify(ev.args ?? {});
        if (ev.tool === 'read_file' && /pack\.json/.test(a)) packRead = true;
      } else if (ev.type === 'tool_error') {
        toolCalls.push({ tool: ev.tool, ok: false, ms: 0 });
      } else if (ev.type === 'done') {
        done = ev;
        const total = ev.tokenUsage?.totalTokens ?? null;
        if (total !== null && total > limits.max_total_tokens) aborted = 'token_limit';
      }
    }
  } catch (e) {
    errorMsg = e instanceof Error ? e.message : String(e);
  } finally {
    clearTimeout(timer);
  }

  if (errorMsg && !done && !aborted) { out(base({ status: 'failed', reason: 'provider_error', unknowns: [errorMsg.slice(0, 300)], tool_calls: toolCalls })); return; }

  const ans = done?.answer ?? '';
  // Dexter reports an LLM failure as a normal `done` whose answer starts with "Error:"; that is a
  // provider failure, never an (unparseable) answer.
  const tuAny = done?.tokenUsage;
  // Tokens already spent are kept on every path (a failed run still cost money).
  const usageOf = () => ({
    input_tokens: tuAny?.inputTokens ?? 0, output_tokens: tuAny?.outputTokens ?? 0, total_tokens: tuAny?.totalTokens ?? 0,
    reasoning_tokens: null, cached_tokens: null, measured: !!tuAny, provider_reported: !!tuAny,
  });
  if (done && !aborted && /^Error:/.test(ans)) {
    out(base({ status: 'failed', reason: 'provider_error', unknowns: [ans.slice(0, 300)], tool_calls: toolCalls, usage: usageOf() }));
    return;
  }
  // Only Dexter's own "max iterations" message means the loop was cut off; finishing on the last
  // allowed iteration with a real answer is a normal completion.
  const hitMax = !!done && (MAX_ITER_MESSAGE.test(ans) || done.iterations > limits.max_iterations);
  const parsedAns = done && !hitMax ? parseAnswerJson(ans) : null;
  const verified = parsedAns ? verifyFindings(parsedAns.findings, pack, req.as_of, packRead) : [];
  const nOk = verified.filter((f) => f.verification === 'verified').length;
  const tu = done?.tokenUsage;
  const grounded = !!parsedAns && nOk > 0 && summaryIsGrounded(parsedAns.answer_markdown, verified, pack);
  const cls = classify({ done: !!done, aborted, hitMaxIterations: hitMax, parseOk: !!parsedAns, verified: nOk, tokensTotal: tu?.totalTokens ?? null, limits });
  out(base({
    status: cls.status, reason: cls.reason,
    findings: verified, unknowns: [...(parsedAns?.unknowns ?? []), ...pack.unknowns.slice(0, 20)],
    answer_markdown: grounded ? parsedAns!.answer_markdown : '',
    ...(nOk > 0 && !grounded ? { summary_dropped: 'answer_markdown contains numbers that are not in the verified citations' } : {}),
    tool_calls: toolCalls, denied_tools: denied,
    usage: {
      input_tokens: tu?.inputTokens ?? 0, output_tokens: tu?.outputTokens ?? 0, total_tokens: tu?.totalTokens ?? 0,
      reasoning_tokens: null, cached_tokens: null, measured: !!tu, provider_reported: !!tu,
    },
  }));
}

function fail(req: { job_id?: string } | null, reason: string, msg: string, startedAt: string, t0: number) {
  return { schema: 'her30.dexter.result/1', job_id: req?.job_id ?? null, status: 'failed', reason, unknowns: [msg],
    findings: [], tool_calls: [], started_at: startedAt, ended_at: new Date().toISOString(), elapsed_ms: Date.now() - t0 };
}

main().then(() => process.exit(0)).catch((e) => { out({ schema: 'her30.dexter.result/1', status: 'failed', reason: 'runner_crash', unknowns: [String(e).slice(0, 300)] }); process.exit(0); });
