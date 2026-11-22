"""Health reporting.

The rule these enforce is a simple one: never show a green light for
something that is broken. It is easy to violate by accident -- the voice
check reported "online" for months on the strength of `import edge_tts`
succeeding, while every actual synthesis came back 403, and the AI itself was
not checked at all, so the strip could read a confident "online" for a system
that could not answer a question.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.systemmon.health import HealthService, Status, _overall


# ---------------------------------------------------------------------------
# stand-ins
# ---------------------------------------------------------------------------
class _DB:
    async def fetch_value(self, *_args, **_kwargs):
        return 1


class _Orchestrator:
    def __init__(self, providers):
        self._providers = providers

    async def provider_status(self):
        return self._providers


class _SlowOrchestrator:
    async def provider_status(self):
        await asyncio.sleep(30)
        return {}


class _Voice:
    def __init__(self, report):
        self._report = report

    async def health(self):
        return self._report


class _Hotword:
    def __init__(self, state):
        self._state = state

    def status(self):
        return self._state


class _Settings:
    METRICS_INTERVAL_SECONDS = 3


def _service(**services):
    services.setdefault("db", _DB())
    return HealthService(services, _Settings())


# ---------------------------------------------------------------------------
# the AI
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unreachable_ai_is_not_reported_as_online():
    report = await _service(
        orchestrator=_Orchestrator({"ollama": {"available": False}})).quick_check()
    assert report["checks"]["ai"]["status"] == Status.OFFLINE
    assert "Ollama" in report["checks"]["ai"]["detail"]
    # An offline check must be capable of pulling the total down.
    assert _overall({"ai": report["checks"]["ai"]}) != Status.ONLINE,         "the strip must not be green with no AI"


@pytest.mark.asyncio
async def test_a_reachable_ai_is_online_and_names_the_provider():
    report = await _service(
        orchestrator=_Orchestrator({"ollama": {"available": True}})).quick_check()
    assert report["checks"]["ai"]["status"] == Status.ONLINE
    assert "ollama" in report["checks"]["ai"]["detail"]


@pytest.mark.asyncio
async def test_a_provider_that_hangs_is_degraded_not_fatal():
    """A health check must never block the loop that runs it."""
    report = await asyncio.wait_for(
        _service(orchestrator=_SlowOrchestrator()).quick_check(), timeout=15)
    assert report["checks"]["ai"]["status"] == Status.DEGRADED


@pytest.mark.asyncio
async def test_a_provider_check_that_raises_is_reported_not_swallowed():
    class _Broken:
        async def provider_status(self):
            raise RuntimeError("socket exploded")

    report = await _service(orchestrator=_Broken()).quick_check()
    assert report["checks"]["ai"]["status"] == Status.ERROR
    assert "socket exploded" in report["checks"]["ai"]["detail"]


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_voice_health_is_passed_through():
    degraded = {"status": Status.DEGRADED, "detail": "edge-tts is failing (403)"}
    report = await _service(voice=_Voice(degraded)).quick_check()
    assert report["checks"]["voice"]["status"] == Status.DEGRADED
    assert "403" in report["checks"]["voice"]["detail"]
    assert _overall({"voice": report["checks"]["voice"]}) != Status.ONLINE


# ---------------------------------------------------------------------------
# the wake word
# ---------------------------------------------------------------------------
def _wake(running, phase, detail="x", heard_anything=False):
    return {"running": running, "phase": phase, "detail": detail,
            "heard_anything": heard_anything}


@pytest.mark.asyncio
async def test_a_wake_word_that_is_off_is_not_a_fault():
    """Switched off is a choice. A red light for it trains people to ignore
    the row, which is how real faults get missed."""
    report = await _service(hotword=_Hotword(_wake(False, "off"))).quick_check()
    assert "wake_word" not in report["checks"]


@pytest.mark.asyncio
async def test_a_listening_wake_word_is_online():
    report = await _service(hotword=_Hotword(_wake(True, "listening"))).quick_check()
    assert report["checks"]["wake_word"]["status"] == Status.ONLINE


@pytest.mark.asyncio
async def test_a_microphone_that_has_never_carried_audio_is_not_healthy():
    """Open but having never received a sound is the exact failure the whole
    subsystem exists to surface. It must not read as green."""
    report = await _service(hotword=_Hotword(
        _wake(True, "silent", "no sound for 20s", heard_anything=False))).quick_check()
    assert report["checks"]["wake_word"]["status"] == Status.DEGRADED
    assert _overall({"wake_word": report["checks"]["wake_word"]}) != Status.ONLINE


@pytest.mark.asyncio
async def test_a_quiet_room_is_not_a_fault():
    """A device that has delivered sound and has gone quiet is a quiet room.
    Flagging that would leave the strip amber every quiet evening, which is
    how a status light stops being read at all."""
    report = await _service(hotword=_Hotword(
        _wake(True, "silent", "no sound for 20s", heard_anything=True))).quick_check()
    assert report["checks"]["wake_word"]["status"] == Status.ONLINE
    assert "quiet" in report["checks"]["wake_word"]["detail"]


@pytest.mark.asyncio
async def test_a_failed_wake_word_is_an_error_with_the_reason():
    report = await _service(
        hotword=_Hotword(_wake(True, "failed", "no usable microphone"))).quick_check()
    assert report["checks"]["wake_word"]["status"] == Status.ERROR
    assert "no usable microphone" in report["checks"]["wake_word"]["detail"]


@pytest.mark.asyncio
async def test_recording_and_transcribing_are_healthy_states():
    for phase in ("recording", "thinking"):
        report = await _service(hotword=_Hotword(_wake(True, phase))).quick_check()
        assert report["checks"]["wake_word"]["status"] == Status.ONLINE, phase


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def test_one_error_outranks_everything_else():
    assert _overall({
        "a": {"status": Status.ONLINE},
        "b": {"status": Status.ERROR},
        "c": {"status": Status.DEGRADED},
    }) == Status.ERROR


def test_all_green_is_green():
    assert _overall({"a": {"status": Status.ONLINE}}) == Status.ONLINE


@pytest.mark.asyncio
async def test_absent_services_are_simply_not_reported():
    """A subsystem that is not built must not invent a check for itself."""
    report = await _service().quick_check()
    for absent in ("ai", "voice", "wake_word"):
        assert absent not in report["checks"]
