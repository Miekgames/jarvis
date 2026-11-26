import { useState } from 'react';

import { api, ApiError } from '../lib/api';
import type { Countdown } from '../lib/countdown';

/**
 * Setting a countdown by hand: its name, its day, its time, its news topic.
 *
 * "Find the date" really looks: it asks several websites and says how many
 * agree. When they don't, it shows what each said and leaves the choosing to
 * you; it never fills in a guess.
 */

interface Found {
  status: 'sure' | 'unsure' | 'past' | 'none';
  day: string | null;
  sources: number;
  source_name: string;
  source_url: string;
  others: { day: string; sources: number }[];
}

export const longDay = (day: string) => {
  const [y, m, d] = day.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  });
};

const sites = (n: number) => `${n} site${n === 1 ? '' : 's'}`;

export function CountdownForm({ item, onDone, onCancel }: {
  /** The one being changed; none to add a new one. */
  item?: Countdown;
  onDone: (saved: Countdown) => void;
  onCancel?: () => void;
}) {
  const [title, setTitle] = useState(item?.title ?? '');
  const [day, setDay] = useState(item?.day ?? '');
  const [clock, setClock] = useState(item?.clock ?? '');
  const [topic, setTopic] = useState(item?.topic ?? '');
  const [found, setFound] = useState<Found | null>(null);
  const [finding, setFinding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const find = async () => {
    setFinding(true);
    setError('');
    setFound(null);
    try {
      const result = await api.post<Found>('/countdowns/find', { event: title });
      setFound(result);
      if (result.status === 'sure' && result.day) setDay(result.day);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'The date could not be looked up.');
    } finally {
      setFinding(false);
    }
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const saved = item
        ? await api.patch<Countdown>(`/countdowns/${item.id}`, { title, day, clock, topic })
        : await api.post<Countdown>('/countdowns', {
          title, day, clock,
          topic: topic.trim() ? topic : null,
          looked_up: found?.status === 'sure' && found.day === day,
        });
      onDone(saved);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'That could not be saved.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="cd-form" onSubmit={save}>
      <label className="cd-form__row">
        <span>Event</span>
        <input className="text-input" value={title} maxLength={70} placeholder="What are you waiting for?"
               onChange={(e) => { setTitle(e.target.value); setFound(null); }} />
      </label>

      <div className="cd-form__pair">
        <label className="cd-form__row">
          <span>Date</span>
          <input className="text-input" type="date" value={day}
                 onChange={(e) => setDay(e.target.value)} />
        </label>
        <label className="cd-form__row">
          <span>Time (optional)</span>
          <input className="text-input" type="time" value={clock}
                 onChange={(e) => setClock(e.target.value)} />
        </label>
      </div>

      <div className="cd-form__find">
        <button type="button" className="mini-action" disabled={!title.trim() || finding} onClick={() => void find()}>
          {finding ? 'Looking…' : 'Find the date'}
        </button>
        {clock && (
          <button type="button" className="mini-action" onClick={() => setClock('')}>No time</button>
        )}
      </div>

      {found && (
        <div className="cd-form__found" role="status">
          {found.status === 'sure' && found.day && (
            <>{longDay(found.day)}. {found.sources ? `${sites(found.sources)} agree` : 'A fixed day in the calendar'}
              {found.source_url && <> · <a href={found.source_url} target="_blank" rel="noopener noreferrer">{found.source_name}</a></>}</>
          )}
          {found.status === 'past' && found.day && (
            <>That has already happened: {longDay(found.day)}
              {found.source_url && <> (<a href={found.source_url} target="_blank" rel="noopener noreferrer">{found.source_name}</a>)</>}.</>
          )}
          {found.status === 'none' && <>No date turned up. It may not have one yet. You can set it yourself.</>}
          {found.status === 'unsure' && found.day && (
            <>
              Not enough sites agree, so the choice is yours:
              <div className="cd-form__maybe">
                {[{ day: found.day, sources: found.sources }, ...found.others].map((m) => (
                  <button type="button" key={m.day} className="mini-action" onClick={() => setDay(m.day)}>
                    {longDay(m.day)} · {sites(m.sources)}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      <label className="cd-form__row">
        <span>News about (optional)</span>
        <input className="text-input" value={topic} maxLength={80}
               placeholder={item ? 'Leave empty for no news' : 'Same as the event'}
               onChange={(e) => setTopic(e.target.value)} />
      </label>

      {error && <p className="cd-form__error" role="alert">{error}</p>}

      <div className="cd-form__actions">
        <button className="btn btn--small btn--primary" disabled={busy || !title.trim() || !day}>
          {busy ? 'Saving…' : item ? 'Save' : 'Add countdown'}
        </button>
        {onCancel && <button type="button" className="btn btn--small" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}
