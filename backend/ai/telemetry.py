"""What one reply cost to produce, measured rather than estimated.

A reply can take several model calls -- one that asks for a tool, one that
answers with the result -- so the figures here are for the whole reply as
the user experienced it: from the moment the question was sent to the
moment the answer finished.

  * tokens in and out, summed over every call;
  * time to first token: how long before anything appeared;
  * generation speed, from the model runtime's own timings where it reports
    them (Ollama does) rather than from wall-clock time, which includes
    tool calls and waiting;
  * GPU energy over the reply, from `systemmon.energy`;
  * what the same tokens would have cost on a cloud model, as a reference
    -- the "saved" side of running locally.

Ported idea: OpenJarvis's per-message "X-ray" telemetry and its insistence
that energy and cost be measured alongside the answer (Stanford Hazy
Research, Apache-2.0). Implementation is JARVIS's own.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

# The reference price for "what this would have cost in the cloud", in
# dollars per million tokens. A mid-tier hosted model; stated wherever the
# figure is shown, because a saving is only as honest as what it is
# measured against.
REFERENCE_PRICE = {"in": 3.00, "out": 15.00}
REFERENCE_LABEL = "$3 / $15 per million tokens in / out"


class ReplyTelemetry:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.first_token: Optional[float] = None
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.eval_ms = 0
        self.load_ms = 0
        self.provider = ""
        self.model = ""

    def token(self) -> None:
        """Something visible reached the user."""
        if self.first_token is None:
            self.first_token = time.monotonic()

    def add(self, completion: Any) -> None:
        usage = getattr(completion, "usage", None) or {}
        self.calls += 1
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        self.eval_ms += int(usage.get("eval_ms", 0) or 0)
        self.load_ms += int(usage.get("load_ms", 0) or 0)
        self.provider = getattr(completion, "provider", "") or self.provider
        self.model = getattr(completion, "model", "") or self.model

    def finish(self, energy: Any = None) -> Dict[str, Any]:
        now = time.monotonic()
        total_ms = int((now - self.started) * 1000)
        ttft_ms = int((self.first_token - self.started) * 1000) if self.first_token else None

        # The runtime's own generation time where it reports one. Otherwise
        # the time between the first token and the end, which is close for
        # a streamed reply without tool calls and is all there is.
        if self.eval_ms > 0 and self.output_tokens:
            speed: Optional[float] = self.output_tokens / (self.eval_ms / 1000.0)
        elif ttft_ms is not None and self.output_tokens and total_ms > ttft_ms:
            speed = self.output_tokens / ((total_ms - ttft_ms) / 1000.0)
        else:
            speed = None

        joules = round(energy.joules, 1) if energy is not None else None
        watts = round(energy.avg_watts, 1) if energy is not None else None
        per_joule = (round(self.output_tokens / energy.joules, 3)
                     if energy is not None and energy.joules > 0 else None)

        cloud = (self.input_tokens / 1e6 * REFERENCE_PRICE["in"]
                 + self.output_tokens / 1e6 * REFERENCE_PRICE["out"])

        return {
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_per_sec": round(speed, 1) if speed else None,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "load_ms": self.load_ms or None,
            "energy_j": joules,
            "avg_power_w": watts,
            "energy_method": getattr(energy, "method", None),
            "tokens_per_joule": per_joule,
            "cloud_cost_usd": round(cloud, 5),
            "cloud_reference": REFERENCE_LABEL,
        }
