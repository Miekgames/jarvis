"""The wallpaper workshop.

Generating packs, and sending their output to the owner's own devices.
Every route is local: nothing here publishes, uploads to a storefront or
posts anywhere, because no such code exists -- which is what makes the whole
of it safe to drive from a phone over the tailnet.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..container import container
from ..core.events import bus
from .deps import current_user

router = APIRouter()


def _service():
    service = container.get("business")
    if service is None:
        raise HTTPException(status_code=503,
                            detail="The product workshop is not available.")
    return service




# ---------------------------------------------------------------------------
@router.get("/business/catalogue", tags=["business"])
async def catalogue(user: Dict[str, Any] = Depends(current_user)):
    return _service().catalogue()


@router.get("/business/packs", tags=["business"])
async def packs(user: Dict[str, Any] = Depends(current_user)):
    service = _service()
    return {"packs": service.products(), "building": service.progress}


class PlanRequest(BaseModel):
    palette: str
    count: int = 8
    name: Optional[str] = None
    styles: Optional[List[str]] = None
    resolutions: Optional[List[str]] = None


@router.post("/business/estimate", tags=["business"])
async def estimate(payload: PlanRequest,
                   user: Dict[str, Any] = Depends(current_user)):
    service = _service()
    try:
        plan = service.plan(payload.palette, count=payload.count,
                            styles=payload.styles,
                            resolutions=payload.resolutions, name=payload.name)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"plan": _plan_dict(plan), "estimate": service.estimate(plan)}


class BuildRequest(PlanRequest):
    overwrite: bool = False


@router.post("/business/build", tags=["business"])
async def build(payload: BuildRequest,
                user: Dict[str, Any] = Depends(current_user)):
    """Start a build and return at once.

    A pack is minutes of rendering. Holding the request open for that long
    means a phone that locks its screen gets a dead connection and no way to
    tell whether the work is still happening, so this starts the job and
    reports progress over the websocket instead.
    """
    service = _service()
    if service.progress:
        raise HTTPException(
            status_code=409,
            detail=f"Already building '{service.progress.get('slug')}'. "
                   "One at a time.")
    try:
        plan = service.plan(payload.palette, count=payload.count,
                            styles=payload.styles,
                            resolutions=payload.resolutions, name=payload.name)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    room = service.estimate(plan)
    if not room.get("fits", True):
        raise HTTPException(status_code=507,
                            detail=room.get("warning", "Not enough disk space."))

    def publish(state: Dict[str, Any]) -> None:
        try:
            bus.publish("business.progress", state, source="business")
        except Exception:
            pass

    async def run() -> None:
        try:
            summary = await service.build(plan, overwrite=payload.overwrite,
                                          on_progress=publish)
            bus.publish("business.built", summary, source="business")
        except Exception as exc:
            bus.publish("business.failed",
                        {"slug": plan.slug, "error": str(exc)[:300]},
                        source="business")

    asyncio.create_task(run(), name=f"build-{plan.slug}")
    return {"started": True, "plan": _plan_dict(plan), "estimate": room}




@router.get("/business/packs/{slug}/preview", tags=["business"])
async def preview(slug: str, user: Dict[str, Any] = Depends(current_user)):
    """Serve a pack's preview image.

    Confined to the workshop directory and to that pack. A slug arrives from
    the client, and joining client-supplied text onto a path is how directory
    traversal happens -- so the result is resolved and checked to be inside
    the pack rather than trusted.
    """
    service = _service()
    product = service.find(slug)
    if product is None or not product.get("preview"):
        raise HTTPException(status_code=404, detail="No preview for that pack.")

    path = Path(product["preview"]).resolve()
    root = Path(service.root).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Outside the workshop.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="The preview is missing.")
    return FileResponse(path, media_type="image/png")





@router.get("/business/devices", tags=["business"])
async def devices(user: Dict[str, Any] = Depends(current_user)):
    """Devices on the tailnet that files can be sent to."""
    service = container.get("taildrop")
    if service is None:
        return {"available": False, "devices": [], "detail": "Not available."}
    ready = service.availability()
    if not ready.get("available"):
        return {**ready, "devices": []}
    found = [d.as_dict() for d in await service.devices()]
    return {**ready, "devices": found}


@router.post("/business/send-wallpapers", tags=["business"])
async def send_wallpapers(payload: SendRequest,
                          user: Dict[str, Any] = Depends(current_user)):
    """Send a pack's phone-sized wallpapers to one of the owner's devices."""
    business = container.get("business")
    taildrop = container.get("taildrop")
    if business is None or taildrop is None:
        raise HTTPException(status_code=503, detail="Sending is not available.")

    product = business.find(payload.pack)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No pack called {payload.pack!r}.")

    mobile = sorted((Path(product["directory"]) / "wallpapers" / "mobile").glob("*.png"))
    if not mobile:
        raise HTTPException(status_code=400,
                            detail="That pack has no phone-sized wallpapers.")

    chosen = mobile[: max(1, min(int(payload.count), 12))]
    return await taildrop.send_many(chosen, payload.device)



def _plan_dict(plan) -> Dict[str, Any]:
    return {
        "name": plan.name, "slug": plan.slug, "palette": plan.palette,
        "styles": list(plan.styles), "count": plan.count,
        "resolutions": list(plan.resolutions), "seed": plan.seed,
        "price_usd": plan.price_usd,
    }
