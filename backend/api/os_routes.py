"""Update 3 routes: structured goals, agents, deep research, scoped memory and
JARVIS Continuity."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from ..core.util import now_iso
from ..db.database import dumps, loads
from .deps import current_user

router = APIRouter()


# ===========================================================================
# goals with milestones
# ===========================================================================
class GoalRequest(BaseModel):
    objective: str
    deadline: Optional[str] = None
    project_id: Optional[str] = None
    start_now: bool = False
    answers: Optional[Dict[str, str]] = None


@router.get("/planner/goals", tags=["goals"])
async def goals(active_only: bool = False, user: Dict[str, Any] = Depends(current_user)):
    planner = container.get("goal_planner")
    await planner.refresh_all()
    return {"goals": await planner.list(active_only=active_only)}


@router.post("/planner/goals", tags=["goals"])
async def create_goal(payload: GoalRequest, user: Dict[str, Any] = Depends(current_user)):
    planner = container.get("goal_planner")
    try:
        return await planner.create(payload.objective, payload.deadline, payload.project_id,
                                    payload.start_now, payload.answers)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Planning failed: {exc}")


@router.post("/planner/goals/assess", tags=["goals"])
async def assess_goal(payload: GoalRequest, user: Dict[str, Any] = Depends(current_user)):
    """Complexity estimate and blocking questions, without creating anything."""
    try:
        return await container.get("goal_planner").assess(payload.objective, payload.deadline)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Assessment failed: {exc}")


@router.get("/planner/goals/{goal_id}", tags=["goals"])
async def goal_progress(goal_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("goal_planner").progress(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such goal.")


@router.post("/planner/goals/{goal_id}/{action}", tags=["goals"])
async def goal_action(
    goal_id: str, action: str, reason: str = Query(""),
    user: Dict[str, Any] = Depends(current_user),
):
    planner = container.get("goal_planner")
    try:
        if action == "start":
            return await planner.start(goal_id)
        if action == "adapt":
            return await planner.adapt(goal_id, reason)
        if action == "report":
            return await planner.report(goal_id)
        if action == "cancel":
            return await planner.cancel(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such goal.")
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")


class AnswersRequest(BaseModel):
    answers: Dict[str, str]


@router.post("/planner/goals/{goal_id}/answers", tags=["goals"])
async def answer_goal(goal_id: str, payload: AnswersRequest,
                      user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("goal_planner").answer(goal_id, payload.answers)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such goal.")


# ===========================================================================
# agents
# ===========================================================================
class AgentRequest(BaseModel):
    objective: str
    agent: Optional[str] = None
    time_limit_minutes: Optional[int] = None


@router.get("/agents", tags=["agents"])
async def agents(user: Dict[str, Any] = Depends(current_user)):
    orchestrator = container.get("agent_orchestrator")
    return {
        "catalogue": orchestrator.catalogue(),
        "stats": await orchestrator.stats(),
        "active": await orchestrator.active(),
        "recent": await orchestrator.history(limit=20),
    }


@router.post("/agents/dispatch", tags=["agents"])
async def dispatch_agent(payload: AgentRequest, user: Dict[str, Any] = Depends(current_user)):
    orchestrator = container.get("agent_orchestrator")
    return await orchestrator.dispatch(
        payload.objective, payload.agent,
        time_limit_s=payload.time_limit_minutes * 60 if payload.time_limit_minutes else None,
    )


@router.get("/agents/select", tags=["agents"])
async def select_agent(objective: str, user: Dict[str, Any] = Depends(current_user)):
    """Which agent would be chosen, and why -- without running anything."""
    return container.get("agent_orchestrator").select(objective)


@router.get("/agents/messages", tags=["agents"])
async def agent_messages(limit: int = 50, user: Dict[str, Any] = Depends(current_user)):
    return {"messages": await container.get("agent_orchestrator").messages(limit)}


@router.get("/agents/{run_id}", tags=["agents"])
async def agent_run(run_id: str, user: Dict[str, Any] = Depends(current_user)):
    record = await container.get("agent_orchestrator").get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail="No such agent run.")
    return record


@router.post("/agents/{run_id}/stop", tags=["agents"])
async def stop_agent(run_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("agent_orchestrator").stop(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such agent run.")


@router.post("/agents/stop-all", tags=["agents"])
async def stop_all_agents(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("agent_orchestrator").stop_all()


# ===========================================================================
# deep research
# ===========================================================================
class ResearchRequest(BaseModel):
    question: str
    depth: str = "standard"
    background: bool = True


@router.get("/research", tags=["research"])
async def research_projects(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("research")
    return {"projects": await service.list(limit=30), "stats": await service.stats()}


@router.post("/research", tags=["research"])
async def start_research(payload: ResearchRequest, user: Dict[str, Any] = Depends(current_user)):
    service = container.get("research")
    project = await service.create(payload.question, payload.depth)
    if payload.background:
        task = await container.get("tasks").create(
            name=f"Research: {payload.question[:60]}",
            task_type="deep_research",
            params={"project_id": project["id"]},
        )
        return {**project, "task_id": task["id"]}
    await service.gather(project["id"])
    return await service.synthesise(project["id"])


@router.get("/research/{project_id}", tags=["research"])
async def research_project(project_id: str, user: Dict[str, Any] = Depends(current_user)):
    service = container.get("research")
    project = await service.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="No such research project.")
    return {**project, "citations": service.citations(project["sources"])}


@router.post("/research/{project_id}/continue", tags=["research"])
async def continue_research(project_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("research").continue_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such research project.")


@router.delete("/research/{project_id}", tags=["research"])
async def delete_research(project_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("research").delete(project_id)}


# ===========================================================================
# memory 2.0
# ===========================================================================
@router.get("/memory2", tags=["memory"])
async def memory_browser(
    scope: Optional[str] = None,
    category: Optional[str] = None,
    project_id: Optional[str] = None,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    manager = container.get("memory2")
    return {
        "memories": await manager.list(scope, category, project_id, search),
        "stats": await manager.stats(),
    }


@router.get("/memory2/search", tags=["memory"])
async def memory_search(q: str, user: Dict[str, Any] = Depends(current_user)):
    """Natural-language search: ask in a sentence."""
    return await container.get("memory2").search_natural(q)


@router.get("/memory2/{memory_id}/explain", tags=["memory"])
async def memory_explain(memory_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("memory2").explain(memory_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such memory.")


@router.post("/memory2/{memory_id}/promote", tags=["memory"])
async def memory_promote(memory_id: str, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("memory2").promote(memory_id)


@router.get("/memory2/cleanup/report", tags=["memory"])
async def memory_cleanup_report(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("memory2").cleanup_report()


@router.post("/memory2/cleanup", tags=["memory"])
async def memory_cleanup(
    include_unused: bool = Query(False), user: Dict[str, Any] = Depends(current_user)
):
    result = await container.get("memory2").cleanup(
        expired=True, duplicates=True, unused=include_unused)
    await container.get("audit").record("data.deleted", subject="memory", detail=result)
    return result


# ===========================================================================
# command resolution
# ===========================================================================
@router.get("/resolve", tags=["intelligence"])
async def resolve(command: str, user: Dict[str, Any] = Depends(current_user)):
    """What does 'this' refer to? Returns candidates and the evidence."""
    return await container.get("resolver").resolve(command)


# ===========================================================================
# JARVIS Continuity
# ===========================================================================
class ContinuityRequest(BaseModel):
    device_id: str
    kind: str = "session"
    state: Dict[str, Any] = {}


@router.get("/continuity", tags=["devices"])
async def continuity(user: Dict[str, Any] = Depends(current_user)):
    """Everything a device needs to pick up exactly where another left off.

    Tasks, goals, agents and notifications are shared state already -- they live
    in one database. What continuity adds is the *session*: which conversation
    was open, which panel, what was being worked on."""
    db = container.get("db")
    rows = await db.fetch_all("SELECT * FROM continuity ORDER BY updated_at DESC")
    devices = await container.get("devices").list()
    tasks = container.get("tasks")
    planner = container.get("goal_planner")

    return {
        "devices": devices,
        "active_device": next((d["name"] for d in devices if d.get("online")), None),
        "sessions": [{**dict(r), "state": loads(r["state"], {})} for r in rows],
        "shared": {
            "active_tasks": await tasks.list(active_only=True, limit=20),
            "active_goals": await planner.list(active_only=True, limit=10),
            "agents": (await container.get("agent_orchestrator").stats())["active"],
            "unread_notifications": await container.get("notifications").unread_count(),
            "settings": await container.get("settings_store").all(),
        },
    }


@router.put("/continuity", tags=["devices"])
async def save_continuity(payload: ContinuityRequest,
                          user: Dict[str, Any] = Depends(current_user)):
    """A device checkpointing its session so another can resume it."""
    db = container.get("db")
    record_id = f"{payload.device_id}:{payload.kind}"
    await db.execute(
        "INSERT INTO continuity (id, device_id, kind, state, updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET state = excluded.state,"
        " updated_at = excluded.updated_at",
        (record_id, payload.device_id, payload.kind, dumps(payload.state), now_iso()),
    )
    await container.get("devices").heartbeat(payload.device_id)
    return {"saved": record_id, "at": now_iso()}


@router.get("/continuity/{device_id}", tags=["devices"])
async def resume(device_id: str, kind: str = "session",
                 user: Dict[str, Any] = Depends(current_user)):
    """Resume the most recent session from *any other* device."""
    db = container.get("db")
    row = await db.fetch_one(
        "SELECT * FROM continuity WHERE kind = ? AND device_id != ?"
        " ORDER BY updated_at DESC LIMIT 1", (kind, device_id),
    )
    if not row:
        return {"resumable": False,
                "message": "No other device has checkpointed a session to resume."}
    return {"resumable": True, "from_device": row["device_id"],
            "updated_at": row["updated_at"], "state": loads(row["state"], {})}


# ===========================================================================
# self-editing
# ===========================================================================
class ProposeRequest(BaseModel):
    path: str
    request: str


class ApplyRequest(BaseModel):
    proposal_id: str
    allow_protected: bool = False


def _selfedit():
    service = container.get("selfedit")
    if service is None:
        raise HTTPException(status_code=503, detail="Self-edit service unavailable.")
    if not container.get("permissions").allowed("code.self_modify"):
        raise HTTPException(
            status_code=403,
            detail="The 'Edit its own code' permission is off. Enable it in "
                   "Settings -> Permissions if you want JARVIS to modify its own source.",
        )
    return service


@router.get("/selfedit", tags=["selfedit"])
async def selfedit_status(user: Dict[str, Any] = Depends(current_user)):
    """Status is readable without the permission, so you can see it is off."""
    service = container.get("selfedit")
    if service is None:
        raise HTTPException(status_code=503, detail="Self-edit service unavailable.")
    return {
        **service.stats(),
        "permission_granted": container.get("permissions").allowed("code.self_modify"),
        "pending": service.pending(),
    }


@router.get("/selfedit/files", tags=["selfedit"])
async def selfedit_files(pattern: str = "", user: Dict[str, Any] = Depends(current_user)):
    return {"files": _selfedit().list_files(pattern)}


@router.get("/selfedit/read", tags=["selfedit"])
async def selfedit_read(path: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return _selfedit().read(path)
    except (PermissionError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/selfedit/propose", tags=["selfedit"])
async def selfedit_propose(payload: ProposeRequest,
                           user: Dict[str, Any] = Depends(current_user)):
    service = _selfedit()          # outside the try: its 403 must reach the client intact
    try:
        return await service.propose(payload.path, payload.request)
    except (PermissionError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The code model failed: {exc}")


@router.post("/selfedit/apply", tags=["selfedit"])
async def selfedit_apply(payload: ApplyRequest,
                         user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _selfedit().apply(payload.proposal_id, payload.allow_protected)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/selfedit/backups", tags=["selfedit"])
async def selfedit_backups(path: str = "", user: Dict[str, Any] = Depends(current_user)):
    return {"backups": _selfedit().backups(path or None)}


@router.post("/selfedit/rollback", tags=["selfedit"])
async def selfedit_rollback(backup: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _selfedit().rollback(backup)
    except (PermissionError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
