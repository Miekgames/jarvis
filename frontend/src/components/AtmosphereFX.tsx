import { useEffect, useMemo, useRef, useState } from 'react';

import { useStore } from '../state/store';

/**
 * Atmosphere.
 *
 * Screen effects that sit over everything and touch nothing: scanlines, a
 * slow scan sweep, a vignette that takes its tone from what JARVIS is doing,
 * telemetry streaming down the margins, a bloom that breathes with the voice,
 * and a brief dropout when something goes wrong.
 *
 * Deliberately a separate layer rather than additions to the HUD. The HUD
 * draws instruments -- every arc on it is a real number, and an unmeasured
 * value draws an empty track. Nothing here means anything, so it is kept
 * where it cannot be mistaken for an instrument, and it is all
 * `pointer-events: none` so it cannot get between the user and a control.
 *
 * Three rules it holds to:
 *
 *   * **No animation frames.** The particle field owns the frame budget and
 *     drops its own quality when it cannot keep up. Every effect here is a
 *     CSS animation on the compositor, so the cost is the GPU's and the main
 *     thread never sees it.
 *   * **It obeys the motion setting.** "Reduced" slows everything and drops
 *     the busiest layers; "still" leaves only the static ones. It also honours
 *     `prefers-reduced-motion` through the same stylesheet rules as the rest
 *     of the interface.
 *   * **Asleep means asleep.** This renders inside the same guard as the HUD,
 *     so a sleeping JARVIS is genuinely a black screen with nothing animating
 *     behind it -- not a dark panel over a running show.
 */

/** Glyphs for the margin telemetry. Hex, because it reads as machine output
 *  at a glance without pretending to be a number anyone should read. */
const GLYPHS = '0123456789ABCDEF';

/** Columns down each margin. Enough to read as a stream, few enough to stay
 *  out of the way -- the HUD already has ladders on both edges, and two sets
 *  of markings competing for the same strip reads as clutter. */
const COLUMNS = 5;

function stream(rows: number, seed: number): string {
  // Deterministic from the seed so a re-render does not reshuffle the text
  // and make the margins flicker.
  let value = seed * 9301 + 49297;
  const out: string[] = [];
  for (let i = 0; i < rows; i += 1) {
    value = (value * 9301 + 49297) % 233280;
    out.push(GLYPHS[Math.floor((value / 233280) * GLYPHS.length)]);
  }
  return out.join('');
}

export function AtmosphereFX() {
  const state = useStore((s) => s.state);
  const motion = useStore((s) => s.settings?.appearance?.motion ?? 'full');

  const still = motion === 'still';
  const calm = motion === 'reduced';
  const classes = `${still ? 'fx--still' : ''} ${calm ? 'fx--calm' : ''}`.trim();

  // Columns are generated once. Their content is meaningless, so there is no
  // reason to pay for it more than once.
  const columns = useMemo(
    () => Array.from({ length: COLUMNS }, (_, i) => ({
      text: stream(26, i + 1),
      duration: 18 + (i % 5) * 7,
      delay: -(i * 3.5),
    })),
    [],
  );

  return (
    <>
      {/* Behind the interface.
          Anything that tints or darkens goes here, below the conversation,
          the rail and the command bar. Over the top it would be dimming the
          very text it is meant to be framing. */}
      <div className={`fx fx--under fx--${state} ${classes}`} aria-hidden="true">
        <div className="fx__vignette" />
        <div className="fx__bloom" />
        <VoiceBloom still={still} />

        {/* Telemetry down both margins. Dropped when motion is reduced: the
            busiest thing here and the least important. */}
        {!still && !calm && (
          <>
            <div className="fx__stream fx__stream--left">
              {columns.map((column, i) => (
                <span key={i} className="fx__column"
                      style={{ animationDuration: `${column.duration}s`,
                               animationDelay: `${column.delay}s` }}>
                  {column.text}
                </span>
              ))}
            </div>
            <div className="fx__stream fx__stream--right">
              {columns.map((column, i) => (
                <span key={i} className="fx__column"
                      style={{ animationDuration: `${column.duration + 5}s`,
                               animationDelay: `${column.delay - 2}s` }}>
                  {column.text}
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      {/* In front of the interface.
          Only the things that are properties of the screen rather than of
          the scene -- scanlines, the scan band, a dropout. They cross the
          whole display including the controls, which is the point, so they
          are kept faint enough to read straight through. */}
      <div className={`fx fx--over fx--${state} ${classes}`} aria-hidden="true">
        <div className="fx__scanlines" />
        {!still && <div className="fx__sweep" />}
        <Dropout state={state} still={still} />
      </div>
    </>
  );
}

/**
 * A glow that breathes with the actual audio amplitude.
 *
 * Its own component because amplitude changes every frame while JARVIS
 * speaks. Keeping that subscription on a leaf that renders one empty div
 * means a frame costs one tiny reconciliation rather than re-running the
 * whole atmosphere.
 */
function VoiceBloom({ still }: { still: boolean }) {
  const amplitude = useStore((s) => s.amplitude);
  if (still) return null;
  return (
    <div
      className="fx__voice"
      style={{
        opacity: Math.min(0.55, amplitude * 0.5),
        transform: `scale(${1 + Math.min(0.35, amplitude * 0.3)})`,
      }}
    />
  );
}

/**
 * A brief dropout when something fails.
 *
 * Fires on the transition into an error, not for as long as the error lasts:
 * a permanent glitch is noise, and an interface that is still shaking five
 * minutes later has stopped telling you anything. One flicker, then it
 * leaves the steady red vignette to carry the message.
 */
function Dropout({ state, still }: { state: string; still: boolean }) {
  const [glitching, setGlitching] = useState(false);
  const previous = useRef(state);

  useEffect(() => {
    const was = previous.current;
    previous.current = state;
    if (still) return;
    if (state !== 'error' || was === 'error') return;

    setGlitching(true);
    const timer = window.setTimeout(() => setGlitching(false), 520);
    return () => window.clearTimeout(timer);
  }, [state, still]);

  return glitching ? <div className="fx__dropout" /> : null;
}
