import { useEffect, useState } from 'react';

import { useStore } from '../state/store';
import { voice } from '../lib/voice';
import { matchWakeWord } from '../lib/wakeword';

/**
 * Sleep mode.
 *
 * The particle stage is unmounted entirely (see ParticleStage), so this really
 * is a black screen with nothing rendering behind it. Only wake listeners stay
 * active: click, key press, wake word.
 */
export function SleepVeil() {
  const sleeping = useStore((s) => s.sleeping);
  const wake = useStore((s) => s.wake);
  const settings = useStore((s) => s.settings);
  const [hint, setHint] = useState(false);

  const wakeWord = settings.wake_word ?? 'jarvis';
  const wakeWordEnabled = (settings.voice ?? {}).wake_word_enabled;

  // -- click / key wake ----------------------------------------------------
  useEffect(() => {
    if (!sleeping) return;

    const onWake = () => void wake();
    const onKey = (event: KeyboardEvent) => {
      // Ignore bare modifiers so an accidental Shift doesn't wake the machine.
      if (['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) return;
      onWake();
    };

    window.addEventListener('click', onWake);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('click', onWake);
      window.removeEventListener('keydown', onKey);
    };
  }, [sleeping, wake]);

  // -- wake word -----------------------------------------------------------
  useEffect(() => {
    if (!sleeping || !wakeWordEnabled || !voice.recognitionSupported) return;

    let active = true;
    const listen = () => {
      if (!active) return;
      voice.startListening(
        (transcript, final) => {
          // Same tolerant match as the awake loop: an exact-string check almost
          // never fires, because the recogniser rarely returns the word verbatim.
          if (final && matchWakeWord(transcript, String(wakeWord)).hit) {
            active = false;
            voice.stopListening();
            void wake();
          }
        },
        () => { if (active) window.setTimeout(listen, 900); },
        true,
        'wake',
      );
    };
    listen();

    return () => {
      active = false;
      voice.stopListening();
    };
  }, [sleeping, wakeWordEnabled, wakeWord, wake]);

  // Fade the hint in after a moment so the screen is genuinely black first.
  useEffect(() => {
    if (!sleeping) {
      setHint(false);
      return;
    }
    const timer = window.setTimeout(() => setHint(true), 2600);
    return () => window.clearTimeout(timer);
  }, [sleeping]);

  if (!sleeping) return null;

  return (
    <div className="sleep" role="button" tabIndex={0} aria-label="JARVIS is asleep. Click or press any key to wake.">
      {hint && (
        <div className="sleep__hint">
          {wakeWordEnabled
            ? `Click, press a key, or say "${wakeWord}" to wake`
            : 'Click or press any key to wake'}
        </div>
      )}
    </div>
  );
}
