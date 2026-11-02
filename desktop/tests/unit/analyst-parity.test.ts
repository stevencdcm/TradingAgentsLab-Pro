import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : /\.(ts|tsx)$/.test(p) ? [p] : [];
  });
}

describe('single-run and batch share one analyst/model assembly (D47)', () => {
  it('pro_config is built in exactly one place, and both the Analyze page and the batch runner go through runAnalysis', () => {
    const src = join(__dirname, '../../src');
    const users = walk(src).filter((p) => /assembleProConfig\(/.test(readFileSync(p, 'utf8')) && !p.endsWith('lib/pro-config.ts'));
    expect(users.map((p) => p.replace(src, ''))).toEqual(['/lib/run-analysis.ts']);
    for (const f of ['components/BatchRunner.tsx', 'pages/Analyze.tsx']) {
      expect(readFileSync(join(src, f), 'utf8')).toMatch(/runAnalysis\(/);
    }
  });
});
