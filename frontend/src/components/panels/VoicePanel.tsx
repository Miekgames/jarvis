import { useCallback, useEffect, useState } from 'react';

import { api } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * What the microphone has heard.
 *
 * An always-on microphone should be able to show its work. Everything here
 * was already being recorded -- a detection writes its confidence, a
 * transcription writes the text -- but it sat scattered through a general
 * activity feed among schedule ticks and health checks, which is no use to
 * someone asking the only two questions that matter: is it listening to me,
 * and what does it think I said?
 *
 * No audio is stored anywhere, and this does not change that. It is a record
 * of decisions, not a recording.
 */

interface VoiceEvent {
  kind: 'detection' | 'command';
  ts: string;
  score: number | null;
  device: string;
  model: string;
  heard: string | null;
}

export function VoicePanel() {
  const [events, setEvents] = useState<VoiceEvent[] | null>(null);
  const [error, setError] = useState('');
  const hotword = useStore((s) => s.hotword);
  const wake = useStore((s) => s.wakeStatus);
  const send = useStore((s) => s.send);

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ events: VoiceEvent[] }>('/voice/log?limit=50');
      setEvents(result.events);
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Refresh when something is actually heard, rather than polling: the
  // detection already arrives over the websocket.
  useEffect(() => {
    const onHeard = () => { window.setTimeout(() => void load(), 2500); };
    window.addEventListener('jarvis:wakeword', onHeard);
    return () => window.removeEventListener('jarvis:wakeword', onHeard);
  }, [load]);

  const listening = hotword?.running && hotword.phase !== 'failed';

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Right now</div>
        <div className="control control--stack">
          <div className="control__label">
            <span className={`wake-dot wake-dot--${
              wake.phase === 'armed' || wake.phase === 'triggered' ? 'ok'
                : wake.phase === 'failed' ? 'bad'
                : wake.phase === 'silent' ? 'warn' : 'idle'}`} />
            {listening ? 'Listening' : 'Not listening'}
          </div>
          <div className="control__help">
            {hotword?.detail || wake.detail || 'The wake word is switched off.'}
          </div>
          {listening && (
            <div className="control__help control__help--mono">
              {hotword?.device?.name}
              {hotword?.detections != null && ` · ${hotword.detections} this session`}
              {hotword?.local_commands ? ' · transcribing here' : ' · browser microphone'}
            </div>
          )}
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Heard</div>
        <p className="panel__note">
          Every time the wake word fired, and what was transcribed next. No audio is
          kept — this is a record of decisions, not a recording.
        </p>

        {error && <div className="control__help control__help--bad">{error}</div>}
        {events === null && !error && <div className="control__help">Loading…</div>}
        {events?.length === 0 && (
          <div className="control__help">
            Nothing yet. Say “hey {useStore.getState().settings?.wake_word ?? 'jarvis'}”
            and it will appear here.
          </div>
        )}

        <div className="heard">
          {events?.map((event, i) => (
            <div className="heard__row" key={`${event.ts}-${i}`}>
              <span className="heard__time">{clock(event.ts)}</span>
              <span className={`heard__score ${confidenceTone(event.score)}`}>
                {event.score == null ? 'BUTTON' : `${Math.round(event.score * 100)}%`}
              </span>
              {event.heard
                ? (
                  <button
                    className="heard__text heard__text--again"
                    title="Run this again"
                    onClick={() => void send(event.heard as string, 'text')}
                  >
                    {event.heard}
                  </button>
                )
                : <span className="heard__text heard__text--empty">nothing followed</span>}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function clock(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime())
    ? '--:--'
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** Low-confidence detections are the interesting ones: they are where a
 *  false trigger or a near-miss shows up. */
function confidenceTone(score: number | null): string {
  if (score == null) return 'heard__score--none';
  if (score < 0.75) return 'heard__score--low';
  return '';
}
