/**
 * Adaptive particle quality.
 *
 * Separate from the renderer because the interesting part is the hysteresis,
 * and that deserves to be testable without a GPU.
 *
 * The rule this replaced was a one-way door: one bad two-second window and
 * the field was permanently poorer until the page was reloaded. Anything
 * transient tripped it -- a tab switch, another application taking the GPU
 * for a moment -- and nothing ever put it back.
 */

// Adaptive quality. The gap between the two thresholds is deliberate: a
// single number would have the field flipping quality back and forth every
// couple of seconds on a machine sitting right at the boundary.
export const DEGRADE_BELOW_FPS = 34;
export const RESTORE_ABOVE_FPS = 52;
/** Two bad windows (~4s) before dropping; five good ones (~10s) before going
 *  back, because a stutter is cheap to ride out and a rebuild is not. */
export const BAD_WINDOWS_TO_DEGRADE = 2;
export const GOOD_WINDOWS_TO_RESTORE = 5;

export interface QualityState {
  degraded: boolean;
  badWindows: number;
  goodWindows: number;
}

/**
 * Should the field change quality, given the frame rate it just measured?
 *
 * Pure, and separate from the renderer, because the interesting part is the
 * hysteresis and that is worth being able to test without a GPU. The rule it
 * replaced was a one-way door -- one bad two-second window and the field was
 * permanently poorer until the page was reloaded.
 */
export function qualityDecision(
  average: number, state: QualityState,
): { next: QualityState; action: 'degrade' | 'restore' | 'none' } {
  let { degraded, badWindows, goodWindows } = state;

  if (average < DEGRADE_BELOW_FPS) {
    goodWindows = 0;
    badWindows += 1;
  } else if (average > RESTORE_ABOVE_FPS) {
    badWindows = 0;
    goodWindows += 1;
  } else {
    // In the dead band: not evidence either way, so nothing moves.
    return { next: state, action: 'none' };
  }

  if (!degraded && badWindows >= BAD_WINDOWS_TO_DEGRADE) {
    return { next: { degraded: true, badWindows: 0, goodWindows: 0 }, action: 'degrade' };
  }
  if (degraded && goodWindows >= GOOD_WINDOWS_TO_RESTORE) {
    return { next: { degraded: false, badWindows: 0, goodWindows: 0 }, action: 'restore' };
  }
  return { next: { degraded, badWindows, goodWindows }, action: 'none' };
}
