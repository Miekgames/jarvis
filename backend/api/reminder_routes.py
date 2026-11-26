"""Reminders: the ones scheduled, adding one in your own words, cancelling."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..container import container
from ..services.reminders import ReminderError
from .deps import current_user

router = APIRouter()


def _service():
    service = container.get("reminders")
    if service is None:
        raise HTTPException(status_code=503, detail="Reminders are not available.")
    return service


class NewReminder(BaseModel):
    message: str
    when: str


class Preview(BaseModel):
    when: str


@router.get("/reminders", tags=["reminders"])
async def reminders(user: Dict[str, Any] = Depends(current_user)):
    return {"items": await _service().list()}


@router.post("/reminders/preview", tags=["reminders"])
async def preview(payload: Preview, user: Dict[str, Any] = Depends(current_user)):
    """How a phrase would be read, before anything is scheduled."""
    try:
        found = _service().understand(payload.when)
    except ReminderError as exc:
        return {"ok": False, "error": str(exc)}
    first = found.run_at.isoformat() if found.run_at else None
    return {"ok": True, "says": found.says, "repeats": found.kind != "once", "first": first,
            "assumed": found.assumed}


@router.post("/reminders", tags=["reminders"])
async def add(payload: NewReminder, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _service().add(payload.message, payload.when)
    except ReminderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/reminders/{reminder_id}", tags=["reminders"])
async def cancel(reminder_id: str, user: Dict[str, Any] = Depends(current_user)):
    if not await _service().cancel(reminder_id):
        raise HTTPException(status_code=404, detail="That reminder is no longer there.")
    return {"cancelled": True}
