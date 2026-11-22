/**
 * Field Dynamics — the three-layer intelligence field.
 *
 * Layer 0  ambient   context, memory, background awareness. Slow, dim, wide.
 * Layer 1  task      active processes and agents. Directed, brighter, mid-radius.
 * Layer 2  core      JARVIS's own presence. Tight, bright, central.
 *
 * A particle's layer is an attribute, but its *effective* layer is computed
 * per-frame from `uLayerPull`, so particles migrate between layers as priority
 * shifts — work starting pulls ambient particles inward into the task layer,
 * completion releases them back outward.
 *
 * Up to six agent clusters orbit the core, each a semi-independent centre of
 * gravity for the particles assigned to it. Bridges between clusters are drawn
 * when agents exchange messages.
 */

export const MAX_AGENTS = 6;

/** Shared motion model. Injected into every vertex shader below. */
const FIELD = /* glsl */ `
  // -- noise ---------------------------------------------------------------
  vec3 hash3(vec3 p) {
    p = vec3(dot(p, vec3(127.1, 311.7, 74.7)),
             dot(p, vec3(269.5, 183.3, 246.1)),
             dot(p, vec3(113.5, 271.9, 124.6)));
    return fract(sin(p) * 43758.5453123) * 2.0 - 1.0;
  }

  float noise(vec3 p) {
    vec3 i = floor(p); vec3 f = fract(p);
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

  vec2 curl(vec2 p, float t) {
    float e = 0.12;
    return vec2(noise(vec3(p.x, p.y + e, t)) - noise(vec3(p.x, p.y - e, t)),
                noise(vec3(p.x - e, p.y, t)) - noise(vec3(p.x + e, p.y, t))) / (2.0 * e);
  }

  const float TAU = 6.28318530718;

  /**
   * seed.x angle   seed.y radius   seed.z jitter   seed.w size
   * aLayer  0 ambient, 1 task, 2 core
   * aAgent  -1 unassigned, else agent slot index
   */
  vec2 fieldPosition(vec4 seed, float depth, float layer, float agent, out float outLayer) {
    // ---- layer migration -------------------------------------------------
    // uLayerPull.x pulls ambient -> task, .y pulls task -> core.
    float effLayer = layer;
    float migrate = fract(seed.z * 91.7);
    if (layer < 0.5) effLayer += step(migrate, uLayerPull.x);
    else if (layer < 1.5) effLayer += step(migrate, uLayerPull.y);
    else effLayer -= step(migrate, uRelease);      // completion releases core outward
    effLayer = clamp(effLayer, 0.0, 2.0);
    outLayer = effLayer;

    // Each layer occupies its own annulus. Smooth so migration is a glide.
    float bandCentre = mix(0.72, 0.30, effLayer * 0.5);
    float bandWidth  = mix(0.34, 0.13, effLayer * 0.5);
    float radius = bandCentre + (seed.y - 0.5) * bandWidth * 2.0;

    float angle = seed.x * TAU;
    float jitter = seed.z;

    // ---- orbital motion --------------------------------------------------
    float layerSpeed = mix(0.55, 1.9, effLayer * 0.5);
    float spin = uTime * (0.04 + jitter * 0.05) * (1.0 + uEnergy * 2.4) * layerSpeed;
    angle += spin * (0.6 + depth * 0.8);
    radius *= 1.0 + sin(uTime * 0.35 + seed.y * 9.0) * 0.03;

    // ---- LISTENING: draw inward -------------------------------------------
    radius *= mix(1.0, 0.36, uConverge);
    radius += sin(uTime * 2.6 - radius * 7.0) * 0.05 * uConverge;

    // ---- CODING: structured lattice ---------------------------------------
    // Quantise into a polar grid so motion reads as precise and deliberate.
    if (uModeCoding > 0.01) {
      float cells = 14.0;
      float qA = (floor(angle / TAU * cells) + 0.5) / cells * TAU;
      float qR = (floor(radius * 9.0) + 0.5) / 9.0;
      angle = mix(angle, qA, uModeCoding);
      radius = mix(radius, qR, uModeCoding * 0.85);
    }

    // ---- RESEARCH: branching neural structure ------------------------------
    if (uModeResearch > 0.01) {
      float branches = 7.0;
      float branch = floor(seed.x * branches);
      float along = fract(seed.x * branches);          // position along the branch
      float branchAngle = branch / branches * TAU + sin(uTime * 0.22 + branch) * 0.25;
      // Sub-branching: split again further out.
      branchAngle += (fract(seed.z * 31.0) - 0.5) * along * 0.75;
      float branchRadius = 0.16 + along * 0.78;
      angle = mix(angle, branchAngle, uModeResearch);
      radius = mix(radius, branchRadius, uModeResearch);
    }

    // ---- MEETING: stable orbital rings -------------------------------------
    if (uModeMeeting > 0.01) {
      float rings = 4.0;
      float ring = (floor(seed.y * rings) + 0.5) / rings;
      radius = mix(radius, 0.22 + ring * 0.52, uModeMeeting);
      angle = mix(angle, seed.x * TAU + uTime * 0.10 * (1.0 + ring), uModeMeeting);
    }

    // ---- WORKING: banded streams -------------------------------------------
    float bands = 7.0;
    float banded = (floor(radius * bands) + 0.5) / bands;
    radius = mix(radius, banded, uOrbit * 0.8);
    angle += uOrbit * uTime * 0.5 * (1.0 - 2.0 * mod(floor(radius * bands), 2.0));

    // ---- SPEAKING: radial waveform -----------------------------------------
    radius += sin(angle * 9.0 + uTime * 5.5) * uAmplitude * 0.15
              * smoothstep(0.05, 0.65, radius);

    // ---- ERROR: turbulence with localised distortion zones ------------------
    if (uDispersion > 0.01) {
      vec2 rough = vec2(cos(angle), sin(angle)) * radius;
      // Two wandering distortion centres tear at the field locally.
      for (int i = 0; i < 2; i++) {
        float fi = float(i);
        vec2 centre = vec2(cos(uTime * 0.5 + fi * 2.1), sin(uTime * 0.37 + fi * 1.3)) * 0.45;
        float d = length(rough - centre);
        float pull = exp(-d * 5.0) * uDispersion;
        angle += pull * 2.2 * (fi * 2.0 - 1.0);
        radius += pull * 0.20;
      }
      radius += uDispersion * (0.10 + jitter * 0.16);
    }

    vec2 pos = vec2(cos(angle), sin(angle)) * radius;

    // ---- AGENT CLUSTERS -----------------------------------------------------
    // Particles assigned to an agent orbit that agent's centre instead.
    if (agent >= 0.0 && uAgentCount > 0.5) {
      int slot = int(agent);
      vec3 info = uAgents[slot];               // xy = centre, z = activity 0..1
      if (info.z > 0.01) {
        float localAngle = seed.x * TAU + uTime * (0.8 + jitter * 0.6);
        float localRadius = 0.045 + seed.y * 0.075;
        vec2 local = info.xy + vec2(cos(localAngle), sin(localAngle)) * localRadius;
        pos = mix(pos, local, info.z * uAgentBlend);
      }
    }

    // ---- flow toward active work -------------------------------------------
    // uFlow points at whatever currently has attention; particles drift along it.
    pos += uFlow * uFlowStrength * (0.35 + depth * 0.5) * smoothstep(0.1, 0.9, radius);

    // ---- ambient turbulence -------------------------------------------------
    vec2 flow = curl(pos * 1.35 + seed.zw * 4.0, uTime * (0.05 + uEnergy * 0.2));
    float turbulence = (0.05 + uChaos * 0.15) * mix(1.4, 0.45, effLayer * 0.5);
    pos += flow * turbulence * (0.45 + depth * 0.9);

    // ---- reasoning ripples ---------------------------------------------------
    // Concentric waves radiating from the core while JARVIS is thinking.
    float d = length(pos);
    pos += normalize(pos + 1e-5) * sin(d * 22.0 - uTime * 4.0) * 0.012 * uRipple;

    // ---- uncertainty vs confidence -------------------------------------------
    // Low confidence disperses and adds hesitation; high confidence tightens.
    pos *= mix(1.06, 0.95, uConfidence);
    pos += hash3(vec3(seed.xy, floor(uTime * 2.0))).xy * (1.0 - uConfidence) * 0.02;

    // ---- pointer --------------------------------------------------------------
    vec2 toPointer = uPointer - pos;
    pos += normalize(toPointer + 1e-5) * uPointerStrength
           * exp(-length(toPointer) * 3.2) * 0.09;

    return pos;
  }
`;

const UNIFORMS = /* glsl */ `
  uniform float uTime;
  uniform vec2  uResolution;
  uniform float uEnergy;
  uniform float uConverge;
  uniform float uOrbit;
  uniform float uAmplitude;
  uniform float uDispersion;
  uniform float uChaos;
  uniform float uAlpha;
  uniform float uScale;
  uniform float uPixelRatio;
  uniform vec2  uPointer;
  uniform float uPointerStrength;

  // -- field dynamics --
  uniform vec2  uLayerPull;      // x: ambient->task, y: task->core
  uniform float uRelease;        // completion: core -> outward
  uniform vec2  uFlow;           // direction of attention
  uniform float uFlowStrength;
  uniform float uRipple;         // reasoning activity
  uniform float uConfidence;     // 0 uncertain .. 1 certain
  uniform float uCollapse;       // sleep: 0 field .. 1 single point

  // -- contextual modes --
  uniform float uModeResearch;
  uniform float uModeCoding;
  uniform float uModeMeeting;
  uniform float uAmbientFade;    // deep focus: dim the ambient layer

  // -- agents --
  uniform vec3  uAgents[${MAX_AGENTS}];   // xy centre, z activity
  uniform float uAgentCount;
  uniform float uAgentBlend;
`;

export const FIELD_VERT = /* glsl */ `#version 300 es
  precision highp float;

  in vec4  aSeed;
  in float aDepth;
  in float aLayer;
  in float aAgent;
  in float aShape;

  ${UNIFORMS}
  uniform vec3  uColorCore;
  uniform vec3  uColorTask;
  uniform vec3  uColorAmbient;
  uniform vec3  uAgentColors[${MAX_AGENTS}];
  uniform float uSizeScale;
  uniform float uMorph;          // 0 round .. 1 shaped by task type

  out vec3  vColor;
  out float vAlpha;
  out float vCore;
  out float vShape;

  ${FIELD}

  void main() {
    float effLayer;
    vec2 pos = fieldPosition(aSeed, aDepth, aLayer, aAgent, effLayer);

    // Sleep collapse: everything converges on a single dim point.
    pos = mix(pos, vec2(0.0), uCollapse);

    float aspect = uResolution.x / max(uResolution.y, 1.0);
    vec2 clip = pos * uScale;
    if (aspect > 1.0) clip.x /= aspect; else clip.y *= aspect;
    gl_Position = vec4(clip, 0.0, 1.0);

    float depthSize = mix(0.55, 1.6, aDepth);
    float layerSize = mix(0.85, 1.5, effLayer * 0.5);
    float size = (0.85 + aSeed.w * 2.0) * depthSize * layerSize * uSizeScale * uPixelRatio;
    size *= 1.0 + uConverge * 0.3 + uAmplitude * 0.5;
    size *= mix(1.0, 0.35, uCollapse);
    gl_PointSize = clamp(size, 1.0, 22.0 * uPixelRatio);

    // ---- colour by layer, then by agent -----------------------------------
    vec3 base = uColorAmbient;
    if (effLayer > 1.5)      base = uColorCore;
    else if (effLayer > 0.5) base = mix(uColorAmbient, uColorTask, 1.0);

    if (aAgent >= 0.0 && uAgentCount > 0.5) {
      int slot = int(aAgent);
      base = mix(base, uAgentColors[slot], uAgents[slot].z * 0.85);
    }

    float coreness = 1.0 - smoothstep(0.0, 0.30, length(pos));
    vCore = coreness;
    vColor = mix(base, uColorCore, coreness * 0.7);
    vShape = aShape * uMorph;

    // Ambient layer fades in deep focus; core never does.
    float layerAlpha = effLayer < 0.5 ? mix(1.0, 0.10, uAmbientFade) : 1.0;
    vAlpha = mix(0.32, 1.0, aDepth) * uAlpha * layerAlpha
             * (0.5 + aSeed.w * 0.5) * (1.0 + coreness * 0.8)
             * mix(1.0, 0.55, uCollapse);
  }
`;

export const FIELD_FRAG = /* glsl */ `#version 300 es
  precision highp float;

  in vec3  vColor;
  in float vAlpha;
  in float vCore;
  in float vShape;

  out vec4 fragColor;

  void main() {
    vec2 uv = gl_PointCoord * 2.0 - 1.0;

    // Contextual morphing: 0 = round, 1 = the task type's silhouette.
    // 0.25 diamond (research)  0.5 square (code)  0.75 ring (meeting)
    float d;
    if (vShape < 0.15) {
      d = dot(uv, uv);
    } else if (vShape < 0.4) {
      float m = abs(uv.x) + abs(uv.y);          // diamond
      d = m * m;
    } else if (vShape < 0.65) {
      float m = max(abs(uv.x), abs(uv.y));      // square
      d = m * m;
    } else {
      float r = length(uv);                      // ring
      d = abs(r - 0.62) * 4.0;
      d = d * d;
    }
    if (d > 1.0) discard;

    float glow = exp(-d * 3.4);
    float core = exp(-d * 12.0);
    float intensity = glow * 0.55 + core * 0.85;

    vec3 colour = vColor + vec3(0.35, 0.5, 0.6) * core * (0.3 + vCore * 0.7);
    fragColor = vec4(colour * intensity, intensity * vAlpha);
  }
`;

/** Connections, now layer- and agent-aware. */
export const LINK_VERT = /* glsl */ `#version 300 es
  precision highp float;

  in vec4  aSeed;
  in vec4  aPartnerSeed;
  in vec2  aDepths;
  in vec2  aLayers;
  in vec2  aAgents;

  ${UNIFORMS}
  uniform vec3  uColorTask;
  uniform vec3  uAgentColors[${MAX_AGENTS}];
  uniform float uLinkRange;
  uniform float uBridge;      // agent-to-agent communication intensity

  out float vAlpha;
  out vec3  vColor;

  ${FIELD}

  void main() {
    float ownLayer, partnerLayer;
    vec2 own     = fieldPosition(aSeed, aDepths.x, aLayers.x, aAgents.x, ownLayer);
    vec2 partner = fieldPosition(aPartnerSeed, aDepths.y, aLayers.y, aAgents.y, partnerLayer);

    own = mix(own, vec2(0.0), uCollapse);

    float dist = distance(own, partner);
    float fade = 1.0 - smoothstep(uLinkRange * 0.35, uLinkRange, dist);

    float aspect = uResolution.x / max(uResolution.y, 1.0);
    vec2 clip = own * uScale;
    if (aspect > 1.0) clip.x /= aspect; else clip.y *= aspect;
    gl_Position = vec4(clip, 0.0, 1.0);

    float activity = clamp(uEnergy * 0.9 + uOrbit * 0.7 + uConverge * 0.3, 0.0, 1.4);
    vColor = mix(vec3(0.30, 0.62, 0.85), uColorTask, activity * 0.6);

    // A link spanning two different agents is a communication bridge: brighter,
    // tinted by the source agent, and only visible while they're talking.
    float crossAgent = (aAgents.x >= 0.0 && aAgents.y >= 0.0 && aAgents.x != aAgents.y)
                       ? 1.0 : 0.0;
    if (crossAgent > 0.5) {
      vColor = uAgentColors[int(aAgents.x)];
      activity += uBridge * 2.0;
      fade = max(fade, uBridge * 0.8);
    }

    vAlpha = fade * uAlpha * (0.05 + activity * 0.2) * mix(0.4, 1.0, aDepths.x)
             * (1.0 - uCollapse);
  }
`;

export const LINK_FRAG = /* glsl */ `#version 300 es
  precision highp float;
  in float vAlpha;
  in vec3  vColor;
  out vec4 fragColor;
  void main() { fragColor = vec4(vColor * vAlpha, vAlpha); }
`;
