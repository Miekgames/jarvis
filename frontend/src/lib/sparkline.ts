/**
 * Sparkline geometry.
 *
 * Separate from the component because the interesting cases are missing data
 * and not-enough data -- GPU and battery are absent on plenty of machines,
 * and a reading can drop for a tick -- and those are far easier to be sure
 * about as numbers than as pixels in a screenshot.
 */

export interface SparkPoints {
  /** `x,y` pairs for a polyline. */
  line: string;
  /** Where the newest reading sits, for the head marker. */
  headX: number;
  headY: number;
}

/**
 * Turn readings into polyline coordinates, oldest first.
 *
 * Returns null when there is not enough to draw. One reading is not a trend,
 * and a flat line in that case would be a claim about data that does not
 * exist -- an empty box says "not yet" honestly.
 *
 * The scale is fixed rather than fitted to the data. Auto-scaling would make
 * 1%-to-2% jitter look like a crisis, which is the opposite of what a glance
 * at a trend line should tell you.
 */
export function buildPoints(
  values: (number | null | undefined)[],
  max: number,
  width: number,
  height: number,
): SparkPoints | null {
  const usable = values.filter(
    (v): v is number => typeof v === 'number' && !Number.isNaN(v));
  if (usable.length < 2) return null;

  const span = Math.max(max, 1);
  const top = 1.2;                       // room for the head marker
  const usableHeight = Math.max(height - top * 2, 1);
  const step = width / (usable.length - 1);

  const coords = usable.map((value, i) => {
    const clamped = Math.min(Math.max(value, 0), span);
    return [round(i * step), round(top + (1 - clamped / span) * usableHeight)] as const;
  });

  const last = coords[coords.length - 1];
  return {
    line: coords.map(([x, y]) => `${x},${y}`).join(' '),
    headX: last[0],
    headY: last[1],
  };
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}
