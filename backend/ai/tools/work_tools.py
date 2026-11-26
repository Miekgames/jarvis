"""Tools for JARVIS's own machinery: tasks, schedules, automations, memory,
notifications, modes and sleep."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ...automations.actions import describe_action
from ...core.permissions import Risk
from ...core.util import ahead, humanize_duration, iso, now_iso, parse_iso, truncate, utcnow
from ...tasks.handlers import catalogue
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    # ==================================================================
    # background tasks
    # ==================================================================
    @registry.add(
        "create_task",
        "Queue long-running work as a background task so it survives page reloads and keeps going while "
        "the user does other things. Use this for research, multi-step preparation, bulk file work and "
        "anything that will take more than a few seconds.\n\nAvailable task types:\n" + catalogue(),
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short human-readable name shown in the task list"},
            "type": {"type": "string", "description": "One of the task types listed above"},
            "params": {"type": "object", "description": "Parameters for that task type"},
            "priority": {"type": "string", "enum": ["critical", "high", "normal", "low", "background"]},
            "description": {"type": "string"},
        }, "required": ["name", "type"]},
        permission="tasks.create",
        category="tasks",
    )
    async def create_task(ctx, name: str, type: str, params: Optional[Dict[str, Any]] = None,
                          priority: str = "normal", description: str = ""):
        engine = ctx.get("tasks")
        try:
            record = await engine.create(
                name=name, task_type=type, params=params or {}, description=description,
                priority=priority, conversation_id=ctx.get("conversation_id"),
            )
        except ValueError as exc:
            return fail(str(exc))
        return ok(
            {"task_id": record["id"], "name": record["name"], "status": record["status"]},
            f"Started background task '{name}' (id {record['id']}). It will keep running "
            f"and I'll report when it finishes.",
        )

    @registry.add(
        "list_tasks",
        "List background tasks and their live status, progress and current step.",
        {"type": "object", "properties": {
            "active_only": {"type": "boolean", "description": "Only queued/running/paused (default true)"},
            "limit": {"type": "integer"},
        }},
        permission="tasks.create",
        category="tasks",
    )
    async def list_tasks(ctx, active_only: bool = True, limit: int = 20):
        tasks = await ctx.get("tasks").list(active_only=active_only, limit=min(limit, 50))
        if not tasks:
            return ok({"tasks": []}, "No tasks are running." if active_only else "No tasks recorded.")
        return ok(
            {"tasks": [
                {"id": t["id"], "name": t["name"], "type": t["type"], "status": t["status"],
                 "progress": round(t["progress"] * 100), "current_step": t["current_step"],
                 "eta": t["eta"], "error": t["error"]}
                for t in tasks
            ]},
            f"{len(tasks)} task(s).",
        )

    @registry.add(
        "task_detail",
        "Get the full state and recent log of one background task, including its result if finished.",
        {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        permission="tasks.create",
        category="tasks",
    )
    async def task_detail(ctx, task_id: str):
        engine = ctx.get("tasks")
        record = await engine.get(task_id)
        if not record:
            return fail(f"No task with id '{task_id}'.")
        logs = await engine.logs(task_id, limit=40)
        return ok({
            "task": {k: v for k, v in record.items() if k not in ("checkpoint", "params")},
            "result": record.get("result"),
            "logs": [{"ts": l["ts"], "level": l["level"], "message": l["message"]} for l in logs[-25:]],
        })

    @registry.add(
        "control_task",
        "Pause, resume, cancel, retry or reprioritise a background task.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry", "prioritise"]},
            "priority": {"type": "string", "enum": ["critical", "high", "normal", "low", "background"]},
        }, "required": ["task_id", "action"]},
        permission="tasks.create",
        category="tasks",
    )
    async def control_task(ctx, task_id: str, action: str, priority: str = "normal"):
        engine = ctx.get("tasks")
        try:
            if action == "pause":
                record = await engine.pause(task_id)
            elif action == "resume":
                record = await engine.resume(task_id)
            elif action == "cancel":
                record = await engine.cancel(task_id)
            elif action == "retry":
                record = await engine.retry(task_id)
            elif action == "prioritise":
                record = await engine.prioritise(task_id, priority)
            else:
                return fail(f"Unknown action '{action}'.")
        except KeyError:
            return fail(f"No task with id '{task_id}'.")
        except (PermissionError, ValueError) as exc:
            return fail(str(exc))
        return ok({"task_id": task_id, "status": record.get("status")},
                  f"Task '{record.get('name')}' is now {record.get('status')}.")

    @registry.add(
        "stop_all_tasks",
        "Emergency stop: cancel every cancellable background task immediately.",
        {"type": "object", "properties": {}},
        permission="tasks.create",
        risk=Risk.HIGH,
        confirm_summary=lambda a: "Cancel every running background task right now?",
        category="tasks",
    )
    async def stop_all_tasks(ctx):
        result = await ctx.get("tasks").stop_all()
        return ok(result, f"Cancelled {result['count']} task(s)."
                          + (f" Could not cancel: {', '.join(result['skipped'])}." if result["skipped"] else ""))

    # ==================================================================
    # scheduling and reminders
    # ==================================================================
    @registry.add(
        "create_schedule",
        "Create a recurring or one-off schedule that runs an action. Use for 'every Monday at 8am give "
        "me my schedule', 'every night at 2am back up', 'every 30 minutes check X'.",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["once", "interval", "cron"]},
            "cron": {"type": "string", "description": "5-field cron, e.g. '0 8 * * 1' for Mondays at 08:00"},
            "interval_seconds": {"type": "integer"},
            "run_at": {"type": "string", "description": "ISO datetime or relative phrase, for kind='once'"},
            "action": {"type": "object", "description":
                       "What to do. Examples: {'kind':'notify','title':...,'body':...}, "
                       "{'kind':'create_task','type':'briefing','name':'Daily briefing'}, "
                       "{'kind':'ai','prompt':...,'speak':true}"},
        }, "required": ["name", "kind", "action"]},
        permission="automation.manage",
        category="scheduling",
    )
    async def create_schedule(ctx, name: str, kind: str, action: Dict[str, Any], cron: str = "",
                              interval_seconds: int = 0, run_at: str = ""):
        if kind == "once" and run_at:
            parsed = _parse_when(run_at)
            if parsed is None:
                return fail(f"Couldn't parse '{run_at}' as a time.")
            run_at = iso(parsed)
        try:
            record = await ctx.get("scheduler").create(
                name=name, action=action, kind=kind, cron=cron,
                interval_seconds=interval_seconds, run_at=run_at or None,
            )
        except ValueError as exc:
            return fail(str(exc))
        return ok(
            {"schedule_id": record["id"], "next_run": record["next_run"]},
            f"Schedule '{name}' created. Next run: {record['next_run'] or 'not scheduled'}.",
        )

    @registry.add(
        "list_schedules",
        "List scheduled jobs and reminders, soonest first.",
        {"type": "object", "properties": {"limit": {"type": "integer"}}},
        permission="tasks.create",
        category="scheduling",
    )
    async def list_schedules(ctx, limit: int = 20):
        schedules = await ctx.get("scheduler").list(limit=min(limit, 50))
        return ok({"schedules": [
            {"id": s["id"], "name": s["name"], "kind": s["kind"], "next_run": s["next_run"],
             "enabled": s["enabled"], "action": s["action"].get("kind")}
            for s in schedules
        ]}, f"{len(schedules)} schedule(s).")

    @registry.add(
        "delete_schedule",
        "Delete a schedule or reminder.",
        {"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]},
        permission="automation.manage",
        category="scheduling",
    )
    async def delete_schedule(ctx, schedule_id: str):
        deleted = await ctx.get("scheduler").delete(schedule_id)
        return ok({"deleted": deleted}, "Schedule deleted." if deleted else "No such schedule.")

    # ==================================================================
    # automations
    # ==================================================================
    @registry.add(
        "create_automation",
        "Create a TRIGGER -> CONDITIONS -> ACTIONS automation. Always describe it back to the user "
        "afterwards so they can see exactly what will happen.\n"
        "Triggers: {'kind':'event','event':'task.completed'|'notification.new'|'system.alert'|...}, "
        "{'kind':'metric','metric':'cpu','op':'above','value':90}, "
        "{'kind':'calendar_before','minutes':15}, {'kind':'mode_change'}, {'kind':'manual'}.\n"
        "Conditions: time_between, day_of_week, mode_is, metric_above, metric_below, field, not_sleeping.\n"
        "Actions: notify, speak, create_task, run_tool, ai, launch_app, set_mode, sleep, wake, webhook.",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "trigger": {"type": "object"},
            "actions": {"type": "array", "items": {"type": "object"}},
            "conditions": {"type": "array", "items": {"type": "object"}},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
        }, "required": ["name", "trigger", "actions"]},
        permission="automation.manage",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Create the automation '{a.get('name')}'? It will run: "
            + "; ".join(describe_action(x) for x in (a.get("actions") or []))
        ),
        category="automation",
    )
    async def create_automation(ctx, name: str, trigger: Dict[str, Any], actions: List[Dict[str, Any]],
                                conditions: Optional[List[Dict[str, Any]]] = None,
                                description: str = "", enabled: bool = True):
        engine = ctx.get("automations")
        try:
            record = await engine.create(
                name=name, trigger=trigger, actions=actions, conditions=conditions or [],
                description=description, enabled=enabled,
            )
        except ValueError as exc:
            return fail(str(exc))
        preview = engine.preview(record)
        return ok(
            {"automation_id": record["id"], "preview": preview, "enabled": record["enabled"]},
            f"Automation created. {preview['summary']}",
        )

    @registry.add(
        "list_automations",
        "List automations with what triggers them and what they do.",
        {"type": "object", "properties": {}},
        permission="automation.manage",
        category="automation",
    )
    async def list_automations(ctx):
        engine = ctx.get("automations")
        records = await engine.list()
        return ok({"automations": [
            {"id": a["id"], "name": a["name"], "enabled": a["enabled"],
             "fire_count": a["fire_count"], "last_fired": a["last_fired"],
             "summary": engine.preview(a)["summary"]}
            for a in records
        ]}, f"{len(records)} automation(s).")

    @registry.add(
        "control_automation",
        "Enable, disable, run or delete an automation.",
        {"type": "object", "properties": {
            "automation_id": {"type": "string"},
            "action": {"type": "string", "enum": ["enable", "disable", "run", "delete"]},
        }, "required": ["automation_id", "action"]},
        permission="automation.manage",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"{a.get('action', '').capitalize()} automation {a.get('automation_id')}?",
        category="automation",
    )
    async def control_automation(ctx, automation_id: str, action: str):
        engine = ctx.get("automations")
        try:
            if action == "enable":
                record = await engine.update(automation_id, enabled=True)
                return ok({"enabled": True}, f"'{record['name']}' enabled.")
            if action == "disable":
                record = await engine.update(automation_id, enabled=False)
                return ok({"enabled": False}, f"'{record['name']}' disabled.")
            if action == "run":
                result = await engine.run_manually(automation_id)
                return ok(result, f"Automation ran: {result['status']}.")
            if action == "delete":
                deleted = await engine.delete(automation_id)
                return ok({"deleted": deleted}, "Automation deleted." if deleted else "No such automation.")
        except KeyError:
            return fail(f"No automation with id '{automation_id}'.")
        return fail(f"Unknown action '{action}'.")

    # ==================================================================
    # memory
    # ==================================================================
    @registry.add(
        "remember",
        "Store something durable about the user: a preference, a fact, a routine, a project detail. "
        "Only store things worth recalling in future conversations.",
        {"type": "object", "properties": {
            "value": {"type": "string", "description": "The fact, written so it makes sense on its own"},
            "category": {"type": "string",
                         "enum": ["preference", "project", "person", "command", "fact", "routine", "general"]},
            "key": {"type": "string", "description": "Optional short key; reusing one updates that memory"},
            "importance": {"type": "integer", "description": "1-5"},
        }, "required": ["value"]},
        permission="memory.write",
        category="memory",
    )
    async def remember(ctx, value: str, category: str = "general", key: str = "", importance: int = 3):
        try:
            record = await ctx.get("memory").remember(value, category, key, importance, source="jarvis")
        except PermissionError as exc:
            return fail(str(exc))
        return ok({"id": record["id"], "category": record["category"]}, "Noted.")

    @registry.add(
        "recall",
        "Search what you remember about the user.",
        {"type": "object", "properties": {
            "query": {"type": "string"},
            "category": {"type": "string"},
        }, "required": ["query"]},
        category="memory",
    )
    async def recall(ctx, query: str, category: str = ""):
        hits = await ctx.get("memory").search(query, limit=10, category=category or None)
        if not hits:
            return ok({"memories": []}, "Nothing stored about that.")
        return ok({"memories": [
            {"id": h["id"], "category": h["category"], "key": h["key"], "value": h["value"]} for h in hits
        ]})

    @registry.add(
        "forget",
        "Delete a stored memory by id.",
        {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]},
        permission="memory.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Permanently delete memory {a.get('memory_id')}?",
        category="memory",
    )
    async def forget(ctx, memory_id: str):
        deleted = await ctx.get("memory").forget(memory_id)
        return ok({"deleted": deleted}, "Forgotten." if deleted else "No such memory.")

    # ==================================================================
    # notifications, modes, presence
    # ==================================================================
    @registry.add(
        "notify",
        "Post a notification to the notification centre.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
        }, "required": ["title"]},
        category="notifications",
    )
    async def notify(ctx, title: str, body: str = "", priority: str = "normal"):
        record = await ctx.get("notifications").create(title, body, priority, source="jarvis")
        return ok({"id": record["id"]}, "Notification posted.")

    @registry.add(
        "check_notifications",
        "Read JARVIS's OWN alerts -- the ones this assistant raised itself about tasks, "
        "schedules, disk space and the like. NOT the notifications other applications "
        "sent to this computer: those are desktop_notifications. Use when asked about "
        "'your alerts' or 'what JARVIS flagged'.",
        {"type": "object", "properties": {"summarise": {"type": "boolean"}}},
        category="notifications",
    )
    async def check_notifications(ctx, summarise: bool = True):
        service = ctx.get("notifications")
        if summarise:
            summary = await service.summarise(ctx.get("orchestrator"))
            return ok(summary, summary["summary"])
        items = await service.list(limit=20)
        return ok({"notifications": items}, f"{len(items)} pending.")

    @registry.add(
        "set_mode",
        "Switch JARVIS into a mode: normal, focus, work, gaming, study, meeting, away, developer, "
        "privacy or sleep. Each mode changes notifications, voice and AI routing.",
        {"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]},
        category="modes",
    )
    async def set_mode(ctx, mode: str):
        try:
            record = await ctx.get("modes").activate(mode.lower().strip())
        except KeyError as exc:
            available = [m["key"] for m in await ctx.get("modes").list()]
            return fail(f"{exc} Available modes: {', '.join(available)}.")
        return ok({"mode": record["key"]},
                  f"{record['name']} mode active. {(record.get('config') or {}).get('description', '')}")

    @registry.add(
        "sleep_mode",
        "Put JARVIS to sleep (display goes black, rendering stops, background services keep running) "
        "or wake it up again.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["sleep", "wake", "status"]}
        }, "required": ["action"]},
        category="modes",
    )
    async def sleep_mode(ctx, action: str):
        presence = ctx.get("presence")
        if action == "sleep":
            result = await presence.sleep(reason="voice")
            return ok(result, result["message"])
        if action == "wake":
            result = await presence.wake(reason="voice")
            return ok(result, result["message"])
        return ok(await presence.status())

    # ==================================================================
    # command history / activity
    # ==================================================================
    @registry.add(
        "recent_activity",
        "Read JARVIS's own activity log -- what it has been doing recently.",
        {"type": "object", "properties": {
            "limit": {"type": "integer"},
            "level": {"type": "string", "enum": ["info", "warn", "error"]},
            "search": {"type": "string"},
        }},
        category="system",
    )
    async def recent_activity(ctx, limit: int = 25, level: str = "", search: str = ""):
        entries = await ctx.get("activity").recent(
            limit=min(limit, 100), level=level or None, search=search or None
        )
        return ok({"activity": [
            {"ts": e["ts"], "level": e["level"], "source": e["source"], "message": e["message"]}
            for e in entries
        ]}, f"{len(entries)} entries.")


# ---------------------------------------------------------------------------
def _parse_when(text: str) -> Optional[datetime]:
    """Accept ISO datetimes and the relative phrasing people actually use."""
    import re

    text = (text or "").strip().lower()
    if not text:
        return None

    parsed = parse_iso(text)
    if parsed:
        return parsed

    now = utcnow().astimezone()

    match = re.match(r"in\s+(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr|day|week)s?", text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        seconds = {
            "second": 1, "sec": 1, "minute": 60, "min": 60,
            "hour": 3600, "hr": 3600, "day": 86400, "week": 604800,
        }[unit]
        return now + timedelta(seconds=amount * seconds)

    time_match = re.search(r"(\d{1,2})[:.](\d{2})\s*(am|pm)?|(\d{1,2})\s*(am|pm)", text)
    if time_match:
        if time_match.group(1):
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
            meridiem = time_match.group(3)
        else:
            hour, minute = int(time_match.group(4)), 0
            meridiem = time_match.group(5)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

        target = now.replace(hour=min(hour, 23), minute=min(minute, 59), second=0, microsecond=0)
        if "tomorrow" in text:
            target += timedelta(days=1)
        elif target <= now:
            target += timedelta(days=1)
        return target

    if "tomorrow" in text:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "tonight" in text:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)

    return None
