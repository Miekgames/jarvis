"""Shared FastAPI dependencies: service access and authentication."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from ..container import container

SESSION_COOKIE = "jarvis_session"


def get_container():
    if not container.started:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JARVIS is still starting up.",
        )
    return container


def service(name: str):
    """Dependency factory: `db = Depends(service("db"))`."""

    def _resolve():
        value = container.get(name)
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The '{name}' service is not available.",
            )
        return value

    return _resolve


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return ""


async def current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_jarvis_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Resolve the session. In local auth mode a loopback request without a
    token is issued one transparently and it comes back on the response."""
    auth = container.get("auth")
    if auth is None:
        raise HTTPException(status_code=503, detail="Authentication service unavailable.")

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    token = token or x_jarvis_token or request.cookies.get(SESSION_COOKIE)

    user = await auth.resolve(
        token,
        ip=client_ip(request),
        client=request.headers.get("user-agent", "")[:200],
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Picked up by the middleware in app.py to set the cookie when auto-issued.
    request.state.issued_token = user["token"] if user.get("auto") else None
    return user


async def optional_user(request: Request) -> Optional[Dict[str, Any]]:
    try:
        return await current_user(request)
    except HTTPException:
        return None


AuthUser = Depends(current_user)
