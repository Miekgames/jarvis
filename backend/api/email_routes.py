"""The Email panel: connect a mailbox, read it, send from it.

The app password arrives once, in the connect request, and is sealed before
it touches the disk. Nothing here ever returns it. Sending from this route is
the owner's own click in the compose form -- the same audit trail as JARVIS
sending after a confirmation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _account():
    account = container.get("email")
    if account is None:
        raise HTTPException(status_code=503, detail="The email service is not available.")
    return account


async def _refresh_integration() -> None:
    integrations = container.get("integrations")
    if integrations is not None:
        try:
            await integrations.refresh("email")
        except Exception:
            pass


class Connect(BaseModel):
    address: str
    password: str
    provider: str = ""
    imap_host: str = ""
    imap_port: Optional[int] = None
    imap_security: str = ""
    smtp_host: str = ""
    smtp_port: Optional[int] = None
    smtp_security: str = ""


class Outgoing(BaseModel):
    to: str
    subject: str = ""
    body: str
    cc: str = ""
    reply_to: str = ""


def _bad(exc: Exception) -> HTTPException:
    from ..integrations.email_account import EmailError

    if isinstance(exc, EmailError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=f"{exc.__class__.__name__}: {exc}")


@router.get("/email", tags=["email"])
async def status(user: Dict[str, Any] = Depends(current_user)):
    return _account().describe()


@router.get("/email/unread", tags=["email"])
async def unread(user: Dict[str, Any] = Depends(current_user)):
    account = _account()
    if not account.connected:
        return {"connected": False, "unread": 0}
    try:
        return {"connected": True, "unread": await account.unread_count()}
    except Exception as exc:
        raise _bad(exc)


@router.post("/email/connect", tags=["email"])
async def connect(payload: Connect, user: Dict[str, Any] = Depends(current_user)):
    try:
        described = await _account().connect(**payload.dict())
    except Exception as exc:
        raise _bad(exc)
    await _refresh_integration()
    # Connecting a mailbox is the owner's yes to JARVIS reading it, so the
    # chat tools can too. Sending stays a separate switch in Settings.
    permissions = container.get("permissions")
    if not permissions.allowed("email.read"):
        await permissions.set("email.read", True, actor="email connect")
    return {**described, "chat_can_send": permissions.allowed("email.send")}


@router.delete("/email", tags=["email"])
async def disconnect(user: Dict[str, Any] = Depends(current_user)):
    removed = await _account().disconnect()
    await _refresh_integration()
    return {"disconnected": removed}


@router.get("/email/messages", tags=["email"])
async def messages(unread: bool = False, limit: int = Query(20, ge=1, le=50),
                   user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _account().inbox(unread_only=unread, limit=limit)
    except Exception as exc:
        raise _bad(exc)


@router.get("/email/search", tags=["email"])
async def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=50),
                 user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _account().search(q, limit=limit)
    except Exception as exc:
        raise _bad(exc)


@router.get("/email/messages/{message_id}", tags=["email"])
async def read(message_id: str, user: Dict[str, Any] = Depends(current_user)):
    from ..integrations.email_account import split_id

    if split_id(message_id) is None:
        raise HTTPException(status_code=400, detail="That isn't a message id.")
    try:
        return await _account().read(message_id)
    except Exception as exc:
        raise _bad(exc)


@router.post("/email/send", tags=["email"])
async def send(payload: Outgoing, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _account().send(**payload.dict())
    except Exception as exc:
        raise _bad(exc)
