import { buildPoints } from '../lib/sparkline';

/**
 * A trend line small enough to sit beside a number.
 *
 * The sidebar used to say "CPU 51%" and nothing else, which tells you where
 * a value is but not where it is going -- and for the things on that list,
 * the direction is usually the interesting part. 51% climbing steadily is a
 * different situation from 51% that has been flat all afternoon.
 *
 * Deliberately unlabelled and unscaled on screen: it is a shape, not a
 * second readout. The number beside it is the measurement.
 */

interface Props {
  /** Oldest first. Nulls and gaps are expected -- GPU and battery are not
   *  present on every machine, and a reading can be missing for a tick. */
  values: (number | null | undefined)[];
  /** Fixed upper bound, so percentages share one scale and a line does not
   *  rescale itself into looking dramatic. */
  max?: number;
  width?: number;
  height?: number;
  className?: string;
}

export function Sparkline({ values, max = 100, width = 46, height = 13, className = '' }: Props) {
  const points = buildPoints(values, max, width, height);

  // Fewer than two readings is not a trend. An empty box says "not enough
  // yet" honestly; a flat line would be a claim about the data.
  if (!points) return <svg className={`spark ${className}`} width={width} height={height} aria-hidden="true" />;

  return (
    <svg className={`spark ${className}`} width={width} height={height}
         viewBox={`0 0 ${width} ${height}`} aria-hidden="true" preserveAspectRatio="none">
      <polyline className="spark__line" points={points.line} />
      <circle className="spark__head" cx={points.headX} cy={points.headY} r="1.4" />
    </svg>
  );
}
