"""Intelligence Layer routes: awareness, suggestions, vision, goals, planning,
learning, gaming and devices."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


# ===========================================================================
# awareness
# ===========================================================================
@router.get("/awareness", tags=["intelligence"])
async def awareness(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("awareness")
    watchers = container.get("watchers")
    return {
        "current": service.snapshot() if service else {},
        "stats": await service.stats() if service else {},
        "watchers": await watchers.stats() if watchers else {},
    }


@router.get("/awareness/usage", tags=["intelligence"])
async def usage(hours: int = Query(24, le=24 * 30), user: Dict[str, Any] = Depends(current_user)):
    service = container.get("awareness")
    if service is None:
        raise HTTPException(status_code=503, detail="Awareness service unavailable.")
    return {
        "hours": hours,
        "apps": await service.usage(hours=hours),
        "by_category": await service.by_category(hours=hours),
        "timeline": await service.timeline(hours=min(hours, 24)),
    }


@router.get("/awareness/performance", tags=["intelligence"])
async def performance(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("awareness")
    return {
        "advice": await service.recommendations(),
        "heaviest": await service.resource_hogs(limit=8),
    }


class WatchFolderRequest(BaseModel):
    path: str


@router.post("/awareness/watch", tags=["intelligence"])
async def watch_folder(payload: WatchFolderRequest, user: Dict[str, Any] = Depends(current_user)):
    watchers = container.get("watchers")
    try:
        return await watchers.watch_folder(payload.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/awareness/watch", tags=["intelligence"])
async def unwatch_folder(path: str, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("watchers").unwatch_folder(path)


# ===========================================================================
# suggestions
# ===========================================================================
@router.get("/suggestions", tags=["intelligence"])
async def suggestions(user: Dict[str, Any] = Depends(current_user)):
    engine = container.get("proactive")
    return {"pending": await engine.pending(), "stats": await engine.stats()}


@router.get("/suggestions/history", tags=["intelligence"])
async def suggestion_history(limit: int = 50, user: Dict[str, Any] = Depends(current_user)):
    return {"history": await container.get("proactive").history(limit)}


@router.post("/suggestions/generate", tags=["intelligence"])
async def generate_suggestions(user: Dict[str, Any] = Depends(current_user)):
    return {"offered": await container.get("proactive").sweep()}


class SuggestionResponse(BaseModel):
    accepted: bool


@router.post("/suggestions/{suggestion_id}", tags=["intelligence"])
async def resolve_suggestion(
    suggestion_id: str, payload: SuggestionResponse,
    user: Dict[str, Any] = Depends(current_user),
):
    try:
        return await container.get("proactive").resolve(suggestion_id, payload.accepted)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown suggestion.")


# ===========================================================================
# vision
# ===========================================================================
class LookRequest(BaseModel):
    task: str = "describe"
    question: str = ""
    image_path: Optional[str] = None


@router.get("/vision/status", tags=["intelligence"])
async def vision_status(user: Dict[str, Any] = Depends(current_user)):
    vision = container.get("vision")
    settings_store = container.get("settings_store")
    awareness_settings = await settings_store.get("awareness", {})
    return {
        **await vision.available(),
        "enabled": awareness_settings.get("vision", True),
        "permission_granted": container.get("permissions").allowed("system.screenshot"),
    }


@router.post("/vision/look", tags=["intelligence"])
async def look(payload: LookRequest, user: Dict[str, Any] = Depends(current_user)):
    """Direct capture-and-analyse. The AI tool path adds a confirmation prompt;
    this endpoint is the user pressing the button themselves."""
    permissions = container.get("permissions")
    if not permissions.allowed("system.screenshot"):
        raise HTTPException(
            status_code=403,
            detail="The 'Take screenshots' permission is off. Enable it in Settings -> Permissions.",
        )
    result = await container.get("vision").look(
        task=payload.task, question=payload.question, image_path=payload.image_path
    )
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("message", "Vision failed."))
    return result


@router.get("/vision/observations", tags=["intelligence"])
async def observations(limit: int = 30, user: Dict[str, Any] = Depends(current_user)):
    return {"observations": await container.get("vision").observations(limit)}


@router.delete("/vision/observations", tags=["intelligence"])
async def purge_observations(
    keep_days: int = Query(0, ge=0), user: Dict[str, Any] = Depends(current_user)
):
    result = await container.get("vision").purge(keep_days=keep_days)
    await container.get("audit").record("data.deleted", subject="observations", detail=result)
    return result


# ===========================================================================
# goals
# ===========================================================================
class GoalRequest(BaseModel):
    objective: str
    auto_start: bool = True


@router.get("/goals", tags=["intelligence"])
async def goals(active_only: bool = False, user: Dict[str, Any] = Depends(current_user)):
    service = container.get("goals")
    await service.refresh_all()
    return {"goals": await service.list(active_only=active_only)}


@router.post("/goals", tags=["intelligence"])
async def create_goal(payload: GoalRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("goals").create(payload.objective, payload.auto_start)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Planning failed: {exc}")


@router.post("/goals/plan", tags=["intelligence"])
async def plan_goal(payload: GoalRequest, user: Dict[str, Any] = Depends(current_user)):
    """Decompose without executing."""
    try:
        return await container.get("goals").plan(payload.objective)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Planning failed: {exc}")


@router.get("/goals/{goal_id}", tags=["intelligence"])
async def goal_progress(goal_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("goals").progress(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such goal.")


@router.post("/goals/{goal_id}/cancel", tags=["intelligence"])
async def cancel_goal(goal_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("goals").cancel(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such goal.")


# ===========================================================================
# planning
# ===========================================================================
@router.get("/planning/next", tags=["intelligence"])
async def next_action(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("planning").next_action()


@router.get("/planning/day", tags=["intelligence"])
async def daily_plan(offset: int = Query(0, ge=-1, le=14),
                     user: Dict[str, Any] = Depends(current_user)):
    return await container.get("planning").daily_plan(day_offset=offset)


@router.get("/planning/week", tags=["intelligence"])
async def weekly_plan(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("planning").weekly_plan()


@router.get("/planning/free", tags=["intelligence"])
async def free_time(offset: int = 0, minimum: int = 25,
                    user: Dict[str, Any] = Depends(current_user)):
    planning = container.get("planning")
    return {
        "blocks": await planning.free_blocks(offset, minimum),
        "conflicts": await planning.conflicts(days=max(1, offset + 1)),
    }


@router.get("/planning/conflicts", tags=["intelligence"])
async def conflicts(days: int = 7, user: Dict[str, Any] = Depends(current_user)):
    return {"conflicts": await container.get("planning").conflicts(days)}


@router.get("/planning/deadlines", tags=["intelligence"])
async def deadlines(user: Dict[str, Any] = Depends(current_user)):
    return {"projects": await container.get("planning").deadline_risk()}


@router.get("/planning/summary", tags=["intelligence"])
async def summary(scope: str = Query("day", pattern="^(day|week)$"),
                  user: Dict[str, Any] = Depends(current_user)):
    planning = container.get("planning")
    return await (planning.weekly_report() if scope == "week" else planning.end_of_day())


class FocusRequest(BaseModel):
    minutes: int = 50
    intent: str = ""


@router.post("/planning/focus", tags=["intelligence"])
async def focus(payload: FocusRequest, user: Dict[str, Any] = Depends(current_user)):
    result = await container.get("planning").start_focus_session(
        max(5, min(payload.minutes, 180)), payload.intent)
    try:
        await container.get("modes").activate("focus")
    except Exception:
        pass
    return result


# ===========================================================================
# learning & transparency
# ===========================================================================
@router.get("/patterns", tags=["intelligence"])
async def patterns(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("patterns")
    return {
        "stats": await service.stats(),
        "frequent": await service.frequent(limit=20),
        "due_now": await service.due_now(),
        "vocabulary": await service.vocabulary(),
    }


@router.delete("/patterns/{pattern_id}", tags=["intelligence"])
async def forget_pattern(pattern_id: int, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("patterns").forget(pattern_id)}


@router.delete("/patterns", tags=["intelligence"])
async def clear_patterns(user: Dict[str, Any] = Depends(current_user)):
    removed = await container.get("patterns").clear()
    await container.get("audit").record("data.deleted", subject="command_patterns",
                                        detail={"rows": removed})
    return {"removed": removed}


class VocabularyRequest(BaseModel):
    phrase: str
    means: str


@router.post("/patterns/vocabulary", tags=["intelligence"])
async def learn_phrase(payload: VocabularyRequest,
                       user: Dict[str, Any] = Depends(current_user)):
    return await container.get("patterns").learn_phrase(payload.phrase, payload.means, "user")


@router.get("/decisions", tags=["intelligence"])
async def decisions(limit: int = 20, subject: Optional[str] = None,
                    user: Dict[str, Any] = Depends(current_user)):
    return {"decisions": await container.get("reasoning").decisions(limit, subject)}


@router.get("/decisions/explain", tags=["intelligence"])
async def explain(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("reasoning").explain_recent()


# ===========================================================================
# gaming
# ===========================================================================
@router.get("/gaming", tags=["intelligence"])
async def gaming(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("gaming")
    return {
        "stats": await service.stats(),
        "current": await service.current(),
        "library": (await service.playtime())["library"],
        "recent": await service.sessions(limit=10),
        "note": "Frame rate is not measured by this build; GPU load and temperature are.",
    }


# ===========================================================================
# devices
# ===========================================================================
@router.get("/devices", tags=["devices"])
async def devices(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("devices")
    return {"devices": await service.list(), "stats": await service.stats()}


class PairRequest(BaseModel):
    name: str = "New device"
    kind: str = "mobile"
    permissions: Optional[List[str]] = None


@router.post("/devices/pair", tags=["devices"])
async def begin_pairing(payload: PairRequest, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("devices").begin_pairing(
        payload.name, payload.kind, payload.permissions)


class ClaimRequest(BaseModel):
    code: str
    device_name: str = ""


@router.post("/devices/claim", tags=["devices"])
async def complete_pairing(payload: ClaimRequest, request: Request):
    """Called by the device being paired. Unauthenticated by necessity -- the
    six-digit code is the credential, and it expires in five minutes."""
    service = container.get("devices")
    if service is None:
        raise HTTPException(status_code=503, detail="Device service unavailable.")
    address = request.client.host if request.client else ""
    try:
        return await service.complete_pairing(payload.code, payload.device_name, address)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.delete("/devices/{device_id}", tags=["devices"])
async def revoke_device(device_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("devices").revoke(device_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such device.")
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class DevicePermissionsRequest(BaseModel):
    permissions: List[str]


@router.put("/devices/{device_id}/permissions", tags=["devices"])
async def set_device_permissions(
    device_id: str, payload: DevicePermissionsRequest,
    user: Dict[str, Any] = Depends(current_user),
):
    return await container.get("devices").set_permissions(device_id, payload.permissions)


class HandoffRequest(BaseModel):
    to_device: str
    payload: Dict[str, Any]


@router.post("/devices/handoff", tags=["devices"])
async def handoff(payload: HandoffRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("devices").handoff(payload.to_device, payload.payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/devices/{device_id}/inbox", tags=["devices"])
async def claim_handoffs(device_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"items": await container.get("devices").claim(device_id)}
