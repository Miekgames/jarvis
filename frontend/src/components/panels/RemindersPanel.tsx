import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Reminders: scheduled notifications, once or repeating.
 *
 * "When" is typed the way you'd say it -- "every weekday at 7:30", "tomorrow
 * at 9", "in 20 minutes" -- and the line underneath shows exactly how it was
 * read before anything is saved. When one goes off it shows here, is spoken if
 * this page is open, and pops up as a Windows notification either way.
 */

interface Reminder {
  id: string;
  message: string;
  says: string;
  repeats: boolean;
  next: string | null;
  next_said: string;
}

interface Preview { ok: boolean; says?: string; repeats?: boolean; first?: string | null; assumed?: string; error?: string }

export function RemindersPanel() {
  const [items, setItems] = useState<Reminder[]>([]);
  const [message, setMessage] = useState('');
  const [when, setWhen] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setItems((await api.get<{ items: Reminder[] }>('/reminders')).items);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Reminders could not load.');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  // A reminder set by asking appears here too.
  useEffect(() => {
    const again = () => void load();
    window.addEventListener('jarvis:reminders', again);
    return () => window.removeEventListener('jarvis:reminders', again);
  }, [load]);

  // How the "when" is read, as it is typed.
  useEffect(() => {
    if (!when.trim()) { setPreview(null); return; }
    const timer = window.setTimeout(async () => {
      try {
        setPreview(await api.post<Preview>('/reminders/preview', { when }));
      } catch { setPreview(null); }
    }, 300);
    return () => window.clearTimeout(timer);
  }, [when]);

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const made = await api.post<Reminder & { assumed: string }>('/reminders', { message, when });
      useStore.getState().notify(`Reminder set: ${made.says}.`);
      setMessage('');
      setWhen('');
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'That reminder could not be saved.');
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (id: string) => {
    try {
      await api.del(`/reminders/${id}`);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'It could not be cancelled.');
    }
    await load();
  };

  return (
    <>
      <div className="panel__section">
        <p className="control__help">
          Or just ask: “remind me at 7pm to call Dad”, “every weekday at 7:30 remind me to take my
          tablets”, or <code>/remind</code> followed by what and when.
        </p>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Coming up · {items.length}</div>
        {items.length === 0 && <p className="empty">No reminders scheduled.</p>}
        {items.map((item) => (
          <div className="cd-row" key={item.id}>
            <div className="cd-row__top">
              <strong>{item.message}</strong>
              <button className="mini-action mini-action--danger" onClick={() => void cancel(item.id)}>
                Cancel
              </button>
            </div>
            <div className="control__help">
              {item.says}{item.repeats ? ` · next ${item.next_said}` : ''}
            </div>
          </div>
        ))}
      </div>

      <div className="panel__section">
        <div className="panel__section-title">New reminder</div>
        <form className="cd-form" onSubmit={add}>
          <label className="cd-form__row">
            <span>Remind me to</span>
            <input className="text-input" value={message} maxLength={200}
                   placeholder="call Dad" onChange={(e) => setMessage(e.target.value)} />
          </label>
          <label className="cd-form__row">
            <span>When</span>
            <input className="text-input" value={when} maxLength={80}
                   placeholder="every Monday at 9am, tomorrow at 7pm, in 20 minutes…"
                   onChange={(e) => setWhen(e.target.value)} />
          </label>
          {preview && (preview.ok
            ? <p className="cd-form__found">
                {preview.repeats ? 'Repeats: ' : 'Once: '}{preview.says}.
                {preview.assumed ? ` ${preview.assumed}` : ''}
              </p>
            : <p className="cd-form__error">{preview.error}</p>)}
          {error && <p className="cd-form__error" role="alert">{error}</p>}
          <div className="cd-form__actions">
            <button className="btn btn--small btn--primary"
                    disabled={busy || !message.trim() || !preview?.ok}>
              {busy ? 'Saving…' : 'Set reminder'}
            </button>
          </div>
        </form>
      </div>
    </>
  );
}
