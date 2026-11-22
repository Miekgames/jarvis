"""First-run setup: who you are, which AI to use, your key, what JARVIS may do.

The same endpoints serve the setup screen and, later, changing the AI
provider from Settings. API keys travel from the browser to this server
once, are sealed (ai/cloud.py), and are never sent back.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..ai import cloud
from ..container import container
from ..core import secrets
from ..core.permissions import REGISTRY, Risk
from ..core.util import now_iso
from .deps import current_user

router = APIRouter()

TONES = ("composed", "warm", "terse", "playful")
PRESETS = ("recommended", "careful", "everything")


class ProviderRequest(BaseModel):
    provider: str
    api_key: str = ""          # blank: use the key already saved
    base_url: str = ""


class TestRequest(ProviderRequest):
    model: str = ""


class SaveRequest(BaseModel):
    owner_name: str = ""
    assistant_name: str = "JARVIS"
    tone: str = "composed"
    provider: str = ""          # a cloud provider id, or "ollama" for local only
    api_key: str = ""           # blank: keep the saved key
    model: str = ""
    fast_model: str = ""
    base_url: str = ""
    weather_location: str = ""
    capabilities: str = "recommended"


def _key_for(provider: str, given: str) -> str:
    keys = container.get("orchestrator").keys
    return (given or "").strip() or keys.get(provider)


async def _ollama() -> Dict[str, Any]:
    provider = container.get("orchestrator").providers.get("ollama")
    if provider is None:
        return {"available": False}
    try:
        alive = await asyncio.wait_for(provider.available(), timeout=2.5)
    except Exception:
        alive = False
    return {"available": bool(alive)}


async def _state() -> Dict[str, Any]:
    store = container.get("settings_store")
    orchestrator = container.get("orchestrator")
    settings = container.get("settings")
    personality = await store.get("personality", {}) or {}
    setup = await store.get("setup", {}) or {}
    return {
        "complete": bool(setup.get("complete")),
        "completed_at": setup.get("completed_at"),
        "owner_name": personality.get("address_user_as") or "",
        "assistant_name": personality.get("name") or "JARVIS",
        "tone": personality.get("tone") or "composed",
        "weather_location": await store.get("weather_location", "") or "",
        "providers": cloud.catalogue(),
        "keys": orchestrator.keys.describe(),
        "cloud": orchestrator.cloud_config(),
        "ollama": await _ollama(),
        "local_only": bool(settings.local_only),
        # Whether a pasted key can be stored here at all (sealed), or must
        # go in .env instead.
        "can_store_keys": not secrets.seal("probe").startswith(secrets.PLAIN),
        "tones": list(TONES),
        "presets": list(PRESETS),
    }


@router.get("/setup", tags=["setup"])
async def setup_state(user: Dict[str, Any] = Depends(current_user)):
    return await _state()


@router.post("/setup/models", tags=["setup"])
async def setup_models(payload: ProviderRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        models = await cloud.list_models(payload.provider, _key_for(payload.provider, payload.api_key),
                                         payload.base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"models": models}


@router.post("/setup/test", tags=["setup"])
async def setup_test(payload: TestRequest, user: Dict[str, Any] = Depends(current_user)):
    if payload.provider not in cloud.PROVIDERS:
        raise HTTPException(400, f"Unknown AI provider '{payload.provider}'.")
    return await cloud.try_key(payload.provider, _key_for(payload.provider, payload.api_key),
                               payload.model, payload.base_url)


def _preset(name: str) -> Dict[str, bool]:
    if name == "everything":
        return {p.key: True for p in REGISTRY}
    if name == "careful":
        return {p.key: bool(p.default and p.risk == Risk.LOW) for p in REGISTRY}
    return {p.key: bool(p.default) for p in REGISTRY}


@router.post("/setup/save", tags=["setup"])
async def setup_save(payload: SaveRequest, user: Dict[str, Any] = Depends(current_user)):
    store = container.get("settings_store")
    orchestrator = container.get("orchestrator")
    audit = container.get("audit")

    provider = (payload.provider or "").strip()
    if provider and provider != "ollama" and provider not in cloud.PROVIDERS:
        raise HTTPException(400, f"Unknown AI provider '{provider}'.")
    if payload.tone not in TONES:
        raise HTTPException(400, f"Tone must be one of {', '.join(TONES)}.")
    if payload.capabilities not in PRESETS:
        raise HTTPException(400, f"Capabilities must be one of {', '.join(PRESETS)}.")

    if provider and provider != "ollama":
        if payload.api_key.strip():
            try:
                orchestrator.keys.save(provider, payload.api_key)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            await audit.record("ai.key_saved", subject=provider, detail={"provider": provider})
        spec = cloud.PROVIDERS[provider]
        if not orchestrator.keys.get(provider) and not spec.get("key_optional"):
            raise HTTPException(400, f"Paste your {spec['label']} API key.")
        if not payload.model.strip():
            raise HTTPException(400, "Choose a model.")
        if spec["base_url"] is None and spec["kind"] == "openai":
            try:
                cloud.base_url_for(provider, payload.base_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc))

    await store.patch("personality", {
        "name": (payload.assistant_name or "JARVIS").strip()[:30] or "JARVIS",
        "address_user_as": (payload.owner_name or "").strip()[:60],
        "tone": payload.tone,
    })
    await store.patch("ai", {"cloud": {
        "provider": provider,
        "model": payload.model.strip(),
        "fast_model": payload.fast_model.strip(),
        "base_url": payload.base_url.strip(),
    }})
    await store.set("weather_location", (payload.weather_location or "").strip()[:80])

    permissions = container.get("permissions")
    for key, enabled in _preset(payload.capabilities).items():
        await permissions.set(key, enabled, actor="setup")

    await store.set("setup", {"complete": True, "completed_at": now_iso()})
    await audit.record("setup.completed", subject=provider or "none",
                       detail={"provider": provider, "model": payload.model,
                               "capabilities": payload.capabilities})
    return await _state()


@router.delete("/setup/keys/{provider}", tags=["setup"])
async def setup_forget_key(provider: str, user: Dict[str, Any] = Depends(current_user)):
    if provider not in cloud.PROVIDERS:
        raise HTTPException(404, "Unknown provider.")
    removed = container.get("orchestrator").keys.remove(provider)
    if removed:
        await container.get("audit").record("ai.key_removed", subject=provider,
                                            detail={"provider": provider})
    return {"removed": removed, "keys": container.get("orchestrator").keys.describe()}
