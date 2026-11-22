import { useEffect } from 'react';

import { useStore } from '../state/store';

/**
 * The confirmation prompt.
 *
 * This is genuinely blocking: the backend tool call is awaiting the answer, so
 * nothing happens until the user decides. Rejecting means the action was not
 * performed, and JARVIS is told exactly that.
 */
export function Confirmation() {
  const confirmations = useStore((s) => s.confirmations);
  const resolve = useStore((s) => s.resolveConfirmation);

  const pending = confirmations[0];

  useEffect(() => {
    if (!pending) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        void resolve(pending.id, false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pending, resolve]);

  if (!pending) return null;

  const critical = pending.risk === 'critical';
  const args = pending.detail?.arguments;

  return (
    <div className="confirm-layer" role="dialog" aria-modal="true" aria-labelledby="confirm-text">
      <div className={`confirm ${critical ? 'confirm--critical' : ''}`}>
        <div className="confirm__kicker">
          <WarnIcon />
          {critical ? 'Critical action' : 'Confirmation required'}
          <span className={`risk-tag risk-tag--${pending.risk}`}>{pending.risk}</span>
        </div>

        <p className="confirm__text" id="confirm-text">{pending.summary}</p>

        {pending.tool === 'send_email' && args ? (
          <Letter args={args as Record<string, unknown>} />
        ) : args && Object.keys(args).length > 0 && (
          <pre className="confirm__detail">{JSON.stringify(args, null, 2)}</pre>
        )}

        <div className="confirm__actions">
          <button className="btn" onClick={() => void resolve(pending.id, false)} autoFocus>
            No, cancel
          </button>
          <button
            className={`btn ${critical ? 'btn--danger' : 'btn--primary'}`}
            onClick={() => void resolve(pending.id, true)}
          >
            Yes, do it
          </button>
        </div>

        {confirmations.length > 1 && (
          <p style={{ marginTop: '0.9rem', fontSize: '0.72rem', color: 'var(--ink-ghost)' }}>
            {confirmations.length - 1} more waiting
          </p>
        )}
      </div>
    </div>
  );
}

/** An email about to be sent, shown the way it will read -- not as JSON. */
function Letter({ args }: { args: Record<string, unknown> }) {
  const text = (key: string) => (typeof args[key] === 'string' ? (args[key] as string) : '');
  return (
    <div className="confirm__letter">
      <div><span className="confirm__letter-key">To</span>{text('to')}</div>
      {text('cc') && <div><span className="confirm__letter-key">Cc</span>{text('cc')}</div>}
      <div><span className="confirm__letter-key">Subject</span>
        {text('subject') || (text('reply_to') ? 'Re: (the email it replies to)' : '(no subject)')}</div>
      {text('replying_to') && (
        <div><span className="confirm__letter-key">Answers</span>{text('replying_to')}</div>
      )}
      <pre className="confirm__letter-body">{text('body')}</pre>
    </div>
  );
}

const WarnIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
    <line x1="12" y1="9" x2="12" y2="13" />
    <line x1="12" y1="17" x2="12.01" y2="17" />
  </svg>
);
