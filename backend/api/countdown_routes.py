"""Countdowns: the ones on screen, finding a date, setting one by hand, news."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from ..services.countdowns import LIMIT, CountdownError
from .deps import current_user

router = APIRouter()


def _service():
    service = container.get("countdowns")
    if service is None:
        raise HTTPException(status_code=503, detail="Countdowns are not available.")
    return service


def _bad(exc: CountdownError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class Find(BaseModel):
    event: str


class NewCountdown(BaseModel):
    title: str
    day: str
    clock: str = ""
    topic: Optional[str] = None
    # The date came from "Find the date". Checked again here, so a source
    # is only ever recorded for a date that source gives.
    looked_up: bool = False


class Change(BaseModel):
    title: Optional[str] = None
    day: Optional[str] = None
    clock: Optional[str] = None
    topic: Optional[str] = None


@router.get("/countdowns", tags=["countdowns"])
async def countdowns(user: Dict[str, Any] = Depends(current_user)):
    return {"items": await _service().list(), "limit": LIMIT}


@router.post("/countdowns/find", tags=["countdowns"])
async def find(payload: Find, user: Dict[str, Any] = Depends(current_user)):
    """When the web says an event is. Adds nothing."""
    try:
        found = await _service().look_up(payload.event)
    except CountdownError as exc:
        raise _bad(exc)
    return {"status": found.status, "day": found.day.isoformat() if found.day else None,
            "sources": found.sources, "source_name": found.source_name,
            "source_url": found.source_url,
            "others": [{"day": day.isoformat(), "sources": n} for day, n in found.others]}


@router.post("/countdowns", tags=["countdowns"])
async def add(payload: NewCountdown, user: Dict[str, Any] = Depends(current_user)):
    try:
        found = None
        if payload.looked_up:
            found = await _service().look_up(payload.title)
            if found.status != "sure" or found.day is None or found.day.isoformat() != payload.day:
                found = None
        return await _service().add(payload.title, payload.day, payload.clock, payload.topic,
                                    origin="web" if found else "owner", found=found)
    except CountdownError as exc:
        raise _bad(exc)


@router.patch("/countdowns/{countdown_id}", tags=["countdowns"])
async def change(countdown_id: str, payload: Change, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _service().update(countdown_id, **payload.dict(exclude_unset=True))
    except CountdownError as exc:
        raise _bad(exc)


@router.delete("/countdowns/{countdown_id}", tags=["countdowns"])
async def remove(countdown_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        item = await _service().remove(countdown_id)
    except CountdownError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"removed": item["title"]}


@router.post("/countdowns/{countdown_id}/recheck", tags=["countdowns"])
async def recheck(countdown_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _service().recheck(countdown_id)
    except CountdownError as exc:
        raise _bad(exc)


@router.get("/countdowns/{countdown_id}/news", tags=["countdowns"])
async def news(countdown_id: str, refresh: bool = Query(False),
               user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _service().news(countdown_id, refresh=refresh)
    except CountdownError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
