"""How much energy the GPU used while JARVIS was producing a reply.

Two ways to find out, best first:

1. **The card's own energy counter.** NVIDIA cards from Volta on (the RTX
   3060 here included) keep a running total of millijoules used. Reading it
   before and after a reply is exact and costs two calls.
2. **Sampling the power draw** with nvidia-smi every half second and adding
   it up -- for older cards, or when the management library is missing.

With neither, there is no number, and the stats line says nothing about
energy rather than inventing one.

This is the whole-card figure: whatever else is using the GPU at the same
moment is in it too. For a machine where the local model is the GPU's main
user that is close to the reply's own cost; it is labelled as the GPU's
energy, not the model's, all the same.

Ported idea: OpenJarvis ``telemetry/energy_nvidia.py`` (Stanford Hazy
Research, Apache-2.0) -- hardware counter first, polling fallback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("jarvis.energy")

POLL_SECONDS = 0.5


@dataclass
class EnergyReading:
    joules: float
    seconds: float
    method: str                        # counter | sampled

    @property
    def avg_watts(self) -> float:
        return self.joules / self.seconds if self.seconds > 0 else 0.0


class _Nvml:
    """The management library, opened once, or not at all."""

    def __init__(self) -> None:
        self.handles: List = []
        self.counter = False
        self.name = ""
        try:
            import pynvml  # nvidia-ml-py

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            self.handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
            if self.handles:
                raw = pynvml.nvmlDeviceGetName(self.handles[0])
                self.name = raw.decode() if isinstance(raw, bytes) else str(raw)
                try:
                    pynvml.nvmlDeviceGetTotalEnergyConsumption(self.handles[0])
                    self.counter = True
                except Exception:
                    self.counter = False
            self._pynvml = pynvml
        except Exception as exc:
            logger.debug("NVML unavailable: %s", exc)
            self._pynvml = None

    @property
    def available(self) -> bool:
        return bool(self.handles)

    def energy_mj(self) -> Optional[int]:
        if not (self.counter and self._pynvml):
            return None
        try:
            return sum(int(self._pynvml.nvmlDeviceGetTotalEnergyConsumption(h))
                       for h in self.handles)
        except Exception:
            return None

    def power_w(self) -> Optional[float]:
        if not self._pynvml:
            return None
        try:
            return sum(self._pynvml.nvmlDeviceGetPowerUsage(h) for h in self.handles) / 1000.0
        except Exception:
            return None


_nvml: Optional[_Nvml] = None


def _library() -> _Nvml:
    global _nvml
    if _nvml is None:
        _nvml = _Nvml()
    return _nvml


async def _smi_power() -> Optional[float]:
    """Power draw from nvidia-smi, for when the library is unavailable."""
    try:
        from ..core.proc import run

        done = await asyncio.to_thread(
            run, ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        if done.returncode != 0:
            return None
        values = [float(line) for line in done.stdout.split()
                  if line.replace(".", "", 1).isdigit()]
        return sum(values) if values else None
    except Exception:
        return None


class EnergyMeter:
    """Start it when a reply begins, stop it when the reply ends."""

    def __init__(self) -> None:
        self._library = _library()
        self._started = 0.0
        self._start_mj: Optional[int] = None
        self._samples: List[tuple] = []
        self._task: Optional[asyncio.Task] = None

    @property
    def method(self) -> str:
        if self._library.counter:
            return "counter"
        return "sampled"

    async def start(self) -> None:
        self._started = time.monotonic()
        self._start_mj = await asyncio.to_thread(self._library.energy_mj)
        if self._start_mj is None:
            self._task = asyncio.create_task(self._sample())

    async def _sample(self) -> None:
        try:
            while True:
                watts = await asyncio.to_thread(self._library.power_w) \
                    if self._library.available else await _smi_power()
                if watts is not None:
                    self._samples.append((time.monotonic(), watts))
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> Optional[EnergyReading]:
        seconds = time.monotonic() - self._started
        if self._start_mj is not None:
            end = await asyncio.to_thread(self._library.energy_mj)
            if end is not None and end >= self._start_mj:
                return EnergyReading((end - self._start_mj) / 1000.0, seconds, "counter")
            return None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if len(self._samples) < 2:
            return None
        joules = 0.0
        for (t0, w0), (t1, w1) in zip(self._samples, self._samples[1:]):
            joules += (w0 + w1) / 2.0 * (t1 - t0)
        return EnergyReading(joules, self._samples[-1][0] - self._samples[0][0], "sampled")


def gpu_name() -> str:
    return _library().name
