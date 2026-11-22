/**
 * Browser voice: speech synthesis, speech recognition, and the audio analyser
 * that drives the particle field while JARVIS speaks.
 *
 * Server-side voice (edge-tts / Whisper) is used when the user selects it in
 * settings; this module handles both paths behind one interface, and tells the
 * backend when speaking or listening starts and stops so the field stays in
 * sync with what is actually happening.
 */
import { api } from './api';

type AmplitudeCallback = (level: number) => void;

/** How long Chrome needs to finish tearing a recognition down. */
const ABORT_SETTLE_MS = 300;

/** How long a successful microphone check is trusted for. */
const MIC_CACHE_MS = 60_000;

/** How long the same line is refused after it was last spoken. */
const REPEAT_GUARD_MS = 2500;

/** Who currently owns the microphone. A command session outranks the wake loop. */
type ListenMode = 'idle' | 'command' | 'wake';

// The Web Speech API isn't in TypeScript's DOM lib.
declare global {
  interface Window {
    webkitSpeechRecognition?: any;
    SpeechRecognition?: any;
  }
}

class VoiceController {
  private audioContext: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private amplitudeFrame = 0;
  private onAmplitude: AmplitudeCallback | null = null;

  private recognition: any = null;
  private listening = false;
  private listenMode: ListenMode = 'idle';
  private currentAudio: HTMLAudioElement | null = null;
  private speaking = false;
  /** When speech last finished, and what was said.

   *  Recognition buffers audio, so a transcript of JARVIS's own voice can
   *  arrive *after* `speaking` has gone false. Without these two the wake word
   *  hears the reply, triggers again, and the assistant talks to itself. */
  lastSpokeEndedAt = 0;
  lastSpokenText = '';

  /** When a recognition was last aborted.
   *
   *  Chrome tears a SpeechRecognition down asynchronously. Starting the next
   *  one in the same tick gets it aborted immediately -- which, because
   *  'aborted' is a normal code we ignore, looked like the session silently
   *  dropping a second after it opened. */
  private lastAbortAt = 0;
  private micOkAt = 0;
  private startTimer = 0;

  /** Rolling lifecycle log. The whole point is that a dropped session should
   *  be explainable afterwards instead of invisible. */
  trace: { at: number; event: string; detail: string; mode: string }[] = [];

  private note(event: string, detail = '') {
    this.trace.push({ at: Date.now(), event, detail, mode: this.listenMode });
    if (this.trace.length > 60) this.trace.shift();
  }

  /** The lifecycle log as readable lines, newest last. */
  traceLines(): string[] {
    const base = this.trace.length ? this.trace[0].at : Date.now();
    return this.trace.map((entry) =>
      `+${String(entry.at - base).padStart(5)}ms  ${entry.mode.padEnd(7)} `
      + `${entry.event}${entry.detail ? ' - ' + entry.detail : ''}`);
  }
  private synthesisVoices: SpeechSynthesisVoice[] = [];
  /** The one browser voice, resolved once. `undefined` means not yet
   *  resolved; `null` means resolved to nothing and the system default
   *  is in use. The distinction matters because the browser returns an
   *  empty voice list until it has finished loading them. */
  private browserVoice: SpeechSynthesisVoice | null | undefined = undefined;
  private language = 'en-GB';
  /** Last real failure, so the UI can explain rather than sit silent. */
  lastError: string | null = null;
  /** Why the server voice was not used, when it was not. Null when it is. */
  engineFallback: string | null = null;

  // ======================================================================
  get isSpeaking() { return this.speaking; }
  get isListening() { return this.listening; }
  get mode() { return this.listenMode; }

  setLanguage(language: string) {
    this.language = language || 'en-GB';
  }

  get recognitionSupported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  get synthesisSupported() {
    return typeof window.speechSynthesis !== 'undefined';
  }

  setAmplitudeCallback(callback: AmplitudeCallback | null) {
    this.onAmplitude = callback;
  }

  /** Re-resolve the browser voice next time. Called when the browser reports
   *  that its voice list has changed. */
  forgetBrowserVoice() {
    this.browserVoice = undefined;
    this.synthesisVoices = [];
  }

  /** Which voice is actually in use, for the settings screen to state. */
  get currentBrowserVoice(): string {
    return this.browserVoice ? `${this.browserVoice.name} (${this.browserVoice.lang})` : '';
  }

  // ======================================================================
  // speaking
  // ======================================================================
  async speak(text: string, options: { engine?: 'browser' | 'edge'; voice?: string; rate?: number } = {}) {
    if (!text.trim()) return;

    // Refuse to repeat what we are already saying, or just said. A feedback
    // path that asks for the same line again -- an echoed event, a duplicated
    // listener -- turns into an audible infinite loop otherwise, and the cost
    // of this guard is only that a genuine repeat has to wait a moment.
    const sameAsCurrent = this.speaking && text === this.lastSpokenText;
    const justSaidIt = text === this.lastSpokenText
      && Date.now() - this.lastSpokeEndedAt < REPEAT_GUARD_MS;
    if (sameAsCurrent || justSaidIt) {
      this.note('speak-suppressed', 'same text');
      return;
    }
    this.lastSpokenText = text;

    this.stopSpeaking();

    const engine = options.engine ?? 'edge';
    await this.reportState('speaking_start', text);
    this.speaking = true;

    try {
      if (engine === 'edge') {
        await this.speakServer(text, options.voice, options.rate ?? 1);
        this.engineFallback = null;
      } else {
        await this.speakBrowser(text, options.rate ?? 1, options.voice);
      }
    } catch (error) {
      // The server voice failing means the *sound* of JARVIS changes: a
      // British server voice becomes whatever this browser offers. That is
      // audible, so it is recorded rather than swallowed -- "it suddenly
      // spoke in a different voice" should have an answer.
      this.engineFallback = (error as Error)?.message?.slice(0, 160)
        || 'the server voice was unavailable';
      console.warn('[voice] synthesis failed, falling back to the browser', error);
      try {
        await this.speakBrowser(text, options.rate ?? 1, options.voice);
      } catch {
        /* speech is best-effort — never break the conversation over it */
      }
    } finally {
      this.speaking = false;
      this.lastSpokeEndedAt = Date.now();
      this.lastSpokenText = text;
      this.onAmplitude?.(0);
      await this.reportState('speaking_end');
    }
  }

  /** Server voice: fetch MP3, play it, and run an analyser so the particles
   *  react to the actual waveform rather than a fake oscillation. */
  private async speakServer(text: string, voice?: string, rate = 1) {
    const response = await api.raw('/voice/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice, rate }),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail.slice(0, 200));
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    this.currentAudio = audio;

    this.ensureAudioContext();
    if (this.audioContext && this.analyser) {
      const source = this.audioContext.createMediaElementSource(audio);
      source.connect(this.analyser);
      this.analyser.connect(this.audioContext.destination);
      if (this.audioContext.state === 'suspended') await this.audioContext.resume();
      this.startAmplitudeLoop();
    }

    await new Promise<void>((resolve) => {
      audio.onended = () => resolve();
      audio.onerror = () => resolve();
      audio.play().catch(() => resolve());
    });

    this.stopAmplitudeLoop();
    URL.revokeObjectURL(url);
    this.currentAudio = null;
  }

  /**
   * Resolve the one browser voice this assistant uses, and keep it.
   *
   * The old rule picked `/daniel|google uk english male|male/i` out of
   * whatever `getVoices()` happened to return. Three things were wrong with
   * that. `getVoices()` returns an empty array until the browser has loaded
   * its voices, and the empty result was being cached -- so early replies got
   * the system default and later ones got a match, in the same session.
   * `/male/i` also matches "female". And the configured voice name is an
   * edge-tts name like `en-GB-RyanNeural`, which no browser voice is ever
   * called, so the lookup always missed and fell through to the regex.
   *
   * Now: resolved once, from an explicit order of preference, and cached for
   * the life of the page. One voice, every time.
   */
  private resolveBrowserVoice(configured?: string): SpeechSynthesisVoice | null {
    if (this.browserVoice !== undefined) return this.browserVoice;

    const voices = window.speechSynthesis.getVoices();
    if (!voices.length) return null;          // not loaded yet; try again later
    this.synthesisVoices = voices;

    const byName = (needle: string) =>
      voices.find((v) => v.name.toLowerCase() === needle.toLowerCase());
    const matching = (pattern: RegExp) => voices.find((v) => pattern.test(v.name));

    // An exact match on the configured name first, in case the user has
    // picked a real browser voice.
    let chosen = configured ? byName(configured) : undefined;

    // Otherwise translate the configured edge-tts voice into the nearest
    // browser equivalent: same language, same apparent gender where we can
    // tell. `en-GB-RyanNeural` is a British male voice, so prefer one.
    if (!chosen && configured) {
      const locale = configured.slice(0, 5).toLowerCase();       // "en-gb"
      const sameLocale = voices.filter(
        (v) => v.lang.toLowerCase().replace('_', '-') === locale);
      chosen = sameLocale.find((v) => /daniel|ryan|george|male/i.test(v.name)
                                      && !/female/i.test(v.name))
            ?? sameLocale[0];
    }

    chosen = chosen
      ?? matching(/^Daniel$/i)
      ?? voices.find((v) => /google uk english male/i.test(v.name))
      ?? voices.find((v) => /\bmale\b/i.test(v.name) && !/female/i.test(v.name))
      ?? voices.find((v) => v.lang.toLowerCase().startsWith('en'))
      ?? voices[0];

    this.browserVoice = chosen ?? null;
    if (this.browserVoice) {
      this.note('voice-chosen', `${this.browserVoice.name} (${this.browserVoice.lang})`);
    }
    return this.browserVoice;
  }

  /** Browser voice: no analyser is available, so drive the field from an
   *  envelope derived from speech boundary events plus a gentle oscillation. */
  private async speakBrowser(text: string, rate = 1, voiceName?: string) {
    if (!this.synthesisSupported) throw new Error('This browser has no speech synthesis.');

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = rate;
    const preferred = this.resolveBrowserVoice(voiceName);
    if (preferred) utterance.voice = preferred;

    let envelope = 0.4;
    const ticker = window.setInterval(() => {
      envelope = 0.28 + Math.random() * 0.42;
      this.onAmplitude?.(envelope);
    }, 90);
    utterance.onboundary = () => { envelope = 0.75; this.onAmplitude?.(envelope); };

    await new Promise<void>((resolve) => {
      utterance.onend = () => resolve();
      utterance.onerror = () => resolve();
      window.speechSynthesis.speak(utterance);
    });

    window.clearInterval(ticker);
    this.onAmplitude?.(0);
  }

  stopSpeaking() {
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio = null;
    }
    if (this.synthesisSupported) window.speechSynthesis.cancel();
    this.stopAmplitudeLoop();
    if (this.speaking) {
      this.speaking = false;
      this.onAmplitude?.(0);
      void this.reportState('speaking_end', '', true);
    }
  }

  // ======================================================================
  // amplitude
  // ======================================================================
  private ensureAudioContext() {
    if (this.audioContext) return;
    try {
      const Ctor = window.AudioContext || (window as any).webkitAudioContext;
      this.audioContext = new Ctor();
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.smoothingTimeConstant = 0.72;
    } catch {
      this.audioContext = null;
      this.analyser = null;
    }
  }

  private startAmplitudeLoop() {
    if (!this.analyser) return;
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (!this.analyser) return;
      this.analyser.getByteFrequencyData(data);
      // Weight the vocal range — it tracks speech far better than a flat mean.
      let sum = 0;
      let weight = 0;
      const limit = Math.min(data.length, 96);
      for (let i = 2; i < limit; i += 1) {
        const w = 1 - i / limit;
        sum += data[i] * w;
        weight += w;
      }
      const level = weight ? (sum / weight) / 190 : 0;
      this.onAmplitude?.(Math.min(1.5, level));
      this.amplitudeFrame = requestAnimationFrame(tick);
    };
    this.amplitudeFrame = requestAnimationFrame(tick);
  }

  private stopAmplitudeLoop() {
    if (this.amplitudeFrame) cancelAnimationFrame(this.amplitudeFrame);
    this.amplitudeFrame = 0;
  }

  // ======================================================================
  // listening
  // ======================================================================
  /**
   * Ask for microphone access explicitly.
   *
   * Speech recognition alone fails *silently* when permission is denied — the
   * error only reaches the console. Requesting the stream first turns that
   * into a real, explainable outcome before anything else is attempted.
   */
  async checkMicrophone(): Promise<{ ok: boolean; reason?: string; detail?: string }> {
    if (!window.isSecureContext) {
      return {
        ok: false, reason: 'insecure',
        detail: 'The microphone needs a secure page. Use http://localhost:3000 rather than a '
              + 'LAN IP address, or serve JARVIS over HTTPS.',
      };
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      return { ok: false, reason: 'unsupported',
               detail: 'This browser exposes no microphone API.' };
    }
    // A recent success is trusted rather than re-probed. Opening and closing
    // a capture stream immediately before starting recognition disturbs the
    // audio device on some systems, and the permission cannot silently
    // change between one click and the next.
    if (Date.now() - this.micOkAt < MIC_CACHE_MS) return { ok: true };

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());   // release it immediately
      this.micOkAt = Date.now();
      return { ok: true };
    } catch (error) {
      const name = (error as Error).name;
      const detail = {
        NotAllowedError: 'Microphone permission was denied. Click the padlock in the address '
                       + 'bar, set Microphone to Allow, then reload.',
        NotFoundError: 'No microphone was found. Check it is plugged in and set as the default '
                     + 'input device.',
        NotReadableError: 'The microphone is in use by another application.',
        SecurityError: 'The browser blocked microphone access on this page.',
      }[name] ?? `Microphone unavailable (${name}).`;
      return { ok: false, reason: name, detail };
    }
  }

  /** Everything that determines whether voice input can work, in one call. */
  async diagnose(): Promise<Record<string, any>> {
    const microphone = await this.checkMicrophone();
    let devices = 0;
    try {
      const list = await navigator.mediaDevices?.enumerateDevices();
      devices = (list ?? []).filter((d) => d.kind === 'audioinput').length;
    } catch { /* enumeration needs permission on some browsers */ }

    return {
      recognition_supported: this.recognitionSupported,
      synthesis_supported: this.synthesisSupported,
      secure_context: window.isSecureContext,
      microphone_ok: microphone.ok,
      microphone_detail: microphone.detail ?? 'Microphone accessible.',
      input_devices: devices,
      browser_note: this.recognitionSupported
        ? 'Speech recognition available.'
        : 'This browser has no Web Speech recognition. Chrome or Edge is required for voice '
        + 'input; text input works everywhere.',
      online: navigator.onLine,
      network_note: navigator.onLine ? '' : 'Speech recognition needs an internet connection.',
    };
  }

  startListening(
    onResult: (text: string, final: boolean) => void,
    onEnd?: () => void,
    continuous = false,
    mode: ListenMode = 'command',
    onError?: (message: string) => void,
  ): boolean {
    if (!this.recognitionSupported) {
      onError?.('Speech recognition needs Chrome or Edge. Text input works everywhere.');
      return false;
    }
    // A command session outranks the wake-word loop: without this the wake
    // loop's restart timer steals the microphone back mid-sentence.
    if (mode === 'wake' && this.listenMode === 'command') return false;
    if (this.recognition) this.stopListening();

    // Claim the microphone synchronously. `onstart` is asynchronous, so a
    // wake-loop restart timer firing in the gap would see an unowned
    // microphone, pass both guards, and abort the command session the user
    // had just started -- which looked like "listening for a second, then
    // straight back to standby".
    const previousMode = this.listenMode;
    this.listenMode = mode;

    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new Recognition();
    recognition.lang = this.language;
    recognition.continuous = continuous;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      this.listening = true;
      this.listenMode = mode;          // already claimed; kept for clarity
      this.note('start', continuous ? 'continuous' : 'single');
      if (mode === 'command') void this.reportState('listening_start');
    };

    recognition.onresult = (event: any) => {
      let interim = '';
      let final = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += transcript;
        else interim += transcript;
      }
      if (final) {
        this.note('final', final.trim().slice(0, 60));
        onResult(final.trim(), true);
      } else if (interim) {
        this.note('interim', interim.trim().slice(0, 60));
        onResult(interim.trim(), false);
      }
    };

    recognition.onerror = (event: any) => {
      const code = event?.error ?? 'unknown';
      // Recorded either way. 'no-speech' and 'aborted' are not worth a toast,
      // but they are exactly what you need to see when a session keeps
      // dropping, so they must not vanish without trace.
      this.note('error', code);
      if (code === 'no-speech' || code === 'aborted') return;

      const message = {
        'not-allowed': 'Microphone permission is blocked. Click the padlock in the address bar, '
                     + 'set Microphone to Allow, then reload.',
        'service-not-allowed': 'The browser blocked the speech service. Check site permissions.',
        'audio-capture': 'No microphone was found. Check it is connected and set as the default '
                       + 'input device.',
        network: 'Speech recognition needs an internet connection — the browser sends audio to '
               + 'its own speech service.',
        'language-not-supported': `The language "${this.language}" is not supported here.`,
      }[code as string] ?? `Speech recognition failed (${code}).`;

      console.warn('[voice] recognition error:', code);
      this.lastError = message;
      onError?.(message);
    };

    recognition.onend = () => {
      this.note('end');
      this.listening = false;
      if (this.listenMode === mode) this.listenMode = 'idle';
      if (mode === 'command') void this.reportState('listening_end');
      onEnd?.();
    };

    this.recognition = recognition;

    // Give a just-aborted recognition time to finish tearing down. Chrome
    // does that asynchronously, and a new instance started in the same tick
    // is aborted on the spot -- silently, because 'aborted' is a normal code.
    const sinceAbort = Date.now() - this.lastAbortAt;
    const delay = sinceAbort < ABORT_SETTLE_MS ? ABORT_SETTLE_MS - sinceAbort : 0;

    const begin = (attempt: number): void => {
      // The session may have been superseded while we waited.
      if (this.recognition !== recognition) return;
      try {
        recognition.start();
      } catch (error) {
        const name = (error as Error).name;
        this.note('start-failed', name);
        // InvalidStateError means the previous instance is still shutting
        // down. One more attempt, a little later, then give up honestly.
        if (name === 'InvalidStateError' && attempt < 2) {
          this.startTimer = window.setTimeout(() => begin(attempt + 1), ABORT_SETTLE_MS * 2);
          return;
        }
        this.listening = false;
        this.listenMode = previousMode === mode ? 'idle' : previousMode;
        this.recognition = null;
        onError?.(`Could not start listening (${name}).`);
      }
    };

    window.clearTimeout(this.startTimer);
    if (delay) {
      this.note('deferred', `${delay}ms after abort`);
      this.startTimer = window.setTimeout(() => begin(1), delay);
    } else {
      begin(1);
    }
    return true;
  }

  stopListening() {
    window.clearTimeout(this.startTimer);
    if (this.recognition) {
      this.note('abort');
      this.lastAbortAt = Date.now();
      try { this.recognition.abort?.() ?? this.recognition.stop(); } catch { /* already stopped */ }
      this.recognition = null;
    }
    this.listening = false;
    // Reset the owner synchronously. `onend` fires asynchronously after abort(),
    // so leaving it until then lets a restart see a stale owner and refuse.
    this.listenMode = 'idle';
  }

  // ======================================================================
  private async reportState(state: string, text = '', interrupted = false) {
    try {
      await api.post('/voice/state', { state, text: text.slice(0, 500), interrupted });
    } catch {
      /* presence reporting is advisory — never block speech on it */
    }
  }
}

export const voice = new VoiceController();

// Chrome populates its voice list asynchronously and fires this when it is
// ready. Without it the first thing JARVIS says can use a different voice
// from everything after it -- which, with several tabs open, was most of what
// made it sound like a room full of different assistants.
if (typeof window !== 'undefined' && window.speechSynthesis) {
  window.speechSynthesis.addEventListener?.('voiceschanged', () => {
    voice.forgetBrowserVoice();
  });
}

/** Strip markdown so speech doesn't read asterisks and backticks aloud. */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' (code omitted) ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/[*_#>]/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, 'a link')
    .replace(/\s+/g, ' ')
    .trim();
}
