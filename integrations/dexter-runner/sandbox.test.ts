// REAL Dexter read_file tool against the per-job sandbox (no model involved):
// path escape, absolute paths and symlinks must be refused; the pack itself must be readable.
import { expect, test } from 'bun:test';
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const ROOT = process.env.DEXTER_ROOT ?? '/Users/steven/.local/share/her30/dexter';
const { readFileTool } = await import(path.join(ROOT, 'src/tools/filesystem/read-file.ts'));

function setup() {
  const base = mkdtempSync(path.join(tmpdir(), 'dexter-sbx-'));
  const job = path.join(base, 'job');
  mkdirSync(path.join(job, 'evidence'), { recursive: true });
  writeFileSync(path.join(job, 'evidence', 'pack.json'), '{"ok":true}');
  writeFileSync(path.join(base, 'secret.txt'), 'TOP-SECRET');
  symlinkSync(path.join(base, 'secret.txt'), path.join(job, 'evidence', 'link.txt'));
  symlinkSync(base, path.join(job, 'dirlink'));
  return { base, job };
}

async function read(job: string, p: string): Promise<string> {
  const prev = process.cwd();
  process.chdir(job);
  try {
    return String(await readFileTool.invoke({ path: p }));
  } catch (e) {
    return `ERROR ${(e as Error).message}`;
  } finally {
    process.chdir(prev);
  }
}

test('the evidence pack is readable', async () => {
  const { job } = setup();
  expect(await read(job, 'evidence/pack.json')).toContain('content');
});

test('absolute path outside the job dir is refused', async () => {
  const { job, base } = setup();
  const r = await read(job, path.join(base, 'secret.txt'));
  expect(r).not.toContain('TOP-SECRET');
});

test('.. escape is refused', async () => {
  const { job } = setup();
  const r = await read(job, '../secret.txt');
  expect(r).not.toContain('TOP-SECRET');
});

test('a symlink inside the job dir (file or directory) is refused', async () => {
  const { job } = setup();
  expect(await read(job, 'evidence/link.txt')).not.toContain('TOP-SECRET');
  expect(await read(job, 'dirlink/secret.txt')).not.toContain('TOP-SECRET');
});
