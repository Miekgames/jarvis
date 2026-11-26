/**
 * Skill colours: the screen fades to the colour of the skill JARVIS is using.
 *
 * The backend says which skills are in use ("skills.active"): one for the
 * reply, if it uses a skill, and one for each long piece of work (research
 * keeps its orange until the report). The reply's skill shows first; without
 * one, the newest long task's; without either, the chosen theme -- blue and
 * violet for Arc Reactor -- exactly as before.
 *
 * Nothing here is saved: the chosen theme in Settings never changes.
 */

export interface ActiveSkill {
  skill: string;
  color: string;
  kind: 'reply' | 'task';
  id: string;
}

export interface Palette {
  accent: string;
  accent_secondary: string;
  particle_near: string;
  particle_far: string;
  backdrop: string;
}

export const FADE_MS = 1400;
/** A skill's colour stays at least this long, so a quick one still shows. */
export const MIN_SHOW_MS = 4000;

const ARC_REACTOR: Palette = {
  accent: '#5ad2ff', accent_secondary: '#7b6cff',
  particle_near: '#3890d9', particle_far: '#17336b', backdrop: '#071322',
};

/** The chosen theme's colours, from settings. */
export function basePalette(theme: Record<string, any>): Palette {
  return {
    accent: theme.accent ?? ARC_REACTOR.accent,
    accent_secondary: theme.accent_secondary ?? ARC_REACTOR.accent_secondary,
    particle_near: theme.particle_near ?? ARC_REACTOR.particle_near,
    particle_far: theme.particle_far ?? ARC_REACTOR.particle_far,
    backdrop: theme.backdrop ?? ARC_REACTOR.backdrop,
  };
}

/** A whole palette from one colour, shaded the way the built-in themes are. */
export function paletteFor(color: string): Palette {
  const [h, s, l] = hexToHsl(color);
  return {
    accent: color.toLowerCase(),
    accent_secondary: hslToHex((h + 334) % 360, s, Math.min(0.66, l)),   // a warmer neighbour
    particle_near: hslToHex(h, s * 0.8, l * 0.74),
    particle_far: hslToHex(h, s * 0.75, l * 0.27),
    backdrop: hslToHex(h, s * 0.6, 0.065),
  };
}

/** The skill the screen should show: the reply's, else the newest long task's. */
export function shownSkill(active: ActiveSkill[]): ActiveSkill | null {
  const newestFirst = [...active].reverse();
  return newestFirst.find((a) => a.kind === 'reply')
    ?? newestFirst.find((a) => a.kind === 'task')
    ?? null;
}

export function mixPalette(from: Palette, to: Palette, t: number): Palette {
  const out = {} as Palette;
  (Object.keys(to) as (keyof Palette)[]).forEach((key) => {
    out[key] = mixHex(from[key], to[key], t);
  });
  return out;
}

// -- the palette the screen is showing right now ---------------------------
// The fade writes here every frame; the particle field listens, so nothing
// re-renders through React sixty times a second.
let shown: Palette = ARC_REACTOR;
const listeners = new Set<(palette: Palette) => void>();

export const shownPalette = {
  get: (): Palette => shown,
  set(palette: Palette) {
    shown = palette;
    listeners.forEach((listener) => listener(palette));
  },
  subscribe(listener: (palette: Palette) => void): () => void {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
};

// -- colour arithmetic ------------------------------------------------------
function toRgb(hex: string): [number, number, number] {
  const clean = (hex || '').replace('#', '');
  const full = clean.length === 3 ? clean.split('').map((c) => c + c).join('') : clean;
  const value = parseInt(full, 16);
  if (Number.isNaN(value)) return [90, 210, 255];
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function toHex(r: number, g: number, b: number): string {
  const part = (n: number) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
  return `#${part(r)}${part(g)}${part(b)}`;
}

function mixHex(a: string, b: string, t: number): string {
  const [r1, g1, b1] = toRgb(a);
  const [r2, g2, b2] = toRgb(b);
  return toHex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t);
}

function hexToHsl(hex: string): [number, number, number] {
  const [r, g, b] = toRgb(hex).map((n) => n / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h *= 60;
  return [h, s, l];
}

function hslToHex(h: number, s: number, l: number): string {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return toHex((r + m) * 255, (g + m) * 255, (b + m) * 255);
}
