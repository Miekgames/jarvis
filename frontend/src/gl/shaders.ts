/**
 * GLSL for the particle field.
 *
 * All motion is computed on the GPU from a per-particle seed plus a handful of
 * state uniforms. The CPU never touches particle positions, so the field costs
 * one draw call for the points and one for the connections regardless of how
 * many thousands of particles are on screen.
 *
 * The state uniforms are continuous, not switches — the CPU eases them toward
 * whatever JARVIS is doing, so every transition between idle / listening /
 * thinking / speaking / working is a smooth morph rather than a cut.
 */

/** Shared noise + motion model, injected into both vertex shaders. */
const MOTION = /* glsl */ `
  // -- cheap value noise ---------------------------------------------------
  vec3 hash3(vec3 p) {
    p = vec3(dot(p, vec3(127.1, 311.7, 74.7)),
             dot(p, vec3(269.5, 183.3, 246.1)),
             dot(p, vec3(113.5, 271.9, 124.6)));
    return fract(sin(p) * 43758.5453123) * 2.0 - 1.0;
  }

  float noise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(dot(hash3(i + vec3(0,0,0)), f - vec3(0,0,0)),
              dot(hash3(i + vec3(1,0,0)), f - vec3(1,0,0)), u.x),
          mix(dot(hash3(i + vec3(0,1,0)), f - vec3(0,1,0)),
              dot(hash3(i + vec3(1,1,0)), f - vec3(1,1,0)), u.x), u.y),
      mix(mix(dot(hash3(i + vec3(0,0,1)), f - vec3(0,0,1)),
              dot(hash3(i + vec3(1,0,1)), f - vec3(1,0,1)), u.x),
          mix(dot(hash3(i + vec3(0,1,1)), f - vec3(0,1,1)),
              dot(hash3(i + vec3(1,1,1)), f - vec3(1,1,1)), u.x), u.y),
      u.z);
  }

  // Curl of a noise field: gives divergence-free, fluid-looking drift.
  vec2 curl(vec2 p, float t) {
    float e = 0.12;
    float n1 = noise(vec3(p.x, p.y + e, t));
    float n2 = noise(vec3(p.x, p.y - e, t));
    float n3 = noise(vec3(p.x + e, p.y, t));
    float n4 = noise(vec3(p.x - e, p.y, t));
    return vec2(n1 - n2, n4 - n3) / (2.0 * e);
  }

  /**
   * seed.x  angle around the core        seed.y  normalised radius 0..1
   * seed.z  individual speed jitter      seed.w  size / brightness jitter
   * depth   0 = far, 1 = near (parallax, size and alpha)
   */
  vec2 particlePosition(vec4 seed, float depth) {
    float angle  = seed.x * 6.28318530718;
    float radius = seed.y;
    float jitter = seed.z;

    // ---- baseline: a slow, breathing orbit -------------------------------
    float spin = uTime * (0.045 + jitter * 0.05) * (1.0 + uEnergy * 2.6);
    // Inner particles orbit faster, like a real accretion disc.
    spin *= mix(1.9, 0.55, radius);
    angle += spin * (0.6 + depth * 0.8);

    float breathe = sin(uTime * 0.35 + seed.y * 9.0) * 0.035;
    float r = radius * (1.0 + breathe);

    // ---- LISTENING: draw inward, with a travelling pulse ------------------
    float pull = mix(1.0, 0.34, uConverge);
    float pulse = sin(uTime * 2.6 - radius * 7.0) * 0.055 * uConverge;
    r = r * pull + pulse;

    // ---- WORKING: collapse into orbiting streams --------------------------
    // Quantising the radius into bands is what reads as "streams" rather than
    // "faster soup".
    float bands = 7.0;
    float banded = (floor(radius * bands) + 0.5) / bands;
    r = mix(r, banded * pull, uOrbit * 0.85);
    angle += uOrbit * uTime * 0.55 * (1.0 + mod(floor(radius * bands), 2.0) * -2.0);

    // ---- SPEAKING: radial waveform driven by live audio -------------------
    float wave = sin(angle * 9.0 + uTime * 5.5) * uAmplitude * 0.16 * smoothstep(0.05, 0.65, radius);
    r += wave;

    // ---- ERROR: push outward and stagger ---------------------------------
    r += uDispersion * (0.22 + jitter * 0.3);
    angle += uDispersion * sin(seed.w * 30.0 + uTime * 3.0) * 0.55;

    vec2 pos = vec2(cos(angle), sin(angle)) * r;

    // ---- ambient turbulence ----------------------------------------------
    float flowScale = 1.35;
    vec2 flow = curl(pos * flowScale + seed.zw * 4.0, uTime * (0.05 + uEnergy * 0.22));
    pos += flow * (0.055 + uChaos * 0.16) * (0.45 + depth * 0.9);

    // ---- pointer attraction ----------------------------------------------
    vec2 toPointer = uPointer - pos;
    float d = length(toPointer);
    pos += normalize(toPointer + 1e-5) * uPointerStrength * exp(-d * 3.2) * 0.09;

    return pos;
  }
`;

const STATE_UNIFORMS = /* glsl */ `
  uniform float uTime;
  uniform vec2  uResolution;
  uniform float uEnergy;          // thinking: speed and complexity
  uniform float uConverge;        // listening: pull toward the core
  uniform float uOrbit;           // working: banded streams
  uniform float uAmplitude;       // speaking: live audio level
  uniform float uDispersion;      // error: outward stagger
  uniform float uChaos;           // turbulence
  uniform float uAlpha;           // global fade (sleep / wake)
  uniform float uScale;           // fits the field to the viewport
  uniform float uPixelRatio;
  uniform vec2  uPointer;
  uniform float uPointerStrength;
`;

export const PARTICLE_VERT = /* glsl */ `#version 300 es
  precision highp float;

  in vec4 aSeed;
  in float aDepth;

  ${STATE_UNIFORMS}
  uniform vec3 uColorNear;
  uniform vec3 uColorFar;
  uniform vec3 uColorAccent;
  uniform float uSizeScale;

  out vec3  vColor;
  out float vAlpha;
  out float vCore;

  ${MOTION}

  void main() {
    vec2 pos = particlePosition(aSeed, aDepth);

    // Aspect-correct so the field is a circle, not an ellipse, on any monitor.
    float aspect = uResolution.x / max(uResolution.y, 1.0);
    vec2 clip = pos * uScale;
    if (aspect > 1.0) clip.x /= aspect; else clip.y *= aspect;

    gl_Position = vec4(clip, 0.0, 1.0);

    // Near particles are bigger and brighter; far ones sit back in the haze.
    float depthSize = mix(0.55, 1.65, aDepth);
    float size = (0.9 + aSeed.w * 2.1) * depthSize * uSizeScale * uPixelRatio;
    // Tighten and brighten as the field converges.
    size *= 1.0 + uConverge * 0.35 + uAmplitude * 0.55;
    gl_PointSize = clamp(size, 1.0, 22.0 * uPixelRatio);

    // Particles nearest the core carry the accent colour: that concentration
    // *is* the core, rather than a separate glowing circle pasted on top.
    float coreness = 1.0 - smoothstep(0.0, 0.30, length(pos));
    vCore = coreness;

    vec3 base = mix(uColorFar, uColorNear, aDepth);
    vColor = mix(base, uColorAccent, coreness * 0.85);

    float depthAlpha = mix(0.30, 1.0, aDepth);
    vAlpha = depthAlpha * uAlpha * (0.55 + aSeed.w * 0.45) * (1.0 + coreness * 0.8);
  }
`;

export const PARTICLE_FRAG = /* glsl */ `#version 300 es
  precision highp float;

  in vec3  vColor;
  in float vAlpha;
  in float vCore;

  uniform vec3 uColorAccent;

  out vec4 fragColor;

  void main() {
    // Soft radial falloff — a gaussian-ish dot with a bright centre.
    vec2 uv = gl_PointCoord * 2.0 - 1.0;
    float d = dot(uv, uv);
    if (d > 1.0) discard;

    float glow = exp(-d * 3.4);
    float core = exp(-d * 12.0);
    float intensity = glow * 0.55 + core * 0.85;

    // The hot centre is tinted toward the accent rather than a fixed
    // near-white. A constant white highlight here washed every theme out to
    // grey once bloom was applied, so the field ignored its own palette.
    vec3 hot = mix(vec3(0.26, 0.40, 0.50), uColorAccent, 0.7);
    vec3 colour = vColor + hot * core * (0.28 + vCore * 0.62);
    fragColor = vec4(colour * intensity, intensity * vAlpha);
  }
`;

/**
 * Connections. Each line is two vertices that both know their own seed and
 * their partner's, so the shader can compute the live distance and fade the
 * link out as the pair drifts apart — no CPU neighbour search per frame.
 */
export const LINE_VERT = /* glsl */ `#version 300 es
  precision highp float;

  in vec4 aSeed;
  in vec4 aPartnerSeed;
  in vec2 aDepths;      // x = own depth, y = partner depth

  ${STATE_UNIFORMS}
  uniform vec3  uColorAccent;
  uniform float uLinkRange;

  out float vAlpha;
  out vec3  vColor;

  ${MOTION}

  void main() {
    vec2 own     = particlePosition(aSeed, aDepths.x);
    vec2 partner = particlePosition(aPartnerSeed, aDepths.y);

    float dist = distance(own, partner);
    float fade = 1.0 - smoothstep(uLinkRange * 0.35, uLinkRange, dist);

    float aspect = uResolution.x / max(uResolution.y, 1.0);
    vec2 clip = own * uScale;
    if (aspect > 1.0) clip.x /= aspect; else clip.y *= aspect;
    gl_Position = vec4(clip, 0.0, 1.0);

    // Links matter most when JARVIS is thinking or working — that's when the
    // field should read as a network rather than a drift.
    float activity = clamp(uEnergy * 0.9 + uOrbit * 0.7 + uConverge * 0.35, 0.0, 1.4);
    vAlpha = fade * uAlpha * (0.05 + activity * 0.22) * mix(0.4, 1.0, aDepths.x);
    vColor = mix(vec3(0.30, 0.62, 0.85), uColorAccent, activity * 0.6);
  }
`;

export const LINE_FRAG = /* glsl */ `#version 300 es
  precision highp float;
  in float vAlpha;
  in vec3  vColor;
  out vec4 fragColor;
  void main() {
    fragColor = vec4(vColor * vAlpha, vAlpha);
  }
`;

/** Thin energy rings around the core. Drawn as line loops with animated gaps. */
export const RING_VERT = /* glsl */ `#version 300 es
  precision highp float;

  in float aT;          // 0..1 around the ring
  in float aRing;       // ring index

  uniform float uTime;
  uniform vec2  uResolution;
  uniform float uScale;
  uniform float uAlpha;
  uniform float uConverge;
  uniform float uEnergy;
  uniform float uAmplitude;
  uniform float uDispersion;
  uniform float uRingCount;

  out float vFade;

  void main() {
    float ring = aRing;
    float baseRadius = 0.10 + ring * 0.052;
    float radius = baseRadius * mix(1.0, 0.72, uConverge)
                 + uAmplitude * 0.05 * (1.0 + ring * 0.4)
                 + uDispersion * 0.06;

    float spin = uTime * (0.16 + ring * 0.11) * (ring == 1.0 ? -1.0 : 1.0) * (1.0 + uEnergy * 1.8);
    float angle = aT * 6.28318530718 + spin;

    // Animated gaps — the rings read as arcs sweeping around the core.
    float arc = sin(angle * (2.0 + ring) - uTime * (0.7 + ring * 0.3));
    vFade = smoothstep(-0.15, 0.75, arc) * uAlpha * (0.30 + uEnergy * 0.45 + uConverge * 0.3);

    vec2 pos = vec2(cos(angle), sin(angle)) * radius;
    float aspect = uResolution.x / max(uResolution.y, 1.0);
    vec2 clip = pos * uScale;
    if (aspect > 1.0) clip.x /= aspect; else clip.y *= aspect;
    gl_Position = vec4(clip, 0.0, 1.0);
  }
`;

export const RING_FRAG = /* glsl */ `#version 300 es
  precision highp float;
  in float vFade;
  uniform vec3 uColorAccent;
  out vec4 fragColor;
  void main() {
    fragColor = vec4(uColorAccent * vFade, vFade * 0.85);
  }
`;

/** Fullscreen quad, used for the trail fade and the final composite. */
export const QUAD_VERT = /* glsl */ `#version 300 es
  precision highp float;
  in vec2 aPos;
  out vec2 vUv;
  void main() {
    vUv = aPos * 0.5 + 0.5;
    gl_Position = vec4(aPos, 0.0, 1.0);
  }
`;

export const FADE_FRAG = /* glsl */ `#version 300 es
  precision highp float;
  uniform float uFade;
  out vec4 fragColor;
  void main() { fragColor = vec4(0.0, 0.0, 0.0, uFade); }
`;

export const COMPOSITE_FRAG = /* glsl */ `#version 300 es
  precision highp float;

  in vec2 vUv;
  uniform sampler2D uScene;
  uniform vec2  uResolution;
  uniform float uAlpha;
  uniform float uDispersion;
  out vec4 fragColor;

  void main() {
    vec3 scene = texture(uScene, vUv).rgb;

    // Cheap bloom: a few taps of the already-blurred trail buffer.
    vec2 texel = 1.5 / uResolution;
    vec3 bloom = vec3(0.0);
    bloom += texture(uScene, vUv + vec2( texel.x,  0.0)).rgb;
    bloom += texture(uScene, vUv + vec2(-texel.x,  0.0)).rgb;
    bloom += texture(uScene, vUv + vec2( 0.0,  texel.y)).rgb;
    bloom += texture(uScene, vUv + vec2( 0.0, -texel.y)).rgb;
    bloom += texture(uScene, vUv + texel * 2.0).rgb;
    bloom += texture(uScene, vUv - texel * 2.0).rgb;
    scene += bloom * 0.085;

    // Error state warms the whole field toward amber without a hard colour cut.
    scene = mix(scene, scene * vec3(1.5, 0.72, 0.42), uDispersion * 0.75);

    // Vignette keeps attention on the core.
    vec2 centred = vUv - 0.5;
    float vignette = 1.0 - dot(centred, centred) * 0.85;

    fragColor = vec4(scene * vignette * uAlpha, 1.0);
  }
`;
