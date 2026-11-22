"""The document index: folders, progress, and a search to try it."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _index():
    index = container.get("documents")
    if index is None:
        raise HTTPException(status_code=503, detail="The document index is not available.")
    return index


class Folder(BaseModel):
    path: str


@router.get("/documents", tags=["documents"])
async def overview(user: Dict[str, Any] = Depends(current_user)):
    index = _index()
    return {"folders": await index.folders(), "totals": await index.stats()}


@router.post("/documents/folders", tags=["documents"])
async def add_folder(payload: Folder, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _index().add_folder(payload.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/documents/refresh", tags=["documents"])
async def refresh(user: Dict[str, Any] = Depends(current_user)):
    await _index().refresh_all()
    return {"refreshing": True}


@router.delete("/documents/folders", tags=["documents"])
async def remove_folder(path: str = Query(...), user: Dict[str, Any] = Depends(current_user)):
    if not await _index().remove_folder(path):
        raise HTTPException(status_code=404, detail="That folder is not in the index.")
    return {"removed": path}


@router.get("/documents/search", tags=["documents"])
async def search(q: str = Query(..., min_length=1), limit: int = Query(8, le=20),
                 user: Dict[str, Any] = Depends(current_user)):
    return {"results": await _index().search(q, limit=limit)}
