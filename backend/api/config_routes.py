"""Settings, permissions, integrations, modes, backup, export and deletion."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..container import container
from ..core.util import new_id, now_iso
from .deps import current_user

router = APIRouter()


# ===========================================================================
# settings
# ===========================================================================
@router.get("/settings", tags=["settings"])
async def get_settings(user: Dict[str, Any] = Depends(current_user)):
    return {
        "settings": await container.get("settings_store").all(),
        "config": container.settings.public_settings(),
    }


class SettingUpdate(BaseModel):
    value: Any


@router.put("/settings/{key}", tags=["settings"])
async def set_setting(
    key: str, payload: SettingUpdate, user: Dict[str, Any] = Depends(current_user)
):
    store = container.get("settings_store")
    value = await store.set(key, payload.value)
    await container.get("audit").record("settings.changed", subject=key, detail={"key": key})
    return {"key": key, "value": value}


@router.patch("/settings/{key}", tags=["settings"])
async def patch_setting(
    key: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    value = await container.get("settings_store").patch(key, payload)
    await container.get("audit").record("settings.changed", subject=key, detail={"key": key})
    return {"key": key, "value": value}


@router.post("/settings/reset", tags=["settings"])
async def reset_settings(
    key: Optional[str] = None, user: Dict[str, Any] = Depends(current_user)
):
    return await container.get("settings_store").reset(key)


# ===========================================================================
# themes
# ===========================================================================
@router.get("/themes", tags=["settings"])
async def themes(user: Dict[str, Any] = Depends(current_user)):
    from ..services import themes as theme_presets

    current = await container.get("settings_store").get("theme", {})
    return {
        "themes": theme_presets.listing(),
        "active": current.get("preset", theme_presets.DEFAULT_THEME),
    }


@router.post("/themes/{key}/apply", tags=["settings"])
async def apply_theme(key: str, user: Dict[str, Any] = Depends(current_user)):
    """Apply a preset's palette while leaving the user's own display
    preferences (density, reduced motion, font scale) untouched."""
    from ..services import themes as theme_presets

    if key not in theme_presets.THEMES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown theme '{key}'. Available: {', '.join(theme_presets.THEMES)}.",
        )
    store = container.get("settings_store")
    updated = await store.patch("theme", {"preset": key, **theme_presets.palette(key)})
    await container.get("activity").record(
        f"Theme changed to {theme_presets.get(key)['name']}.", source="settings"
    )
    return {"key": key, "theme": updated}


# ===========================================================================
# permissions
# ===========================================================================
@router.get("/permissions", tags=["security"])
async def permissions(user: Dict[str, Any] = Depends(current_user)):
    return {"permissions": await container.get("permissions").list()}


class PermissionUpdate(BaseModel):
    enabled: bool


@router.put("/permissions/{key}", tags=["security"])
async def set_permission(
    key: str, payload: PermissionUpdate, user: Dict[str, Any] = Depends(current_user)
):
    try:
        return await container.get("permissions").set(key, payload.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/permissions", tags=["security"])
async def set_permissions(
    payload: Dict[str, bool], user: Dict[str, Any] = Depends(current_user)
):
    return {"updated": await container.get("permissions").set_many(payload)}


@router.post("/permissions/revoke-all", tags=["security"])
async def revoke_all(user: Dict[str, Any] = Depends(current_user)):
    count = await container.get("permissions").revoke_all()
    return {"revoked": count, "message": "Every capability is now disabled."}


# ===========================================================================
# integrations
# ===========================================================================
@router.get("/integrations", tags=["integrations"])
async def integrations(user: Dict[str, Any] = Depends(current_user)):
    registry = container.get("integrations")
    return {"integrations": await registry.list(), "health": await registry.health()}


@router.post("/integrations/{key}/refresh", tags=["integrations"])
async def refresh_integration(key: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("integrations").refresh(key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/integrations/refresh", tags=["integrations"])
async def refresh_all_integrations(user: Dict[str, Any] = Depends(current_user)):
    return {"status": await container.get("integrations").refresh_all()}


@router.post("/integrations/{key}/sync", tags=["integrations"])
async def sync_integration(key: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("integrations").sync(key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/integrations/{key}/disconnect", tags=["integrations"])
async def disconnect_integration(key: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("integrations").disconnect(key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ===========================================================================
# modes
# ===========================================================================
@router.get("/modes", tags=["modes"])
async def modes(user: Dict[str, Any] = Depends(current_user)):
    service = container.get("modes")
    return {"modes": await service.list(), "active": await service.active()}


@router.post("/modes/{key}/activate", tags=["modes"])
async def activate_mode(key: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("modes").activate(key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class ModeRequest(BaseModel):
    key: str
    name: str
    icon: str = ""
    config: Dict[str, Any] = {}


@router.post("/modes", tags=["modes"])
async def create_mode(payload: ModeRequest, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("modes").create(payload.key, payload.name, payload.config, payload.icon)


@router.delete("/modes/{key}", tags=["modes"])
async def delete_mode(key: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return {"deleted": await container.get("modes").delete(key)}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ===========================================================================
# backup / export / delete
# ===========================================================================
@router.get("/backups", tags=["data"])
async def backups(user: Dict[str, Any] = Depends(current_user)):
    rows = await container.get("db").fetch_all("SELECT * FROM backups ORDER BY ts DESC LIMIT 50")
    for row in rows:
        row["verified"] = bool(row["verified"])
        row["exists"] = Path(row["path"]).exists()
    return {"backups": rows}


@router.post("/backups", tags=["data"])
async def create_backup(user: Dict[str, Any] = Depends(current_user)):
    """Runs as a background task so a large database doesn't block the request."""
    record = await container.get("tasks").create(
        name="Manual backup", task_type="backup", params={"note": "manual"}, priority="high"
    )
    return {"task_id": record["id"], "message": "Backup started. Watch the task for progress."}


@router.get("/export", tags=["data"])
async def export_data(
    include: str = Query("all", description="Comma-separated: conversations,tasks,memory,settings,logs"),
    user: Dict[str, Any] = Depends(current_user),
):
    """Export local data as JSON. Nothing leaves the machine unless the user
    downloads the file themselves."""
    db = container.get("db")
    wanted = {p.strip() for p in include.split(",")} if include != "all" else {
        "conversations", "tasks", "memory", "settings", "logs", "projects", "contacts",
        "calendar", "automations", "schedules",
    }
    payload: Dict[str, Any] = {"exported_at": now_iso(), "version": "1.0.0"}

    if "conversations" in wanted:
        payload["conversations"] = await db.fetch_all("SELECT * FROM conversations")
        payload["messages"] = await db.fetch_all(
            "SELECT id, conversation_id, role, text, created_at, model FROM messages")
    if "tasks" in wanted:
        payload["tasks"] = await db.fetch_all("SELECT * FROM tasks")
    if "memory" in wanted:
        payload["memory"] = await db.fetch_all("SELECT * FROM memory")
    if "settings" in wanted:
        payload["settings"] = await container.get("settings_store").all()
        payload["permissions"] = await container.get("permissions").list()
    if "logs" in wanted:
        payload["activity"] = await db.fetch_all(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT 5000")
    if "projects" in wanted:
        payload["projects"] = await db.fetch_all("SELECT * FROM projects")
        payload["project_items"] = await db.fetch_all("SELECT * FROM project_items")
    if "contacts" in wanted:
        payload["contacts"] = await db.fetch_all("SELECT * FROM contacts")
    if "calendar" in wanted:
        payload["calendar_events"] = await db.fetch_all("SELECT * FROM calendar_events")
    if "automations" in wanted:
        payload["automations"] = await db.fetch_all("SELECT * FROM automations")
    if "schedules" in wanted:
        payload["schedules"] = await db.fetch_all("SELECT * FROM schedules")

    await container.get("audit").record("data.exported", detail={"include": sorted(wanted)})
    return JSONResponse(
        content=json.loads(json.dumps(payload, default=str)),
        headers={"Content-Disposition": f'attachment; filename="jarvis_export_{now_iso()[:10]}.json"'},
    )


DELETABLE = {
    "conversations": ["DELETE FROM messages", "DELETE FROM conversations"],
    "tasks": ["DELETE FROM task_logs", "DELETE FROM task_deps", "DELETE FROM tasks"],
    "memory": ["DELETE FROM memory"],
    "logs": ["DELETE FROM activity_log"],
    "metrics": ["DELETE FROM metrics"],
    "notifications": ["DELETE FROM notifications"],
    "commands": ["DELETE FROM commands"],
    "communications": ["DELETE FROM communications"],
    "audit": ["DELETE FROM audit_log"],
}


@router.get("/data/deletable", tags=["data"])
async def deletable(user: Dict[str, Any] = Depends(current_user)):
    db = container.get("db")
    counts = await db.table_counts()
    return {"categories": [
        {
            "key": key,
            "tables": [statement.split()[-1] for statement in statements],
            "rows": sum(counts.get(statement.split()[-1], 0) for statement in statements),
        }
        for key, statements in DELETABLE.items()
    ]}


@router.delete("/data/{category}", tags=["data"])
async def delete_data(category: str, user: Dict[str, Any] = Depends(current_user)):
    statements = DELETABLE.get(category)
    if statements is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category. Deletable: {', '.join(sorted(DELETABLE))}.",
        )
    db = container.get("db")
    removed = 0
    for statement in statements:
        removed += await db.execute(statement)
    await container.get("audit").record(
        "data.deleted", subject=category, detail={"rows": removed}, result="ok"
    )
    return {"category": category, "rows_deleted": removed}
