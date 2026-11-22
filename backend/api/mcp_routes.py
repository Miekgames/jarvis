"""MCP plug-ins: add, list, switch on and off, trust, remove."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _manager():
    manager = container.get("mcp")
    if manager is None:
        raise HTTPException(status_code=503, detail="Plug-ins are not available.")
    return manager


class NewServer(BaseModel):
    name: str
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""
    token: str = ""


class Paste(BaseModel):
    text: str


class Change(BaseModel):
    enabled: Optional[bool] = None
    trusted: Optional[bool] = None


@router.get("/mcp", tags=["plugins"])
async def list_servers(user: Dict[str, Any] = Depends(current_user)):
    return {"servers": _manager().status()}


@router.post("/mcp", tags=["plugins"])
async def add_server(payload: NewServer, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _manager().add(payload.name, payload.dict())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/mcp/import", tags=["plugins"])
async def import_servers(payload: Paste, user: Dict[str, Any] = Depends(current_user)):
    """Paste the JSON a server's README gives (Claude Desktop format works)."""
    try:
        return {"servers": await _manager().import_text(payload.text)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/mcp/{name}", tags=["plugins"])
async def change_server(name: str, payload: Change,
                        user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _manager().update(name, payload.enabled, payload.trusted)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No plug-in called '{name}'.")


@router.post("/mcp/{name}/reconnect", tags=["plugins"])
async def reconnect_server(name: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _manager().connect(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No plug-in called '{name}'.")


@router.delete("/mcp/{name}", tags=["plugins"])
async def remove_server(name: str, user: Dict[str, Any] = Depends(current_user)):
    if not await _manager().remove(name):
        raise HTTPException(status_code=404, detail=f"No plug-in called '{name}'.")
    return {"removed": name}
