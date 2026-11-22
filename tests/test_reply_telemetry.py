"""Measuring what a reply cost: tokens, time, GPU energy, cloud reference."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.ai.telemetry import REFERENCE_PRICE, ReplyTelemetry
from backend.systemmon import energy as energy_module
from backend.systemmon.energy import EnergyMeter, EnergyReading


def completion(tokens_in, tokens_out, eval_ms=0, load_ms=0, model="qwen2.5", provider="ollama"):
    return SimpleNamespace(
        usage={"input_tokens": tokens_in, "output_tokens": tokens_out,
               "eval_ms": eval_ms, "load_ms": load_ms},
        model=model, provider=provider)


def test_a_reply_over_several_model_calls_is_one_reply():
    t = ReplyTelemetry()
    t.add(completion(400, 20, eval_ms=400))       # asks for a tool
    t.token()
    t.add(completion(650, 180, eval_ms=3600))     # answers with it
    out = t.finish()
    assert out["calls"] == 2
    assert out["input_tokens"] == 1050 and out["output_tokens"] == 200
    # From the runtime's own generation time, not wall-clock.
    assert out["tokens_per_sec"] == 50.0
    assert out["ttft_ms"] is not None and out["total_ms"] >= out["ttft_ms"]


def test_without_runtime_timings_speed_comes_from_the_stream():
    t = ReplyTelemetry()
    t.started -= 3.0
    t.first_token = t.started + 1.0
    t.add(completion(100, 100, eval_ms=0, provider="anthropic", model="claude"))
    out = t.finish()
    assert 45 <= out["tokens_per_sec"] <= 55


def test_nothing_is_invented_when_nothing_was_measured():
    out = ReplyTelemetry().finish(None)
    assert out["tokens_per_sec"] is None
    assert out["energy_j"] is None and out["tokens_per_joule"] is None
    assert out["ttft_ms"] is None


def test_energy_and_tokens_per_joule():
    t = ReplyTelemetry()
    t.add(completion(10, 300, eval_ms=6000))
    out = t.finish(EnergyReading(joules=150.0, seconds=6.0, method="counter"))
    assert out["energy_j"] == 150.0 and out["avg_power_w"] == 25.0
    assert out["tokens_per_joule"] == 2.0
    assert out["energy_method"] == "counter"


def test_the_cloud_reference_uses_the_stated_price():
    t = ReplyTelemetry()
    t.add(completion(1_000_000, 1_000_000))
    out = t.finish()
    assert out["cloud_cost_usd"] == REFERENCE_PRICE["in"] + REFERENCE_PRICE["out"]
    assert "$3" in out["cloud_reference"]


# ---------------------------------------------------------------------------
class _FakeLibrary:
    def __init__(self, counter=True):
        self.counter = counter
        self.available = True
        self.handles = [object()]
        self.reading = 1_000_000
        self.watts = 40.0

    def energy_mj(self):
        return self.reading if self.counter else None

    def power_w(self):
        return self.watts


@pytest.mark.asyncio
async def test_the_hardware_counter_gives_an_exact_reading(monkeypatch):
    library = _FakeLibrary(counter=True)
    monkeypatch.setattr(energy_module, "_library", lambda: library)
    meter = EnergyMeter()
    await meter.start()
    library.reading += 12_500          # 12.5 J later
    reading = await meter.stop()
    assert reading.method == "counter" and reading.joules == 12.5


@pytest.mark.asyncio
async def test_without_a_counter_power_is_sampled_and_added_up(monkeypatch):
    import asyncio

    library = _FakeLibrary(counter=False)
    monkeypatch.setattr(energy_module, "_library", lambda: library)
    monkeypatch.setattr(energy_module, "POLL_SECONDS", 0.05)
    meter = EnergyMeter()
    await meter.start()
    await asyncio.sleep(0.3)
    reading = await meter.stop()
    assert reading.method == "sampled"
    assert 30 <= reading.avg_watts <= 50
    assert reading.joules > 0


@pytest.mark.asyncio
async def test_no_gpu_means_no_reading(monkeypatch):
    library = _FakeLibrary(counter=False)
    library.available = False
    library.handles = []

    async def none():
        return None

    monkeypatch.setattr(energy_module, "_library", lambda: library)
    monkeypatch.setattr(energy_module, "_smi_power", none)
    meter = EnergyMeter()
    await meter.start()
    assert await meter.stop() is None
