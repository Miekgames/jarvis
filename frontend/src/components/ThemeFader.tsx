import { useEffect, useRef, useState } from 'react';

import {
  FADE_MS, MIN_SHOW_MS, basePalette, mixPalette, paletteFor, shownPalette, shownSkill,
  type ActiveSkill,
} from '../state/skillColours';
import { useStore } from '../state/store';

/**
 * Fades the screen to the colour of the skill in use, and back.
 *
 * Owns the accent colours (CSS variables) and feeds the particle field
 * through `shownPalette`. A skill's colour stays at least MIN_SHOW_MS, so
 * one that finishes in half a second still shows. With skill colours turned
 * off ("stop changing colours"), the chosen theme shows; with reduced motion
 * on, colours change without fading.
 */
export function ThemeFader() {
  const theme = useStore((s) => s.settings.theme) ?? {};
  const active = useStore((s) => s.activeSkills);
  const first = useRef(true);

  const wanted = theme.skill_colors === false ? null : shownSkill(active);
  const [showing, setShowing] = useState<ActiveSkill | null>(wanted);
  const since = useRef(performance.now());

  // A new skill shows at once; leaving one waits until it has been seen.
  useEffect(() => {
    if (wanted?.skill === showing?.skill && wanted?.color === showing?.color) return;
    const seenFor = performance.now() - since.current;
    const wait = showing ? Math.max(0, MIN_SHOW_MS - seenFor) : 0;
    const timer = window.setTimeout(() => {
      setShowing(wanted);
      since.current = performance.now();
    }, wait);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted?.skill, wanted?.color, showing?.skill]);

  const target = showing ? paletteFor(showing.color) : basePalette(theme);
  const key = Object.values(target).join(' ');

  useEffect(() => {
    const from = shownPalette.get();
    const instant = first.current || !!theme.reduced_motion
      || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    first.current = false;
    const root = document.documentElement;
    const began = performance.now();
    let frame = 0;

    const step = (now: number) => {
      const t = instant ? 1 : Math.min(1, (now - began) / FADE_MS);
      const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      const palette = mixPalette(from, target, eased);
      root.style.setProperty('--accent', palette.accent);
      root.style.setProperty('--secondary', palette.accent_secondary);
      shownPalette.set(palette);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
    // The target's colours are the key: a new skill, or none, starts a fade.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Which skill is showing, for anything that wants to say so.
  useEffect(() => {
    document.documentElement.dataset.skill = showing?.skill ?? '';
  }, [showing?.skill]);

  return null;
}
