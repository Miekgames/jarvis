import { useCallback, useEffect, useState } from 'react';

import { api } from '../../lib/api';

/**
 * The notifications Windows itself has received.
 *
 * Read through `UserNotificationListener`, the API Windows provides for
 * exactly this. Windows asks the user for permission itself, on top of the
 * one in this application -- neither of which this screen can grant.
 *
 * Notification text is some of the most sensitive material on a machine:
 * message previews, two-factor codes, calendar entries. It is held in memory
 * while it is current and discarded when watching stops. Nothing reaches the
 * database and no notification text is logged. That is stated on the screen
 * as well as in the code, because it is the sort of claim a person should be
 * able to check rather than take on trust.
 */

interface DesktopItem {
  id: string;
  app: string;
  title: string;
  body: string;
  at: string;
}

interface State {
  running: boolean;
  status: string;
  detail: string;
  supported: boolean;
  available: boolean;
  permitted: boolean;
  count: number;
  muted: string[];
  note: string;
  items: DesktopItem[];
}

export function DesktopNotifications() {
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setState(await api.get<State>('/desktop/notifications?limit=40'));
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Arriving notifications come over the websocket; refresh on one rather
  // than polling a second time on top of the backend's own poll.
  useEffect(() => {
    const onArrival = () => void load();
    window.addEventListener('jarvis:desktop-notification', onArrival);
    return () => window.removeEventListener('jarvis:desktop-notification', onArrival);
  }, [load]);

  const mute = async (app: string, muted: boolean) => {
    try {
      setState(await api.post<State>('/desktop/notifications/mute', { app, muted }));
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const toggle = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post(state?.running
        ? '/desktop/notifications/stop'
        : '/desktop/notifications/start', {});
      await load();
    } catch (exc) {
      setError((exc as Error).message);
      await load();
    } finally {
      setBusy(false);
    }
  };

  if (!state) {
    return <p className="control__help">{error || 'Checking…'}</p>;
  }

  if (!state.supported || !state.available) {
    return (
      <>
        <div className="control__help">{state.detail}</div>
      </>
    );
  }

  return (
    <>
      <div className="control control--stack">
        <div className="control__label">
          <span className={`wake-dot wake-dot--${
            state.running ? 'ok' : state.status === 'denied' ? 'bad' : 'idle'}`} />
          {state.running ? 'Reading this computer’s notifications' : 'Not reading notifications'}
        </div>
        <div className="control__help">{state.detail}</div>
        <div className="control__help">{state.note}</div>

        {!state.permitted && (
          <div className="control__help control__help--bad">
            The “Read desktop notifications” permission is off. Turn it on under
            Settings → Permissions first.
          </div>
        )}
        {error && <div className="control__help control__help--bad">{error}</div>}

        <button className="btn btn--small" onClick={() => void toggle()} disabled={busy}>
          {busy ? 'Working…' : state.running ? 'Stop reading' : 'Start reading'}
        </button>
      </div>

      {state.running && state.muted.length > 0 && (
        <div className="control control--stack">
          <div className="control__label">Muted</div>
          <div className="control__help">
            Dropped as they arrive — nothing from these is held at all.
          </div>
          <div className="muted-apps">
            {state.muted.map((app) => (
              <button key={app} className="muted-apps__chip"
                      title={`Show notifications from ${app} again`}
                      onClick={() => void mute(app, false)}>
                {app} <span aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {state.running && (
        <div className="heard" style={{ marginTop: '0.8rem' }}>
          {state.items.length === 0 && (
            <div className="control__help">
              Nothing in the Action Center yet. Anything that arrives will appear here.
            </div>
          )}
          {state.items.map((item) => (
            <div className="desktop-note" key={item.id}>
              <div className="desktop-note__head">
                <span className="desktop-note__app">{item.app}</span>
                <span className="desktop-note__time">{clock(item.at)}</span>
                <button
                  className="desktop-note__mute"
                  title={`Stop showing notifications from ${item.app}`}
                  onClick={() => void mute(item.app, true)}
                >
                  mute
                </button>
              </div>
              <div className="desktop-note__title">{item.title}</div>
              {item.body && <div className="desktop-note__body">{item.body}</div>}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
function clock(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
