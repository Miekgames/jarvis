/**
 * The particle field renderer.
 *
 * WebGL2, four draw calls per frame regardless of particle count:
 *   1. fade quad into the trail buffer  (what makes the motion read as flow)
 *   2. connection lines
 *   3. energy rings
 *   4. particles
 * then one composite pass to the screen.
 *
 * Sleep genuinely stops the loop and releases the GL context — the canvas is
 * removed and nothing is rendered at all, rather than a black panel being
 * drawn over a still-running simulation.
 */

import {
  COMPOSITE_FRAG,
  FADE_FRAG,
  QUAD_VERT,
  RING_FRAG,
  RING_VERT,
} from './shaders';
import { FIELD_FRAG, FIELD_VERT, LINK_FRAG, LINK_VERT, MAX_AGENTS } from './shaders2';
import { qualityDecision } from './quality';

export type FieldState =
  | 'idle' | 'listening' | 'thinking' | 'speaking'
  | 'working' | 'error' | 'offline' | 'sleep';

/** Contextual mode — what kind of work the field is depicting. */
export type FieldContext = 'neutral' | 'research' | 'coding' | 'meeting' | 'focus';

export interface AgentCluster {
  id: string;
  colour: string;
  /** 0..1 — how active this agent is; drives its cluster's gravity. */
  activity: number;
}

interface StateProfile {
  energy: number;
  converge: number;
  orbit: number;
  dispersion: number;
  chaos: number;
  alpha: number;
  trail: number;
  sizeScale: number;
  /** ambient -> task migration */
  pullTask: number;
  /** task -> core migration */
  pullCore: number;
  /** core -> outward, used on completion */
  release: number;
  /** reasoning ripples radiating from the core */
  ripple: number;
  /** strength of directional drift toward active work */
  flow: number;
}

/** Target values per state. The renderer eases toward these every frame. */
const PROFILES: Record<FieldState, StateProfile> = {
  //          energy converge orbit  disp  chaos alpha trail size  pullTask pullCore release ripple flow
  idle:      { energy: 0.10, converge: 0.00, orbit: 0.00, dispersion: 0.00, chaos: 0.30, alpha: 1.00, trail: 0.16, sizeScale: 1.00, pullTask: 0.05, pullCore: 0.05, release: 0.00, ripple: 0.05, flow: 0.00 },
  listening: { energy: 0.22, converge: 0.85, orbit: 0.00, dispersion: 0.00, chaos: 0.16, alpha: 1.00, trail: 0.13, sizeScale: 1.10, pullTask: 0.45, pullCore: 0.55, release: 0.00, ripple: 0.20, flow: 0.10 },
  thinking:  { energy: 0.95, converge: 0.28, orbit: 0.18, dispersion: 0.00, chaos: 0.95, alpha: 1.00, trail: 0.09, sizeScale: 1.02, pullTask: 0.55, pullCore: 0.30, release: 0.00, ripple: 1.00, flow: 0.25 },
  speaking:  { energy: 0.34, converge: 0.42, orbit: 0.00, dispersion: 0.00, chaos: 0.34, alpha: 1.00, trail: 0.12, sizeScale: 1.08, pullTask: 0.25, pullCore: 0.45, release: 0.00, ripple: 0.35, flow: 0.10 },
  working:   { energy: 0.52, converge: 0.16, orbit: 0.92, dispersion: 0.00, chaos: 0.34, alpha: 1.00, trail: 0.10, sizeScale: 0.98, pullTask: 0.80, pullCore: 0.15, release: 0.00, ripple: 0.30, flow: 0.55 },
  error:     { energy: 0.30, converge: 0.00, orbit: 0.00, dispersion: 0.60, chaos: 0.75, alpha: 0.94, trail: 0.14, sizeScale: 1.04, pullTask: 0.10, pullCore: 0.00, release: 0.25, ripple: 0.10, flow: 0.00 },
  offline:   { energy: 0.03, converge: 0.00, orbit: 0.00, dispersion: 0.05, chaos: 0.12, alpha: 0.42, trail: 0.22, sizeScale: 0.86, pullTask: 0.00, pullCore: 0.00, release: 0.10, ripple: 0.00, flow: 0.00 },
  sleep:     { energy: 0.01, converge: 0.30, orbit: 0.00, dispersion: 0.00, chaos: 0.05, alpha: 0.00, trail: 0.30, sizeScale: 0.70, pullTask: 0.00, pullCore: 0.00, release: 0.00, ripple: 0.00, flow: 0.00 },
};

/** Contextual modes are eased independently of state, so "thinking about code"
 *  reads differently from "thinking about a meeting". */
const CONTEXT_TARGETS: Record<FieldContext, { research: number; coding: number; meeting: number; ambientFade: number }> = {
  neutral:  { research: 0, coding: 0, meeting: 0, ambientFade: 0.0 },
  research: { research: 1, coding: 0, meeting: 0, ambientFade: 0.1 },
  coding:   { research: 0, coding: 1, meeting: 0, ambientFade: 0.2 },
  meeting:  { research: 0, coding: 0, meeting: 1, ambientFade: 0.1 },
  focus:    { research: 0, coding: 0, meeting: 0, ambientFade: 0.9 },
};

export type Rgb = [number, number, number];

export interface FieldOptions {
  particleCount?: number;
  connections?: boolean;
  reducedMotion?: boolean;
  /** Core and highlight colour. */
  accent?: Rgb;
  /** Foreground particle colour. */
  colorNear?: Rgb;
  /** Receding-haze particle colour. */
  colorFar?: Rgb;
}

const ACCENT_DEFAULT: Rgb = [0.35, 0.82, 1.0];
const NEAR_DEFAULT: Rgb = [0.22, 0.55, 0.85];
const FAR_DEFAULT: Rgb = [0.09, 0.20, 0.42];

export class ParticleField {
  private canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext | null = null;
  private frame = 0;
  private running = false;
  private startTime = 0;
  private lastFrame = 0;

  private particleProgram: WebGLProgram | null = null;
  private lineProgram: WebGLProgram | null = null;
  private ringProgram: WebGLProgram | null = null;
  private fadeProgram: WebGLProgram | null = null;
  private compositeProgram: WebGLProgram | null = null;

  private particleVao: WebGLVertexArrayObject | null = null;
  private lineVao: WebGLVertexArrayObject | null = null;
  private ringVao: WebGLVertexArrayObject | null = null;
  private quadVao: WebGLVertexArrayObject | null = null;

  private framebuffer: WebGLFramebuffer | null = null;
  private sceneTexture: WebGLTexture | null = null;
  private buffers: WebGLBuffer[] = [];

  private particleCount = 0;
  private lineVertexCount = 0;
  private ringVertexCount = 0;
  private ringCount = 3;

  private options: Required<FieldOptions>;
  private pixelRatio = 1;

  // Eased state
  private current: StateProfile = { ...PROFILES.idle };
  private target: StateProfile = { ...PROFILES.idle };
  private state: FieldState = 'idle';
  private amplitude = 0;
  private amplitudeTarget = 0;
  private pointer: [number, number] = [0, 0];
  private pointerStrength = 0;
  private pointerTarget = 0;

  // Field dynamics
  private context: FieldContext = 'neutral';
  private contextCurrent = { research: 0, coding: 0, meeting: 0, ambientFade: 0 };
  private confidence = 0.8;
  private confidenceTarget = 0.8;
  private collapse = 0;
  private collapseTarget = 0;
  private flowDirection: [number, number] = [0, 0];
  private flowTarget: [number, number] = [0, 0];
  private bridge = 0;
  private bridgeTarget = 0;
  private agents: AgentCluster[] = [];
  private agentActivity = new Float32Array(MAX_AGENTS);
  private agentTargets = new Float32Array(MAX_AGENTS);
  private agentColours = new Float32Array(MAX_AGENTS * 3);
  private agentUniform = new Float32Array(MAX_AGENTS * 3);
  private morph = 0;
  private morphTarget = 0;

  private onFps?: (fps: number) => void;
  private fpsSamples: number[] = [];
  private degraded = false;
  /** Consecutive measurement windows below / above the thresholds. */
  private badWindows = 0;
  private goodWindows = 0;
  /** What to go back to. Null unless currently degraded. */
  private fullQuality: { particleCount: number; connections: boolean } | null = null;

  constructor(canvas: HTMLCanvasElement, options: FieldOptions = {}) {
    this.canvas = canvas;
    this.options = {
      particleCount: options.particleCount ?? 4200,
      connections: options.connections ?? true,
      reducedMotion: options.reducedMotion ?? false,
      accent: options.accent ?? ACCENT_DEFAULT,
      colorNear: options.colorNear ?? NEAR_DEFAULT,
      colorFar: options.colorFar ?? FAR_DEFAULT,
    };
  }

  // ======================================================================
  init(): boolean {
    const gl = this.canvas.getContext('webgl2', {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      powerPreference: 'high-performance',
      preserveDrawingBuffer: false,
    });
    if (!gl) return false;
    this.gl = gl;

    this.particleProgram = this.buildProgram(FIELD_VERT, FIELD_FRAG);
    this.lineProgram = this.buildProgram(LINK_VERT, LINK_FRAG);
    this.ringProgram = this.buildProgram(RING_VERT, RING_FRAG);
    this.fadeProgram = this.buildProgram(QUAD_VERT, FADE_FRAG);
    this.compositeProgram = this.buildProgram(QUAD_VERT, COMPOSITE_FRAG);
    if (!this.particleProgram || !this.lineProgram || !this.ringProgram ||
        !this.fadeProgram || !this.compositeProgram) {
      return false;
    }

    this.buildParticles();
    this.buildLines();
    this.buildRings();
    this.buildQuad();
    this.resize();

    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    return true;
  }

  // ----------------------------------------------------------------------
  private buildProgram(vertexSource: string, fragmentSource: string): WebGLProgram | null {
    const gl = this.gl!;
    const compile = (type: number, source: string) => {
      const shader = gl.createShader(type)!;
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error('[particles] shader compile failed:', gl.getShaderInfoLog(shader));
        gl.deleteShader(shader);
        return null;
      }
      return shader;
    };

    const vertex = compile(gl.VERTEX_SHADER, vertexSource);
    const fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
    if (!vertex || !fragment) return null;

    const program = gl.createProgram()!;
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);

    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error('[particles] program link failed:', gl.getProgramInfoLog(program));
      return null;
    }
    return program;
  }

  private createBuffer(data: Float32Array): WebGLBuffer {
    const gl = this.gl!;
    const buffer = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    this.buffers.push(buffer);
    return buffer;
  }

  private attribute(program: WebGLProgram, name: string, size: number, stride = 0, offset = 0) {
    const gl = this.gl!;
    const location = gl.getAttribLocation(program, name);
    if (location < 0) return;
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset);
  }

  // ----------------------------------------------------------------------
  private buildParticles() {
    const gl = this.gl!;
    const count = this.options.particleCount;
    this.particleCount = count;

    const seeds = new Float32Array(count * 4);
    const depths = new Float32Array(count);
    const layers = new Float32Array(count);
    const agents = new Float32Array(count);
    const shapes = new Float32Array(count);

    // Layer population: a wide ambient field, a working middle, a tight core.
    const AMBIENT = 0.62;
    const TASK = 0.30;

    for (let i = 0; i < count; i += 1) {
      const roll = Math.random();
      const layer = roll < AMBIENT ? 0 : roll < AMBIENT + TASK ? 1 : 2;
      layers[i] = layer;

      // Radius within the layer's band; sqrt keeps the distribution even by area.
      seeds[i * 4 + 0] = Math.random();
      seeds[i * 4 + 1] = Math.sqrt(Math.random());
      seeds[i * 4 + 2] = Math.random();
      seeds[i * 4 + 3] = Math.random();
      depths[i] = Math.random();

      // A slice of the task layer is reserved for agent clusters, assigned
      // round-robin so every active agent gets a visible body of particles.
      agents[i] = layer === 1 && Math.random() < 0.42
        ? Math.floor(Math.random() * MAX_AGENTS)
        : -1;

      // Shape band drives contextual morphing in the fragment shader.
      shapes[i] = Math.random();
    }

    this.particleVao = gl.createVertexArray();
    gl.bindVertexArray(this.particleVao);
    this.createBuffer(seeds);
    this.attribute(this.particleProgram!, 'aSeed', 4);
    this.createBuffer(depths);
    this.attribute(this.particleProgram!, 'aDepth', 1);
    this.createBuffer(layers);
    this.attribute(this.particleProgram!, 'aLayer', 1);
    this.createBuffer(agents);
    this.attribute(this.particleProgram!, 'aAgent', 1);
    this.createBuffer(shapes);
    this.attribute(this.particleProgram!, 'aShape', 1);
    gl.bindVertexArray(null);

    this.particleSeeds = seeds;
    this.particleDepths = depths;
    this.particleLayers = layers;
    this.particleAgents = agents;
  }

  private particleSeeds = new Float32Array(0);
  private particleDepths = new Float32Array(0);
  private particleLayers = new Float32Array(0);
  private particleAgents = new Float32Array(0);

  private buildLines() {
    const gl = this.gl!;
    if (!this.options.connections) {
      this.lineVertexCount = 0;
      return;
    }

    // Pair each particle with a nearby one in seed space. Pairs are fixed at
    // build time; the shader recomputes the live distance every frame and fades
    // the link, so links appear and vanish organically with no CPU cost.
    const count = this.particleCount;
    const pairCount = Math.min(2600, Math.floor(count * 0.45));
    const own = new Float32Array(pairCount * 2 * 4);
    const partner = new Float32Array(pairCount * 2 * 4);
    const depths = new Float32Array(pairCount * 2 * 2);
    const layers = new Float32Array(pairCount * 2 * 2);
    const agentPairs = new Float32Array(pairCount * 2 * 2);

    const seedOf = (i: number) => [
      this.particleSeeds[i * 4], this.particleSeeds[i * 4 + 1],
      this.particleSeeds[i * 4 + 2], this.particleSeeds[i * 4 + 3],
    ];

    for (let p = 0; p < pairCount; p += 1) {
      const a = Math.floor(Math.random() * count);
      // Choose the closest of a few candidates in (angle, radius) space.
      let best = -1;
      let bestDistance = Infinity;
      for (let attempt = 0; attempt < 6; attempt += 1) {
        const b = Math.floor(Math.random() * count);
        if (b === a) continue;
        const da = Math.abs(this.particleSeeds[a * 4] - this.particleSeeds[b * 4]);
        const dr = Math.abs(this.particleSeeds[a * 4 + 1] - this.particleSeeds[b * 4 + 1]);
        const distance = Math.min(da, 1 - da) * 2.2 + dr;
        if (distance < bestDistance) { bestDistance = distance; best = b; }
      }
      if (best < 0) best = (a + 1) % count;

      const seedA = seedOf(a);
      const seedB = seedOf(best);
      for (let endpoint = 0; endpoint < 2; endpoint += 1) {
        const base = (p * 2 + endpoint) * 4;
        const [self, other] = endpoint === 0 ? [seedA, seedB] : [seedB, seedA];
        own.set(self, base);
        partner.set(other, base);
        const pairBase = (p * 2 + endpoint) * 2;
        const [selfIndex, otherIndex] = endpoint === 0 ? [a, best] : [best, a];
        depths[pairBase] = this.particleDepths[selfIndex];
        depths[pairBase + 1] = this.particleDepths[otherIndex];
        layers[pairBase] = this.particleLayers[selfIndex];
        layers[pairBase + 1] = this.particleLayers[otherIndex];
        agentPairs[pairBase] = this.particleAgents[selfIndex];
        agentPairs[pairBase + 1] = this.particleAgents[otherIndex];
      }
    }

    this.lineVertexCount = pairCount * 2;
    this.lineVao = gl.createVertexArray();
    gl.bindVertexArray(this.lineVao);
    this.createBuffer(own);
    this.attribute(this.lineProgram!, 'aSeed', 4);
    this.createBuffer(partner);
    this.attribute(this.lineProgram!, 'aPartnerSeed', 4);
    this.createBuffer(depths);
    this.attribute(this.lineProgram!, 'aDepths', 2);
    this.createBuffer(layers);
    this.attribute(this.lineProgram!, 'aLayers', 2);
    this.createBuffer(agentPairs);
    this.attribute(this.lineProgram!, 'aAgents', 2);
    gl.bindVertexArray(null);
  }

  private buildRings() {
    const gl = this.gl!;
    const segments = 180;
    const rings = this.ringCount;
    const t = new Float32Array(segments * rings);
    const index = new Float32Array(segments * rings);

    for (let r = 0; r < rings; r += 1) {
      for (let s = 0; s < segments; s += 1) {
        const i = r * segments + s;
        t[i] = s / segments;
        index[i] = r;
      }
    }
    this.ringVertexCount = segments * rings;
    this.ringSegments = segments;

    this.ringVao = gl.createVertexArray();
    gl.bindVertexArray(this.ringVao);
    this.createBuffer(t);
    this.attribute(this.ringProgram!, 'aT', 1);
    this.createBuffer(index);
    this.attribute(this.ringProgram!, 'aRing', 1);
    gl.bindVertexArray(null);
  }

  private ringSegments = 180;

  private buildQuad() {
    const gl = this.gl!;
    const vertices = new Float32Array([-1, -1, 3, -1, -1, 3]);
    this.quadVao = gl.createVertexArray();
    gl.bindVertexArray(this.quadVao);
    this.createBuffer(vertices);
    this.attribute(this.fadeProgram!, 'aPos', 2);
    gl.bindVertexArray(null);
  }

  // ======================================================================
  resize() {
    const gl = this.gl;
    if (!gl) return;

    // Cap the pixel ratio: a 4K display at devicePixelRatio 2 would otherwise
    // shade 33 million pixels per frame for no visible gain.
    this.pixelRatio = Math.min(window.devicePixelRatio || 1, this.degraded ? 1 : 1.75);
    const width = Math.max(1, Math.floor(this.canvas.clientWidth * this.pixelRatio));
    const height = Math.max(1, Math.floor(this.canvas.clientHeight * this.pixelRatio));
    if (this.canvas.width === width && this.canvas.height === height && this.framebuffer) return;

    this.canvas.width = width;
    this.canvas.height = height;
    gl.viewport(0, 0, width, height);

    if (this.sceneTexture) gl.deleteTexture(this.sceneTexture);
    if (this.framebuffer) gl.deleteFramebuffer(this.framebuffer);

    this.sceneTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.sceneTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    this.framebuffer = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.sceneTexture, 0);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  // ======================================================================
  setState(state: FieldState) {
    if (state === this.state) return;
    this.state = state;
    this.target = { ...PROFILES[state] };
  }

  getState(): FieldState {
    return this.state;
  }

  setAmplitude(value: number) {
    this.amplitudeTarget = Math.max(0, Math.min(1.6, value));
  }

  setPointer(x: number, y: number, active: boolean) {
    this.pointer = [x, y];
    this.pointerTarget = active ? 1 : 0;
  }

  setOptions(options: FieldOptions) {
    const rebuild =
      (options.particleCount !== undefined && options.particleCount !== this.options.particleCount) ||
      (options.connections !== undefined && options.connections !== this.options.connections);
    this.options = { ...this.options, ...options };
    if (rebuild && this.gl) {
      this.releaseGeometry();
      this.buildParticles();
      this.buildLines();
    }
  }

  onFpsSample(callback: (fps: number) => void) {
    this.onFps = callback;
  }

  // ======================================================================
  start() {
    if (this.running || !this.gl) return;
    this.running = true;
    this.startTime = performance.now();
    this.lastFrame = this.startTime;
    // Discard samples from before the pause. A hidden tab gets no frames at
    // all, so mixing what was measured before it was hidden with what comes
    // after would judge the machine on a gap it did not cause.
    this.fpsSamples = [];
    this.badWindows = 0;
    this.goodWindows = 0;
    this.frame = requestAnimationFrame(this.loop);
  }

  stop() {
    this.running = false;
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  get isRunning() {
    return this.running;
  }

  private loop = (now: number) => {
    if (!this.running) return;
    const delta = Math.min((now - this.lastFrame) / 1000, 0.1);
    this.lastFrame = now;

    this.trackPerformance(delta);
    this.ease(delta);
    this.render((now - this.startTime) / 1000);

    this.frame = requestAnimationFrame(this.loop);
  };

  /**
   * Drop quality automatically rather than letting the whole UI stutter --
   * and put it back when the machine can afford it again.
   *
   * This used to be a one-way door: a single bad two-second window halved
   * the particle count and switched the connections off for the rest of the
   * session. Anything transient did it -- another application taking the GPU
   * for a moment, a tab switch, a heavy page load -- and the field stayed
   * visibly poorer until a reload, with nothing to say why.
   *
   * So it now needs sustained evidence in both directions, with a wide gap
   * between the two thresholds so it cannot sit on the boundary flipping
   * back and forth.
   */
  private trackPerformance(delta: number) {
    if (delta <= 0) return;
    this.fpsSamples.push(1 / delta);
    if (this.fpsSamples.length < 120) return;          // roughly two seconds

    const average = this.fpsSamples.reduce((a, b) => a + b, 0) / this.fpsSamples.length;
    this.fpsSamples = [];
    this.onFps?.(Math.round(average));

    const { next, action } = qualityDecision(average, {
      degraded: this.degraded,
      badWindows: this.badWindows,
      goodWindows: this.goodWindows,
    });
    this.degraded = next.degraded;
    this.badWindows = next.badWindows;
    this.goodWindows = next.goodWindows;

    if (action === 'degrade') {
      this.fullQuality = {
        particleCount: this.options.particleCount,
        connections: this.options.connections,
      };
      console.info(
        `[particles] ${Math.round(average)} fps — reducing quality (it will be restored `
        + 'if the frame rate recovers)');
      this.setOptions({
        particleCount: Math.max(900, Math.floor(this.options.particleCount * 0.5)),
        connections: false,
      });
      this.framebuffer = null;
      this.resize();
    } else if (action === 'restore' && this.fullQuality) {
      console.info(`[particles] ${Math.round(average)} fps — restoring full quality`);
      this.setOptions(this.fullQuality);
      this.fullQuality = null;
      this.framebuffer = null;
      this.resize();
    }
  }

  private ease(delta: number) {
    // Time-corrected exponential easing, so transition feel is frame-rate
    // independent.
    const k = 1 - Math.pow(0.0025, delta);
    const motion = this.options.reducedMotion ? 0.35 : 1;
    const keys = Object.keys(this.target) as (keyof StateProfile)[];
    for (const key of keys) {
      const goal = key === 'alpha' || key === 'trail'
        ? this.target[key]
        : this.target[key] * (key === 'chaos' || key === 'energy' ? motion : 1);
      this.current[key] += (goal - this.current[key]) * k;
    }
    // Audio drives the field directly: fast attack so speech feels immediate,
    // slower release so it doesn't flicker between syllables.
    const audioK = this.amplitudeTarget > this.amplitude ? 0.45 : 0.10;
    this.amplitude += (this.amplitudeTarget - this.amplitude) * audioK;
    this.pointerStrength += (this.pointerTarget - this.pointerStrength) * k;

    // -- field dynamics, all eased so nothing ever cuts -------------------
    const contextTarget = CONTEXT_TARGETS[this.context];
    // Mode changes are a deliberate morph, slower than state changes.
    const modeK = 1 - Math.pow(0.15, delta);
    this.contextCurrent.research += (contextTarget.research - this.contextCurrent.research) * modeK;
    this.contextCurrent.coding += (contextTarget.coding - this.contextCurrent.coding) * modeK;
    this.contextCurrent.meeting += (contextTarget.meeting - this.contextCurrent.meeting) * modeK;
    this.contextCurrent.ambientFade +=
      (contextTarget.ambientFade - this.contextCurrent.ambientFade) * modeK;

    this.confidence += (this.confidenceTarget - this.confidence) * k;
    this.morph += (this.morphTarget - this.morph) * modeK;
    this.bridge += (this.bridgeTarget - this.bridge) * (1 - Math.pow(0.02, delta));
    this.bridgeTarget *= Math.pow(0.35, delta);   // a bridge is a pulse, not a state

    // Sleep collapse is deliberately slow in, slower out: the field should
    // reconstruct gradually on wake, never snap back.
    const collapseK = this.collapseTarget > this.collapse
      ? 1 - Math.pow(0.35, delta)      // ~2s to collapse
      : 1 - Math.pow(0.55, delta);     // ~4s to rebuild
    this.collapse += (this.collapseTarget - this.collapse) * collapseK;

    this.flowDirection[0] += (this.flowTarget[0] - this.flowDirection[0]) * k;
    this.flowDirection[1] += (this.flowTarget[1] - this.flowDirection[1]) * k;

    for (let i = 0; i < MAX_AGENTS; i += 1) {
      this.agentActivity[i] += (this.agentTargets[i] - this.agentActivity[i]) * k;
    }
  }

  // ======================================================================
  // Field Dynamics API
  // ======================================================================

  /** Contextual mode — changes how the field is *shaped*, not just how fast. */
  setContext(context: FieldContext) {
    if (context === this.context) return;
    this.context = context;
    // Only research and coding morph particle silhouettes.
    this.morphTarget = context === 'research' ? 0.3
      : context === 'coding' ? 0.55
      : context === 'meeting' ? 0.8
      : 0;
  }

  getContext(): FieldContext {
    return this.context;
  }

  /** 0 = uncertain (dispersed, hesitant), 1 = certain (tight, coherent). */
  setConfidence(value: number) {
    this.confidenceTarget = Math.max(0, Math.min(1, value));
  }

  /** Direction attention is flowing, in field space. */
  setFlow(x: number, y: number) {
    const length = Math.hypot(x, y) || 1;
    this.flowTarget = [x / length * 0.12, y / length * 0.12];
  }

  clearFlow() {
    this.flowTarget = [0, 0];
  }

  /** Collapse to a single dim point (sleep), or reconstruct from it. */
  setCollapsed(collapsed: boolean) {
    this.collapseTarget = collapsed ? 1 : 0;
  }

  /** Active agents, each becoming a sub-cluster with its own colour. */
  setAgents(agents: AgentCluster[]) {
    this.agents = agents.slice(0, MAX_AGENTS);
    this.agentTargets.fill(0);
    for (let i = 0; i < this.agents.length; i += 1) {
      this.agentTargets[i] = Math.max(0, Math.min(1, this.agents[i].activity));
      const [r, g, b] = hexToRgb(this.agents[i].colour);
      this.agentColours[i * 3 + 0] = r;
      this.agentColours[i * 3 + 1] = g;
      this.agentColours[i * 3 + 2] = b;
    }
  }

  /** Pulse a communication bridge between agent clusters. */
  pulseBridge(strength = 1) {
    this.bridgeTarget = Math.max(this.bridgeTarget, Math.min(1, strength));
  }

  /** A completed task: particles collapse to the core, then release outward. */
  pulseCompletion() {
    this.current.pullCore = Math.min(1, this.current.pullCore + 0.6);
    this.target.release = 0.5;
    window.setTimeout(() => { this.target.release = PROFILES[this.state].release; }, 1400);
  }

  // ----------------------------------------------------------------------
  private setStateUniforms(program: WebGLProgram, time: number) {
    const gl = this.gl!;
    const u = (name: string) => gl.getUniformLocation(program, name);
    gl.uniform1f(u('uTime'), time);
    gl.uniform2f(u('uResolution'), this.canvas.width, this.canvas.height);
    gl.uniform1f(u('uEnergy'), this.current.energy);
    gl.uniform1f(u('uConverge'), this.current.converge);
    gl.uniform1f(u('uOrbit'), this.current.orbit);
    gl.uniform1f(u('uAmplitude'), this.amplitude);
    gl.uniform1f(u('uDispersion'), this.current.dispersion);
    gl.uniform1f(u('uChaos'), this.current.chaos);
    gl.uniform1f(u('uAlpha'), this.current.alpha);
    gl.uniform1f(u('uScale'), 1.42);
    gl.uniform1f(u('uPixelRatio'), this.pixelRatio);
    gl.uniform2f(u('uPointer'), this.pointer[0], this.pointer[1]);
    gl.uniform1f(u('uPointerStrength'), this.pointerStrength);
    gl.uniform3fv(u('uColorAccent'), this.options.accent);

    // -- field dynamics --------------------------------------------------
    gl.uniform2f(u('uLayerPull'), this.current.pullTask, this.current.pullCore);
    gl.uniform1f(u('uRelease'), this.current.release);
    gl.uniform2f(u('uFlow'), this.flowDirection[0], this.flowDirection[1]);
    gl.uniform1f(u('uFlowStrength'), this.current.flow);
    gl.uniform1f(u('uRipple'), this.current.ripple);
    gl.uniform1f(u('uConfidence'), this.confidence);
    gl.uniform1f(u('uCollapse'), this.collapse);

    gl.uniform1f(u('uModeResearch'), this.contextCurrent.research);
    gl.uniform1f(u('uModeCoding'), this.contextCurrent.coding);
    gl.uniform1f(u('uModeMeeting'), this.contextCurrent.meeting);
    gl.uniform1f(u('uAmbientFade'), this.contextCurrent.ambientFade);

    gl.uniform3fv(u('uAgents'), this.agentUniform);
    gl.uniform1f(u('uAgentCount'), this.agents.length);
    gl.uniform1f(u('uAgentBlend'), 0.85);
  }

  /** Agent cluster centres orbit the core, evenly spaced and slowly rotating. */
  private updateAgentCentres(time: number) {
    for (let i = 0; i < MAX_AGENTS; i += 1) {
      const angle = (i / MAX_AGENTS) * Math.PI * 2 + time * 0.06;
      const radius = 0.52;
      this.agentUniform[i * 3 + 0] = Math.cos(angle) * radius;
      this.agentUniform[i * 3 + 1] = Math.sin(angle) * radius;
      this.agentUniform[i * 3 + 2] = this.agentActivity[i];
    }
  }

  private render(time: number) {
    const gl = this.gl;
    if (!gl || !this.framebuffer) return;

    this.updateAgentCentres(time);

    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);

    // ---- 1. fade the previous frame (the trail) --------------------------
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(this.fadeProgram!);
    gl.uniform1f(gl.getUniformLocation(this.fadeProgram!, 'uFade'), this.current.trail);
    gl.bindVertexArray(this.quadVao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // ---- 2..4 additive passes -------------------------------------------
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

    if (this.lineVertexCount > 0 && this.options.connections) {
      gl.useProgram(this.lineProgram!);
      this.setStateUniforms(this.lineProgram!, time);
      gl.uniform1f(gl.getUniformLocation(this.lineProgram!, 'uLinkRange'), 0.17);
      gl.uniform3fv(gl.getUniformLocation(this.lineProgram!, 'uColorTask'), this.options.colorNear);
      gl.uniform3fv(gl.getUniformLocation(this.lineProgram!, 'uAgentColors'), this.agentColours);
      gl.uniform1f(gl.getUniformLocation(this.lineProgram!, 'uBridge'), this.bridge);
      gl.bindVertexArray(this.lineVao);
      gl.drawArrays(gl.LINES, 0, this.lineVertexCount);
    }

    gl.useProgram(this.ringProgram!);
    this.setStateUniforms(this.ringProgram!, time);
    gl.uniform1f(gl.getUniformLocation(this.ringProgram!, 'uRingCount'), this.ringCount);
    gl.bindVertexArray(this.ringVao);
    for (let ring = 0; ring < this.ringCount; ring += 1) {
      gl.drawArrays(gl.LINE_LOOP, ring * this.ringSegments, this.ringSegments);
    }

    gl.useProgram(this.particleProgram!);
    this.setStateUniforms(this.particleProgram!, time);
    const u = (n: string) => gl.getUniformLocation(this.particleProgram!, n);
    gl.uniform3fv(u('uColorCore'), this.options.accent);
    gl.uniform3fv(u('uColorTask'), this.options.colorNear);
    gl.uniform3fv(u('uColorAmbient'), this.options.colorFar);
    gl.uniform3fv(u('uAgentColors'), this.agentColours);
    gl.uniform1f(u('uMorph'), this.morph);
    gl.uniform1f(gl.getUniformLocation(this.particleProgram!, 'uSizeScale'), this.current.sizeScale);
    gl.bindVertexArray(this.particleVao);
    gl.drawArrays(gl.POINTS, 0, this.particleCount);

    // ---- composite to the screen ----------------------------------------
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.blendFunc(gl.ONE, gl.ZERO);
    gl.useProgram(this.compositeProgram!);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.sceneTexture);
    gl.uniform1i(gl.getUniformLocation(this.compositeProgram!, 'uScene'), 0);
    gl.uniform2f(gl.getUniformLocation(this.compositeProgram!, 'uResolution'),
      this.canvas.width, this.canvas.height);
    gl.uniform1f(gl.getUniformLocation(this.compositeProgram!, 'uAlpha'), this.current.alpha);
    gl.uniform1f(gl.getUniformLocation(this.compositeProgram!, 'uDispersion'), this.current.dispersion);
    gl.bindVertexArray(this.quadVao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
  }

  // ======================================================================
  private releaseGeometry() {
    const gl = this.gl;
    if (!gl) return;
    for (const buffer of this.buffers) gl.deleteBuffer(buffer);
    this.buffers = [];
    if (this.particleVao) gl.deleteVertexArray(this.particleVao);
    if (this.lineVao) gl.deleteVertexArray(this.lineVao);
    this.particleVao = null;
    this.lineVao = null;
  }

  /** Full teardown. Called on sleep so nothing is left running. */
  destroy() {
    this.stop();
    const gl = this.gl;
    if (!gl) return;
    this.releaseGeometry();
    if (this.ringVao) gl.deleteVertexArray(this.ringVao);
    if (this.quadVao) gl.deleteVertexArray(this.quadVao);
    if (this.sceneTexture) gl.deleteTexture(this.sceneTexture);
    if (this.framebuffer) gl.deleteFramebuffer(this.framebuffer);
    for (const program of [this.particleProgram, this.lineProgram, this.ringProgram,
                           this.fadeProgram, this.compositeProgram]) {
      if (program) gl.deleteProgram(program);
    }
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    this.gl = null;
  }
}


// ---------------------------------------------------------------------------
function hexToRgb(hex: string): Rgb {
  const clean = (hex || '').replace('#', '');
  const full = clean.length === 3 ? clean.split('').map((c) => c + c).join('') : clean;
  const value = parseInt(full, 16);
  if (Number.isNaN(value)) return [0.55, 0.7, 0.85];
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255];
}
