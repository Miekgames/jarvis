import { useEffect, useState } from 'react';

import { useStore } from '../state/store';

/**
 * The startup status sequence. Lines appear as the backend reports each
 * subsystem coming up — these are real boot events over the WebSocket, not a
 * scripted animation.
 */
export function BootSequence() {
  const steps = useStore((s) => s.bootSteps);
  const booted = useStore((s) => s.booted);
  const connected = useStore((s) => s.connected);
  const [dismissed, setDismissed] = useState(false);
  const [revealed, setRevealed] = useState(0);

  // Reveal lines with a slight stagger so a fast boot still reads as a sequence.
  useEffect(() => {
    if (revealed >= steps.length) return;
    const timer = window.setTimeout(() => setRevealed((n) => n + 1), 110);
    return () => window.clearTimeout(timer);
  }, [revealed, steps.length]);

  useEffect(() => {
    if (!booted || revealed < steps.length) return;
    const timer = window.setTimeout(() => setDismissed(true), 900);
    return () => window.clearTimeout(timer);
  }, [booted, revealed, steps.length]);

  // Never trap the user behind a boot screen if the backend can't be reached.
  useEffect(() => {
    const timer = window.setTimeout(() => setDismissed(true), 12000);
    return () => window.clearTimeout(timer);
  }, []);

  if (dismissed) return null;

  const complete = booted && revealed >= steps.length;

  return (
    <div className={`boot ${complete ? 'boot--done' : ''}`} role="status" aria-live="polite">
      <div className="boot__inner">
        <div className="boot__title">System initialising</div>

        {steps.slice(0, revealed).map((step, index) => (
          <div key={`${step.key}-${index}`}>
            <div className="boot__line" style={{ animationDelay: `${index * 20}ms` }}>
              <span>{step.label}</span>
              <span className="boot__dots" />
              <span className={`boot__status boot__status--${step.status}`}>
                {step.status.toUpperCase()}
              </span>
            </div>
            {step.detail && <div className="boot__detail">{step.detail}</div>}
          </div>
        ))}

        {!steps.length && (
          <div className="boot__line">
            <span>{connected ? 'WAITING FOR BACKEND' : 'CONNECTING'}</span>
            <span className="boot__dots" />
            <span className="boot__status boot__status--degraded">…</span>
          </div>
        )}

        {complete && <div className="boot__final">JARVIS ONLINE</div>}
      </div>
    </div>
  );
}
