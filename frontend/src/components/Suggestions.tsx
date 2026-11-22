import { useEffect, useState } from 'react';

import { api, eventStream } from '../lib/api';
import { useStore } from '../state/store';

interface Suggestion {
  id: string;
  kind: string;
  title: string;
  body: string;
  confidence: number;
  reason: string;
}

/**
 * Proactive suggestions.
 *
 * Deliberately small and dismissible, anchored away from the core. A suggestion
 * is an offer, never something already done — and the reasoning is always one
 * click away, so an unwanted suggestion is explainable rather than mysterious.
 */
export function Suggestions() {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [showReason, setShowReason] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const sleeping = useStore((s) => s.sleeping);
  const send = useStore((s) => s.send);
  const notify = useStore((s) => s.notify);

  // Initial load plus live updates.
  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const result = await api.get<{ pending: Suggestion[] }>('/suggestions');
        if (!cancelled) setItems(result.pending ?? []);
      } catch { /* transient */ }
    };
    void load();

    const unsubscribe = eventStream.subscribe((event) => {
      if (event.type === 'suggestion.offered') {
        setItems((current) =>
          current.some((s) => s.id === event.data.id)
            ? current
            : [...current, event.data as Suggestion]);
      } else if (event.type === 'suggestion.resolved') {
        setItems((current) => current.filter((s) => s.id !== event.data.id));
      } else if (event.type === 'suggestion.run_command') {
        // The backend accepted a "run this" suggestion; put it through the
        // normal conversation path so tools and confirmations still apply.
        if (event.data.text) void send(event.data.text, 'suggestion');
      }
    });

    return () => { cancelled = true; unsubscribe(); };
  }, [send]);

  const resolve = async (id: string, accepted: boolean) => {
    setBusy(id);
    setItems((current) => current.filter((s) => s.id !== id));
    try {
      const result = await api.post<{ result?: Record<string, any> }>(
        `/suggestions/${id}`, { accepted });
      if (accepted && result.result?.error) notify(`That didn't work: ${result.result.error}`);
    } catch (error) {
      notify(`Couldn't respond to the suggestion: ${(error as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  if (sleeping || items.length === 0) return null;
  const suggestion = items[0];

  return (
    <div className="suggestion" role="status">
      <div className="suggestion__kind">
        {suggestion.kind}
        <span className="suggestion__confidence" title="How confident JARVIS is">
          {Math.round((suggestion.confidence ?? 0) * 100)}%
        </span>
      </div>

      <div className="suggestion__title">{suggestion.title}</div>
      {suggestion.body && <div className="suggestion__body">{suggestion.body}</div>}

      {showReason === suggestion.id && suggestion.reason && (
        <div className="suggestion__reason">{suggestion.reason}</div>
      )}

      <div className="suggestion__actions">
        <button className="mini-action" disabled={busy === suggestion.id}
                onClick={() => void resolve(suggestion.id, true)}>
          Yes
        </button>
        <button className="mini-action" disabled={busy === suggestion.id}
                onClick={() => void resolve(suggestion.id, false)}>
          Not now
        </button>
        {suggestion.reason && (
          <button
            className="mini-action"
            onClick={() => setShowReason(showReason === suggestion.id ? null : suggestion.id)}
          >
            {showReason === suggestion.id ? 'Hide why' : 'Why?'}
          </button>
        )}
        {items.length > 1 && (
          <span className="row__meta">+{items.length - 1} more</span>
        )}
      </div>
    </div>
  );
}
