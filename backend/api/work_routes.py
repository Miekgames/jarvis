"""Tasks, schedules, automations and system-monitoring routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


# ===========================================================================
# tasks
# ===========================================================================
class CreateTaskRequest(BaseModel):
    name: str
    type: str
    params: Dict[str, Any] = {}
    description: str = ""
    priority: str = "normal"
    project_id: Optional[str] = None
    depends_on: Optional[List[str]] = None


@router.get("/tasks", tags=["tasks"])
async def list_tasks(
    status: Optional[str] = None,
    active_only: bool = False,
    limit: int = Query(100, le=500),
    project_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    engine = container.get("tasks")
    return {
        "tasks": await engine.list(status, limit, active_only, project_id),
        "stats": await engine.stats(),
    }


@router.post("/tasks", tags=["tasks"])
async def create_task(payload: CreateTaskRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("tasks").create(
            name=payload.name, task_type=payload.type, params=payload.params,
            description=payload.description, priority=payload.priority,
            project_id=payload.project_id, depends_on=payload.depends_on,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/tasks/types", tags=["tasks"])
async def task_types(user: Dict[str, Any] = Depends(current_user)):
    from ..tasks.handlers import HANDLERS

    return {"types": [
        {"type": key, "label": spec["label"], "description": spec["description"],
         "resumable": spec["resumable"]}
        for key, spec in sorted(HANDLERS.items())
    ]}


@router.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(task_id: str, user: Dict[str, Any] = Depends(current_user)):
    engine = container.get("tasks")
    record = await engine.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="No such task.")
    return {"task": record, "logs": await engine.logs(task_id)}


@router.post("/tasks/{task_id}/{action}", tags=["tasks"])
async def control_task(
    task_id: str,
    action: str,
    priority: str = Query("normal"),
    user: Dict[str, Any] = Depends(current_user),
):
    engine = container.get("tasks")
    handlers = {
        "pause": lambda: engine.pause(task_id),
        "resume": lambda: engine.resume(task_id),
        "cancel": lambda: engine.cancel(task_id),
        "retry": lambda: engine.retry(task_id),
        "prioritise": lambda: engine.prioritise(task_id, priority),
        "prioritize": lambda: engine.prioritise(task_id, priority),
    }
    handler = handlers.get(action)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")
    try:
        return await handler()
    except KeyError:
        raise HTTPException(status_code=404, detail="No such task.")
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tasks/control/stop-all", tags=["tasks"])
async def stop_all(user: Dict[str, Any] = Depends(current_user)):
    result = await container.get("tasks").stop_all()
    await container.get("audit").record("tasks.stop_all", detail=result)
    return result


# ===========================================================================
# schedules
# ===========================================================================
class ScheduleRequest(BaseModel):
    name: str
    kind: str = "once"
    action: Dict[str, Any]
    cron: str = ""
    interval_seconds: int = 0
    run_at: Optional[str] = None
    enabled: bool = True


@router.get("/schedules", tags=["schedules"])
async def list_schedules(user: Dict[str, Any] = Depends(current_user)):
    scheduler = container.get("scheduler")
    return {"schedules": await scheduler.list(), "stats": await scheduler.stats()}


@router.post("/schedules", tags=["schedules"])
async def create_schedule(payload: ScheduleRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("scheduler").create(
            name=payload.name, action=payload.action, kind=payload.kind, cron=payload.cron,
            interval_seconds=payload.interval_seconds, run_at=payload.run_at, enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/schedules/{schedule_id}", tags=["schedules"])
async def update_schedule(
    schedule_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    return await container.get("scheduler").update(schedule_id, **payload)


@router.delete("/schedules/{schedule_id}", tags=["schedules"])
async def delete_schedule(schedule_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("scheduler").delete(schedule_id)}


@router.post("/schedules/{schedule_id}/run", tags=["schedules"])
async def run_schedule(schedule_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("scheduler").run_now(schedule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such schedule.")


@router.get("/schedules/validate/cron", tags=["schedules"])
async def validate_cron(expression: str, user: Dict[str, Any] = Depends(current_user)):
    from ..scheduler.cron import CronExpression, validate

    error = validate(expression)
    if error:
        return {"valid": False, "error": error}
    following = CronExpression(expression).next_after()
    return {"valid": True, "next_run": following.isoformat() if following else None}


# ===========================================================================
# automations
# ===========================================================================
class AutomationRequest(BaseModel):
    name: str
    trigger: Dict[str, Any]
    actions: List[Dict[str, Any]]
    conditions: List[Dict[str, Any]] = []
    description: str = ""
    enabled: bool = True
    cooldown: int = 0


@router.get("/automations", tags=["automations"])
async def list_automations(user: Dict[str, Any] = Depends(current_user)):
    engine = container.get("automations")
    records = await engine.list()
    return {"automations": [{**a, "preview": engine.preview(a)} for a in records]}


@router.get("/automations/schema", tags=["automations"])
async def automation_schema(user: Dict[str, Any] = Depends(current_user)):
    from ..automations.actions import ACTION_KINDS
    from ..automations.engine import CONDITION_KINDS, TRIGGER_KINDS

    return {
        "triggers": TRIGGER_KINDS,
        "conditions": CONDITION_KINDS,
        "actions": ACTION_KINDS,
        "task_types": container.get("tasks").handler_types(),
        "tools": container.get("registry").names(),
    }


@router.post("/automations", tags=["automations"])
async def create_automation(payload: AutomationRequest, user: Dict[str, Any] = Depends(current_user)):
    engine = container.get("automations")
    try:
        record = await engine.create(
            name=payload.name, trigger=payload.trigger, actions=payload.actions,
            conditions=payload.conditions, description=payload.description,
            enabled=payload.enabled, cooldown=payload.cooldown,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**record, "preview": engine.preview(record)}


@router.post("/automations/preview", tags=["automations"])
async def preview_automation(payload: AutomationRequest, user: Dict[str, Any] = Depends(current_user)):
    """Render an automation in plain English before it's created."""
    return container.get("automations").preview(payload.dict())


@router.patch("/automations/{automation_id}", tags=["automations"])
async def update_automation(
    automation_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    return await container.get("automations").update(automation_id, **payload)


@router.delete("/automations/{automation_id}", tags=["automations"])
async def delete_automation(automation_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("automations").delete(automation_id)}


@router.post("/automations/{automation_id}/run", tags=["automations"])
async def run_automation(automation_id: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("automations").run_manually(automation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such automation.")


@router.get("/automations/{automation_id}/runs", tags=["automations"])
async def automation_runs(automation_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"runs": await container.get("automations").runs(automation_id)}


# ===========================================================================
# system monitoring
# ===========================================================================
@router.get("/system/metrics", tags=["system"])
async def metrics(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("metrics")
    return {"latest": service.latest(), "recent": service.recent(90)}


@router.get("/system/metrics/history", tags=["system"])
async def metrics_history(
    hours: int = Query(6, le=48), user: Dict[str, Any] = Depends(current_user)
):
    return {"history": await container.get("metrics").history(hours)}


@router.get("/system/info", tags=["system"])
async def system_info(user: Dict[str, Any] = Depends(current_user)):
    return container.get("metrics").static_info()


@router.get("/system/processes", tags=["system"])
async def processes(
    sort_by: str = Query("cpu"), limit: int = Query(25, le=100),
    user: Dict[str, Any] = Depends(current_user),
):
    import asyncio

    from ..systemmon.apps import list_processes

    items = await asyncio.get_event_loop().run_in_executor(None, list_processes, limit, sort_by)
    return {"processes": items}


@router.get("/system/observability", tags=["system"])
async def observability(user: Dict[str, Any] = Depends(current_user)):
    """Developer diagnostics: latency, queue depth, worker state, DB size."""
    db = container.get("db")
    orchestrator = container.get("orchestrator")
    tasks = container.get("tasks")

    usage = await orchestrator.usage_summary(24)
    task_stats = await tasks.stats()
    counts = await db.table_counts()
    failures = int(await db.fetch_value(
        "SELECT COUNT(*) FROM tasks WHERE status = 'failed'", default=0))

    return {
        "ai": usage,
        "tasks": task_stats,
        "task_failures": failures,
        "database": {
            "path": str(container.settings.DB_PATH),
            "size_bytes": container.settings.DB_PATH.stat().st_size
            if container.settings.DB_PATH.exists() else 0,
            "tables": counts,
            "integrity": await db.integrity_check(),
        },
        "realtime": container.get("hub").stats(),
        "integrations": await container.get("integrations").health(),
        "events": {"recent_count": len(container.get("hub").stats()["connections"])},
    }
