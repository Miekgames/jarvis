"""Auth, status, activity, audit and websocket routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel

from ..container import container
from ..core.events import bus
from ..core.security import AuthError
from ..core.ws import hub
from .deps import SESSION_COOKIE, client_ip, current_user, service

router = APIRouter()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    password: str = ""


@router.post("/auth/login", tags=["auth"])
async def login(payload: LoginRequest, request: Request, response: Response):
    auth = container.get("auth")
    try:
        token = await auth.login(
            payload.password, ip=client_ip(request), client=request.headers.get("user-agent", "")
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=container.settings.SESSION_HOURS * 3600,
    )
    return {"token": token, "mode": container.settings.AUTH_MODE}


@router.post("/auth/logout", tags=["auth"])
async def logout(request: Request, response: Response, user: Dict[str, Any] = Depends(current_user)):
    await container.get("auth").logout(user["token"], ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.post("/auth/logout-all", tags=["auth"])
async def logout_all(request: Request, user: Dict[str, Any] = Depends(current_user)):
    count = await container.get("auth").logout_all(user["id"], ip=client_ip(request))
    return {"revoked": count}


@router.get("/auth/me", tags=["auth"])
async def me(user: Dict[str, Any] = Depends(current_user)):
    return {
        "user": {k: v for k, v in user.items() if k != "token"},
        "config": container.settings.public_settings(),
    }


@router.get("/auth/sessions", tags=["auth"])
async def sessions(user: Dict[str, Any] = Depends(current_user)):
    records = await container.get("auth").sessions(user["id"])
    return {"sessions": [
        {k: v for k, v in dict(s).items() if k != "token_hash"} for s in records
    ]}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@router.get("/status", tags=["system"])
async def status(user: Dict[str, Any] = Depends(current_user)):
    return await container.status()


@router.get("/health", tags=["system"])
async def health_public():
    """Unauthenticated liveness probe -- deliberately reveals nothing beyond
    whether the process is up."""
    return {"ok": container.started, "service": "jarvis"}


@router.get("/health/full", tags=["system"])
async def health_full(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("health").full_check()


@router.get("/boot", tags=["system"])
async def boot_sequence():
    return {"steps": container.boot_log, "online": container.started}


# ---------------------------------------------------------------------------
# activity & audit
# ---------------------------------------------------------------------------
@router.get("/activity", tags=["system"])
async def activity(
    limit: int = Query(100, le=1000),
    level: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    entries = await container.get("activity").recent(limit, level, source, search)
    return {"activity": entries}


@router.delete("/activity", tags=["system"])
async def clear_activity(user: Dict[str, Any] = Depends(current_user)):
    removed = await container.get("activity").purge()
    await container.get("audit").record("data.deleted", subject="activity_log", detail={"rows": removed})
    return {"removed": removed}


@router.get("/audit", tags=["security"])
async def audit_log(
    limit: int = Query(200, le=2000),
    event: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    return {"audit": await container.get("audit").recent(limit, event)}


@router.get("/events/recent", tags=["system"])
async def recent_events(limit: int = Query(100, le=500), user: Dict[str, Any] = Depends(current_user)):
    return {"events": bus.recent(limit)}


# ---------------------------------------------------------------------------
# confirmations
# ---------------------------------------------------------------------------
class ConfirmRequest(BaseModel):
    approved: bool


@router.get("/confirmations", tags=["security"])
async def pending_confirmations(user: Dict[str, Any] = Depends(current_user)):
    return {"pending": await container.get("confirmations").pending()}


@router.get("/confirmations/history", tags=["security"])
async def confirmation_history(limit: int = 50, user: Dict[str, Any] = Depends(current_user)):
    return {"history": await container.get("confirmations").history(limit)}


@router.post("/confirmations/{confirmation_id}", tags=["security"])
async def resolve_confirmation(
    confirmation_id: str, payload: ConfirmRequest, user: Dict[str, Any] = Depends(current_user)
):
    try:
        record = await container.get("confirmations").resolve(confirmation_id, payload.approved)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown confirmation.")
    return record


# ---------------------------------------------------------------------------
# presence / sleep
# ---------------------------------------------------------------------------
class PresenceRequest(BaseModel):
    reason: str = "user"


@router.get("/presence", tags=["presence"])
async def presence_status(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("presence").status()


@router.post("/presence/sleep", tags=["presence"])
async def sleep(payload: PresenceRequest, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("presence").sleep(payload.reason)


@router.post("/presence/wake", tags=["presence"])
async def wake(payload: PresenceRequest, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("presence").wake(payload.reason)


@router.post("/presence/toggle", tags=["presence"])
async def toggle(payload: PresenceRequest, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("presence").toggle(payload.reason)


# ---------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time event stream.

    Auth follows the same rules as HTTP: in local mode a loopback connection is
    accepted, otherwise the session is read from the cookie the browser sends
    with the handshake, falling back to ?token=... for non-browser clients.

    The cookie is the important half, and it was missing. Reading only the
    query parameter worked everywhere it was tested -- on this machine, local
    mode accepts loopback before the token is even looked at. The moment the
    same page is opened from a phone, with a password, the handshake carries
    no `token`, this closes with 4401, and the interface loads fully and then
    sits at OFFLINE forever: no streaming replies, no task updates, nothing
    live. A browser cannot attach the query parameter itself, because the
    session cookie is httpOnly and deliberately unreadable from script.

    The query parameter stays for clients that have a token and no cookie jar,
    but it is the fallback: a token in a URL ends up in server logs, proxy
    logs and browser history, and a session token is a password equivalent.
    """
    auth = container.get("auth")
    if auth is None:
        await websocket.close(code=1011)
        return

    token = websocket.cookies.get(SESSION_COOKIE) or websocket.query_params.get("token")
    ip = websocket.client.host if websocket.client else ""
    user = await auth.resolve(token, ip=ip, client="websocket")
    if user is None:
        await websocket.close(code=4401)
        return

    await hub.serve(websocket, user_id=user["id"])
