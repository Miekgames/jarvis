"""Pairing what the microphone heard with what it then did.

An always-on microphone should be able to show its work, and the pieces were
already being recorded -- a detection logs its confidence, a transcription
logs the text. The subtlety is the order.

Activity rows arrive **newest first**, and a transcript is logged *after* the
detection it belongs to. So in that order the detection appears **later** in
the list, not earlier. Pairing forwards hands each transcript to the next
detection up, which is a different, newer utterance -- the first version did
exactly that and reported one command against someone else's detection.
"""
from __future__ import annotations

from backend.api.ai_routes import COMMAND_PAIRING_WINDOW_S, pair_voice_events


def detection(ts: str, score: float = 0.98) -> dict:
    return {"ts": ts, "message": "Wake word heard (confidence 98%).",
            "meta": {"score": score, "device": "Webcam", "model": "hey_jarvis"}}


def heard(ts: str, text: str) -> dict:
    return {"ts": ts, "message": f"Heard: {text}", "meta": {}}


def noise(ts: str) -> dict:
    return {"ts": ts, "message": "wake word listening started", "meta": {}}


# ---------------------------------------------------------------------------
def test_a_detection_is_paired_with_the_command_that_followed_it():
    rows = [                                        # newest first
        heard("2026-11-05T02:44:27+00:00", "what is my CPU usage"),
        detection("2026-11-05T02:44:18+00:00"),
    ]
    events = pair_voice_events(rows, 10)
    assert len(events) == 1
    assert events[0]["kind"] == "detection"
    assert events[0]["heard"] == "what is my CPU usage"


def test_a_transcript_is_not_given_to_a_newer_detection():
    """The regression. Pairing forwards attaches the older transcript to the
    newer detection, reporting a command against the wrong utterance."""
    rows = [
        detection("2026-11-05T02:44:18+00:00", 0.98),   # newest, nothing followed
        heard("2026-11-05T02:40:05+00:00", "name one fruit"),
        detection("2026-11-05T02:40:00+00:00", 0.91),   # this one said it
    ]
    events = pair_voice_events(rows, 10)
    newest, older = events[0], events[1]
    assert newest["ts"].startswith("2026-11-05T02:44")
    assert newest["heard"] is None, "the newest detection had nothing after it"
    assert older["heard"] == "name one fruit"


def test_a_detection_with_nothing_after_it_says_so():
    events = pair_voice_events([detection("2026-11-05T02:44:18+00:00")], 10)
    assert events[0]["heard"] is None


def test_a_command_with_no_detection_is_push_to_talk():
    """The microphone button records without a wake word."""
    rows = [heard("2026-11-05T02:44:27+00:00", "open the calculator")]
    events = pair_voice_events(rows, 10)
    assert len(events) == 1
    assert events[0]["kind"] == "command"
    assert events[0]["score"] is None
    assert events[0]["heard"] == "open the calculator"


def test_a_transcript_long_after_a_detection_is_not_its_command():
    """Capture and transcription take seconds. Minutes later is unrelated."""
    rows = [
        heard("2026-11-05T03:00:00+00:00", "something much later"),
        detection("2026-11-05T02:44:18+00:00"),
    ]
    events = pair_voice_events(rows, 10)
    kinds = {e["kind"] for e in events}
    assert kinds == {"detection", "command"}, "they must not be paired"
    detection_event = next(e for e in events if e["kind"] == "detection")
    assert detection_event["heard"] is None


def test_other_voice_logging_is_ignored():
    """The feed carries model loads and start/stop notices too."""
    rows = [
        noise("2026-11-05T02:45:00+00:00"),
        heard("2026-11-05T02:44:27+00:00", "what is my CPU usage"),
        detection("2026-11-05T02:44:18+00:00"),
        noise("2026-11-05T02:40:00+00:00"),
    ]
    events = pair_voice_events(rows, 10)
    assert len(events) == 1
    assert events[0]["heard"] == "what is my CPU usage"


def test_two_transcripts_in_a_row_do_not_merge():
    """The older one simply never had a detection of its own."""
    rows = [
        heard("2026-11-05T02:44:30+00:00", "second thing"),
        heard("2026-11-05T02:44:20+00:00", "first thing"),
        detection("2026-11-05T02:44:15+00:00"),
    ]
    events = pair_voice_events(rows, 10)
    texts = [e["heard"] for e in events]
    assert "second thing" in texts and "first thing" in texts
    paired = next(e for e in events if e["kind"] == "detection")
    assert paired["heard"] == "first thing", "the nearer transcript belongs to it"


def test_results_are_newest_first():
    rows = [
        detection("2026-11-05T02:50:00+00:00"),
        detection("2026-11-05T02:40:00+00:00"),
        detection("2026-11-05T02:30:00+00:00"),
    ]
    events = pair_voice_events(rows, 10)
    assert [e["ts"] for e in events] == sorted((e["ts"] for e in events), reverse=True)


def test_the_limit_is_respected():
    rows = [detection(f"2026-11-05T02:{m:02d}:00+00:00") for m in range(50, 10, -1)]
    assert len(pair_voice_events(rows, 5)) == 5


def test_the_confidence_survives():
    events = pair_voice_events([detection("2026-11-05T02:44:18+00:00", 0.873)], 10)
    assert events[0]["score"] == 0.873
    assert events[0]["device"] == "Webcam"


def test_the_pairing_window_is_sane():
    assert 10 <= COMMAND_PAIRING_WINDOW_S <= 300


def test_empty_input_is_handled():
    assert pair_voice_events([], 10) == []
