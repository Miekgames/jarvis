"""The Checklists panel: pick a list, tick items off, add, remove, reset."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _service():
    service = container.get("checklists")
    if service is None:
        raise HTTPException(status_code=503, detail="Checklists aren't available.")
    return service


async def _or_404(work):
    try:
        return await work
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class NewChecklist(BaseModel):
    title: str
    items: List[str] = []


class Rename(BaseModel):
    title: str


class NewItems(BaseModel):
    items: List[str]


class ItemChange(BaseModel):
    done: Optional[bool] = None
    text: Optional[str] = None


class Reset(BaseModel):
    mode: str = "uncheck"            # or "clear_checked"


@router.get("/checklists", tags=["checklists"])
async def checklists(_user=Depends(current_user)):
    return {"checklists": await _service().all()}


@router.post("/checklists", tags=["checklists"])
async def make(body: NewChecklist, _user=Depends(current_user)):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="A checklist needs a name.")
    return await _or_404(_service().create(body.title, body.items))


@router.get("/checklists/{checklist_id}", tags=["checklists"])
async def one(checklist_id: str, _user=Depends(current_user)):
    return await _or_404(_service().get(checklist_id))


@router.patch("/checklists/{checklist_id}", tags=["checklists"])
async def rename(checklist_id: str, body: Rename, _user=Depends(current_user)):
    return await _or_404(_service().rename(checklist_id, body.title))


@router.delete("/checklists/{checklist_id}", tags=["checklists"])
async def delete(checklist_id: str, _user=Depends(current_user)):
    return await _or_404(_service().delete(checklist_id))


@router.post("/checklists/{checklist_id}/items", tags=["checklists"])
async def add(checklist_id: str, body: NewItems, _user=Depends(current_user)):
    return await _or_404(_service().add(checklist_id, body.items))


@router.patch("/checklists/{checklist_id}/items/{item_id}", tags=["checklists"])
async def change_item(checklist_id: str, item_id: str, body: ItemChange, _user=Depends(current_user)):
    service = _service()
    if body.text is not None:
        await _or_404(service.edit_item(checklist_id, item_id, body.text))
    if body.done is not None:
        return await _or_404(service.set_done(checklist_id, item_id, body.done))
    return await _or_404(service.get(checklist_id))


@router.delete("/checklists/{checklist_id}/items/{item_id}", tags=["checklists"])
async def remove_item(checklist_id: str, item_id: str, _user=Depends(current_user)):
    return await _or_404(_service().remove_item(checklist_id, item_id))


@router.post("/checklists/{checklist_id}/reset", tags=["checklists"])
async def reset(checklist_id: str, body: Reset, _user=Depends(current_user)):
    service = _service()
    if body.mode == "clear_checked":
        return await _or_404(service.clear_checked(checklist_id))
    return await _or_404(service.uncheck_all(checklist_id))
