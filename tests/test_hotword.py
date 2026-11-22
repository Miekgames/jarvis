"""Wake-word detection.

These tests exercise the real openWakeWord model where it is installed, and
the service's own logic everywhere. The point of the audio tests is to keep
honest the one claim that matters -- that saying "Jarvis" fires and saying
something else does not -- rather than asserting that a function was called.
"""
from __future__ import annotations

import asyncio
import os
import time
import wave

import pytest

from backend.voice import hotword as hw


# ---------------------------------------------------------------------------
# device selection
#
# The bug that actually stopped the wake word working was not the matcher: it
# was that the default input device returns digital silence, and nothing in
# the stack noticed. These lock the "measure, don't assume" behaviour in.
# ---------------------------------------------------------------------------
def _device(index, name, default=False, opens=True, signal=False, rms=0.0):
    return {"index": index, "name": name, "default": default, "opens": opens,
            "signal": signal, "rms": rms, "peak": 0, "channels": 1, "detail": ""}


def test_chooses_a_live_device_over_a_silent_default():
    devices = [
        _device(1, "Headset Microphone (Oculus Virtual)", default=True, signal=False, rms=0.5),
        _device(2, "Microphone (HD Pro Webcam C920)", signal=True, rms=26.8),
    ]
    chosen = hw.choose_device(devices)
    assert chosen["index"] == 2, "a silent default must lose to a device carrying audio"


def test_the_os_default_is_a_tie_break_not_evidence():
    """When several inputs carry audio the loudest wins, not the OS default.

    The default on the machine this was written for is a switched-off virtual
    headset, so treating it as a signal is how the wake word ended up deaf.
    Stickiness (`preferred`) is what corrects a wrong loudest-wins pick, not
    deference to the OS.
    """
    devices = [
        _device(2, "Webcam", signal=True, rms=12.0),
        _device(5, "Headset", default=True, signal=True, rms=9.0),
    ]
    assert hw.choose_device(devices)["index"] == 2


def test_falls_back_to_an_openable_device_when_none_carry_audio():
    devices = [
        _device(7, "Line In", opens=False, signal=False),
        _device(1, "Headset", default=True, opens=True, signal=False),
    ]
    chosen = hw.choose_device(devices)
    assert chosen is not None and chosen["index"] == 1


def test_no_device_at_all_is_reported_rather_than_guessed():
    assert hw.choose_device([]) is None
    assert hw.choose_device([_device(3, "Dead", opens=False)]) is None


# ---------------------------------------------------------------------------
# service state
# ---------------------------------------------------------------------------
class _Settings:
    pass


class _Store:
    def __init__(self, values=None):
        self.values = values or {}

    async def get(self, key, default=None):
        return self.values.get(key, default)


class _Activity:
    def __init__(self):
        self.entries = []

    async def record(self, message, level="info", source="jarvis", meta=None, task_id=None):
        self.entries.append((message, meta))


class _Audit:
    def __init__(self):
        self.entries = []

    async def record(self, event, subject="", detail=None, **kwargs):
        self.entries.append((event, subject, detail))


def _service(voice=None, store=None):
    return hw.HotwordService(_Settings(), store or _Store(), voice, _Activity(), _Audit())


def test_status_is_honest_before_anything_starts():
    status = _service().status()
    assert status["running"] is False
    assert status["phase"] == "off"
    assert status["detections"] == 0
    assert status["device"] is None


@pytest.mark.asyncio
async def test_unknown_model_fails_loudly_instead_of_listening():
    service = _service()
    if not service.availability()["available"]:
        pytest.skip("wake-word packages not installed")
    status = await service.start(model="not_a_real_model")
    assert status["phase"] == "failed"
    assert "not_a_real_model" in status["detail"]
    assert status["running"] is False


@pytest.mark.asyncio
async def test_stopping_a_detector_that_never_started_is_safe():
    service = _service()
    status = await service.stop()
    assert status["phase"] == "off"
    assert status["running"] is False


def test_suspend_and_resume_gate_scoring():
    service = _service()
    assert service.status()["suspended"] is False
    service.suspend(5.0)
    assert service.status()["suspended"] is True
    service.resume()
    assert service.status()["suspended"] is False


def test_availability_names_what_is_missing():
    ready = _service().availability()
    assert isinstance(ready["available"], bool)
    if not ready["available"]:
        assert ready["missing"], "an unavailable detector must say which package is missing"
        assert "pip install" in ready["detail"]


@pytest.mark.asyncio
async def test_a_failure_is_recorded_without_an_event_loop_crash():
    """`_fail` publishes, and publishing needs the loop. It is reached from the
    capture thread, so it must never raise there."""
    service = _service()
    service._loop = asyncio.get_running_loop()
    service._fail("microphone on fire")
    assert service.status()["phase"] == "failed"
    assert service.status()["detail"] == "microphone on fire"


def test_fail_without_a_loop_does_not_raise():
    service = _service()
    service._loop = None
    service._fail("no loop here")              # must not raise
    assert service.status()["phase"] == "failed"


# ---------------------------------------------------------------------------
# detection, against real audio
# ---------------------------------------------------------------------------
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "wakeword")


def _clips():
    if not os.path.isdir(FIXTURES):
        return []
    return sorted(f for f in os.listdir(FIXTURES) if f.endswith(".wav"))


def _model():
    try:
        from openwakeword.model import Model
    except ImportError:
        pytest.skip("openwakeword is not installed")
    try:
        return Model(wakeword_models=[hw.MODELS["hey_jarvis"]], inference_framework="onnx")
    except Exception as exc:
        pytest.skip(f"wake-word model unavailable: {exc}")


def _peak_score(model, path):
    import numpy as np

    with wave.open(path, "rb") as handle:
        assert handle.getframerate() == hw.SAMPLE_RATE
        audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    model.reset()
    best = 0.0
    for i in range(0, max(0, len(audio) - hw.FRAME), hw.FRAME):
        scores = model.predict(audio[i:i + hw.FRAME])
        if scores:
            best = max(best, float(max(scores.values())))
    return best


@pytest.mark.skipif(not _clips(), reason="no wake-word audio fixtures")
def test_the_wake_word_is_detected_in_real_audio():
    model = _model()
    missed = []
    for name in _clips():
        if not name.startswith("pos_"):
            continue
        score = _peak_score(model, os.path.join(FIXTURES, name))
        if score < hw.DEFAULT_THRESHOLD:
            missed.append((name, score))
    assert not missed, f"the wake word was not detected in: {missed}"


@pytest.mark.skipif(not _clips(), reason="no wake-word audio fixtures")
def test_ordinary_speech_does_not_wake_it():
    """Negative fixtures are deliberately adversarial: 'Travis', 'hey Google',
    plain commands. One phonetic near-miss is allowed and named, because
    pretending a keyword spotter has none would be the dishonest option."""
    model = _model()
    ALLOWED_NEAR_MISS = "neg_drive_us"       # "hey, drive us" ~ "hey jarvis"
    false_alarms = []
    for name in _clips():
        if not name.startswith("neg_"):
            continue
        score = _peak_score(model, os.path.join(FIXTURES, name))
        if score >= hw.DEFAULT_THRESHOLD and not name.startswith(ALLOWED_NEAR_MISS):
            false_alarms.append((name, round(score, 3)))
    assert not false_alarms, f"ordinary speech woke the detector: {false_alarms}"


@pytest.mark.skipif(not _clips(), reason="no wake-word audio fixtures")
def test_a_single_hot_frame_is_not_enough_to_fire():
    """MIN_RUN exists to discard one-frame spikes. Check the constant is doing
    something rather than sitting at 1."""
    assert hw.MIN_RUN >= 2


# ---------------------------------------------------------------------------
# the level meter
#
# Its only job is to make "listening but deaf" visibly different from
# "listening in a quiet room". A linear scale failed at exactly that, so the
# distinction is worth a test.
# ---------------------------------------------------------------------------
def test_silence_reads_as_zero():
    assert hw._meter(0.0) == 0.0
    assert hw._meter(0.5) == 0.0


def test_a_quiet_room_is_visibly_above_silence():
    ambient = hw._meter(20.0)
    assert ambient > 0.05, "ambient noise must not be indistinguishable from a dead microphone"
    assert ambient < 0.5, "ambient noise must not look like someone talking"


def test_speech_fills_most_of_the_meter():
    assert hw._meter(600.0) > 0.6
    assert hw._meter(3000.0) >= 0.95


def test_the_meter_is_monotonic_and_bounded():
    previous = -1.0
    for rms in (0, 1, 5, 20, 80, 300, 1200, 5000, 32767):
        value = hw._meter(float(rms))
        assert 0.0 <= value <= 1.0
        assert value >= previous
        previous = value


# ---------------------------------------------------------------------------
# device selection, continued
#
# A probe that lands in a quiet moment measures nothing. These cover the cases
# where there is no signal to go on, which is when a bad default does damage.
# ---------------------------------------------------------------------------
def test_a_proven_device_wins_over_a_fresh_probe():
    devices = [
        _device(1, "Headset Microphone (Oculus Virtual)", default=True, signal=True, rms=40.0),
        _device(2, "Microphone (HD Pro Webcam C920)", signal=False, rms=0.4),
    ]
    # Device 2 is silent right now, but it is the one known to work.
    assert hw.choose_device(devices, preferred=2)["index"] == 2


def test_a_proven_device_that_cannot_be_opened_is_ignored():
    devices = [
        _device(2, "Webcam", opens=False, signal=False),
        _device(3, "Desk mic", opens=True, signal=True, rms=15.0),
    ]
    assert hw.choose_device(devices, preferred=2)["index"] == 3


def test_with_no_signal_a_physical_device_beats_a_virtual_default():
    devices = [
        _device(1, "Headset Microphone (Oculus Virtual Audio Device)", default=True),
        _device(2, "Microphone (HD Pro Webcam C920)"),
    ]
    chosen = hw.choose_device(devices)
    assert chosen["index"] == 2, "a virtual default must not win a silent probe"


def test_virtual_device_names_are_recognised():
    for name in ("Headset Microphone (Oculus Virtual Audio Device)",
                 "Microphone (Steam Streaming Microphone)",
                 "Stereo Mix (Realtek HD Audio Stereo input)",
                 "Microsoft Sound Mapper - Input",
                 "Microphone (Voicemod VAD Wave)",
                 "Primary Sound Capture Driver"):
        assert hw._looks_virtual(name), name
    for name in ("Microphone (HD Pro Webcam C920)",
                 "Microphone (Realtek HD Audio Mic input)",
                 "Desk Microphone"):
        assert not hw._looks_virtual(name), name


def test_the_loudest_live_device_wins_regardless_of_default():
    devices = [
        _device(1, "Quiet default", default=True, signal=True, rms=6.0),
        _device(4, "Loud desk mic", signal=True, rms=48.0),
    ]
    assert hw.choose_device(devices)["index"] == 4


# ---------------------------------------------------------------------------
# knowing when the user has stopped talking
#
# This decides when to stop recording and transcribe. Getting it wrong means
# either clipping people mid-sentence or leaving the microphone open, so it is
# driven here with synthetic frames rather than by talking at a laptop.
# ---------------------------------------------------------------------------
import numpy as np


def _loud_frame(amplitude=9000):
    """A frame webrtcvad and the energy fallback both call speech."""
    t = np.arange(hw.FRAME) / float(hw.SAMPLE_RATE)
    # A voiced-sounding fundamental plus harmonics, not a pure tone.
    wave_ = (np.sin(2 * np.pi * 140 * t)
             + 0.5 * np.sin(2 * np.pi * 280 * t)
             + 0.25 * np.sin(2 * np.pi * 560 * t))
    return (wave_ / 1.75 * amplitude).astype(np.int16)


def _quiet_frame():
    return np.zeros(hw.FRAME, dtype=np.int16)


def _drive(boundary, script):
    """Feed (frame, count) pairs and return the first terminal verdict."""
    for frame, count in script:
        for _ in range(count):
            verdict = boundary.feed(frame)
            if verdict in ("done", "nothing"):
                return verdict
    return "running"


FPS = hw.SAMPLE_RATE / hw.FRAME          # 12.5 frames per second


def test_silence_after_the_wake_word_is_not_a_command():
    """Woken with nothing said after it must not transcribe room noise."""
    boundary = hw.SpeechBoundary()
    verdict = _drive(boundary, [(_quiet_frame(), int(FPS * 5))])
    assert verdict == "nothing"


def test_a_sentence_then_a_pause_ends_the_recording():
    boundary = hw.SpeechBoundary()
    verdict = _drive(boundary, [
        (_loud_frame(), int(FPS * 1.5)),          # ~1.5s of speech
        (_quiet_frame(), int(FPS * 2.0)),         # then a pause
    ])
    assert verdict == "done"


def test_a_pause_shorter_than_the_threshold_does_not_end_it():
    """People pause mid-sentence. Cutting them off there is the failure that
    makes voice assistants infuriating."""
    boundary = hw.SpeechBoundary()
    gap = int((hw.COMMAND_TRAILING_SILENCE_S * 0.5) * FPS)
    verdict = _drive(boundary, [
        (_loud_frame(), int(FPS * 1.0)),
        (_quiet_frame(), gap),                    # a mid-sentence breath
        (_loud_frame(), int(FPS * 1.0)),
    ])
    assert verdict == "running", "a short pause must not end the recording"


def test_a_cough_is_not_a_command():
    boundary = hw.SpeechBoundary()
    verdict = _drive(boundary, [
        (_loud_frame(), 1),                       # 80ms, well under the minimum
        (_quiet_frame(), int(FPS * 2.0)),
    ])
    assert verdict == "nothing"


def test_recording_cannot_run_forever():
    """A stuck detector must not leave the microphone open indefinitely."""
    boundary = hw.SpeechBoundary()
    verdict = _drive(boundary, [(_loud_frame(), int(FPS * (hw.COMMAND_MAX_S + 3)))])
    assert verdict == "done"
    assert boundary.elapsed_s <= hw.COMMAND_MAX_S + 1


def test_the_boundary_reports_which_detector_it_is_using():
    """The energy fallback is worse in a noisy room, so which one is running
    is not something to leave implicit."""
    assert isinstance(hw.SpeechBoundary().using_vad, bool)


def test_timings_are_sane_relative_to_each_other():
    assert hw.COMMAND_MIN_SPEECH_S < hw.COMMAND_TRAILING_SILENCE_S
    assert hw.COMMAND_TRAILING_SILENCE_S < hw.COMMAND_LEAD_IN_S < hw.COMMAND_MAX_S
    assert hw.FRAME % hw.VAD_FRAME == 0, "capture frames must split evenly for webrtcvad"


# ---------------------------------------------------------------------------
# surviving a microphone that goes away
#
# A USB microphone unplugged mid-session takes the capture stream with it.
# For a feature whose whole job is to be there when you speak, dying quietly
# until someone toggles a setting is the same as not working.
# ---------------------------------------------------------------------------
import threading


def _fast_retries(monkeypatch):
    """Shrink the backoff so a recovery can be tested in milliseconds."""
    monkeypatch.setattr(hw, "RETRY_BASE_S", 0.01)
    monkeypatch.setattr(hw, "RETRY_MAX_S", 0.02)


def test_the_detector_retries_after_a_failure(monkeypatch):
    _fast_retries(monkeypatch)
    service = _service()
    attempts = []
    recovered = threading.Event()

    def fake_run(device):
        attempts.append(device)
        if len(attempts) < 3:
            service._fail("the microphone was unplugged")
            return
        service._phase = "listening"        # a clean run
        recovered.set()

    service._run = fake_run
    service._supervise(2)

    assert recovered.is_set(), "the detector must come back after a transient failure"
    assert len(attempts) == 3
    assert service.status()["phase"] == "listening"


def test_a_retry_re_probes_rather_than_reusing_a_dead_index(monkeypatch):
    """Indices move when devices come and go, so a retry must look again."""
    _fast_retries(monkeypatch)
    service = _service()
    seen = []

    def fake_run(device):
        seen.append(device)
        if len(seen) < 2:
            service._fail("gone")
            return
        service._phase = "listening"

    service._run = fake_run
    service._supervise(7)

    assert seen[0] == 7, "the first attempt uses the chosen device"
    assert seen[1] is None, "a retry must re-probe instead of trusting the old index"


def test_it_gives_up_eventually_and_says_so(monkeypatch):
    _fast_retries(monkeypatch)
    monkeypatch.setattr(hw, "MAX_RETRIES", 3)
    service = _service()
    attempts = []

    def always_fails(device):
        attempts.append(device)
        service._fail("no microphone at all")

    service._run = always_fails
    service._supervise(None)

    assert len(attempts) == 3, "it must stop rather than spin for ever"
    status = service.status()
    assert status["phase"] == "failed"
    assert "Giving up" in status["detail"]
    assert "switch the wake word off and on" in status["detail"]


def test_stopping_ends_the_retry_loop_promptly(monkeypatch):
    _fast_retries(monkeypatch)
    service = _service()
    attempts = []

    def fails_then_stopped(device):
        attempts.append(device)
        service._fail("gone")
        service._stop.set()                 # as `stop()` would

    service._run = fails_then_stopped
    service._supervise(None)
    assert len(attempts) == 1, "a stop must not be followed by another attempt"


def test_a_clean_exit_is_not_retried(monkeypatch):
    """Stopping normally is not a failure to recover from."""
    _fast_retries(monkeypatch)
    service = _service()
    attempts = []

    def clean(device):
        attempts.append(device)
        service._phase = "off"

    service._run = clean
    service._supervise(None)
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# push to talk
#
# The microphone button used to record through the browser, which records
# through whatever input Chrome prefers -- here, a silent one. It has to use
# the same device the detector has already proven.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_push_to_talk_declines_clearly_when_capture_is_off():
    """Declining is fine; pretending to listen is not. The caller needs to
    know it should fall back to the browser."""
    service = _service()
    service._capture_commands = False
    result = await service.listen_now()
    assert result["ok"] is False
    assert "browser" in result["reason"]


@pytest.mark.asyncio
async def test_push_to_talk_declines_when_nothing_is_listening():
    service = _service()
    service._capture_commands = True       # capture wanted, but no stream open
    result = await service.listen_now()
    assert result["ok"] is False
    assert "not running" in result["reason"]


@pytest.mark.asyncio
async def test_push_to_talk_asks_the_open_stream_to_record():
    service = _service()
    service._capture_commands = True
    service._device = {"name": "Desk mic", "index": 2}

    class _Alive:
        @staticmethod
        def is_alive():
            return True

    service._thread = _Alive()
    assert service._capture_requested is False

    result = await service.listen_now()
    assert result["ok"] is True
    assert service._capture_requested is True, "the capture loop must be asked to record"
    assert result["device"] == "Desk mic"


@pytest.mark.asyncio
async def test_push_to_talk_clears_a_suspension():
    """Pressing the button is an explicit request to be heard, so it must
    override a suspension left over from a command session."""
    service = _service()
    service._capture_commands = True
    service._device = {"name": "Desk mic", "index": 2}

    class _Alive:
        @staticmethod
        def is_alive():
            return True

    service._thread = _Alive()
    service.suspend(30.0)
    assert service.status()["suspended"] is True

    await service.listen_now()
    assert service.status()["suspended"] is False


@pytest.mark.asyncio
async def test_push_to_talk_refuses_to_start_a_second_recording():
    service = _service()
    service._capture_commands = True
    service._capturing = True

    class _Alive:
        @staticmethod
        def is_alive():
            return True

    service._thread = _Alive()
    result = await service.listen_now()
    assert result["ok"] is False
    assert "Already recording" in result["reason"]


# ---------------------------------------------------------------------------
# interrupting a reply
#
# While JARVIS speaks, its own voice reaches the microphone. The loop used to
# stop scoring entirely for the duration, which made it impossible to
# interrupt: you had to wait for the sentence to finish before it would hear
# you at all.
#
# Telling "the user spoke over it" from "JARVIS heard itself" needs no echo
# cancellation, because we know exactly what is coming out of the speakers.
# ---------------------------------------------------------------------------
def test_a_confident_wake_word_over_an_ordinary_reply_is_an_interruption():
    assert hw.may_barge_in("Your CPU is at forty percent.", 0.95)


def test_the_reply_saying_the_wake_word_is_not_an_interruption():
    """JARVIS hearing its own voice say its own name. The commonest false
    positive, and the reason the detector used to go deaf entirely."""
    for reply in ("I am Jarvis, your assistant.",
                  "You can say hey Jarvis at any time.",
                  "JARVIS is listening."):
        assert not hw.may_barge_in(reply, 0.99), reply


def test_a_hesitant_detection_over_a_reply_is_ignored():
    """The bar is higher during playback, not lower: the microphone is
    hearing the reply as well as the room."""
    assert not hw.may_barge_in("Your CPU is at forty percent.", 0.55)
    assert hw.BARGE_IN_THRESHOLD > hw.DEFAULT_THRESHOLD


def test_the_threshold_is_exclusive_at_the_boundary():
    assert hw.may_barge_in("the weather is fine", hw.BARGE_IN_THRESHOLD)
    assert not hw.may_barge_in("the weather is fine", hw.BARGE_IN_THRESHOLD - 0.01)


def test_an_empty_reply_does_not_block_an_interruption():
    """Nothing known about what is playing is not evidence that JARVIS said
    its own name."""
    assert hw.may_barge_in("", 0.95)
    assert hw.may_barge_in(None, 0.95)


def test_common_mishearings_of_the_name_also_count_as_self_reference():
    for reply in ("Jarvus reporting in.", "This is Jervis speaking."):
        assert not hw.may_barge_in(reply, 0.99), reply


def test_the_check_is_case_insensitive():
    assert not hw.may_barge_in("JARVIS HERE.", 0.99)
    assert not hw.may_barge_in("jarvis here.", 0.99)


def test_a_reply_merely_containing_hey_is_still_interruptible():
    """"Hey" is everywhere; only the distinctive part of the name matters."""
    assert hw.may_barge_in("Hey, that is a good question.", 0.95)


def test_settling_time_is_allowed_before_recording():
    """Playback does not stop on the instant it is asked to, and the tail
    would otherwise land in the recorded command."""
    assert hw.BARGE_IN_SETTLE_S >= 0.15, (
        "too short and the tail of the reply is transcribed as part of the command")
    assert hw.BARGE_IN_SETTLE_S <= 0.35, (
        "too long and it eats the start of what the user is saying -- at 0.45s "
        "'Hey Jarvis, what is my CPU usage?' was recorded as 'My CPU usage.'")


# ---------------------------------------------------------------------------
# not losing the first word
#
# The spotter only recognises the wake word once it has heard all of it, so
# by the time a detection fires the speaker is already into the next word:
# "Hey Jarvis, what is my CPU usage?" was captured as "My CPU usage."
#
# A rolling buffer of recent audio is prepended to the capture, which puts
# the wake word itself in the recording -- so the transcript is trimmed to
# whatever follows it.
# ---------------------------------------------------------------------------
def test_the_wake_word_is_trimmed_from_the_command():
    assert hw.strip_wake_word("Hey Jarvis, what is my CPU usage?") == "what is my CPU usage?"
    assert hw.strip_wake_word("Jarvis open the calculator") == "open the calculator"


def test_trimming_handles_the_punctuation_that_follows_a_name():
    for said in ("Jarvis, open it", "Jarvis. open it", "Jarvis: open it",
                 "Jarvis - open it", "Jarvis open it"):
        assert hw.strip_wake_word(said) == "open it", said


def test_anything_before_the_wake_word_goes_too():
    """During a barge-in the pre-roll catches the tail of the reply being
    interrupted. That belongs before the command, not in it."""
    heard = "and light winds from the south west. Hey Jarvis, what is my CPU usage?"
    assert hw.strip_wake_word(heard) == "what is my CPU usage?"


def test_a_command_without_the_wake_word_is_left_alone():
    """Push-to-talk has no wake word in it at all."""
    assert hw.strip_wake_word("what is my CPU usage?") == "what is my CPU usage?"
    assert hw.strip_wake_word("open the calculator") == "open the calculator"


def test_mishearings_of_the_name_are_trimmed_too():
    """Whisper returns "Travis" and "Javis" often enough to matter, and a
    command starting with a stray name confuses the model."""
    assert hw.strip_wake_word("Hey Travis, what time is it?") == "what time is it?"
    assert hw.strip_wake_word("Javis open settings") == "open settings"


def test_a_bare_wake_word_does_not_become_empty():
    """Saying only the name is a real thing to do; it must not come back as
    an empty command that looks like a failure."""
    assert hw.strip_wake_word("Jarvis").strip()
    assert hw.strip_wake_word("Hey Jarvis.").strip()


def test_the_last_occurrence_wins():
    """"Jarvis, ask Jarvis about X" -- the command starts after the last one
    that could plausibly be the wake word."""
    assert hw.strip_wake_word("Hey Jarvis, Jarvis, what is the time?") == "what is the time?"


def test_empty_input_is_handled():
    assert hw.strip_wake_word("") == ""
    assert hw.strip_wake_word(None) == ""


def test_the_preroll_is_long_enough_to_cover_the_detection_lag():
    """The score stays high for several frames after the word ends, and
    MIN_RUN adds more, so a second is the right order of magnitude."""
    assert hw.PREROLL_S >= 0.8
    assert hw.PREROLL_FRAMES == int(hw.PREROLL_S * hw.SAMPLE_RATE / hw.FRAME)
    assert hw.PREROLL_FRAMES > hw.MIN_RUN
