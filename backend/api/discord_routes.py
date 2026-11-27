"""The Discord panel: the bot's token, on/off, pairing, and who is paired.

The token arrives once, in the save request, and is sealed before it touches
the disk. Nothing here ever returns it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _bridge():
    bridge = container.get("discord")
    if bridge is None:
        raise HTTPException(status_code=503, detail="The Discord service is not available.")
    return bridge


class Token(BaseModel):
    token: str


class Switch(BaseModel):
    on: bool


@router.get("/discord", tags=["discord"])
async def discord_status(_user=Depends(current_user)):
    return await _bridge().status()


@router.post("/discord/token", tags=["discord"])
async def discord_token(body: Token, _user=Depends(current_user)):
    try:
        return await _bridge().set_token(body.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/discord/token", tags=["discord"])
async def discord_forget(_user=Depends(current_user)):
    return await _bridge().forget_token()


@router.post("/discord/enabled", tags=["discord"])
async def discord_enabled(body: Switch, _user=Depends(current_user)):
    return await _bridge().set_enabled(body.on)


@router.post("/discord/servers", tags=["discord"])
async def discord_servers(body: Switch, _user=Depends(current_user)):
    return await _bridge().set_servers(body.on)


@router.post("/discord/notify", tags=["discord"])
async def discord_notify(body: Switch, _user=Depends(current_user)):
    return await _bridge().set_notify(body.on)


@router.post("/discord/pair", tags=["discord"])
async def discord_pair(_user=Depends(current_user)):
    bridge = _bridge()
    status = await bridge.status()
    if not status["connected"]:
        raise HTTPException(status_code=409, detail="The bot isn't connected yet: save its token first.")
    from ..integrations.discord_bridge import PAIR_MINUTES

    return {"code": bridge.new_pairing_code(), "minutes": PAIR_MINUTES, "bot": status["bot"]}


@router.delete("/discord/owners", tags=["discord"])
async def discord_unpair_all(_user=Depends(current_user)):
    return await _bridge().unpair("")


@router.delete("/discord/owners/{owner_id}", tags=["discord"])
async def discord_unpair(owner_id: str, _user=Depends(current_user)):
    return await _bridge().unpair(owner_id)
