import { useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { inOrder, left, partsOf, targetParts } from '../../lib/countdown';
import { useStore } from '../../state/store';
import { CountdownForm, longDay } from '../CountdownForm';
import { Arrived, Units, useCountdowns, useNow } from '../Countdowns';

/**
 * Countdowns: add one, set its date and time by hand, change or remove one.
 *
 * The ones here are the ones on the main screen. Up to three at a time, so
 * they stay something you glance at rather than a list you read.
 */
export function CountdownsPanel() {
  const { items, limit, load } = useCountdowns();
  const now = useNow(items.length > 0);
  const [editing, setEditing] = useState<string | null>(null);
  const [added, setAdded] = useState(0);       // a fresh form after each one
  const [error, setError] = useState('');

  const remove = async (id: string) => {
    setError('');
    try {
      await api.del(`/countdowns/${id}`);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'It could not be removed.');
    }
    await load();
  };

  const here = partsOf(now);
  return (
    <>
      <div className="panel__section">
        <p className="control__help">
          Shown under the clock on the main screen. Click one there for its latest news. You can also
          just ask: “countdown to …”, or <code>/countdown</code> and the name of an event.
        </p>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Running · {items.length} of {limit}</div>
        {items.length === 0 && <p className="empty">None yet.</p>}
        {error && <p className="cd-form__error" role="alert">{error}</p>}
        {inOrder(items, here).map((item) => {
          const l = left(here, targetParts(item));
          return (
            <div className="cd-row" key={item.id}>
              <div className="cd-row__top">
                <strong>{item.title}</strong>
                {l ? <Units l={l} /> : <Arrived item={item} now={now} />}
              </div>
              <div className="control__help">
                {longDay(item.day)}{item.clock && ` at ${item.clock}`}
                {item.origin === 'web'
                  ? ` · ${item.sources > 1 ? `${item.sources} sites agree` : item.source_name}`
                  : ' · set by you'}
              </div>
              {editing === item.id ? (
                <CountdownForm item={item} onCancel={() => setEditing(null)}
                               onDone={() => { setEditing(null); void load(); }} />
              ) : (
                <div className="cd-row__actions">
                  <button className="mini-action" onClick={() => setEditing(item.id)}>Edit date, time, name</button>
                  <button className="mini-action mini-action--danger" onClick={() => void remove(item.id)}>Remove</button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Add a countdown</div>
        {items.length >= limit ? (
          <p className="control__help">
            {limit} are running, which is the most at once. Remove one to add another.
          </p>
        ) : (
          <CountdownForm key={added} onDone={(saved) => {
            setAdded((n) => n + 1);
            useStore.getState().notify(`Counting down to ${saved.title}.`);
            void load();
          }} />
        )}
      </div>
    </>
  );
}
