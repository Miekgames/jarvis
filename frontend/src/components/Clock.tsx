import { useEffect, useState } from 'react';

import { useStore } from '../state/store';

/**
 * The time, above the core.
 *
 * Typographic rather than boxed, so it reads as part of the field rather
 * than as a widget placed on top of it — the same reason nothing else here
 * has a border.
 *
 * It ticks on the second but only re-renders when the displayed string
 * actually changes. A component that sets state sixty times a minute while
 * a WebGL field is running is a component that shows up in a frame budget.
 */

export function Clock() {
  const [now, setNow] = useState(() => new Date());
  const sleeping = useStore((s) => s.sleeping);

  useEffect(() => {
    if (sleeping) return;          // nothing renders while asleep anyway
    const tick = () => setNow(new Date());
    // Line up with the start of the next second, so the display changes
    // when the clock does rather than up to a second late.
    const delay = 1000 - (Date.now() % 1000);
    let interval: number | undefined;
    const timeout = window.setTimeout(() => {
      tick();
      interval = window.setInterval(tick, 1000);
    }, delay);

    return () => {
      window.clearTimeout(timeout);
      if (interval) window.clearInterval(interval);
    };
  }, [sleeping]);

  if (sleeping) return null;

  const time = now.toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const seconds = String(now.getSeconds()).padStart(2, '0');
  const date = now.toLocaleDateString([], {
    weekday: 'long', day: 'numeric', month: 'long',
  });
  // On a phone the long date ran into "STANDING BY": it shows this instead.
  const shortDate = now.toLocaleDateString([], {
    weekday: 'short', day: 'numeric', month: 'short',
  });

  return (
    <div className="clock" aria-hidden="true">
      <div className="clock__time">
        {time}
        <span className="clock__seconds">{seconds}</span>
      </div>
      <div className="clock__date">
        <span className="clock__date-long">{date}</span>
        <span className="clock__date-short">{shortDate}</span>
      </div>
    </div>
  );
}
