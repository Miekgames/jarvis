import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '../lib/api';
import { useStore } from '../state/store';
import { claimUtterance, speakerLease } from '../lib/leader';
import { loadDraft, recallList, rememberSent, saveDraft } from '../lib/recall';
import { speakable, voice } from '../lib/voice';
import { matchWakeWord } from '../lib/wakeword';

/**
 * The command interface: text, voice, push-to-talk and wake word, in a shell
 * that sits inside the particle environment rather than looking like a form.
 */
/** How long after a trigger to ignore further matches of the same utterance. */
const WAKE_COOLDOWN_MS = 2500;

/** How long after JARVIS stops speaking to distrust the microphone.
 *  Recognition delivers buffered audio late, so the tail of a spoken reply
 *  can arrive as a "new" transcript a second after the speakers go quiet. */
const ECHO_WINDOW_MS = 1800;

/** More triggers than this inside the window means something is feeding the
 *  microphone, not that you said the wake word five times. */
const RUNAWAY_LIMIT = 4;
const RUNAWAY_WINDOW_MS = 20000;

/**
 * Does this transcript look like JARVIS hearing its own last reply?
 *
 * Compared word-by-word rather than as a substring: recognition of speech
 * played through speakers is lossy, so the transcript is never an exact match
 * for what was said. A high overlap of distinctive words is the usable signal.
 */
function echoesOwnSpeech(transcript: string, spoken: string): boolean {
  if (!spoken || !transcript) return false;
  const words = (text: string) =>
    text.toLowerCase().replace(/[^\w\s]/g, ' ').split(/\s+/).filter((w) => w.length > 3);

  const heard = words(transcript);
  if (heard.length < 3) return false;          // too short to judge
  const said = new Set(words(spoken));
  if (!said.size) return false;

  const shared = heard.filter((word) => said.has(word)).length;
  return shared / heard.length >= 0.6;
}

/**
 * A short rising two-tone chirp, so you know the wake word registered without
 * having to watch the screen. Synthesised rather than loaded: no asset, no
 * network, and it cannot fail to decode.
 */
function cue() {
  try {
    const Ctx = window.AudioContext ?? (window as any).webkitAudioContext;
    if (!Ctx) return;
    const context = new Ctx();
    const gain = context.createGain();
    gain.connect(context.destination);
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.09, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22);

    const tone = context.createOscillator();
    tone.type = 'sine';
    tone.frequency.setValueAtTime(660, context.currentTime);
    tone.frequency.linearRampToValueAtTime(990, context.currentTime + 0.18);
    tone.connect(gain);
    tone.start();
    tone.stop(context.currentTime + 0.24);
    tone.onended = () => void context.close();
  } catch {
    /* audio is a courtesy — never let it break the wake word */
  }
}

export function CommandBar() {
  // Whatever was half-typed when the page was closed or reloaded -- JARVIS
  // restarting reloads the page, and losing a long message to that is
  // exactly the kind of small cruelty an interface should not commit.
  const [text, setTextState] = useState(loadDraft);
  const setText = useCallback((value: string) => {
    setTextState(value);
    saveDraft(value);
  }, []);
  // Stepping back through earlier messages with ↑ / ↓, like a terminal.
  // -1 means "not recalling"; `draftRef` is what ↓ returns to at the end.
  const recallRef = useRef(-1);
  const draftRef = useRef('');
  const [interim, setInterim] = useState('');
  const [listening, setListening] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const wakeRef = useRef(false);
  // Interim results repeat for the same utterance, so a trigger has to be
  // rate-limited or one "Jarvis, open X" fires several times.
  const lastTriggerRef = useRef(0);
  const recentTriggersRef = useRef<number[]>([]);
  // Whether the last thing the user sent was spoken rather than typed.
  const lastInputWasSpokenRef = useRef(false);
  // Lets the onEnd closure retry without startListening depending on itself.
  const startListeningRef = useRef<
    ((continuous?: boolean, isRetry?: boolean) => Promise<void>) | null>(null);
  // Serialises wake-word start/stop so overlapping effect runs cannot fight.
  const syncRef = useRef<Promise<void>>(Promise.resolve());
  // Identifies one run of the browser fallback loop, so a superseded run
  // cannot keep listening alongside its replacement.
  const generationRef = useRef(0);

  const send = useStore((s) => s.send);
  const busy = useStore((s) => s.busy);
  const setState = useStore((s) => s.setState);
  const setAmplitude = useStore((s) => s.setAmplitude);
  const settings = useStore((s) => s.settings);
  const utterances = useStore((s) => s.utterances);
  const sleeping = useStore((s) => s.sleeping);
  const hotword = useStore((s) => s.hotword);

  const voiceSettings = settings.voice ?? {};
  const shortcuts = settings.shortcuts ?? {};
  const voiceEnabled = voiceSettings.enabled !== false;
  /** Have settings actually arrived? Until they do, `wake_word_enabled` is
   *  undefined, which is not the same as false -- acting on it stops a
   *  detector the backend started on purpose. */
  const settingsReady = Object.keys(settings).length > 0;
  const wakeWanted = voiceEnabled && !!voiceSettings.wake_word_enabled && !sleeping;

  /** Is the local keyword spotter the thing currently listening for the wake
   *  word? If so the browser fallback must stay out of the way entirely. */
  const localWakeActive = !!hotword?.running && hotword.phase !== 'failed';

  /** Is the backend also capturing and transcribing the command itself? */
  const localCommands = localWakeActive && !!hotword?.local_commands;

  // -- particle amplitude comes from the real audio ------------------------
  useEffect(() => {
    voice.setAmplitudeCallback((level) => setAmplitude(level));
    return () => voice.setAmplitudeCallback(null);
  }, [setAmplitude]);

  // -- only one tab owns the speakers ---------------------------------------
  // Spoken replies are broadcast to every page, so without a lease three open
  // tabs answer out loud in chorus.
  useEffect(() => { speakerLease.start(); }, []);

  // -- one place decides whether JARVIS speaks ------------------------------
  //
  // There used to be three. A reply could reach all of them -- the new
  // utterance, the broadcast of the same message, and the automation
  // channel -- and only one of the three checked whether this tab owned the
  // speakers. With a few tabs open that is a room full of assistants all
  // answering at once, in whatever voice each had resolved.
  //
  // Everything that wants speech now goes through `say`, which is the single
  // gate: voice on, awake, this tab holds the lease, and not the same line
  // twice.
  const lastSaidRef = useRef<{ text: string; at: number }>({ text: '', at: 0 });
  const say = useCallback((text: string) => {
    if (!voiceEnabled || sleeping) return;
    if (!speakerLease.shouldSpeak) return;

    const clean = speakable(text);
    if (!clean.trim()) return;

    // The same line arriving by two routes within a moment is one reply, not
    // two. (The controller guards this as well; doing it here keeps the
    // second request from ever reaching it.)
    const now = Date.now();
    if (clean === lastSaidRef.current.text && now - lastSaidRef.current.at < 4000) return;
    lastSaidRef.current = { text: clean, at: now };

    // And nobody else is already saying it. The lease should have settled
    // this, but a lease can be briefly wrong -- two tabs claiming in the same
    // instant, or one opened while another was mid-reply -- and the cost of
    // being wrong is the chorus this is here to stop.
    if (!claimUtterance(clean)) return;

    void voice.speak(clean, {
      engine: voiceSettings.tts_engine === 'browser' ? 'browser' : 'edge',
      voice: voiceSettings.voice,
      rate: voiceSettings.rate ?? 1,
    });
  }, [voiceEnabled, sleeping, voiceSettings.tts_engine, voiceSettings.voice,
      voiceSettings.rate]);

  // -- speak new JARVIS replies, but only to spoken input -------------------
  //
  // Answer in the mode you were addressed in: type and you get text back,
  // speak and you get speech back. Reading every typed reply aloud is the
  // fastest way to make an assistant something you turn off.
  const spokenRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const last = utterances[utterances.length - 1];
    if (!last || last.role !== 'jarvis' || last.streaming || last.error) return;
    if (spokenRef.current.has(last.id) || !last.text.trim()) return;

    // Marked either way, so a later re-render cannot speak it belatedly.
    spokenRef.current.add(last.id);
    if (!lastInputWasSpokenRef.current) return;
    say(last.text);
  }, [utterances, say]);

  // -- automations, schedules, and replies the backend produced -------------
  //
  // `jarvis:speak` is an automation asking for something to be said.
  // `jarvis:spoke` is a reply to a spoken command that the backend ran, so it
  // did not come through this tab's own stream.
  useEffect(() => {
    const onAutomation = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      if (detail) say(detail);
    };
    const onReply = (event: Event) => {
      if (!lastInputWasSpokenRef.current) return;
      const detail = (event as CustomEvent<string>).detail;
      if (detail) say(detail);
    };
    window.addEventListener('jarvis:speak', onAutomation);
    const onCompose = (event: Event) => {
      const wanted = String((event as CustomEvent).detail ?? '');
      setText(wanted);
      window.setTimeout(() => {
        const box = inputRef.current;
        if (box) { box.focus(); box.setSelectionRange(wanted.length, wanted.length); }
      }, 0);
    };
    window.addEventListener('jarvis:compose', onCompose);
    window.addEventListener('jarvis:spoke', onReply);
    return () => {
      window.removeEventListener('jarvis:speak', onAutomation);
      window.removeEventListener('jarvis:spoke', onReply);
    };
  }, [say]);

  // ======================================================================
  const submit = useCallback(
    (value: string, source = 'text') => {
      const trimmed = value.trim();
      if (!trimmed) return;
      // Decides whether the reply is spoken. Recorded at submit time rather
      // than read later, because by the time the answer arrives the input
      // field has been cleared and the source is gone.
      lastInputWasSpokenRef.current = source === 'voice' || source === 'wake_word';
      rememberSent(trimmed);
      recallRef.current = -1;
      setText('');
      setInterim('');
      void send(trimmed, source);
    },
    [send, setText],
  );

  /** ↑ and ↓ in the message box: step through what was asked before. */
  const recall = useCallback((step: -1 | 1, current: string): boolean => {
    const asked = useStore.getState().utterances
      .filter((u) => u.role === 'user').map((u) => u.text);
    const list = recallList(asked);
    if (!list.length) return false;

    let index = recallRef.current;
    if (index === -1) {
      // Only start recalling from an empty box, so ↑ never throws away
      // something being written.
      if (step === 1 || current.trim()) return false;
      draftRef.current = current;
      index = list.length;
    }
    index += step;
    if (index < 0) index = 0;
    if (index >= list.length) {
      recallRef.current = -1;
      setText(draftRef.current);
      return true;
    }
    recallRef.current = index;
    setText(list[index]);
    return true;
  }, [setText]);

  const startListening = useCallback(
    async (continuous = false, isRetry = false) => {
      const notify = useStore.getState().notify;
      const openedAt = Date.now();
      let heardAnything = false;

      // Prefer the backend's microphone.
      //
      // The browser records through whatever input Chrome considers the
      // default, which is not necessarily one that works -- on this machine
      // 25 of 26 inputs, the default among them, return silence. The backend
      // has already measured which device carries audio and is holding it
      // open, so pressing the button should use that, exactly as the wake
      // word does. Falling back to the browser if it declines.
      if (localCommands && !isRetry) {
        try {
          await api.post('/voice/listen', {});
          lastInputWasSpokenRef.current = true;
          setListening(true);
          setState('listening');
          return;
        } catch (error) {
          // A 409 means "not available, use the browser" -- expected, not a
          // failure worth showing. Anything else is worth saying.
          const message = (error as Error).message ?? '';
          if (!message.includes('409')) console.warn('[voice] local capture declined:', message);
        }
      }

      if (!voice.recognitionSupported) {
        notify('Speech recognition needs Chrome or Edge. Text input works everywhere.');
        return;
      }

      // Ask for the microphone explicitly first. Recognition on its own fails
      // silently when permission is blocked, which looks like "nothing happens".
      const microphone = await voice.checkMicrophone();
      if (!microphone.ok) {
        notify(microphone.detail ?? 'The microphone is unavailable.');
        return;
      }

      // Interrupting JARVIS mid-sentence is expected behaviour, not an error.
      if (voice.isSpeaking) voice.stopSpeaking();

      const started = voice.startListening(
        (transcript, final) => {
          heardAnything = true;
          if (final) {
            setInterim('');
            submit(transcript, 'voice');
          } else {
            setInterim(transcript);
          }
        },
        () => {
          // A session that dies in under a second and a half without hearing
          // anything did not time out -- something took the microphone. Try
          // once more rather than dropping the user back to standby with no
          // explanation. Exactly once: a retry loop would be worse than the
          // bug it is covering.
          if (!heardAnything && !isRetry && Date.now() - openedAt < 1500) {
            window.setTimeout(() => void startListeningRef.current?.(continuous, true), 180);
            return;
          }
          setListening(false);
          setInterim('');
          if (useStore.getState().state === 'listening') setState('idle');
        },
        continuous,
        'command',
        (message) => {
          notify(message);
          setListening(false);
          setInterim('');
          setState('idle');
        },
      );
      if (started) {
        setListening(true);
        setState('listening');
      } else if (voice.lastError) {
        notify(voice.lastError);
      }
    },
    [setState, submit, localCommands],
  );

  // Keep the ref pointing at the current callback.
  useEffect(() => { startListeningRef.current = startListening; }, [startListening]);

  const stopListening = useCallback(() => {
    voice.stopListening();
    setListening(false);
    setInterim('');
    setState('idle');
  }, [setState]);

  const toggleListening = useCallback(() => {
    if (listening) stopListening();
    else void startListening(false);
  }, [listening, startListening, stopListening]);

  // -- keep the local engine in step with the setting ------------------------
  //
  // The switch in Settings is the single source of truth: turning it on starts
  // the detector, turning it off stops capturing. Sleep stops it too — an
  // assistant that is asleep must not be holding the microphone open.
  useEffect(() => {
    if (!settingsReady) return;
    const { refreshHotword, startHotword, stopHotword } = useStore.getState();
    let cancelled = false;

    // Serialised. React runs effects twice in development, and two
    // overlapping syncs produce a start/stop/stop/start churn that leaves a
    // couple of seconds where nothing is listening.
    syncRef.current = syncRef.current.then(async () => {
      const status = await refreshHotword();
      if (cancelled || !status?.available) return;
      if (wakeWanted && !status.running) await startHotword();
      else if (!wakeWanted && status.running) await stopHotword();
    }).catch(() => { /* reported by the actions themselves */ });

    return () => { cancelled = true; };
  }, [wakeWanted, settingsReady]);

  // While it is meant to be listening, poll its status. This is what makes the
  // level meter live and what turns "open but deaf" from an invisible failure
  // into something the interface can state out loud.
  useEffect(() => {
    if (!wakeWanted) return;
    const refresh = useStore.getState().refreshHotword;
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [wakeWanted]);

  // -- wake word, local engine ----------------------------------------------
  //
  // The backend runs a real keyword spotter (openWakeWord) against the
  // microphone directly. It fires this event; the page's only job is to open a
  // command session, exactly as if the microphone button had been pressed.
  // Nothing is transcribed or recorded until that session opens.
  useEffect(() => {
    const onWake = (event: Event) => {
      if (!voiceEnabled || sleeping) return;
      const detail = (event as CustomEvent<{ interrupted?: boolean }>).detail ?? {};

      // Interrupting a reply.
      //
      // This used to stop the reply and then stop, full stop -- so saying
      // the wake word over the top silenced JARVIS and left it waiting for
      // nothing. Cutting it off is only half of what you meant: the other
      // half is that it should now be listening.
      //
      // The backend decides whether a detection during playback is really
      // you or JARVIS hearing its own voice, because it knows what is coming
      // out of the speakers. Here we simply act on the verdict.
      const speaking = voice.isSpeaking;
      if (speaking) voice.stopSpeaking();

      // The echo window exists to ignore the tail of a reply arriving late.
      // A detection the backend has already judged to be an interruption is
      // exactly the case it must not swallow.
      if (!speaking && !detail.interrupted
          && Date.now() - voice.lastSpokeEndedAt < ECHO_WINDOW_MS) return;

      const now = Date.now();
      if (now - lastTriggerRef.current < WAKE_COOLDOWN_MS) return;
      lastTriggerRef.current = now;

      if (speakerLease.shouldSpeak) cue();
      if (localCommands) {
        // The backend is already recording the sentence on the device it
        // knows works. Opening a browser session too would record the same
        // words twice, on two microphones, and submit whichever finished
        // first. Just show that it is listening.
        setListening(true);
        setState('listening');
        return;
      }
      void startListening(false);
    };

    // The backend can also ask for silence directly -- it notices the
    // interruption a moment before the wake event is handled here, and a tab
    // that is not the one speaking still needs to know.
    const onSilence = () => voice.stopSpeaking();

    window.addEventListener('jarvis:wakeword', onWake);
    window.addEventListener('jarvis:silence', onSilence);
    return () => {
      window.removeEventListener('jarvis:wakeword', onWake);
      window.removeEventListener('jarvis:silence', onSilence);
    };
  }, [voiceEnabled, sleeping, startListening, localCommands]);

  // -- the command the backend captured and transcribed ---------------------
  useEffect(() => {
    const onCommand = (event: Event) => {
      if (!voiceEnabled || sleeping) return;
      const detail = (event as CustomEvent<{ text: string; problem: string }>).detail;

      setListening(false);
      setInterim('');

      if (!detail?.text) {
        // Woken but nothing usable followed. Say so rather than leaving the
        // interface sitting in a listening state that will never resolve.
        if (useStore.getState().state === 'listening') setState('idle');
        if (detail?.problem) useStore.getState().notify(`Wake word: ${detail.problem}.`);
        return;
      }

      // The backend runs the command itself, once, and broadcasts the
      // exchange. Submitting it here as well would run it again -- and with
      // several tabs open, once per tab. Only mark that a reply is expected,
      // and that it should be spoken because it was spoken to.
      lastInputWasSpokenRef.current = true;
      setState('thinking');
    };

    window.addEventListener('jarvis:wakecommand', onCommand);
    return () => window.removeEventListener('jarvis:wakecommand', onCommand);
  }, [voiceEnabled, sleeping, submit, setState]);

  // -- wake word, browser fallback ------------------------------------------
  //
  // Only runs when the local engine is not available. Two wake words on one
  // microphone handle the same utterance twice and fight over the device, so
  // this is deliberately exclusive rather than belt-and-braces.
  useEffect(() => {
    const setWakeStatus = useStore.getState().setWakeStatus;
    if (localWakeActive) {
      wakeRef.current = false;
      return;
    }

    if (!voiceEnabled || !voiceSettings.wake_word_enabled || sleeping) {
      wakeRef.current = false;
      setWakeStatus({
        phase: 'off', heard: '',
        detail: sleeping ? 'Asleep — the sleep screen listens instead.'
              : !voiceEnabled ? 'Voice is switched off in Settings.'
              : 'Turn on Settings → Voice → Wake word.',
      });
      return;
    }
    if (!voice.recognitionSupported) {
      setWakeStatus({
        phase: 'failed',
        detail: 'This browser has no speech recognition. Chrome or Edge is required.',
      });
      return;
    }
    if (wakeRef.current) return;

    wakeRef.current = true;
    // A generation token. React runs effects twice in development: mount,
    // clean up, mount. Both runs await the microphone check, both resolve, and
    // both then start a loop -- two loops on one recognition object, each
    // aborting the other's session on every restart, forever. The symptom is a
    // wake word that is "running" and never hears anything. The ref alone
    // cannot catch it, because cleanup clears it before the second run reads
    // it; only an identity check can.
    generationRef.current += 1;
    const generation = generationRef.current;
    const current = () => wakeRef.current && generationRef.current === generation;

    setWakeStatus({ phase: 'starting', detail: 'Checking the microphone…', heard: '' });

    const wakeWord = settings.wake_word ?? 'jarvis';
    let backoff = 700;
    const timers: number[] = [];
    const later = (fn: () => void, ms: number) => {
      timers.push(window.setTimeout(() => { if (current()) fn(); }, ms));
    };

    const listen = () => {
      if (!current()) return;
      // Never grab the microphone while a command session owns it — that was
      // the bug that made push-to-talk cut out whenever the wake word was on.
      if (voice.mode === 'command') {
        later(listen, 1200);
        return;
      }

      const started = voice.startListening(
        (transcript, final) => {
          // Show what was heard either way, so "it isn't working" becomes a
          // question you can actually answer by looking at the screen.
          setWakeStatus({ heard: transcript });

          // Ignore our own voice. JARVIS says its own name often enough that
          // without this it can trigger itself through the speakers — and
          // tolerant matching makes that more likely, not less.
          //
          // Three guards, because one is not enough: recognition buffers
          // audio, so a transcript of the reply arrives *after* speaking has
          // finished. Without the echo window and the overlap check, JARVIS
          // hears its own answer, treats it as a new command, and talks to
          // itself indefinitely.
          if (voice.isSpeaking) return;
          if (Date.now() - voice.lastSpokeEndedAt < ECHO_WINDOW_MS) return;
          if (echoesOwnSpeech(transcript, voice.lastSpokenText)) return;

          const match = matchWakeWord(transcript, wakeWord);
          if (!match.hit) return;

          // Fire on an interim result once the wake word is clearly there.
          // Chrome can sit on a final result for a second or two, which reads
          // as "it didn't hear me" and makes people repeat themselves. The
          // cooldown is what makes this safe: interim results arrive
          // repeatedly for the same utterance.
          const now = Date.now();
          if (now - lastTriggerRef.current < WAKE_COOLDOWN_MS) return;

          // Runaway guard. If the wake word fires repeatedly in a short
          // window, something is feeding the microphone — speakers, a second
          // tab, a stuck recogniser. Stop and say so, rather than letting
          // JARVIS submit commands in a loop.
          recentTriggersRef.current = recentTriggersRef.current
            .filter((at) => now - at < RUNAWAY_WINDOW_MS)
            .concat(now);
          if (recentTriggersRef.current.length > RUNAWAY_LIMIT) {
            wakeRef.current = false;
            generationRef.current += 1;
            voice.stopListening();
            setWakeStatus({
              phase: 'failed',
              detail: `The wake word fired ${RUNAWAY_LIMIT + 1} times in `
                    + `${RUNAWAY_WINDOW_MS / 1000}s, which usually means the microphone is `
                    + 'picking up the speakers. Stopped. Use headphones, lower the volume, '
                    + 'or turn the wake word off and on again.',
            });
            useStore.getState().notify('Wake word stopped — it was triggering itself.');
            return;
          }
          // A bare wake word is only acted on once finalised — mid-utterance
          // it is indistinguishable from the start of "Jarvis, do X".
          if (!final && match.rest.length <= 2) return;
          lastTriggerRef.current = now;

          voice.stopListening();
          setWakeStatus({ phase: 'triggered', lastTrigger: now });
          cue();

          if (match.rest.length > 2) {
            // "jarvis, what's my CPU" — one utterance, one command.
            // 'voice', not 'wake_word': the backend reserves that source for
            // commands it ran itself, and refuses it from a client so that a
            // stale page cannot resurrect the duplicate-submission bug. This
            // path is the browser fallback actually submitting speech.
            submit(match.rest, 'voice');
          } else {
            // Just the wake word. Open a command session and listen for what
            // comes next, rather than silently discarding it — that omission
            // was why saying "Jarvis" on its own appeared to do nothing.
            // Non-continuous: it submits one command, then the wake loop resumes.
            void startListening(false);
          }
        },
        () => {
          // Recognition drops out every minute or so by design; restart it.
          later(listen, backoff);
        },
        true,
        'wake',
        (message) => {
          // A permanent failure (permission, no device) must stop the loop and
          // tell the user, not spin silently forever.
          wakeRef.current = false;
          setWakeStatus({ phase: 'failed', detail: message });
          useStore.getState().notify(`Wake word off — ${message}`);
        },
      );

      if (started) {
        backoff = 700;
        setWakeStatus({ phase: 'armed', detail: '' });
      } else {
        // Back off rather than hammering the API.
        backoff = Math.min(backoff * 2, 15000);
        later(listen, backoff);
      }
    };

    // Confirm the microphone once up front so a blocked permission surfaces
    // immediately instead of after a minute of silence.
    void voice.checkMicrophone().then((microphone) => {
      if (!current()) return;
      if (!microphone.ok) {
        wakeRef.current = false;
        setWakeStatus({ phase: 'failed', detail: microphone.detail ?? 'Microphone unavailable.' });
        useStore.getState().notify(`Wake word off — ${microphone.detail}`);
        return;
      }
      listen();
    });

    return () => {
      wakeRef.current = false;
      timers.forEach(window.clearTimeout);
      if (voice.mode === 'wake') voice.stopListening();
      setWakeStatus({ phase: 'off', detail: '', heard: '' });
    };
  }, [voiceEnabled, voiceSettings.wake_word_enabled, sleeping, settings.wake_word, submit,
      startListening, localWakeActive]);

  // -- keyboard shortcuts ---------------------------------------------------
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const combo = comboOf(event);

      if (combo === (shortcuts.command_bar ?? 'ctrl+space')) {
        event.preventDefault();
        inputRef.current?.focus();
        return;
      }
      if (combo === (shortcuts.push_to_talk ?? 'ctrl+shift+space')) {
        event.preventDefault();
        toggleListening();
        return;
      }
      if (event.key === 'Escape') {
        if (voice.isSpeaking) voice.stopSpeaking();
        if (listening) stopListening();
        inputRef.current?.blur();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [shortcuts.command_bar, shortcuts.push_to_talk, toggleListening, listening, stopListening]);

  // ======================================================================
  const displayed = interim || text;

  return (
    <div className="command">
      <form
        className={`command__shell ${listening ? 'command__shell--listening' : ''}`}
        onSubmit={(event) => {
          event.preventDefault();
          submit(text);
        }}
      >
        <span className={`command__icon ${busy ? 'command__icon--busy' : ''}`} aria-hidden="true">
          {busy ? <Spinner /> : <Chevron />}
        </span>

        <input
          ref={inputRef}
          className="command__input"
          value={displayed}
          onChange={(event) => {
            // Editing a recalled message makes it a new one: ↑ from here
            // would otherwise replace the edit.
            recallRef.current = -1;
            setText(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
            if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
              if (recall(event.key === 'ArrowUp' ? -1 : 1, text)) event.preventDefault();
            }
          }}
          placeholder={listening ? 'Listening…' : 'Ask JARVIS anything…'}
          aria-label="Ask JARVIS anything"
          autoComplete="off"
          spellCheck={false}
          disabled={!!interim}
        />

        {voice.isSpeaking && (
          <button
            type="button"
            className="command__icon"
            onClick={() => voice.stopSpeaking()}
            title="Stop speaking (Esc)"
            aria-label="Stop speaking"
          >
            <StopIcon />
          </button>
        )}

        <button
          type="button"
          className={`command__icon ${listening ? 'command__icon--active' : ''}`}
          onClick={toggleListening}
          title={listening ? 'Stop listening' : 'Speak to JARVIS'}
          aria-label={listening ? 'Stop listening' : 'Speak to JARVIS'}
        >
          <MicIcon />
        </button>
      </form>

      <div className="command__hint">
        <kbd>{formatCombo(shortcuts.command_bar ?? 'ctrl+space')}</kbd> focus
        {'  ·  '}
        <kbd>{formatCombo(shortcuts.push_to_talk ?? 'ctrl+shift+space')}</kbd> talk
        {'  ·  '}
        <kbd>Esc</kbd> interrupt
        {'  ·  '}
        <kbd>↑</kbd> earlier messages
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function comboOf(event: KeyboardEvent): string {
  const parts: string[] = [];
  if (event.ctrlKey) parts.push('ctrl');
  if (event.altKey) parts.push('alt');
  if (event.shiftKey) parts.push('shift');
  if (event.metaKey) parts.push('meta');
  const key = event.key === ' ' ? 'space' : event.key.toLowerCase();
  if (!['control', 'alt', 'shift', 'meta'].includes(key)) parts.push(key);
  return parts.join('+');
}

function formatCombo(combo: string): string {
  return combo
    .split('+')
    .map((part) => (part === 'space' ? 'Space' : part.charAt(0).toUpperCase() + part.slice(1)))
    .join(' + ');
}

const Chevron = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
       strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 18 15 12 9 6" />
  </svg>
);

const MicIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
    <line x1="12" y1="19" x2="12" y2="23" />
  </svg>
);

const StopIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
    <rect x="5" y="5" width="14" height="14" rx="2.5" />
  </svg>
);

const Spinner = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
       strokeLinecap="round">
    <path d="M12 3a9 9 0 1 0 9 9">
      <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12"
                        dur="0.9s" repeatCount="indefinite" />
    </path>
  </svg>
);
