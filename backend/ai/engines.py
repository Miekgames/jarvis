"""Which local model servers are running, and which models fit this machine.

**Discovery.** Local model servers sit on well-known ports. Each is asked,
briefly, for its model list on the OpenAI-compatible path; whatever answers
is offered in Settings as something JARVIS can use. Ports that are JARVIS's
own are skipped.

**Fit.** A model runs at full speed only if it fits in the GPU's memory
alongside its context cache; past that, layers spill to system memory and
generation slows several-fold. For every model Ollama has pulled, its real
size on disk is compared with the card's real memory -- both measured, not
looked up -- and it is labelled as fitting, tight, or spilling over.

Ported idea: OpenJarvis ``engine/_discovery.py`` (probe running engines,
aggregate their models) and its model catalogue's fit-to-hardware advice
(Stanford Hazy Research, Apache-2.0).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.engines")

# (name, port) -- the defaults each server ships with.
KNOWN = [
    ("LM Studio", 1234),
    ("llama.cpp server", 8080),
    ("Jan", 1337),
    ("KoboldCpp", 5001),
    ("text-generation-webui", 5000),
    ("vLLM", 8001),
    ("LocalAI", 8081),
    ("Ollama (OpenAI-compatible)", 11434),
]

# The context cache and runtime need room beyond the weights themselves.
OVERHEAD_GB = 1.4


async def _probe(client, name: str, port: int) -> Optional[Dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/v1"
    try:
        response = await client.get(f"{url}/models", timeout=1.5)
        if response.status_code != 200:
            return None
        models = [m.get("id") for m in (response.json().get("data") or []) if m.get("id")]
    except Exception:
        return None
    # JARVIS already talks to Ollama natively, which keeps its own timings
    # and context size; going through this door instead reloads the model.
    return {"name": name, "port": port, "base_url": url, "models": models,
            "native": port == 11434}


async def discover(own_port: int = 8000) -> List[Dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient() as client:
        found = await asyncio.gather(*(_probe(client, name, port) for name, port in KNOWN
                                       if port != own_port))
    return [engine for engine in found if engine]


def gpu_memory_gb() -> Optional[Dict[str, float]]:
    try:
        from ..systemmon.energy import _library

        library = _library()
        if not library.available:
            return None
        import pynvml

        info = pynvml.nvmlDeviceGetMemoryInfo(library.handles[0])
        return {"total": round(info.total / 1e9, 1), "free": round(info.free / 1e9, 1)}
    except Exception:
        return None


def fit(size_gb: float, vram_gb: Optional[float]) -> str:
    if not vram_gb:
        return "unknown"
    need = size_gb + OVERHEAD_GB
    if need <= vram_gb * 0.85:
        return "fits"
    if need <= vram_gb:
        return "tight"
    return "spills"


async def ollama_models(host: str) -> List[Dict[str, Any]]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(f"{host.rstrip('/')}/api/tags")
            response.raise_for_status()
            return response.json().get("models") or []
    except Exception:
        return []


async def overview(ollama_host: str, own_port: int = 8000) -> Dict[str, Any]:
    engines, installed = await asyncio.gather(discover(own_port), ollama_models(ollama_host))
    memory = gpu_memory_gb()
    total = memory["total"] if memory else None
    models = []
    for model in installed:
        size = round(float(model.get("size") or 0) / 1e9, 1)
        details = model.get("details") or {}
        models.append({
            "name": model.get("name", ""),
            "size_gb": size,
            "parameters": details.get("parameter_size", ""),
            "quantization": details.get("quantization_level", ""),
            "fit": fit(size, total),
        })
    models.sort(key=lambda m: m["size_gb"])
    return {"engines": engines, "ollama_models": models, "gpu_memory": memory,
            "overhead_gb": OVERHEAD_GB}
