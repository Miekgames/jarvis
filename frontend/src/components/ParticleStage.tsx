import { useEffect, useRef, useState } from 'react';

import { ParticleField, type FieldOptions, type FieldState, type Rgb } from '../gl/ParticleField';
import { eventStream } from '../lib/api';
import { shownPalette, type Palette } from '../state/skillColours';
import { useStore } from '../state/store';

/**
 * Hosts the WebGL field and keeps it in step with JARVIS's state.
 *
 * On sleep the canvas is unmounted and the GL context is destroyed, so the GPU
 * genuinely goes idle rather than rendering behind a black layer.
 */
export function ParticleStage() {
  const hostRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<ParticleField | null>(null);
  const [supported, setSupported] = useState(true);

  const state = useStore((s) => s.state);
  const sleeping = useStore((s) => s.sleeping);
  const amplitude = useStore((s) => s.amplitude);
  const settings = useStore((s) => s.settings);
  const fieldContext = useStore((s) => s.fieldContext);
  const confidence = useStore((s) => s.confidence);
  const agents = useStore((s) => s.agents);

  const particleSettings = settings.particles ?? {};
  const theme = settings.theme ?? {};

  // -- lifecycle -----------------------------------------------------------
  useEffect(() => {
    if (sleeping) return;
    const host = hostRef.current;
    if (!host) return;

    // The canvas is created per effect run rather than rendered by React.
    // Destroying the field releases its WebGL context, and a released context
    // cannot be revived on the same element — which would otherwise break the
    // remount that StrictMode (and every sleep/wake cycle) performs.
    const canvas = document.createElement('canvas');
    canvas.className = 'field-canvas';
    host.appendChild(canvas);

    const field = new ParticleField(canvas, fieldOptions(particleSettings, theme));

    if (!field.init()) {
      setSupported(false);
      canvas.remove();
      return;
    }
    setSupported(true);
    fieldRef.current = field;
    field.setState(state);
    field.start();

    const onResize = () => field.resize();
    window.addEventListener('resize', onResize);

    // Colours follow the palette the ThemeFader is showing, frame by frame
    // during a fade -- a skill's colour, or the chosen theme's.
    const paint = (palette: Palette) => {
      field.setOptions(paletteOptions(palette));
      host.style.background = backdrop(palette.backdrop);
    };
    paint(shownPalette.get());
    const stopPainting = shownPalette.subscribe(paint);

    // Pause when the tab is hidden — no reason to burn GPU on a background tab.
    const onVisibility = () => {
      if (document.hidden) field.stop();
      else if (!useStore.getState().sleeping) field.start();
    };
    document.addEventListener('visibilitychange', onVisibility);

    const onPointerMove = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      const aspect = rect.width / Math.max(rect.height, 1);
      let x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      let y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
      if (aspect > 1) x *= aspect; else y /= aspect;
      field.setPointer(x / 1.42, y / 1.42, true);
    };
    const onPointerLeave = () => field.setPointer(0, 0, false);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerleave', onPointerLeave);

    return () => {
      stopPainting();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerleave', onPointerLeave);
      field.destroy();
      canvas.remove();
      fieldRef.current = null;
    };
    // Rebuilt only when sleep flips: everything else is applied via setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sleeping]);

  // -- state, amplitude, settings ------------------------------------------
  useEffect(() => {
    fieldRef.current?.setState(state as FieldState);
  }, [state]);

  // -- Field Dynamics: the field reflects what JARVIS is actually doing -----
  useEffect(() => {
    fieldRef.current?.setContext(fieldContext);
  }, [fieldContext]);

  useEffect(() => {
    fieldRef.current?.setConfidence(confidence);
  }, [confidence]);

  useEffect(() => {
    fieldRef.current?.setAgents(
      agents.map((a) => ({ id: a.id, colour: a.colour, activity: a.activity })),
    );
  }, [agents]);

  // Discrete events become one-off physical responses in the field.
  useEffect(() => {
    const unsubscribe = eventStream.subscribe((event) => {
      const field = fieldRef.current;
      if (!field) return;

      switch (event.type) {
        case 'agent.message':
          // Communication between clusters lights the bridge between them.
          field.pulseBridge(1);
          break;
        case 'task.completed':
        case 'goal.milestone':
        case 'goal.completed':
          // Work collapses into the core, then dissolves outward.
          field.pulseCompletion();
          break;
        case 'conversation.delta':
          // Responses "form" from motion: each token nudges the flow outward.
          field.setFlow(Math.cos(Date.now() * 0.002), Math.sin(Date.now() * 0.002));
          break;
        case 'conversation.message':
          if (event.data.role === 'user') field.setFlow(0, -1);   // toward the core
          else field.clearFlow();
          break;
      }
    });
    return unsubscribe;
  }, []);

  useEffect(() => {
    fieldRef.current?.setAmplitude(amplitude);
  }, [amplitude]);

  useEffect(() => {
    fieldRef.current?.setOptions(fieldOptions(particleSettings, theme));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [particleSettings.count, particleSettings.quality, particleSettings.connections,
      theme.reduced_motion]);

  if (sleeping) return null;

  return (
    <div
      className="stage"
      ref={hostRef}
      aria-hidden="true"
    >
      {!supported && (
        <div className="field-fallback">
          <div>
            <p style={{ letterSpacing: '0.3em', fontSize: '0.8rem', marginBottom: '0.8rem' }}>
              JARVIS
            </p>
            <p style={{ fontSize: '0.85rem' }}>
              This browser doesn&rsquo;t support WebGL2, so the particle field can&rsquo;t render.
              Everything else works — try Chrome, Edge or Firefox, or enable hardware
              acceleration in your browser settings.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
/** One place that turns stored settings into renderer options, so the initial
 *  build and later updates can never drift apart. */
function fieldOptions(particles: Record<string, any>, theme: Record<string, any>): FieldOptions {
  return {
    particleCount: qualityToCount(particles),
    connections: particles.connections !== false,
    reducedMotion: !!theme.reduced_motion || prefersReducedMotion(),
    ...paletteOptions(shownPalette.get()),
  };
}

function paletteOptions(palette: Palette): FieldOptions {
  return {
    accent: hexToRgb(palette.accent),
    colorNear: hexToRgb(palette.particle_near),
    colorFar: hexToRgb(palette.particle_far),
  };
}

function backdrop(colour: string): string {
  return `radial-gradient(circle at 50% 45%, ${colour} 0%, #04070d 62%, #02040a 100%)`;
}

function qualityToCount(settings: Record<string, any>): number {
  const explicit = Number(settings.count);
  const quality = settings.quality ?? 'auto';

  if (quality === 'low') return 1400;
  if (quality === 'balanced') return 2800;
  if (quality === 'high') return Math.max(explicit || 0, 6000);
  if (quality === 'auto') {
    // Scale to the display so an ultrawide isn't sparse and a laptop isn't melted.
    const pixels = window.innerWidth * window.innerHeight;
    const cores = navigator.hardwareConcurrency ?? 4;
    const base = Math.round(pixels / 620);
    const capped = Math.min(base, cores >= 8 ? 6500 : 3200);
    return Math.max(1200, capped);
  }
  return explicit > 0 ? explicit : 4200;
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

function hexToRgb(hex: string): Rgb {
  const clean = hex.replace('#', '');
  const full = clean.length === 3 ? clean.split('').map((c) => c + c).join('') : clean;
  const value = parseInt(full, 16);
  if (Number.isNaN(value)) return [0.35, 0.82, 1];
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255];
}
