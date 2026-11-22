"""
Always-on wake-word detection.

Why this exists at all
----------------------
The wake word used to run in the browser on top of the Web Speech API. That is
a *dictation* engine, not a keyword spotter: it uploads audio to Google, returns
free text, stops itself after a minute, and transcribes "Jarvis" as "Travis",
"drives" or "service". Matching that tolerantly enough to catch the real word
also catches words that aren't it, and the whole loop dies silently whenever the
tab loses the microphone.

This module replaces it with a real keyword spotter: openWakeWord's pretrained
`hey_jarvis` model, running locally through ONNX on 80 ms frames. Measured on
this machine: ~38x realtime (about 2.6% of one core), every utterance of "hey
jarvis" and bare "jarvis" scoring >= 0.98, and "Travis", "hey Google" and
ordinary speech scoring < 0.01. No audio leaves the machine and no network is
involved.

The honest limitation: phonetically adjacent phrases can fire. "Hey, drive us
to the shop" scored 0.99 in one synthetic voice (and 0.08 in another). That is
inherent to keyword spotting, which is why the threshold is a setting and why
every detection is logged.

The failure that was actually biting
------------------------------------
On this machine 25 of 26 input devices return digital silence, including the
Windows default. An open stream that yields nothing looks identical to "nobody
is talking", which is why the old wake word appeared to be running while being
deaf. So this service:

  * picks a device by *measuring* signal rather than trusting the OS default,
  * reports a distinct `silent` state when the stream is open but flat,
  * exposes the live input level so "is the microphone working" is a question
    you answer by looking, not by guessing.

Safety
------
Continuous microphone capture is the most invasive thing in this application,
so it is off until switched on, it stops on request, it never records to disk,
it keeps no audio beyond the 80 ms frame being scored, and every start, stop
and detection goes to the audit log.
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso

logger = logging.getLogger("jarvis.hotword")

# openWakeWord is trained on 80 ms frames of 16 kHz mono signed 16-bit audio.
SAMPLE_RATE = 16000
FRAME = 1280

# Default score above which a frame counts as a hit.
DEFAULT_THRESHOLD = 0.5

# Consecutive hot frames required. Every genuine utterance measured here held
# for at least three frames, so two costs nothing and discards single-frame
# spikes, which are the common false positive.
MIN_RUN = 2

# After a detection, ignore the microphone for this long: the command session
# that follows owns it, and the tail of the same utterance would re-trigger.
REFRACTORY_S = 2.5

# How long the stream may be flat before we stop calling it "listening". A
# muted or dead device is the single most likely reason a wake word "doesn't
# work", and it has to be visible rather than inferred.
SILENT_AFTER_S = 20.0

# Below this RMS a frame is treated as silence for the purpose above.
SILENCE_RMS = 2.0

# How long after JARVIS speaks to keep ignoring the microphone, so it does not
# hear its own voice through the speakers and wake itself.
ECHO_GUARD_S = 1.5

# -- interrupting a reply ----------------------------------------------------
# Barge-in: saying the wake word over the top of JARVIS speaking stops it and
# starts listening. The bar is deliberately higher than the ordinary
# threshold, because the microphone is hearing the reply as well as the room
# and a false interruption -- JARVIS cutting itself off to listen to nothing
# -- is more annoying than having to wait for a sentence to end.
BARGE_IN_THRESHOLD = 0.7

# Words that mean the reply is saying the wake word itself, in which case a
# detection is JARVIS hearing its own voice and not an interruption.
_SELF_REFERENCE = ("jarvis", "jarvus", "jervis")

# How the wake word tends to come back from the transcriber, so it can be
# trimmed off the front of a command.
_TRANSCRIBED_NAME = ("jarvis", "jarvus", "jervis", "travis", "javis", "jarvis.")

# After telling the browser to stop, give the audio a moment to actually stop
# before recording. Playback does not end on the instant the request is made.
#
# This is a balance measured rather than guessed. Too long and it eats the
# start of what the user is saying -- at 0.45s "Hey Jarvis, what is my CPU
# usage?" was recorded as "My CPU usage." Too short and the tail of the reply
# lands in the recording and gets transcribed as part of the command, which
# is worse: a clipped command still has the right intent, a command with
# someone else's words in it does not.
BARGE_IN_SETTLE_S = 0.25

# -- not losing the first word -----------------------------------------------
# The spotter can only recognise the wake word once it has heard all of it,
# and its score stays high for a few frames after. By the time a detection
# fires the speaker is already into the next word: "Hey Jarvis, what is my CPU
# usage?" arrived as "My CPU usage."
#
# So a second or so of audio is kept at all times and prepended to whatever is
# captured. The wake word itself ends up in the recording, which is fine --
# the transcript is trimmed to whatever follows it.
PREROLL_S = 1.2
PREROLL_FRAMES = int(PREROLL_S * SAMPLE_RATE / FRAME)

# Recovering from a microphone that goes away mid-session. The backoff keeps a
# genuinely broken setup from spinning, and the cap keeps a recoverable one
# from taking minutes to notice the device is back.
RETRY_BASE_S = 5.0
RETRY_MAX_S = 60.0
MAX_RETRIES = 8

# -- capturing the command that follows the wake word -----------------------
# Having heard the word on a device known to carry audio, it would be perverse
# to hand the sentence after it to a different microphone. The browser's Web
# Speech API uses whatever Chrome considers the default input, which on this
# machine is a virtual headset that returns silence -- so a perfectly detected
# wake word would still be followed by a deaf command session.

# Stop after this much trailing silence. Shorter feels clipped mid-sentence;
# longer feels unresponsive.
COMMAND_TRAILING_SILENCE_S = 1.1

# Give up waiting for speech to begin.
COMMAND_LEAD_IN_S = 3.0

# Nobody issues a voice command longer than this, and an open-ended recording
# is not something to leave running on a stuck VAD.
COMMAND_MAX_S = 14.0

# Shorter than this is a cough, a click, or the tail of the wake word.
COMMAND_MIN_SPEECH_S = 0.3

# webrtcvad wants 10/20/30 ms frames; 20 ms at 16 kHz is 320 samples, so each
# 80 ms capture frame splits into exactly four.
VAD_FRAME = 320

# The models openWakeWord ships that we offer. The key is what a user picks.
MODELS = {
    "hey_jarvis": "hey_jarvis_v0.1",
    "alexa": "alexa_v0.1",
    "hey_mycroft": "hey_mycroft_v0.1",
    "hey_rhasspy": "hey_rhasspy_v0.1",
}


class HotwordUnavailable(RuntimeError):
    """Raised with an actionable reason when detection cannot run at all."""


# ---------------------------------------------------------------------------
def strip_wake_word(text: str) -> str:
    """Return whatever the speaker said *after* the wake word.

    The pre-roll deliberately includes the wake word, and during a barge-in
    it can also catch the tail of the reply being interrupted. Both belong
    before the command, not in it, so everything up to and including the last
    occurrence of the name is dropped.

    Mishearings are included because this runs on a transcript: Whisper hears
    "Hey, Travis" and "Hi Jarvis" often enough to matter, and leaving those in
    sends the model a command that starts with a stray name.
    """
    if not text:
        return ""

    lowered = text.lower()
    best = -1
    for token in _TRANSCRIBED_NAME:
        position = lowered.rfind(token)
        if position > best:
            best = position
            matched = token
    if best < 0:
        return text.strip()

    rest = text[best + len(matched):]
    # Drop the punctuation that usually follows a name: "Jarvis, what is..."
    return rest.lstrip(" ,.;:!?-—–").strip() or text.strip()


def may_barge_in(spoken_text: str, score: float,
                 threshold: float = BARGE_IN_THRESHOLD) -> bool:
    """Is this detection the user interrupting, or JARVIS hearing itself?

    While JARVIS speaks, its own voice reaches the microphone. The detector
    used to simply stop scoring for the duration, which made it impossible to
    interrupt: you had to wait for it to finish before it would hear you.

    Two things separate the cases, and neither needs echo cancellation:

    * **What is being said.** We know exactly what is coming out of the
      speakers. If the reply does not contain the wake word, then a wake word
      arriving through the microphone did not come from the speakers -- it
      came from the room.
    * **How sure the model is.** Speech competing with playback is a harder
      signal, so the bar is raised rather than lowered: a confident detection
      over the top of a reply is far more likely to be a person leaning in
      than an artefact.

    The conservative direction matters. A missed barge-in means waiting a few
    seconds for a sentence to finish. A false one means JARVIS cutting itself
    off mid-answer and listening to nothing, which is the more annoying of
    the two by a wide margin.
    """
    if score < threshold:
        return False

    lowered = (spoken_text or "").lower()
    # Only the distinctive part needs checking: "hey" is everywhere.
    if any(token in lowered for token in _SELF_REFERENCE):
        return False
    return True


def _meter(rms: float) -> float:
    """Map an int16 RMS onto 0..1 the way a level meter should.

    Ambient room noise sits around rms 20 and speech around 500-3000, which a
    linear scale renders as 1% and 100% -- indistinguishable from a dead
    microphone at the quiet end. On a dB scale the same range spans roughly
    0.15 to 1.0, so a quiet room visibly moves and silence visibly does not.
    """
    if rms <= 1.0:
        return 0.0
    import math

    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db - 20.0) / 40.0))


class SpeechBoundary:
    """Decides when the user has finished speaking.

    Kept as a plain state machine, fed one 80 ms frame at a time, so the thing
    that decides "they have stopped talking" can be tested with synthetic
    frames instead of by talking at a laptop and hoping.

    webrtcvad does the voiced/unvoiced call when it is installed; otherwise an
    energy threshold stands in. The fallback is worse in a noisy room, which
    is said out loud rather than papered over.
    """

    def __init__(self, aggressiveness: int = 2):
        self.vad = None
        try:
            import webrtcvad

            self.vad = webrtcvad.Vad(aggressiveness)
        except Exception:
            logger.debug("hotword: webrtcvad unavailable; using an energy threshold")

        self.started = False
        self.speech_s = 0.0
        self.silence_s = 0.0
        self.elapsed_s = 0.0

    @property
    def using_vad(self) -> bool:
        return self.vad is not None

    def _voiced(self, frame) -> bool:
        if self.vad is not None:
            raw = frame.tobytes()
            step = VAD_FRAME * 2                      # bytes per 20 ms frame
            voiced = 0
            chunks = 0
            for i in range(0, len(raw) - step + 1, step):
                chunks += 1
                try:
                    if self.vad.is_speech(raw[i:i + step], SAMPLE_RATE):
                        voiced += 1
                except Exception:
                    return self._loud(frame)
            # One voiced chunk in four is noise; two is someone talking.
            return chunks > 0 and voiced * 2 >= chunks
        return self._loud(frame)

    @staticmethod
    def _loud(frame) -> bool:
        import numpy as np

        return float(np.sqrt((frame.astype(np.float32) ** 2).mean())) > 220.0

    def feed(self, frame) -> str:
        """Returns 'waiting', 'speaking', 'done' or 'nothing'."""
        duration = len(frame) / float(SAMPLE_RATE)
        self.elapsed_s += duration

        if self._voiced(frame):
            self.started = True
            self.speech_s += duration
            self.silence_s = 0.0
        elif self.started:
            self.silence_s += duration

        if self.started:
            if self.silence_s >= COMMAND_TRAILING_SILENCE_S:
                return "done" if self.speech_s >= COMMAND_MIN_SPEECH_S else "nothing"
            if self.elapsed_s >= COMMAND_MAX_S:
                return "done" if self.speech_s >= COMMAND_MIN_SPEECH_S else "nothing"
            return "speaking"

        if self.elapsed_s >= COMMAND_LEAD_IN_S:
            return "nothing"
        return "waiting"


# ---------------------------------------------------------------------------
def probe_devices(duration: float = 0.6, limit: int = 40) -> List[Dict[str, Any]]:
    """Every input device, with whether it opens and whether it carries audio.

    This is the diagnostic that matters. A device list alone is useless -- the
    OS default was present, openable and completely silent -- so each entry is
    actually opened and measured.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise HotwordUnavailable(
            "Microphone capture needs the 'sounddevice' and 'numpy' packages "
            "(pip install sounddevice numpy)."
        ) from exc

    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = -1

    frames = max(1, int(duration * SAMPLE_RATE / FRAME))
    out: List[Dict[str, Any]] = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) < 1:
            continue
        if len(out) >= limit:
            break
        entry = {
            "index": index,
            "name": str(device.get("name", "")),
            "channels": int(device.get("max_input_channels", 0)),
            "default": index == default_input,
            "opens": False,
            "rms": 0.0,
            "peak": 0,
            "signal": False,
            "detail": "",
        }
        try:
            with sd.InputStream(device=index, samplerate=SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=FRAME) as stream:
                chunks = [stream.read(FRAME)[0].reshape(-1) for _ in range(frames)]
            audio = np.concatenate(chunks).astype(np.float32)
            entry["opens"] = True
            entry["rms"] = round(float(np.sqrt((audio ** 2).mean())), 2)
            entry["peak"] = int(np.abs(audio).max())
            entry["signal"] = entry["rms"] > SILENCE_RMS
            entry["detail"] = ("Carrying audio." if entry["signal"]
                               else "Opens, but returned silence. Muted, unplugged, or a "
                                    "virtual device with nothing behind it.")
        except Exception as exc:
            entry["detail"] = "Cannot be opened at 16 kHz mono ({}).".format(
                type(exc).__name__)
        out.append(entry)
    return out


# Devices that exist without anything behind them. A loopback, a game-capture
# shim or a headset that is switched off opens happily and returns silence
# forever, so when nothing is making a sound during the probe these are the
# worst possible guess -- which is exactly what "trust the OS default" did.
VIRTUAL_HINTS = (
    "virtual", "steam streaming", "voicemod", "stereo mix", "sound mapper",
    "primary sound capture", "loopback", "wave speaker", "@system32",
)


def _looks_virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_HINTS)


def choose_device(devices: List[Dict[str, Any]],
                  preferred: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Pick the input most likely to be the one being spoken into.

    In order:

    1. A device the user chose, or that was seen carrying audio before. Past
       evidence beats a fresh guess, and a probe that happens to land in a
       quiet moment carries no information at all.
    2. Whatever is carrying audio right now, loudest first.
    3. Of the rest, a physical-looking device over a virtual one. When nothing
       is making a sound there is no measurement to go on, and the OS default
       here is a switched-off headset that returns silence forever.

    The OS default only breaks ties. It is not evidence.
    """
    if preferred is not None:
        match = next((d for d in devices if d["index"] == preferred and d["opens"]), None)
        if match is not None:
            return match

    live = [d for d in devices if d["signal"]]
    if live:
        live.sort(key=lambda d: -d["rms"])
        return live[0]

    openable = [d for d in devices if d["opens"]]
    if openable:
        openable.sort(key=lambda d: (_looks_virtual(d["name"]), not d["default"]))
        return openable[0]
    return None


# ---------------------------------------------------------------------------
class HotwordService:
    """Owns the capture thread, the detector, and an honest account of both."""

    def __init__(self, settings, settings_store, voice, activity, audit=None):
        self.settings = settings
        self.settings_store = settings_store
        self.voice = voice
        self.activity = activity
        self.audit = audit

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Captured when started. The capture loop runs in its own thread, and
        # `bus.publish` needs a running event loop -- called from a plain
        # thread it finds none and *drops the event*, which would make every
        # detection vanish without a trace. Everything the thread wants to
        # announce goes back through here.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Everything the UI needs to explain what is happening.
        self._phase = "off"            # off|starting|listening|silent|failed
        self._detail = "Not started."
        self._device: Optional[Dict[str, Any]] = None
        self._level = 0.0
        self._score = 0.0
        self._last_detection: Optional[str] = None
        self._detections = 0
        self._last_audio_at = 0.0
        self._started_at: Optional[str] = None
        self._model_key = "hey_jarvis"
        self._threshold = DEFAULT_THRESHOLD

        # Set while a browser command session owns the microphone, so the two
        # do not compete for the device or double-handle the same utterance.
        self._suspended_until = 0.0

        # The device last seen actually carrying audio, remembered across
        # restarts. A probe that lands in a quiet moment tells you nothing, so
        # evidence from a previous run is worth more than a fresh guess.
        self._remembered: Optional[int] = None
        self._proved = False

        # Whether the packages are importable. Probed once: the probe is an
        # import, and the answer cannot change while the process runs.
        self._availability: Optional[Dict[str, Any]] = None

        # Capture the command locally after the wake word, rather than handing
        # it to the browser's microphone. Off unless speech-to-text is there
        # to turn it into anything.
        self._capture_commands = False
        self._capturing = False
        # Set by `listen_now()`; cleared by the capture loop when it acts.
        self._capture_requested = False
        self._last_command: Optional[str] = None

        # Set by the container to an async callable that runs a spoken
        # command. Left unset the service still detects and transcribes; it
        # just has nowhere to send the result.
        self.run_command = None

    # -- state ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        running = self._thread is not None and self._thread.is_alive()
        return {
            "running": running,
            "phase": self._phase,
            "detail": self._detail,
            "engine": "openwakeword",
            "model": self._model_key,
            "threshold": self._threshold,
            "device": self._device,
            "level": round(self._level, 2),
            "score": round(self._score, 3),
            "detections": self._detections,
            "last_detection": self._last_detection,
            "started_at": self._started_at,
            "suspended": time.time() < self._suspended_until,
            "available": self.availability()["available"],
            # Has this device ever actually delivered sound since it started?
            # Silence from a device that has worked is a quiet room. Silence
            # from one that never has is a dead input. Only the second is a
            # fault, and conflating them would keep the status strip amber
            # every quiet evening until nobody looked at it any more.
            "heard_anything": self._proved,
            # Whether the sentence after the wake word is captured here or
            # left to the browser. The difference matters: the browser uses
            # its own default input, which may not be the one that works.
            "local_commands": self._capture_commands,
            "capturing": self._capturing,
            "last_command": self._last_command,
        }

    @staticmethod
    def _stt_available() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            return False
        return True

    def availability(self) -> Dict[str, Any]:
        """Can this run here, and if not exactly what is missing.

        Cached, because the probe is an import and imports are not cheap:
        `openwakeword` measured 13.7 seconds here. This is called from
        `status()`, which the interface polls every two seconds, so an
        uncached probe would be a stall on the event loop over and over --
        and during boot it was part of why the launcher gave up waiting.
        """
        if self._availability is not None:
            return self._availability

        missing = []
        for module, package in (("openwakeword", "openwakeword"),
                                ("sounddevice", "sounddevice"),
                                ("numpy", "numpy")):
            try:
                __import__(module)
            except ImportError:
                missing.append(package)
        if missing:
            self._availability = {
                "available": False,
                "missing": missing,
                "detail": "Local wake-word detection needs: {}. Install with "
                          "'pip install {}'.".format(", ".join(missing), " ".join(missing)),
            }
        else:
            self._availability = {"available": True, "missing": [],
                                  "detail": "Local wake-word detection ready."}
        return self._availability

    def suspend(self, seconds: float = 12.0) -> None:
        """Stop scoring for a while -- a browser command session has the mic."""
        self._suspended_until = max(self._suspended_until, time.time() + seconds)

    def resume(self) -> None:
        self._suspended_until = 0.0

    # -- lifecycle --------------------------------------------------------
    async def start(self, device: Optional[int] = None,
                    model: Optional[str] = None,
                    threshold: Optional[float] = None) -> Dict[str, Any]:
        """Begin listening. Idempotent: starting a running detector is a no-op."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()

            ready = self.availability()
            if not ready["available"]:
                self._fail(ready["detail"])
                return self.status()

            stored = await self.settings_store.get("voice", {}) or {}
            self._model_key = model or stored.get("wake_model") or "hey_jarvis"
            if self._model_key not in MODELS:
                self._fail("Unknown wake-word model '{}'. Available: {}.".format(
                    self._model_key, ", ".join(sorted(MODELS))))
                return self.status()
            try:
                self._threshold = float(
                    threshold if threshold is not None
                    else stored.get("wake_threshold", DEFAULT_THRESHOLD))
            except (TypeError, ValueError):
                self._threshold = DEFAULT_THRESHOLD
            self._threshold = min(0.99, max(0.05, self._threshold))

            chosen = device if device is not None else stored.get("wake_device")
            remembered = stored.get("wake_device_proven")
            self._remembered = int(remembered) if isinstance(remembered, int) else None
            self._proved = False

            # Only claim local command capture when speech-to-text is actually
            # importable. Promising it and then silently doing nothing after
            # the wake word fires would be the worst of both.
            wants_capture = stored.get("local_command_capture", True)
            self._capture_commands = bool(wants_capture) and self._stt_available()
            self._loop = asyncio.get_running_loop()
            self._phase = "starting"
            self._detail = "Choosing an input device..."
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._supervise, args=(chosen,), name="jarvis-hotword",
                daemon=True)
            self._thread.start()

        await self._log("wake word listening started",
                        {"model": self._model_key, "threshold": self._threshold})
        # Give the thread a moment so the caller sees a real phase rather than
        # "starting" for something that has already failed.
        #
        # `await asyncio.sleep`, never `time.sleep`: this runs on the event
        # loop, and sleeping on it would freeze every other request, the
        # websocket included, for as long as we waited here.
        for _ in range(40):
            if self._phase != "starting":
                break
            await asyncio.sleep(0.05)
        return self.status()

    async def stop(self) -> Dict[str, Any]:
        thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive():
            # Joining takes up to a few hundred milliseconds -- long enough
            # that doing it on the event loop would stall the whole backend.
            await asyncio.get_running_loop().run_in_executor(
                None, thread.join, 3.0)
        self._thread = None
        self._phase = "off"
        self._detail = "Stopped."
        self._level = 0.0
        self._score = 0.0
        self._started_at = None
        await self._log("wake word listening stopped", {})
        return self.status()

    async def listen_now(self) -> Dict[str, Any]:
        """Record a command now, as if the wake word had just fired.

        This is what the microphone button should do. Recording in the page
        instead means recording through whatever input the browser prefers,
        which is not necessarily -- and on this machine is not -- the one that
        works. Reusing the stream the detector already holds avoids a second
        device hand-off as well.
        """
        if not self._capture_commands:
            return {"ok": False,
                    "reason": "Local command capture is off, or speech-to-text is not "
                              "installed. The browser microphone is being used instead."}
        if not (self._thread is not None and self._thread.is_alive()):
            return {"ok": False,
                    "reason": "The wake-word service is not running, so there is no "
                              "microphone stream to record from. Turn the wake word on, or "
                              "use the browser microphone."}
        if self._capturing:
            return {"ok": False, "reason": "Already recording."}

        # Clear any suspension: the user has explicitly asked to be heard.
        self.resume()
        self._capture_requested = True
        return {"ok": True, "detail": "Listening.", "device":
                self._device["name"] if self._device else ""}

    async def restart(self, **kwargs) -> Dict[str, Any]:
        await self.stop()
        return await self.start(**kwargs)

    async def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Off the loop, like `stop()`: shutdown still has other services
            # to close and should not stall on this one.
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 2.0)
        self._thread = None

    # -- the capture loop -------------------------------------------------
    # -- supervision ------------------------------------------------------
    def _supervise(self, device: Optional[int]) -> None:
        """Keep the detector alive across recoverable failures.

        A USB microphone unplugged mid-session takes the capture stream down
        with it. Without this the detector simply dies and stays dead until
        someone notices and toggles the setting -- which, for a feature whose
        entire job is to be there when you speak, is the same as not working.

        Each retry re-probes the devices, so plugging a different microphone
        in is enough to bring it back. It gives up only after a long run of
        failures, and says so rather than looping in silence.
        """
        delay = RETRY_BASE_S
        failures = 0

        while not self._stop.is_set():
            self._run(device)

            if self._stop.is_set():
                return
            if self._phase != "failed":
                return                      # a clean exit, not a crash

            failures += 1
            if failures >= MAX_RETRIES:
                self._detail = (
                    "{} Giving up after {} attempts -- switch the wake word off and on "
                    "again once the microphone is back.".format(self._detail, failures))
                logger.warning("hotword: giving up after %d failures", failures)
                return

            logger.info("hotword: retrying in %.0fs (attempt %d) -- %s",
                        delay, failures, self._detail)
            self._detail = "{} Retrying in {:.0f}s.".format(self._detail, delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, RETRY_MAX_S)
            # Re-probe rather than reusing a device index that may now belong
            # to something else, or have gone away entirely.
            device = None
            self._phase = "starting"

    def _run(self, device: Optional[int]) -> None:
        try:
            import numpy as np
            import sounddevice as sd
            from openwakeword.model import Model
        except ImportError as exc:                           # pragma: no cover
            self._fail("Wake-word packages are missing: {}".format(exc))
            return

        # Device choice first: a model loaded against a dead microphone is a
        # waste of two seconds and produces a misleading "listening".
        try:
            if device is None:
                picked = choose_device(probe_devices(duration=0.4), preferred=self._remembered)
                if picked is None:
                    self._fail("No usable microphone. No input device could be opened at "
                               "16 kHz mono.")
                    return
                if not picked["signal"]:
                    logger.warning("hotword: no input device carried audio; using '%s' anyway",
                                   picked["name"])
                self._device = picked
                device = picked["index"]
            else:
                info = sd.query_devices(device)
                self._device = {
                    "index": int(device), "name": str(info.get("name", "")),
                    "channels": int(info.get("max_input_channels", 0)),
                    "default": False, "opens": True, "rms": 0.0, "peak": 0,
                    "signal": False, "detail": "Chosen in settings.",
                }
        except HotwordUnavailable as exc:
            self._fail(str(exc))
            return
        except Exception as exc:
            self._fail("Could not inspect audio devices: {}".format(exc))
            return

        try:
            model = Model(wakeword_models=[MODELS[self._model_key]],
                          inference_framework="onnx")
        except Exception as exc:
            self._fail(
                "The wake-word model could not be loaded: {}. The models download on first "
                "use -- run: python -c \"import openwakeword.utils as u; "
                "u.download_models()\"".format(exc))
            return

        run = 0
        last_fire = 0.0
        preroll = deque(maxlen=PREROLL_FRAMES)
        self._last_audio_at = time.time()
        self._started_at = now_iso()
        self._phase = "listening"
        self._detail = "Listening for the wake word on '{}'.".format(self._device["name"])
        logger.info("hotword: listening on device %s (%s), model %s, threshold %.2f",
                    self._device["index"], self._device["name"], self._model_key,
                    self._threshold)

        try:
            with sd.InputStream(device=device, samplerate=SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=FRAME) as stream:
                while not self._stop.is_set():
                    try:
                        data, _overflow = stream.read(FRAME)
                    except Exception as exc:
                        self._fail("The microphone stream failed: {}".format(exc))
                        return

                    frame = data.reshape(-1)
                    # Everything heard recently, so a capture can begin
                    # before the moment the wake word was recognised.
                    preroll.append(frame)
                    audio = frame.astype(np.float32)
                    rms = float(np.sqrt((audio ** 2).mean()))
                    # Logarithmic, because hearing is. A linear scale put a
                    # quiet room and a dead microphone both at zero, which
                    # defeats the entire purpose of showing a level at all:
                    # on this machine ambient noise is rms ~20 and speech is
                    # ~500-3000. In dB that is a wide, readable range.
                    self._level = self._level * 0.6 + _meter(rms) * 0.4

                    now = time.time()
                    if rms > SILENCE_RMS:
                        self._last_audio_at = now
                        # First real audio on this device: remember it, so a
                        # future start goes straight here instead of probing
                        # during a quiet moment and picking a dead input.
                        if not self._proved:
                            self._proved = True
                            self._device["signal"] = True
                            self._remember_device(int(device))
                        if self._phase == "silent":
                            self._phase = "listening"
                            self._detail = "Listening for the wake word on '{}'.".format(
                                self._device["name"])
                    elif (self._phase == "listening"
                          and now - self._last_audio_at > SILENT_AFTER_S):
                        self._phase = "silent"
                        self._detail = (
                            "'{}' has been open {:.0f}s without a sound. Either the room is "
                            "genuinely silent, or the input is muted, unplugged, or virtual "
                            "with nothing behind it. Still listening -- it will pick up as "
                            "soon as any sound arrives.".format(
                                self._device["name"], now - self._last_audio_at))

                    # Is a reply playing? This decides three things below:
                    # whether a detection counts as an interruption, how
                    # confident it has to be, and whether the pre-roll is
                    # usable (it is not, with a reply on top of it).
                    speaking = (self.voice is not None
                                and getattr(self.voice, "speaking", False))

                    # Push-to-talk. The microphone button used to record
                    # through the browser, which uses whatever input Chrome
                    # considers the default -- the silent one here. Handling
                    # it on the stream that is already open and known to work
                    # makes the button behave like the wake word.
                    if self._capture_requested:
                        self._capture_requested = False
                        if speaking:
                            # Pressing the button while it talks means the
                            # same thing as saying its name over the top.
                            self._silence_reply()
                        self._capture_command(stream, np, preroll,
                                              interrupted=speaking)
                        model.reset()
                        last_fire = time.time()
                        continue

                    if now < self._suspended_until:
                        run = 0
                        model.reset()
                        continue

                    # While JARVIS is speaking, keep listening -- but judge
                    # what is heard against a higher bar. This is what makes
                    # it interruptible: the loop used to stop scoring
                    # entirely, so you had to wait for a reply to finish
                    # before it would hear you at all.
                    if now - last_fire < REFRACTORY_S:
                        continue

                    try:
                        scores = model.predict(frame)
                    except Exception as exc:                 # pragma: no cover
                        self._fail("Wake-word inference failed: {}".format(exc))
                        return
                    score = float(max(scores.values())) if scores else 0.0
                    self._score = score

                    bar = max(self._threshold, BARGE_IN_THRESHOLD) if speaking                         else self._threshold
                    run = run + 1 if score >= bar else 0
                    if run >= MIN_RUN:
                        run = 0
                        if speaking and not may_barge_in(
                                getattr(self.voice, "speaking_text", ""), score):
                            # JARVIS hearing itself say its own name.
                            model.reset()
                            self._score = 0.0
                            continue
                        last_fire = now
                        model.reset()
                        fired = self._fire(score, interrupted=speaking)
                        # Having heard the word on a device that demonstrably
                        # works, capture the sentence after it on the same
                        # device rather than handing it to whichever input
                        # the browser happens to prefer.
                        if fired and self._capture_commands:
                            self._capture_command(stream, np, preroll, interrupted=speaking)
                            model.reset()
                            last_fire = time.time()
        except Exception as exc:
            self._fail("Microphone capture stopped: {}".format(exc))
            return

        if self._phase != "failed":
            self._phase = "off"
            self._detail = "Stopped."
        self._level = 0.0
        self._score = 0.0

    # -- capturing the command ---------------------------------------------
    def _capture_command(self, stream, np, preroll=None,
                         interrupted: bool = False) -> None:
        """Record what is said after the wake word, then transcribe it.

        Runs on the capture thread, holding the same already-open stream, so
        there is no device hand-off and no second microphone to get wrong.
        Audio lives in memory for the length of one sentence and is never
        written to disk.
        """
        self._capturing = True
        self._phase = "recording"
        self._detail = "Listening for your command..."
        boundary = SpeechBoundary()

        # Seed with what was already heard, so the words spoken while the
        # spotter was still making up its mind are not lost -- but only when
        # nothing was playing over them.
        #
        # Measured: interrupting a reply and seeding from the pre-roll makes
        # the transcript *worse*, because that second of audio has the reply
        # at full volume on top of the speaker. "Hey Jarvis, what is my CPU
        # usage?" came back as "to my CPU usage." A barge-in therefore starts
        # clean, after the reply has stopped -- which also matches how people
        # actually interrupt: say the name, wait for it to stop, then speak.
        frames = [] if interrupted else (list(preroll) if preroll else [])
        if preroll:
            preroll.clear()

        try:
            while not self._stop.is_set():
                try:
                    data, _overflow = stream.read(FRAME)
                except Exception as exc:
                    logger.warning("hotword: capture read failed: %s", exc)
                    return

                frame = data.reshape(-1)
                frames.append(frame)
                audio = frame.astype(np.float32)
                self._level = self._level * 0.6 + _meter(
                    float(np.sqrt((audio ** 2).mean()))) * 0.4

                verdict = boundary.feed(frame)
                if verdict == "done":
                    break
                if verdict == "nothing":
                    # Woken by something that was not a command: a false
                    # positive, or the word said in passing. Say so quietly
                    # rather than transcribing a second of room noise and
                    # presenting whatever Whisper hallucinates from it.
                    logger.info("hotword: woken, but no command followed")
                    self._publish_command("", "nothing was said after the wake word")
                    return
            else:
                return

            audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
            seconds = len(audio) / float(SAMPLE_RATE)
            logger.info("hotword: captured %.1fs of command audio", seconds)
            self._phase = "thinking"
            self._detail = "Transcribing..."
            self._spawn_async(lambda: self._transcribe_and_publish(audio, seconds))
        finally:
            self._capturing = False
            if self._phase in ("recording",):
                self._phase = "listening"
            # Whatever happened, the detector should not immediately re-fire
            # on the tail of what was just said.
            self.suspend(1.0)

    async def _transcribe_and_publish(self, audio, seconds: float) -> None:
        """Turn captured audio into text, off the capture thread."""
        try:
            import numpy as np

            samples = audio.astype(np.float32) / 32768.0
            # Bounded. Transcription runs at several times realtime once the
            # model is warm, but a cold or wedged model load would otherwise
            # leave the interface saying "Transcribing..." for ever -- which
            # is precisely the kind of silent hang this whole exercise was
            # about removing.
            budget = max(30.0, seconds * 10)
            text = await asyncio.wait_for(
                self.voice.transcribe_samples(samples), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning("hotword: transcription timed out")
            self._phase = "listening"
            self._publish_command(
                "", "the speech model did not respond in time. If this is the first time, "
                    "it may still be downloading")
            return
        except Exception as exc:
            logger.warning("hotword: transcription failed: %s", exc)
            self._phase = "listening"
            self._publish_command("", "could not transcribe that: {}".format(exc))
            return

        # The pre-roll contains the wake word by design, and during a
        # barge-in it can also catch the tail of the reply. Neither is part
        # of the command.
        raw = (text or "").strip()
        text = strip_wake_word(raw)
        if raw != text:
            logger.debug("hotword: trimmed %r -> %r", raw, text)
        self._phase = "listening"
        self._detail = "Listening for the wake word on '{}'.".format(
            self._device["name"] if self._device else "")
        if not text:
            self._publish_command("", "nothing could be made out")
            return

        self._last_command = text
        logger.info("hotword: command heard: %r (%.1fs)", text, seconds)
        if self.activity is not None:
            try:
                await self.activity.record("Heard: {}".format(text),
                                           source="voice", level="info")
            except Exception:
                logger.debug("hotword: activity log failed", exc_info=True)
        self._publish_command(text, "")

    def _publish_command(self, text: str, problem: str) -> None:
        """Announce the command, and run it here rather than in the browser.

        The browser used to run it, which meant every open tab ran it: one
        sentence produced three conversations and three model calls. The
        machine heard it, so the machine answers it, once -- and because
        `chat()` publishes to the event bus, every tab still sees the whole
        exchange. It also means the wake word works with no page open at all.
        """
        payload = {"text": text, "problem": problem, "source": "wake_word"}
        self._on_loop(bus.publish, Events.WAKE_COMMAND, payload, source="hotword")
        if text and self.run_command is not None:
            self._spawn_async(lambda: self._run_command(text))

    async def _run_command(self, text: str) -> None:
        try:
            await self.run_command(text)
        except Exception:
            logger.exception("hotword: the spoken command failed")

    def _remember_device(self, index: int) -> None:
        """Persist 'this input really carries audio', from the capture thread."""
        async def save():
            try:
                current = await self.settings_store.get("voice", {}) or {}
                if current.get("wake_device_proven") == index:
                    return
                # patch, not set: this runs while the user may be changing
                # other voice settings, and a read-modify-write would discard
                # whatever they just chose.
                await self.settings_store.patch(
                    "voice", {"wake_device_proven": index}, actor="system")
                logger.info("hotword: device %s carries audio; remembered", index)
            except Exception:
                logger.debug("hotword: could not remember the device", exc_info=True)

        self._spawn_async(save)

    # -- talking to the rest of the app from the capture thread -----------
    def _on_loop(self, fn, *args, **kwargs) -> None:
        """Run `fn` on the event loop, from whichever thread we are on.

        Called from the capture thread, where publishing directly would be
        dropped for want of a running loop.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("hotword: no event loop; dropped %s", getattr(fn, "__name__", fn))
            return
        try:
            loop.call_soon_threadsafe(lambda: fn(*args, **kwargs))
        except RuntimeError:
            logger.debug("hotword: event loop gone; dropped %s",
                         getattr(fn, "__name__", fn))

    def _spawn_async(self, coro_factory) -> None:
        """Schedule a coroutine on the event loop from the capture thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        except RuntimeError:
            logger.debug("hotword: event loop gone; dropped a log write")

    # -- outcomes ---------------------------------------------------------
    def _fire(self, score: float, interrupted: bool = False) -> bool:
        """A detection. Publish it; the caller decides whether to record next.

        `interrupted` means this arrived over the top of a reply, so the
        reply has to be stopped before anything is recorded.
        """
        # JARVIS's own voice reaching the microphone a beat after it stops.
        # Not applied to a barge-in: being interrupted is exactly when the
        # reply has only just stopped.
        speaking_ended = getattr(self.voice, "speaking_ended_at", 0.0) if self.voice else 0.0
        if (not interrupted and speaking_ended
                and time.time() - speaking_ended < ECHO_GUARD_S):
            logger.debug("hotword: suppressed a detection inside the echo guard")
            return False

        self._detections += 1
        self._last_detection = now_iso()
        # The command session that follows owns the microphone next.
        self.suspend(REFRACTORY_S)
        logger.info("hotword: detected (score %.3f)", score)

        payload = {"score": round(score, 3), "model": self._model_key,
                   "device": self._device["name"] if self._device else "",
                   "at": self._last_detection, "interrupted": interrupted}
        self._on_loop(bus.publish, Events.WAKE_WORD, payload, source="hotword")

        if interrupted:
            logger.info("hotword: interrupted mid-reply")
            self._silence_reply()

        if self.activity is not None:
            self._spawn_async(lambda: self.activity.record(
                "Wake word heard (confidence {:.0%}).".format(score),
                source="voice", level="info", meta=payload))
        return True

    def _silence_reply(self) -> None:
        """Stop the reply that is playing, and wait for it to actually stop.

        Playback does not end on the instant the request is made -- it is a
        websocket hop and then an audio element -- and the tail would land in
        the recording and be transcribed as part of the command.
        """
        self._on_loop(bus.publish, Events.SPEAKING_STOP,
                      {"reason": "wake word"}, source="hotword")
        if self.voice is not None:
            try:
                self.voice.speaking_ended(interrupted=True)
            except Exception:
                logger.debug("hotword: could not clear the speaking state", exc_info=True)
        self._stop.wait(BARGE_IN_SETTLE_S)

    def _fail(self, detail: str) -> None:
        self._phase = "failed"
        self._detail = detail
        self._level = 0.0
        self._score = 0.0
        logger.warning("hotword: %s", detail)
        # May be called from either thread, so it goes through the loop hop
        # either way -- call_soon_threadsafe is safe from the loop thread too.
        self._on_loop(bus.publish, Events.WAKE_WORD_STATE,
                      {"phase": "failed", "detail": detail}, source="hotword")

    async def _log(self, message: str, meta: Dict[str, Any]) -> None:
        if self.activity is not None:
            try:
                await self.activity.record(message, source="voice", level="info", meta=meta)
            except Exception:
                logger.debug("hotword: activity log failed", exc_info=True)
        if self.audit is not None:
            try:
                await self.audit.record("voice.hotword", subject=message, detail=meta)
            except Exception:
                logger.debug("hotword: audit failed", exc_info=True)
