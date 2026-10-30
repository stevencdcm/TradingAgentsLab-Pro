// Readability guard: every text/background pair that the UI really uses keeps WCAG AA contrast in BOTH themes,
// computed from the oklch tokens in styles/tokens.css (so a palette edit cannot silently bring back grey-on-grey).
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const css = readFileSync(new URL('../../src/styles/tokens.css', import.meta.url), 'utf8');

function block(selector: RegExp): Record<string, string> {
  const m = selector.exec(css);
  if (!m) throw new Error('block not found');
  const body = m[1];
  const out: Record<string, string> = {};
  for (const d of body.matchAll(/(--tal-[a-z-]+):\s*([^;]+);/g)) out[d[1]] = d[2].trim();
  return out;
}
const dark = block(/^:root \{([\s\S]*?)\n\}/m);
const light = { ...dark, ...block(/:root\[data-theme="light"\] \{([\s\S]*?)\n\}/) };

function oklchToRgb(v: string): [number, number, number] {
  const m = /oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(v);
  if (!m) throw new Error(`not oklch: ${v}`);
  const [L, C, h] = [Number(m[1]), Number(m[2]), (Number(m[3]) * Math.PI) / 180];
  const a = C * Math.cos(h), b = C * Math.sin(h);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b, m_ = L - 0.1055613458 * a - 0.0638541728 * b, s_ = L - 0.0894841775 * a - 1.291485548 * b;
  const l = l_ ** 3, mm = m_ ** 3, s = s_ ** 3;
  const lin = [4.0767416621 * l - 3.3077115913 * mm + 0.2309699292 * s, -1.2684380046 * l + 2.6097574011 * mm - 0.3413193965 * s, -0.0041960863 * l - 0.7034186147 * mm + 1.707614701 * s];
  return lin.map((c) => Math.min(1, Math.max(0, c))) as [number, number, number];
}
const lum = ([r, g, b]: [number, number, number]) => 0.2126 * r + 0.7152 * g + 0.0722 * b; // already linear
function ratio(fg: string, bg: string): number {
  const [a, b] = [lum(oklchToRgb(fg)), lum(oklchToRgb(bg))];
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

const SURFACES = ['--tal-bg-base', '--tal-bg-elevated', '--tal-bg-overlay', '--tal-bg-input'];
const TEXTS = ['--tal-text-primary', '--tal-text-secondary', '--tal-text-muted'];

describe.each([['dark', dark], ['light', light]] as const)('%s theme contrast', (_name, tk) => {
  it.each(TEXTS.flatMap((t) => SURFACES.map((s) => [t, s])))('%s on %s >= 4.5:1', (t, s) => {
    expect(ratio(tk[t], tk[s])).toBeGreaterThanOrEqual(4.5);
  });
  it('text on accent buttons and accent/danger/success/warning text on surfaces', () => {
    expect(ratio(tk['--tal-text-inverse'], tk['--tal-accent'])).toBeGreaterThanOrEqual(4.5);
    for (const k of ['--tal-accent-strong', '--tal-danger', '--tal-success', '--tal-warning']) {
      expect(ratio(tk[k], tk['--tal-bg-elevated']), `${k} on elevated`).toBeGreaterThanOrEqual(4.5);
    }
  });
});
