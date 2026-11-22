"""
HTTP routes for the personal operating layer.

Notes, clipboard, snippets, focus, habits, wellbeing, files, network, toolkit,
reading, search and interface customisation.

Errors are translated once, here, so every endpoint reports a missing record as
404 and a bad argument as 400 rather than a 500 with a stack trace.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from ..services import toolkit
from .deps import container

logger = logging.getLogger("api.personal")
router = APIRouter()


def _service(name: str):
    service = container.get(name)
    if service is None:
        raise HTTPException(503, "The {} service is not available in this build.".format(name))
    return service


async def _call(coro):
    """Run a service call, mapping its exceptions onto HTTP status codes."""
    try:
        return await coro
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ======================================================================
# notes
# ======================================================================
@router.get("/notes", tags=["notes"])
async def list_notes(kind: str = "", tag: str = "", archived: bool = False,
                     limit: int = 100):
    notes = _service("notes")
    return {"notes": await notes.list(kind=kind, tag=tag, archived=archived, limit=limit),
            "stats": await notes.stats()}


@router.get("/notes/search", tags=["notes"])
async def search_notes(q: str = Query(""), limit: int = 40):
    return {"results": await _service("notes").search(q, limit=limit), "query": q}


@router.get("/notes/tags", tags=["notes"])
async def note_tags():
    return {"tags": await _service("notes").tags()}


@router.get("/notes/graph", tags=["notes"])
async def note_graph():
    return await _service("notes").graph()


@router.get("/notes/journal", tags=["notes"])
async def get_journal(day: str = ""):
    return await _call(_service("notes").journal(day))


@router.post("/notes/journal", tags=["notes"])
async def append_journal(payload: Dict[str, Any] = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to add.")
    return await _call(_service("notes").journal_append(text, payload.get("day", "")))


@router.get("/notes/{note_id}", tags=["notes"])
async def get_note(note_id: str):
    return await _call(_service("notes").get(note_id))


@router.get("/notes/{note_id}/links", tags=["notes"])
async def note_links(note_id: str):
    return await _call(_service("notes").links(note_id))


@router.get("/notes/{note_id}/tasks", tags=["notes"])
async def note_tasks(note_id: str):
    return await _call(_service("notes").extract_tasks(note_id))


@router.post("/notes/{note_id}/summarise", tags=["notes"])
async def summarise_note(note_id: str):
    return await _call(_service("notes").summarise(note_id))


@router.post("/notes", tags=["notes"])
async def create_note(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("notes").create(
        payload.get("title", ""), payload.get("body", ""),
        payload.get("tags"), payload.get("kind", "note")))


@router.patch("/notes/{note_id}", tags=["notes"])
async def update_note(note_id: str, payload: Dict[str, Any] = Body(...)):
    return await _call(_service("notes").update(note_id, **payload))


@router.delete("/notes/{note_id}", tags=["notes"])
async def delete_note(note_id: str):
    return await _call(_service("notes").delete(note_id))


# ======================================================================
# clipboard and snippets
# ======================================================================
@router.get("/clipboard", tags=["clipboard"])
async def clipboard_history(q: str = "", kind: str = "", limit: int = 50):
    clipboard = _service("clipboard")
    return {"clips": await clipboard.history(query=q, kind=kind, limit=limit),
            "stats": await clipboard.stats()}


@router.get("/clipboard/{clip_id}", tags=["clipboard"])
async def get_clip(clip_id: str):
    return await _call(_service("clipboard").get(clip_id))


@router.post("/clipboard/capture", tags=["clipboard"])
async def capture_clip():
    return await _service("clipboard").capture_once()


@router.post("/clipboard/{clip_id}/pin", tags=["clipboard"])
async def pin_clip(clip_id: str, payload: Dict[str, Any] = Body(default={})):
    return await _call(_service("clipboard").pin(clip_id, bool(payload.get("pinned", True))))


@router.delete("/clipboard/{clip_id}", tags=["clipboard"])
async def delete_clip(clip_id: str):
    return await _call(_service("clipboard").delete(clip_id))


@router.post("/clipboard/clear", tags=["clipboard"])
async def clear_clipboard(payload: Dict[str, Any] = Body(default={})):
    return await _service("clipboard").clear(bool(payload.get("include_pinned", False)))


@router.get("/snippets", tags=["clipboard"])
async def list_snippets(q: str = ""):
    return {"snippets": await _service("clipboard").snippets(q)}


@router.post("/snippets", tags=["clipboard"])
async def create_snippet(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("clipboard").add_snippet(
        payload.get("name", ""), payload.get("body", ""),
        payload.get("trigger", ""), payload.get("tags")))


@router.post("/snippets/{reference}/expand", tags=["clipboard"])
async def expand_snippet(reference: str):
    return await _call(_service("clipboard").expand(reference))


@router.delete("/snippets/{snippet_id}", tags=["clipboard"])
async def delete_snippet(snippet_id: str):
    return await _call(_service("clipboard").delete_snippet(snippet_id))


# ======================================================================
# focus, habits, wellbeing
# ======================================================================
@router.get("/focus", tags=["focus"])
async def focus_state():
    focus = _service("focus")
    return {
        "current": await focus.current_session(),
        "today": await focus.time_report(),
        "score": await focus.focus_score(),
        "streak": await focus.session_streak(),
    }


@router.post("/focus/start", tags=["focus"])
async def start_focus(payload: Dict[str, Any] = Body(default={})):
    return await _call(_service("focus").start_session(
        payload.get("goal", ""), int(payload.get("minutes", 25) or 25),
        payload.get("kind", "focus")))


@router.post("/focus/end", tags=["focus"])
async def end_focus(payload: Dict[str, Any] = Body(default={})):
    return await _service("focus").end_session(
        payload.get("status", "completed"), payload.get("notes", ""))


@router.post("/focus/interrupt", tags=["focus"])
async def interrupt_focus(payload: Dict[str, Any] = Body(default={})):
    return await _service("focus").interrupt(payload.get("app", ""))


@router.get("/focus/sessions", tags=["focus"])
async def focus_sessions(limit: int = 30):
    return {"sessions": await _service("focus").sessions(limit)}


@router.get("/focus/time", tags=["focus"])
async def time_report(day: str = ""):
    return await _service("focus").time_report(day)


@router.get("/focus/week", tags=["focus"])
async def week_review():
    return await _service("focus").week_review()


@router.get("/habits", tags=["focus"])
async def list_habits(include_archived: bool = False):
    return {"habits": await _service("focus").habits(include_archived)}


@router.post("/habits", tags=["focus"])
async def create_habit(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("focus").add_habit(
        payload.get("name", ""), payload.get("cadence", "daily"),
        float(payload.get("target", 1) or 1), payload.get("unit", ""),
        payload.get("colour", "#5ad2ff")))


@router.post("/habits/{reference}/check-in", tags=["focus"])
async def habit_check_in(reference: str, payload: Dict[str, Any] = Body(default={})):
    return await _call(_service("focus").check_in(
        reference, float(payload.get("value", 1) or 1),
        payload.get("day", ""), payload.get("note", "")))


@router.get("/habits/{reference}/history", tags=["focus"])
async def habit_history(reference: str, days: int = 30):
    return await _call(_service("focus").habit_history(reference, days))


@router.get("/habits/{reference}/correlate", tags=["focus"])
async def habit_correlate(reference: str, days: int = 21):
    return await _call(_service("focus").correlate(reference, days))


@router.delete("/habits/{reference}", tags=["focus"])
async def archive_habit(reference: str):
    return await _call(_service("focus").archive_habit(reference))


@router.get("/wellbeing", tags=["focus"])
async def get_wellbeing(kind: str = "", days: int = 14):
    return await _service("focus").wellbeing(kind, days)


@router.post("/wellbeing", tags=["focus"])
async def log_wellbeing(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("focus").log_wellbeing(
        payload.get("kind", ""), float(payload.get("value", 0) or 0),
        payload.get("note", ""), payload.get("day", "")))


# ======================================================================
# file operations
# ======================================================================
@router.get("/fileops/duplicates", tags=["files"])
async def find_duplicates(folder: str, min_bytes: int = 1024, limit: int = 100):
    return await _call(_service("fileops").duplicates(folder, min_bytes, limit))


@router.get("/fileops/large", tags=["files"])
async def find_large(folder: str, min_mb: float = 50, limit: int = 40):
    return await _call(_service("fileops").large_files(folder, limit, min_mb))


@router.get("/fileops/usage", tags=["files"])
async def disk_usage(folder: str):
    return await _call(_service("fileops").disk_usage(folder))


@router.get("/fileops/grep", tags=["files"])
async def grep_files(folder: str, pattern: str, glob: str = "",
                     regex: bool = False, limit: int = 100):
    return await _call(_service("fileops").find_content(folder, pattern, glob, limit, regex))


@router.get("/fileops/recent", tags=["files"])
async def recent_files(hours: int = 24, limit: int = 40):
    return await _service("fileops").recent_files(hours, limit)


@router.post("/fileops/rename/plan", tags=["files"])
async def plan_rename(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("fileops").plan_rename(
        payload.get("folder", ""), payload.get("find", ""), payload.get("replace", ""),
        payload.get("glob", "*"), bool(payload.get("regex", False))))


@router.post("/fileops/rename/apply", tags=["files"])
async def apply_rename(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("fileops").apply_rename(payload.get("plan_id", "")))


@router.post("/fileops/organise/plan", tags=["files"])
async def plan_organise(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("fileops").organise_plan(payload.get("folder", "")))


@router.post("/fileops/organise/apply", tags=["files"])
async def apply_organise(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("fileops").apply_organise(payload.get("plan_id", "")))


@router.get("/fileops/tags", tags=["files"])
async def list_tagged(tag: str = ""):
    return await _service("fileops").tagged(tag)


@router.post("/fileops/tags", tags=["files"])
async def tag_file(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("fileops").tag(payload.get("path", ""), payload.get("tags")))


@router.delete("/fileops/tags", tags=["files"])
async def untag_file(path: str, tag: str):
    return await _call(_service("fileops").untag(path, tag))


# ======================================================================
# network
# ======================================================================
@router.get("/net/ports", tags=["network"])
async def scan_ports(host: str = "127.0.0.1", start: int = 1, end: int = 10000):
    return await _call(_service("netdiag").scan_ports(host, start, end))


@router.get("/net/listening", tags=["network"])
async def listening():
    return await _service("netdiag").listening()


@router.get("/net/port/{port}", tags=["network"])
async def port_conflict(port: int):
    return await _service("netdiag").port_conflict(port)


@router.get("/net/dns", tags=["network"])
async def dns_lookup(hostname: str):
    return await _call(_service("netdiag").dns_lookup(hostname))


@router.get("/net/latency", tags=["network"])
async def latency(host: str = "1.1.1.1", port: int = 443, attempts: int = 4):
    return await _call(_service("netdiag").latency(host, port, attempts))


@router.get("/net/interfaces", tags=["network"])
async def interfaces():
    return await _service("netdiag").interfaces()


@router.get("/net/connections", tags=["network"])
async def connections(limit: int = 60):
    return await _service("netdiag").connections(limit)


@router.get("/net/wifi", tags=["network"])
async def wifi():
    return await _service("netdiag").wifi()


# ======================================================================
# toolkit -- pure functions, no service needed
# ======================================================================
@router.post("/toolkit/json", tags=["toolkit"])
async def format_json(payload: Dict[str, Any] = Body(...)):
    return toolkit.format_json(payload.get("text", ""), int(payload.get("indent", 2)),
                               bool(payload.get("sort_keys", False)))


@router.post("/toolkit/json/paths", tags=["toolkit"])
async def json_paths(payload: Dict[str, Any] = Body(...)):
    return toolkit.json_paths(payload.get("text", ""))


@router.post("/toolkit/base64", tags=["toolkit"])
async def base64_convert(payload: Dict[str, Any] = Body(...)):
    if (payload.get("mode") or "encode") == "decode":
        return toolkit.base64_decode(payload.get("text", ""))
    return toolkit.base64_encode(payload.get("text", ""), bool(payload.get("url_safe", False)))


@router.post("/toolkit/hash", tags=["toolkit"])
async def hash_text(payload: Dict[str, Any] = Body(...)):
    return toolkit.hash_text(payload.get("text", ""), payload.get("algorithm", "sha256"))


@router.get("/toolkit/uuid", tags=["toolkit"])
async def make_uuid(version: int = 4, count: int = 1):
    return toolkit.make_uuid(version, count)


@router.post("/toolkit/regex", tags=["toolkit"])
async def test_regex(payload: Dict[str, Any] = Body(...)):
    return toolkit.test_regex(payload.get("pattern", ""), payload.get("text", ""),
                              payload.get("flags", ""))


@router.post("/toolkit/diff", tags=["toolkit"])
async def diff_text(payload: Dict[str, Any] = Body(...)):
    return toolkit.diff_text(payload.get("left", ""), payload.get("right", ""))


@router.post("/toolkit/csv", tags=["toolkit"])
async def analyse_csv(payload: Dict[str, Any] = Body(...)):
    return toolkit.analyse_csv(payload.get("text", ""))


@router.get("/toolkit/convert", tags=["toolkit"])
async def convert_units(value: float, source: str, target: str):
    return toolkit.convert_units(value, source, target)


@router.get("/toolkit/colour", tags=["toolkit"])
async def convert_colour(value: str):
    return toolkit.convert_colour(value)


@router.get("/toolkit/cron", tags=["toolkit"])
async def explain_cron(expression: str):
    return toolkit.explain_cron(expression)


@router.get("/toolkit/timestamp", tags=["toolkit"])
async def convert_timestamp(value: str):
    return toolkit.convert_timestamp(value)


@router.post("/toolkit/words", tags=["toolkit"])
async def word_count(payload: Dict[str, Any] = Body(...)):
    return toolkit.word_count(payload.get("text", ""))


@router.get("/toolkit/slug", tags=["toolkit"])
async def slugify(text: str):
    return toolkit.slugify(text)


# ======================================================================
# reading
# ======================================================================
@router.get("/reading/bookmarks", tags=["reading"])
async def list_bookmarks(unread: bool = False, tag: str = "", q: str = "", limit: int = 100):
    return {"bookmarks": await _service("reading").bookmarks(unread, tag, q, limit)}


@router.post("/reading/bookmarks", tags=["reading"])
async def add_bookmark(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("reading").add_bookmark(
        payload.get("url", ""), payload.get("title", ""),
        payload.get("note", ""), payload.get("tags")))


@router.post("/reading/bookmarks/{bookmark_id}/read", tags=["reading"])
async def mark_bookmark_read(bookmark_id: str, payload: Dict[str, Any] = Body(default={})):
    return await _call(_service("reading").mark_read(bookmark_id, bool(payload.get("read", True))))


@router.post("/reading/bookmarks/{bookmark_id}/summarise", tags=["reading"])
async def summarise_bookmark(bookmark_id: str):
    return await _call(_service("reading").summarise_bookmark(bookmark_id))


@router.delete("/reading/bookmarks/{bookmark_id}", tags=["reading"])
async def delete_bookmark(bookmark_id: str):
    return await _call(_service("reading").delete_bookmark(bookmark_id))


@router.get("/reading/queue", tags=["reading"])
async def reading_queue(limit: int = 20):
    return await _service("reading").queue(limit)


@router.get("/reading/feeds", tags=["reading"])
async def list_feeds():
    return {"feeds": await _service("reading").feeds()}


@router.post("/reading/feeds", tags=["reading"])
async def add_feed(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("reading").add_feed(payload.get("url", "")))


@router.post("/reading/feeds/refresh", tags=["reading"])
async def refresh_feeds():
    return await _service("reading").refresh_all()


@router.post("/reading/feeds/{feed_id}/refresh", tags=["reading"])
async def refresh_feed(feed_id: str):
    return await _call(_service("reading").refresh_feed(feed_id))


@router.delete("/reading/feeds/{feed_id}", tags=["reading"])
async def delete_feed(feed_id: str):
    return await _call(_service("reading").delete_feed(feed_id))


@router.get("/reading/items", tags=["reading"])
async def feed_items(feed_id: str = "", unread: bool = False, limit: int = 50):
    return {"items": await _service("reading").feed_items(feed_id, unread, limit)}


@router.post("/reading/items/{item_id}/read", tags=["reading"])
async def mark_item_read(item_id: str, payload: Dict[str, Any] = Body(default={})):
    return await _call(_service("reading").mark_item_read(
        item_id, bool(payload.get("read", True))))


@router.get("/reading/digest", tags=["reading"])
async def reading_digest(limit: int = 12):
    return await _service("reading").digest(limit)


# ======================================================================
# universal search
# ======================================================================
@router.get("/search", tags=["search"])
async def universal_search(q: str = Query(""), sources: str = "", limit: int = 40):
    wanted: Optional[List[str]] = [s.strip() for s in sources.split(",") if s.strip()] \
        if sources else None
    return await _service("search").search(q, wanted, limit)


# ======================================================================
# interface customisation
# ======================================================================
@router.get("/interface", tags=["interface"])
async def get_interface():
    customise = _service("customise")
    return {"interface": await customise.interface(),
            "themes": await customise.all_themes(),
            "layouts": await customise.layouts()}


@router.patch("/interface", tags=["interface"])
async def set_interface(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("customise").set_interface(**payload))


@router.post("/interface/themes", tags=["interface"])
async def create_theme(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("customise").create_theme(
        payload.get("name", ""), payload.get("colours", {}),
        payload.get("description", "")))


@router.post("/interface/themes/preview", tags=["interface"])
async def preview_theme(payload: Dict[str, Any] = Body(...)):
    return await _service("customise").preview_contrast(payload.get("colours", {}))


@router.delete("/interface/themes/{theme_id}", tags=["interface"])
async def delete_theme(theme_id: str):
    return await _call(_service("customise").delete_theme(theme_id))


@router.post("/interface/layouts", tags=["interface"])
async def save_layout(payload: Dict[str, Any] = Body(...)):
    return await _call(_service("customise").save_layout(
        payload.get("name", ""), payload.get("config", {})))


@router.delete("/interface/layouts/{layout_id}", tags=["interface"])
async def delete_layout(layout_id: str):
    return await _call(_service("customise").delete_layout(layout_id))


# ======================================================================
# code audit
# ======================================================================
@router.get("/audit/todos", tags=["developer"])
async def audit_todos(workspace: str = "", limit: int = 200):
    return await _call(_service("codeaudit").todos(workspace, limit))


@router.get("/audit/metrics", tags=["developer"])
async def audit_metrics(workspace: str = ""):
    return await _call(_service("codeaudit").metrics(workspace))


@router.get("/audit/licences", tags=["developer"])
async def audit_licences(workspace: str = ""):
    return await _call(_service("codeaudit").licences(workspace))


@router.get("/audit/env", tags=["developer"])
async def audit_env(workspace: str = ""):
    return await _call(_service("codeaudit").env_check(workspace))


@router.get("/audit/branches", tags=["developer"])
async def audit_branches(workspace: str = "", days: int = 60):
    return await _call(_service("codeaudit").stale_branches(workspace, days))


@router.get("/audit/changelog", tags=["developer"])
async def audit_changelog(workspace: str = "", since: str = ""):
    return await _call(_service("codeaudit").changelog(workspace, since))


@router.get("/audit/contributors", tags=["developer"])
async def audit_contributors(workspace: str = ""):
    return await _call(_service("codeaudit").contributors(workspace))


@router.get("/audit/coverage", tags=["developer"])
async def audit_coverage(workspace: str = ""):
    return await _call(_service("codeaudit").coverage(workspace))


# ======================================================================
# desktop notifications
#
# Reading the machine's own Action Center. Every one of these is explicit:
# nothing starts watching as a side effect of being asked about.
#
# Deliberately under /desktop/ rather than /notifications/desktop: a
# `/notifications/{id}/{action}` route already exists elsewhere, and it
# matched first -- "desktop" was read as a notification id and "start" as an
# action on it. Depending on router registration order to resolve that is a
# trap for whoever reorders them next.
# ======================================================================
@router.get("/desktop/notifications", tags=["notifications"])
async def desktop_notifications(limit: int = 40):
    """What Windows has received, newest first.

    Held in memory only. Stopping the watcher discards them, and nothing
    here is ever written to disk.
    """
    service = container.get("desktop_notifications")
    if service is None:
        raise HTTPException(503, "Desktop notifications are not available in this build.")
    return {**service.status(), "items": service.recent(limit)}


@router.post("/desktop/notifications/start", tags=["notifications"])
async def desktop_notifications_start():
    service = container.get("desktop_notifications")
    if service is None:
        raise HTTPException(503, "Desktop notifications are not available in this build.")
    status = await service.start()
    if not status["running"]:
        # A 200 saying "denied" invites the interface to show a green light
        # over a watcher that is not watching.
        raise HTTPException(409, status["detail"])
    return status


@router.post("/desktop/notifications/stop", tags=["notifications"])
async def desktop_notifications_stop():
    service = container.get("desktop_notifications")
    if service is None:
        raise HTTPException(503, "Desktop notifications are not available in this build.")
    return await service.stop()


@router.post("/desktop/notifications/mute", tags=["notifications"])
async def desktop_notifications_mute(payload: Dict[str, Any] = Body(...)):
    """Mute or unmute one application.

    Muted apps are dropped as they arrive, so nothing from them is held in
    memory at all -- this is not a display filter over content kept anyway.
    """
    service = container.get("desktop_notifications")
    if service is None:
        raise HTTPException(503, "Desktop notifications are not available in this build.")
    try:
        return await service.set_muted(payload.get("app", ""),
                                       bool(payload.get("muted", True)))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
