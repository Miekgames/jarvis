import { useMemo } from 'react';

import { useStore } from '../state/store';

/**
 * The HUD.
 *
 * The screen furniture that makes this read as JARVIS rather than as a web app:
 * concentric rings around the core, segmented block arcs, radial spokes, a
 * hexagonal inner frame, bezel ticks, corner brackets and edge ladders. Vector,
 * not bitmap -- crisp at any resolution, and next to nothing for the GPU
 * compared with the particle field, which keeps flowing behind all of it.
 *
 * Two rules it holds to:
 *
 *   * **Nothing here is decoration pretending to be data.** Every arc that
 *     represents a value is driven by a real number from the store, and an
 *     unmeasured value draws an empty track rather than a plausible position.
 *     The ornamental rings are obviously ornamental: constant rate, no units.
 *   * **It reacts to state.** Ring speed, sweep and tone follow the same eight
 *     states as the field, so the furniture and the particles always agree
 *     about what JARVIS is doing.
 */

const VIEW = 1000;
const HALF = VIEW / 2;

interface RingSpec {
  radius: number;
  /** Seconds per revolution. Negative runs anticlockwise. */
  period: number;
  dash: string;
  width: number;
  opacity: number;
  /** Fraction of the circle actually drawn, for open arcs. */
  arc?: number;
}

// Layered deliberately: fine hairlines, then dashed mid rings, then heavy
// segmented blocks at the outside. Density is what sells the aesthetic.
const RINGS: RingSpec[] = [
  { radius: 96, period: 150, dash: '1 6', width: 1, opacity: 0.4 },
  { radius: 118, period: -54, dash: '26 10', width: 1.5, opacity: 0.7, arc: 0.72 },
  { radius: 134, period: 88, dash: '2 9', width: 1, opacity: 0.32 },
  { radius: 158, period: -34, dash: '54 18', width: 3.5, opacity: 0.8, arc: 0.55 },
  { radius: 178, period: 120, dash: '1 5', width: 1, opacity: 0.3 },
  { radius: 196, period: -72, dash: '90 22', width: 2, opacity: 0.55, arc: 0.84 },
  { radius: 214, period: 46, dash: '6 14', width: 1.5, opacity: 0.45 },
  { radius: 238, period: -190, dash: '140 30', width: 5, opacity: 0.42, arc: 0.68 },
  { radius: 262, period: 260, dash: '2 12', width: 1, opacity: 0.28 },
  { radius: 330, period: -320, dash: '180 60', width: 1.5, opacity: 0.3, arc: 0.9 },
];

const STATE_STYLE: Record<string, { speed: number; glow: number; sweep: boolean; tone: string }> = {
  idle: { speed: 1, glow: 0.7, sweep: false, tone: 'var(--accent)' },
  listening: { speed: 1.9, glow: 1.15, sweep: true, tone: 'var(--accent)' },
  thinking: { speed: 3.1, glow: 1, sweep: true, tone: 'var(--secondary)' },
  speaking: { speed: 1.5, glow: 1.15, sweep: false, tone: 'var(--accent)' },
  working: { speed: 2.4, glow: 0.95, sweep: true, tone: 'var(--secondary)' },
  error: { speed: 0.5, glow: 1.1, sweep: false, tone: 'var(--danger)' },
  offline: { speed: 0.25, glow: 0.3, sweep: false, tone: 'var(--ink-faint)' },
  sleep: { speed: 0.1, glow: 0.1, sweep: false, tone: 'var(--ink-ghost)' },
};

function arcPath(radius: number, fraction: number): string {
  if (fraction >= 1) {
    return `M ${-radius} 0 a ${radius} ${radius} 0 1 1 ${radius * 2} 0`
         + ` a ${radius} ${radius} 0 1 1 ${-radius * 2} 0`;
  }
  const start = -Math.PI / 2;
  const end = fraction * Math.PI * 2 - Math.PI / 2;
  const large = fraction > 0.5 ? 1 : 0;
  return `M ${(Math.cos(start) * radius).toFixed(2)} ${(Math.sin(start) * radius).toFixed(2)}`
       + ` A ${radius} ${radius} 0 ${large} 1`
       + ` ${(Math.cos(end) * radius).toFixed(2)} ${(Math.sin(end) * radius).toFixed(2)}`;
}

function segment(radius: number, from: number, to: number): string {
  const a0 = from * Math.PI * 2 - Math.PI / 2;
  const a1 = to * Math.PI * 2 - Math.PI / 2;
  const large = to - from > 0.5 ? 1 : 0;
  return `M ${(Math.cos(a0) * radius).toFixed(2)} ${(Math.sin(a0) * radius).toFixed(2)}`
       + ` A ${radius} ${radius} 0 ${large} 1`
       + ` ${(Math.cos(a1) * radius).toFixed(2)} ${(Math.sin(a1) * radius).toFixed(2)}`;
}

function ticks(radius: number, count: number, length: number, every = 5) {
  return Array.from({ length: count }, (_, i) => {
    const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
    const major = i % every === 0;
    const inner = radius - (major ? length * 2.1 : length);
    return {
      x1: Math.cos(angle) * inner, y1: Math.sin(angle) * inner,
      x2: Math.cos(angle) * radius, y2: Math.sin(angle) * radius, major,
    };
  });
}

/** Regular polygon path — the hexagonal inner frame. */
function polygon(radius: number, sides: number, rotation = 0): string {
  return Array.from({ length: sides }, (_, i) => {
    const angle = (i / sides) * Math.PI * 2 - Math.PI / 2 + rotation;
    return `${(Math.cos(angle) * radius).toFixed(2)},${(Math.sin(angle) * radius).toFixed(2)}`;
  }).join(' ');
}

export function HudFrame() {
  const state = useStore((s) => s.state);
  const metrics = useStore((s) => s.metrics);
  const amplitude = useStore((s) => s.amplitude);
  const confidence = useStore((s) => s.confidence);
  const connected = useStore((s) => s.connected);
  const tasks = useStore((s) => s.tasks);
  const agents = useStore((s) => s.agents);
  const motion = useStore((s) => s.settings?.appearance?.motion ?? 'full');

  const style = STATE_STYLE[state] ?? STATE_STYLE.idle;
  const still = motion === 'still';
  const calm = motion === 'reduced';
  const speed = still ? 0 : style.speed * (calm ? 0.4 : 1);

  const running = tasks.filter((t) => t.status === 'running').length;

  // Gauges are measured values only. `null` renders an empty track, because
  // "not measured" and "measured as zero" must not look the same.
  const gauges = [
    { label: 'CPU', value: metrics.cpu ?? null, radius: 290, from: 0.545, to: 0.70 },
    { label: 'MEM', value: metrics.mem ?? null, radius: 290, from: 0.80, to: 0.955 },
    { label: 'DSK', value: metrics.disk ?? null, radius: 308, from: 0.045, to: 0.20 },
    { label: 'CNF', value: connected ? confidence * 100 : null,
      radius: 308, from: 0.295, to: 0.45 },
  ];

  const bezel = useMemo(() => ticks(276, 96, 6), []);
  const inner = useMemo(() => ticks(84, 48, 4, 4), []);
  // Radial spokes at 8 points, drawn as gaps between the ring bands.
  const spokes = useMemo(() => Array.from({ length: 8 }, (_, i) => {
    const angle = (i / 8) * Math.PI * 2 - Math.PI / 2;
    return {
      x1: Math.cos(angle) * 150, y1: Math.sin(angle) * 150,
      x2: Math.cos(angle) * 250, y2: Math.sin(angle) * 250,
    };
  }), []);

  return (
    <div className={`hud hud--${state} ${still ? 'hud--still' : ''}`} aria-hidden="true">
      {/* -------------------------------------------------- corner brackets */}
      <svg className="hud__corners" viewBox="0 0 100 100" preserveAspectRatio="none">
        <path d="M 0.5 9 L 0.5 0.5 L 9 0.5" />
        <path d="M 91 0.5 L 99.5 0.5 L 99.5 9" />
        <path d="M 99.5 91 L 99.5 99.5 L 91 99.5" />
        <path d="M 9 99.5 L 0.5 99.5 L 0.5 91" />
        <path className="hud__corners-inner" d="M 2.2 13 L 2.2 2.2 L 13 2.2" />
        <path className="hud__corners-inner" d="M 87 97.8 L 97.8 97.8 L 97.8 87" />
      </svg>

      {/* ------------------------------------------------------ the reactor */}
      <svg
        className="hud__core"
        viewBox={`${-HALF} ${-HALF} ${VIEW} ${VIEW}`}
        style={{ ['--glow' as string]: style.glow, ['--tone' as string]: style.tone }}
      >
        <defs>
          <radialGradient id="hud-core-glow">
            <stop offset="0%" stopColor="var(--tone)" stopOpacity="0.32" />
            <stop offset="45%" stopColor="var(--tone)" stopOpacity="0.10" />
            <stop offset="100%" stopColor="var(--tone)" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="hud-sweep" x1="0" y1="0" x2="1" y2="0.35">
            <stop offset="0%" stopColor="var(--tone)" stopOpacity="0" />
            <stop offset="100%" stopColor="var(--tone)" stopOpacity="0.42" />
          </linearGradient>
        </defs>

        <circle r="300" fill="url(#hud-core-glow)" className="hud__halo" />

        {/* Faint concentric guides — the graph-paper under everything else. */}
        <g className="hud__guides">
          {[60, 110, 170, 230, 290, 350].map((r) => (
            <circle key={r} r={r} />
          ))}
        </g>

        {/* Ornamental rings: constant rate, never labelled. */}
        <g className="hud__rings">
          {RINGS.map((ring, index) => (
            <path
              key={index}
              d={arcPath(ring.radius, ring.arc ?? 1)}
              className="hud__ring"
              style={{
                strokeDasharray: ring.dash,
                strokeWidth: ring.width,
                opacity: ring.opacity * (still ? 0.6 : 1),
                animationDuration: speed ? `${Math.abs(ring.period) / speed}s` : '0s',
                animationDirection: ring.period < 0 ? 'reverse' : 'normal',
                animationPlayState: speed ? 'running' : 'paused',
              }}
            />
          ))}
        </g>

        {/* Hexagonal inner frame — the reactor housing. */}
        <polygon points={polygon(70, 6)} className="hud__hex" />
        <polygon points={polygon(52, 6, Math.PI / 6)} className="hud__hex hud__hex--inner" />

        {/* Radial spokes. */}
        <g className="hud__spokes">
          {spokes.map((spoke, index) => (
            <line key={index} {...spoke} />
          ))}
        </g>

        {/* Bezels. */}
        <g className="hud__ticks">
          {bezel.map((mark, index) => (
            <line key={index} x1={mark.x1} y1={mark.y1} x2={mark.x2} y2={mark.y2}
                  className={mark.major ? 'hud__tick hud__tick--major' : 'hud__tick'} />
          ))}
        </g>
        <g className="hud__ticks hud__ticks--inner">
          {inner.map((mark, index) => (
            <line key={index} x1={mark.x1} y1={mark.y1} x2={mark.x2} y2={mark.y2}
                  className={mark.major ? 'hud__tick hud__tick--major' : 'hud__tick'} />
          ))}
        </g>

        {/* Cardinal reticle marks outside the bezel. */}
        <g className="hud__reticle">
          {[0, 0.25, 0.5, 0.75].map((fraction) => {
            const angle = fraction * Math.PI * 2 - Math.PI / 2;
            return (
              <g key={fraction}>
                <line
                  x1={Math.cos(angle) * 352} y1={Math.sin(angle) * 352}
                  x2={Math.cos(angle) * 380} y2={Math.sin(angle) * 380}
                />
                <circle cx={Math.cos(angle) * 390} cy={Math.sin(angle) * 390} r="2.5" />
              </g>
            );
          })}
        </g>

        {/* Live gauges. */}
        {gauges.map((gauge) => <Gauge key={gauge.label} {...gauge} />)}

        {/* Agent pips: one per running agent, placed around the bezel. */}
        {agents.slice(0, 6).map((agent, index) => {
          const angle = (index / 6) * Math.PI * 2 - Math.PI / 2;
          return (
            <circle
              key={agent.id}
              cx={Math.cos(angle) * 258} cy={Math.sin(angle) * 258}
              r={3 + agent.activity * 3}
              fill={agent.colour}
              className="hud__agent"
            />
          );
        })}

        {/* Sweep, only while JARVIS is actually doing something. */}
        {style.sweep && !still && (
          <g className="hud__sweep"
             style={{ animationDuration: `${3.2 / Math.max(0.2, speed)}s` }}>
            <path d={`M 0 0 L 0 -246 A 246 246 0 0 1 ${(246 * Math.sin(0.6)).toFixed(1)} `
                   + `${(-246 * Math.cos(0.6)).toFixed(1)} Z`} fill="url(#hud-sweep)" />
          </g>
        )}

        {/* The core. Breathes with real audio amplitude while speaking. */}
        <circle r={44 + amplitude * 20} className="hud__iris" />
        <circle r={22 + amplitude * 14} className="hud__pupil" />
      </svg>

      {/* --------------------------------------------------- edge telemetry */}
      {(['left', 'right'] as const).map((side) => (
        <div key={side} className={`hud__ladder hud__ladder--${side}`}>
          {Array.from({ length: 30 }).map((_, index) => (
            <span key={index}
                  className={`hud__rung ${index % 5 === 0 ? 'hud__rung--major' : ''}`} />
          ))}
        </div>
      ))}

      {running > 0 && (
        <div className="hud__badge">
          <span className="hud__badge-dot" />
          {running} RUNNING
        </div>
      )}
    </div>
  );
}

/** An arc gauge bound to one measured value. */
function Gauge({ label, value, radius, from, to }: {
  label: string; value: number | null; radius: number; from: number; to: number;
}) {
  const span = to - from;
  const fill = value === null
    ? ''
    : segment(radius, from, from + span * Math.max(0, Math.min(1, value / 100)));
  const mid = (from + span / 2) * Math.PI * 2 - Math.PI / 2;

  return (
    <g className="hud__gauge">
      <path d={segment(radius, from, to)} className="hud__gauge-track" />
      {fill && <path d={fill} className="hud__gauge-fill" />}
      <text className="hud__gauge-label"
            x={Math.cos(mid) * (radius + 24)} y={Math.sin(mid) * (radius + 24)}>
        {label} {value === null ? '--' : Math.round(value)}
      </text>
    </g>
  );
}
