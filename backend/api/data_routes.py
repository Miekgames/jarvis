"""Memory, notifications, calendar, contacts, projects, files and search."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from ..core.util import new_id, now_iso, truncate
from ..db.database import dumps, loads
from ..services.files import PathNotAllowed
from .deps import current_user

router = APIRouter()


# ===========================================================================
# memory
# ===========================================================================
class MemoryRequest(BaseModel):
    value: str
    category: str = "general"
    key: str = ""
    importance: int = 3
    pinned: bool = False


@router.get("/memory", tags=["memory"])
async def list_memory(
    category: Optional[str] = None,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_user),
):
    store = container.get("memory")
    return {"memories": await store.list(category, 500, search), "stats": await store.stats()}


@router.post("/memory", tags=["memory"])
async def create_memory(payload: MemoryRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("memory").remember(
            payload.value, payload.category, payload.key, payload.importance,
            source="user", pinned=payload.pinned,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.patch("/memory/{memory_id}", tags=["memory"])
async def update_memory(
    memory_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    record = await container.get("memory").update(memory_id, **payload)
    if not record:
        raise HTTPException(status_code=404, detail="No such memory.")
    return record


@router.delete("/memory/{memory_id}", tags=["memory"])
async def delete_memory(memory_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("memory").forget(memory_id)}


@router.delete("/memory", tags=["memory"])
async def clear_memory(
    category: Optional[str] = None, user: Dict[str, Any] = Depends(current_user)
):
    removed = await container.get("memory").clear(category)
    await container.get("audit").record(
        "data.deleted", subject="memory", detail={"category": category, "rows": removed}
    )
    return {"removed": removed}


# ===========================================================================
# notifications
# ===========================================================================
@router.get("/notifications", tags=["notifications"])
async def list_notifications(
    include_dismissed: bool = False,
    limit: int = Query(100, le=500),
    user: Dict[str, Any] = Depends(current_user),
):
    service = container.get("notifications")
    return {
        "notifications": await service.list(include_dismissed, limit),
        "unread": await service.unread_count(),
    }


@router.get("/notifications/summary", tags=["notifications"])
async def notification_summary(user: Dict[str, Any] = Depends(current_user)):
    return await container.get("notifications").summarise(container.get("orchestrator"))


class NotificationRequest(BaseModel):
    title: str
    body: str = ""
    priority: str = "normal"


@router.post("/notifications", tags=["notifications"])
async def create_notification(
    payload: NotificationRequest, user: Dict[str, Any] = Depends(current_user)
):
    return await container.get("notifications").create(
        payload.title, payload.body, payload.priority, source="user"
    )


@router.post("/notifications/{notification_id}/{action}", tags=["notifications"])
async def notification_action(
    notification_id: str,
    action: str,
    minutes: int = Query(15),
    user: Dict[str, Any] = Depends(current_user),
):
    service = container.get("notifications")
    if action == "read":
        return await service.mark_read(notification_id)
    if action == "dismiss":
        return await service.dismiss(notification_id)
    if action == "snooze":
        return await service.snooze(notification_id, minutes)
    raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")


@router.post("/notifications/read-all", tags=["notifications"])
async def read_all(user: Dict[str, Any] = Depends(current_user)):
    return {"updated": await container.get("notifications").mark_all_read()}


@router.delete("/notifications", tags=["notifications"])
async def clear_notifications(user: Dict[str, Any] = Depends(current_user)):
    return {"removed": await container.get("notifications").clear()}


# ===========================================================================
# calendar
# ===========================================================================
class EventRequest(BaseModel):
    title: str
    start_at: str
    end_at: Optional[str] = None
    location: str = ""
    link: str = ""
    description: str = ""
    attendees: List[str] = []
    all_day: bool = False


@router.get("/calendar", tags=["calendar"])
async def calendar(
    range: str = Query("upcoming"),
    limit: int = Query(50, le=200),
    user: Dict[str, Any] = Depends(current_user),
):
    service = container.get("calendar")
    if range == "today":
        events = await service.today()
    elif range == "week":
        events = await service.upcoming(limit, hours=24 * 7)
    else:
        events = await service.upcoming(limit)
    return {"events": events, "stats": await service.stats()}


@router.post("/calendar", tags=["calendar"])
async def create_event(payload: EventRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("calendar").create(**payload.dict())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/calendar/{event_id}", tags=["calendar"])
async def update_event(
    event_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    return await container.get("calendar").update(event_id, **payload)


@router.delete("/calendar/{event_id}", tags=["calendar"])
async def delete_event(event_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"deleted": await container.get("calendar").delete(event_id)}


# ===========================================================================
# contacts
# ===========================================================================
class ContactRequest(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    organization: str = ""
    notes: str = ""
    tags: List[str] = []


@router.get("/contacts", tags=["contacts"])
async def contacts(search: Optional[str] = None, user: Dict[str, Any] = Depends(current_user)):
    sql = "SELECT * FROM contacts"
    params: List[Any] = []
    if search:
        sql += " WHERE name LIKE ? OR email LIKE ? OR organization LIKE ?"
        params.extend([f"%{search}%"] * 3)
    sql += " ORDER BY name LIMIT 500"
    rows = await container.get("db").fetch_all(sql, params)
    for row in rows:
        row["tags"] = loads(row.get("tags"), [])
    return {"contacts": rows}


@router.post("/contacts", tags=["contacts"])
async def create_contact(payload: ContactRequest, user: Dict[str, Any] = Depends(current_user)):
    contact_id = new_id("con")
    await container.get("db").execute(
        "INSERT INTO contacts (id, name, phone, email, organization, notes, tags, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (contact_id, payload.name, payload.phone, payload.email, payload.organization,
         payload.notes, dumps(payload.tags), now_iso(), now_iso()),
    )
    return {"id": contact_id, **payload.dict()}


@router.patch("/contacts/{contact_id}", tags=["contacts"])
async def update_contact(
    contact_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(current_user)
):
    allowed = {"name", "phone", "email", "organization", "notes", "tags"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    if "tags" in updates:
        updates["tags"] = dumps(updates["tags"])
    if not updates:
        return {"ok": True}
    assignments = ", ".join(f"{key} = ?" for key in updates)
    await container.get("db").execute(
        f"UPDATE contacts SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), now_iso(), contact_id),
    )
    return {"ok": True}


@router.delete("/contacts/{contact_id}", tags=["contacts"])
async def delete_contact(contact_id: str, user: Dict[str, Any] = Depends(current_user)):
    deleted = await container.get("db").execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    return {"deleted": bool(deleted)}


@router.get("/communications", tags=["contacts"])
async def communications(limit: int = Query(100, le=500), user: Dict[str, Any] = Depends(current_user)):
    rows = await container.get("db").fetch_all(
        "SELECT * FROM communications ORDER BY ts DESC LIMIT ?", (limit,)
    )
    for row in rows:
        row["meta"] = loads(row.get("meta"), {})
    return {"communications": rows}


# ===========================================================================
# projects
# ===========================================================================
class ProjectRequest(BaseModel):
    name: str
    description: str = ""
    deadline: Optional[str] = None
    color: str = "#4dd8ff"


@router.get("/projects", tags=["projects"])
async def projects(user: Dict[str, Any] = Depends(current_user)):
    db = container.get("db")
    rows = await db.fetch_all("SELECT * FROM projects ORDER BY updated_at DESC")
    for row in rows:
        row["task_count"] = int(await db.fetch_value(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (row["id"],), default=0))
        row["item_count"] = int(await db.fetch_value(
            "SELECT COUNT(*) FROM project_items WHERE project_id = ?", (row["id"],), default=0))
    return {"projects": rows}


@router.post("/projects", tags=["projects"])
async def create_project(payload: ProjectRequest, user: Dict[str, Any] = Depends(current_user)):
    project_id = new_id("prj")
    await container.get("db").execute(
        "INSERT INTO projects (id, name, description, status, deadline, color, created_at, updated_at)"
        " VALUES (?,?,?,'active',?,?,?,?)",
        (project_id, payload.name, payload.description, payload.deadline, payload.color,
         now_iso(), now_iso()),
    )
    return {"id": project_id, **payload.dict()}


@router.get("/projects/{project_id}", tags=["projects"])
async def project_detail(project_id: str, user: Dict[str, Any] = Depends(current_user)):
    db = container.get("db")
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(status_code=404, detail="No such project.")
    return {
        "project": project,
        "items": await db.fetch_all(
            "SELECT * FROM project_items WHERE project_id = ? ORDER BY created_at DESC", (project_id,)),
        "tasks": await container.get("tasks").list(project_id=project_id),
        "conversations": await db.fetch_all(
            "SELECT id, title, updated_at FROM conversations WHERE project_id = ?"
            " ORDER BY updated_at DESC LIMIT 20", (project_id,)),
        "events": await db.fetch_all(
            "SELECT * FROM calendar_events WHERE description LIKE ? ORDER BY start_at LIMIT 10",
            (f"%{project['name']}%",)),
    }


@router.delete("/projects/{project_id}", tags=["projects"])
async def delete_project(project_id: str, user: Dict[str, Any] = Depends(current_user)):
    deleted = await container.get("db").execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return {"deleted": bool(deleted)}


class ProjectItemRequest(BaseModel):
    kind: str = "note"
    title: str = ""
    body: str = ""
    ref_id: str = ""


@router.post("/projects/{project_id}/items", tags=["projects"])
async def add_project_item(
    project_id: str, payload: ProjectItemRequest, user: Dict[str, Any] = Depends(current_user)
):
    await container.get("db").execute(
        "INSERT INTO project_items (project_id, kind, ref_id, title, body, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (project_id, payload.kind, payload.ref_id, payload.title or truncate(payload.body, 60),
         payload.body, now_iso()),
    )
    return {"ok": True}


# ===========================================================================
# files
# ===========================================================================
@router.get("/files/roots", tags=["files"])
async def file_roots(user: Dict[str, Any] = Depends(current_user)):
    return {"roots": container.get("files").roots_info()}


@router.get("/files/list", tags=["files"])
async def list_files(path: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("files").list_dir(path)
    except (PathNotAllowed, NotADirectoryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/files/search", tags=["files"])
async def search_files(
    pattern: str,
    root: Optional[str] = None,
    content: bool = False,
    limit: int = Query(40, le=100),
    user: Dict[str, Any] = Depends(current_user),
):
    try:
        return {"matches": await container.get("files").search(pattern, root, limit, content)}
    except PathNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/files/read", tags=["files"])
async def read_file(path: str, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await container.get("files").read(path)
    except (PathNotAllowed, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/files/trash", tags=["files"])
async def trash(user: Dict[str, Any] = Depends(current_user)):
    return {"items": await container.get("files").trash_contents()}


@router.delete("/files/trash", tags=["files"])
async def empty_trash(user: Dict[str, Any] = Depends(current_user)):
    removed = await container.get("files").empty_trash()
    await container.get("audit").record("data.deleted", subject="trash", detail={"items": removed})
    return {"removed": removed}


# ===========================================================================
# universal search
# ===========================================================================
@router.get("/search", tags=["search"])
async def universal_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, le=25),
    user: Dict[str, Any] = Depends(current_user),
):
    """Search everything JARVIS knows about: conversations, tasks, memory,
    projects, calendar, contacts, commands and files."""
    db = container.get("db")
    like = f"%{q}%"
    results: Dict[str, List[Dict[str, Any]]] = {}

    results["conversations"] = await db.fetch_all(
        "SELECT DISTINCT c.id, c.title, c.updated_at FROM conversations c"
        " JOIN messages m ON m.conversation_id = c.id"
        " WHERE c.title LIKE ? OR m.text LIKE ? ORDER BY c.updated_at DESC LIMIT ?",
        (like, like, limit),
    )
    results["tasks"] = await db.fetch_all(
        "SELECT id, name, status, type FROM tasks WHERE name LIKE ? OR description LIKE ?"
        " ORDER BY created_at DESC LIMIT ?", (like, like, limit),
    )
    results["memory"] = await container.get("memory").list(search=q, limit=limit)
    results["projects"] = await db.fetch_all(
        "SELECT id, name, description FROM projects WHERE name LIKE ? OR description LIKE ? LIMIT ?",
        (like, like, limit),
    )
    results["calendar"] = await container.get("calendar").search(q, limit)
    results["contacts"] = await db.fetch_all(
        "SELECT id, name, email, organization FROM contacts WHERE name LIKE ? OR email LIKE ?"
        " OR organization LIKE ? LIMIT ?", (like, like, like, limit),
    )
    results["commands"] = await db.fetch_all(
        "SELECT id, text, ts FROM commands WHERE text LIKE ? ORDER BY id DESC LIMIT ?", (like, limit)
    )
    try:
        results["files"] = await container.get("files").search(q, limit=limit)
    except Exception:
        results["files"] = []

    return {
        "query": q,
        "results": results,
        "total": sum(len(v) for v in results.values()),
    }
