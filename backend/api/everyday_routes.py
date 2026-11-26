"""Everyday lookups over HTTP: the same answers the chat tools give."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..container import container
from ..services import quickfacts as qf
from .deps import current_user

router = APIRouter()


def _service() -> qf.QuickFacts:
    service = container.get("quickfacts")
    if service is None:
        raise HTTPException(status_code=503, detail="Everyday lookups are not available.")
    return service


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _web() -> qf.QuickFacts:
    """The lookups that go online follow the same switch as the chat tools."""
    permissions = container.get("permissions")
    if permissions is not None and not permissions.allowed("web.fetch"):
        raise HTTPException(status_code=403, detail="“Open web pages” is switched off. Turn it on in "
                                                    "Settings → Permissions to use this lookup.")
    return _service()


@router.get("/everyday/convert")
async def convert(amount: float, from_unit: str = Query(..., alias="from"),
                  to_unit: str = Query(..., alias="to"),
                  _user=Depends(current_user)) -> Dict[str, Any]:
    try:
        return qf.convert_units(amount, from_unit, to_unit)
    except qf.QuickFactError as exc:
        raise _bad(exc)


@router.get("/everyday/currency")
async def currency(amount: float, from_currency: str = Query(..., alias="from"),
                   to_currency: str = Query(..., alias="to"),
                   _user=Depends(current_user)) -> Dict[str, Any]:
    try:
        return await _web().convert_currency(amount, from_currency, to_currency)
    except qf.QuickFactError as exc:
        raise _bad(exc)


@router.get("/everyday/time")
async def time_in(place: str, _user=Depends(current_user)) -> Dict[str, Any]:
    try:
        return qf.world_time(place)
    except qf.QuickFactError as exc:
        raise _bad(exc)


@router.get("/everyday/wikipedia")
async def wikipedia(topic: str, _user=Depends(current_user)) -> Dict[str, Any]:
    try:
        return await _web().wikipedia(topic)
    except qf.QuickFactError as exc:
        raise _bad(exc)


@router.get("/everyday/define")
async def define(word: str, _user=Depends(current_user)) -> Dict[str, Any]:
    try:
        return await _web().define(word)
    except qf.QuickFactError as exc:
        raise _bad(exc)


@router.get("/everyday/random")
async def random_pick(kind: str = "coin", dice: str = "", choices: Optional[str] = None,
                      low: Optional[int] = None, high: Optional[int] = None,
                      _user=Depends(current_user)) -> Dict[str, Any]:
    options: List[str] = [c for c in (choices or "").split("|") if c.strip()]
    try:
        return qf.roll(kind, dice=dice, choices=options, low=low, high=high)
    except (qf.QuickFactError, ValueError) as exc:
        raise _bad(exc)
