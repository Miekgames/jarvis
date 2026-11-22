"""Calendar, contacts, projects, timers and communication drafting."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import ahead, humanize_duration, iso, new_id, now_iso, parse_iso, truncate, utcnow
from ...db.database import dumps, loads
from .registry import ToolRegistry, fail, ok
from .work_tools import _parse_when


def register(registry: ToolRegistry) -> None:

    # ==================================================================
    # calendar
    # ==================================================================
    @registry.add(
        "calendar_view",
        "See what's on the calendar. Use for 'what's my next meeting', 'what's on today', "
        "'what does my week look like'.",
        {"type": "object", "properties": {
            "range": {"type": "string", "enum": ["next", "today", "tomorrow", "week", "upcoming"]},
            "limit": {"type": "integer"},
        }},
        permission="calendar.read",
        category="calendar",
    )
    async def calendar_view(ctx, range: str = "upcoming", limit: int = 10):
        service = ctx.get("calendar")
        if range == "next":
            event = await service.next_event()
            if not event:
                return ok({"event": None}, "Nothing is scheduled.")
            when = parse_iso(event["start_at"])
            local = when.astimezone().strftime("%H:%M on %A %d %B") if when else event["start_at"]
            return ok({"event": _slim(event)}, f"{event['title']} at {local}.")

        if range == "today":
            events = await service.today()
        elif range == "tomorrow":
            start = utcnow().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            events = await service.range(iso(start), iso(start + timedelta(days=1)))
        elif range == "week":
            events = await service.upcoming(limit=50, hours=24 * 7)
        else:
            events = await service.upcoming(limit=min(limit, 30))

        if not events:
            return ok({"events": []}, f"Nothing scheduled for {range}.")
        return ok({"events": [_slim(e) for e in events]}, f"{len(events)} event(s).")

    @registry.add(
        "calendar_create",
        "Add an event to the calendar.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO datetime or a phrase like 'tomorrow 14:00'"},
            "duration_minutes": {"type": "integer"},
            "location": {"type": "string"},
            "link": {"type": "string", "description": "Meeting URL"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
        }, "required": ["title", "start"]},
        permission="calendar.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Add '{a.get('title')}' to the calendar at {a.get('start')}?",
        category="calendar",
    )
    async def calendar_create(ctx, title: str, start: str, duration_minutes: int = 60,
                              location: str = "", link: str = "", description: str = "",
                              attendees: Optional[List[str]] = None):
        when = _parse_when(start)
        if when is None:
            return fail(f"Couldn't parse '{start}' as a date and time.")
        record = await ctx.get("calendar").create(
            title=title, start_at=iso(when), end_at=iso(when + timedelta(minutes=duration_minutes)),
            location=location, link=link, description=description, attendees=attendees or [],
        )
        return ok({"event_id": record["id"]},
                  f"Added '{title}' at {when.astimezone().strftime('%H:%M on %A %d %B')}.")

    @registry.add(
        "calendar_modify",
        "Update or cancel a calendar event.",
        {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "action": {"type": "string", "enum": ["update", "cancel"]},
            "title": {"type": "string"},
            "start": {"type": "string"},
            "location": {"type": "string"},
            "link": {"type": "string"},
        }, "required": ["event_id", "action"]},
        permission="calendar.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Cancel event {a.get('event_id')}?" if a.get("action") == "cancel"
            else f"Update event {a.get('event_id')}?"
        ),
        category="calendar",
    )
    async def calendar_modify(ctx, event_id: str, action: str, title: str = "", start: str = "",
                              location: str = "", link: str = ""):
        service = ctx.get("calendar")
        if action == "cancel":
            deleted = await service.delete(event_id)
            return ok({"deleted": deleted}, "Event cancelled." if deleted else "No such event.")
        updates: Dict[str, Any] = {}
        if title:
            updates["title"] = title
        if start:
            when = _parse_when(start)
            if when is None:
                return fail(f"Couldn't parse '{start}'.")
            updates["start_at"] = iso(when)
        if location:
            updates["location"] = location
        if link:
            updates["link"] = link
        record = await service.update(event_id, **updates)
        return ok({"event": _slim(record)}, "Event updated.")

    # ==================================================================
    # contacts
    # ==================================================================
    @registry.add(
        "find_contact",
        "Look someone up in the contacts book.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        permission="contacts.read",
        category="contacts",
    )
    async def find_contact(ctx, name: str):
        db = ctx.get("db")
        rows = await db.fetch_all(
            "SELECT * FROM contacts WHERE name LIKE ? OR email LIKE ? OR organization LIKE ?"
            " ORDER BY name LIMIT 10",
            (f"%{name}%", f"%{name}%", f"%{name}%"),
        )
        if not rows:
            return ok({"contacts": []}, f"No contact matching '{name}'.")
        return ok({"contacts": [
            {"id": r["id"], "name": r["name"], "phone": r["phone"], "email": r["email"],
             "organization": r["organization"], "notes": truncate(r["notes"], 200)}
            for r in rows
        ]})

    @registry.add(
        "save_contact",
        "Create or update a contact.",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "organization": {"type": "string"},
            "notes": {"type": "string"},
        }, "required": ["name"]},
        permission="contacts.write",
        category="contacts",
    )
    async def save_contact(ctx, name: str, phone: str = "", email: str = "",
                           organization: str = "", notes: str = ""):
        db = ctx.get("db")
        existing = await db.fetch_one("SELECT id FROM contacts WHERE name = ?", (name,))
        if existing:
            await db.execute(
                "UPDATE contacts SET phone = COALESCE(NULLIF(?,''), phone),"
                " email = COALESCE(NULLIF(?,''), email),"
                " organization = COALESCE(NULLIF(?,''), organization),"
                " notes = COALESCE(NULLIF(?,''), notes), updated_at = ? WHERE id = ?",
                (phone, email, organization, notes, now_iso(), existing["id"]),
            )
            return ok({"contact_id": existing["id"]}, f"Updated {name}.")

        contact_id = new_id("con")
        await db.execute(
            "INSERT INTO contacts (id, name, phone, email, organization, notes, tags, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (contact_id, name, phone, email, organization, notes, dumps([]), now_iso(), now_iso()),
        )
        return ok({"contact_id": contact_id}, f"Saved {name}.")

    # ==================================================================
    # projects
    # ==================================================================
    @registry.add(
        "project",
        "Create, list or inspect a project. Projects group tasks, notes, files and conversations, and "
        "give later commands context ('add that to the website project').",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "create", "get", "add_note"]},
            "name": {"type": "string"},
            "project_id": {"type": "string"},
            "description": {"type": "string"},
            "deadline": {"type": "string"},
            "note": {"type": "string"},
        }, "required": ["action"]},
        category="projects",
    )
    async def project(ctx, action: str, name: str = "", project_id: str = "",
                      description: str = "", deadline: str = "", note: str = ""):
        db = ctx.get("db")

        if action == "list":
            rows = await db.fetch_all(
                "SELECT * FROM projects WHERE status = 'active' ORDER BY updated_at DESC LIMIT 25"
            )
            return ok({"projects": [
                {"id": r["id"], "name": r["name"], "description": truncate(r["description"], 150),
                 "deadline": r["deadline"]} for r in rows
            ]}, f"{len(rows)} active project(s).")

        if action == "create":
            if not name:
                return fail("A project needs a name.")
            new_project_id = new_id("prj")
            deadline_iso = iso(_parse_when(deadline)) if deadline else None
            await db.execute(
                "INSERT INTO projects (id, name, description, status, deadline, created_at, updated_at)"
                " VALUES (?,?,?,'active',?,?,?)",
                (new_project_id, name, description, deadline_iso, now_iso(), now_iso()),
            )
            return ok({"project_id": new_project_id}, f"Project '{name}' created.")

        if action == "get":
            row = await db.fetch_one(
                "SELECT * FROM projects WHERE id = ? OR name LIKE ?", (project_id, f"%{name}%")
            )
            if not row:
                return fail("No matching project.")
            items = await db.fetch_all(
                "SELECT kind, title, body, created_at FROM project_items WHERE project_id = ?"
                " ORDER BY created_at DESC LIMIT 20",
                (row["id"],),
            )
            tasks = await db.fetch_all(
                "SELECT id, name, status FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 10",
                (row["id"],),
            )
            return ok({
                "project": {"id": row["id"], "name": row["name"], "description": row["description"],
                            "deadline": row["deadline"]},
                "items": [dict(i) for i in items],
                "tasks": [dict(t) for t in tasks],
            })

        if action == "add_note":
            row = await db.fetch_one(
                "SELECT id, name FROM projects WHERE id = ? OR name LIKE ?", (project_id, f"%{name}%")
            )
            if not row:
                return fail("No matching project.")
            await db.execute(
                "INSERT INTO project_items (project_id, kind, ref_id, title, body, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (row["id"], "note", "", truncate(note, 80), note, now_iso()),
            )
            return ok({"project_id": row["id"]}, f"Note added to '{row['name']}'.")

        return fail(f"Unknown action '{action}'.")

    # ==================================================================
    # timers
    # ==================================================================
    @registry.add(
        "timer",
        "Start, check, stop or list timers and stopwatches.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "list", "stop", "stopwatch"]},
            "minutes": {"type": "number"},
            "label": {"type": "string"},
            "timer_id": {"type": "string"},
        }, "required": ["action"]},
        category="productivity",
    )
    async def timer(ctx, action: str, minutes: float = 0, label: str = "Timer", timer_id: str = ""):
        db = ctx.get("db")
        scheduler = ctx.get("scheduler")

        if action == "start":
            if minutes <= 0:
                return fail("How long should the timer run?")
            seconds = int(minutes * 60)
            record_id = new_id("tmr")
            ends = ahead(seconds)
            await db.execute(
                "INSERT INTO timers (id, label, kind, duration, started_at, ends_at, status)"
                " VALUES (?,?,?,?,?,?,'running')",
                (record_id, label, "timer", seconds, now_iso(), ends),
            )
            await scheduler.create_once(
                name=f"Timer: {label}",
                run_at=ends,
                action={"kind": "notify", "title": f"Timer finished: {label}",
                        "body": f"{humanize_duration(seconds)} is up.", "priority": "high", "speak": True},
            )
            return ok({"timer_id": record_id, "ends_at": ends},
                      f"{label} set for {humanize_duration(seconds)}.")

        if action == "stopwatch":
            record_id = new_id("tmr")
            await db.execute(
                "INSERT INTO timers (id, label, kind, duration, started_at, status)"
                " VALUES (?,?,?,0,?,'running')",
                (record_id, label or "Stopwatch", "stopwatch", now_iso()),
            )
            return ok({"timer_id": record_id}, "Stopwatch started.")

        if action == "list":
            rows = await db.fetch_all("SELECT * FROM timers WHERE status = 'running' ORDER BY started_at")
            now = utcnow()
            items = []
            for row in rows:
                if row["kind"] == "stopwatch":
                    started = parse_iso(row["started_at"])
                    elapsed = (now - started).total_seconds() if started else 0
                    items.append({"id": row["id"], "label": row["label"], "kind": "stopwatch",
                                  "elapsed": humanize_duration(elapsed)})
                else:
                    ends = parse_iso(row["ends_at"])
                    remaining = (ends - now).total_seconds() if ends else 0
                    items.append({"id": row["id"], "label": row["label"], "kind": "timer",
                                  "remaining": humanize_duration(max(0, remaining))})
            if not items:
                return ok({"timers": []}, "No timers running.")
            return ok({"timers": items})

        if action == "stop":
            if timer_id:
                await db.execute("UPDATE timers SET status = 'stopped' WHERE id = ?", (timer_id,))
            else:
                await db.execute("UPDATE timers SET status = 'stopped' WHERE status = 'running'")
            return ok(None, "Timer stopped.")

        return fail(f"Unknown action '{action}'.")

    # ==================================================================
    # communications drafting
    # ==================================================================
    @registry.add(
        "desktop_notifications",
        "Notifications OTHER APPLICATIONS sent to this computer -- Discord, email, calendar, "
        "messages -- read from the Windows Action Center. This is what someone means by "
        "'what did I miss', 'any notifications', 'did anyone message me', 'what came in'. "
        "NOT JARVIS's own alerts about tasks and disk space: those are check_notifications. "
        "Only works while notification reading is switched on; say so plainly if it is off "
        "rather than guessing at what they missed.",
        {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many to return (default 15)"},
            "app": {"type": "string", "description": "Only from this application"},
        }},
        permission="notifications.read",
        risk=Risk.LOW,
        category="personal",
    )
    async def desktop_notifications(ctx, limit: int = 15, app: str = ""):
        service = ctx.get("desktop_notifications")
        if service is None:
            return fail("Desktop notification reading is not available in this build.")

        state = service.status()
        if not state["running"]:
            return fail(
                "I am not reading this computer's notifications, so I cannot tell you what "
                "you missed. {} Turn it on in Settings -> Notifications.".format(state["detail"]))

        items = service.recent(200)
        if app:
            needle = app.lower()
            items = [i for i in items if needle in i["app"].lower()]
        items = items[:max(1, min(limit, 50))]

        if not items:
            return ok({"notifications": [], "count": 0},
                      "Nothing has come in{}.".format(f" from {app}" if app else ""))

        apps = sorted({i["app"] for i in items})
        return ok(
            {"notifications": items, "count": len(items), "apps": apps},
            "{} notification(s) from {}.".format(len(items), ", ".join(apps)))

    @registry.add(
        "draft_message",
        "Draft an email or message to a contact and save it for the user to review. This never sends "
        "anything -- to send an email, use send_email, which asks the owner to approve it first.",
        {"type": "object", "properties": {
            "to": {"type": "string", "description": "Contact name, email address or phone number"},
            "channel": {"type": "string", "enum": ["email", "sms", "note"]},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["to", "body"]},
        permission="contacts.read",
        category="communication",
    )
    async def draft_message(ctx, to: str, body: str, channel: str = "email", subject: str = ""):
        db = ctx.get("db")
        contact = await db.fetch_one(
            "SELECT * FROM contacts WHERE name LIKE ? OR email = ? OR phone = ?", (f"%{to}%", to, to)
        )
        draft_id = new_id("cmm")
        await db.execute(
            "INSERT INTO communications (id, ts, contact_id, channel, direction, subject, body, status, meta)"
            " VALUES (?,?,?,?,'outbound',?,?, 'draft', ?)",
            (draft_id, now_iso(), contact["id"] if contact else None, channel, subject, body,
             dumps({"to": to})),
        )
        recipient = contact["name"] if contact else to
        email = ctx.get("email")
        if channel == "email" and email is not None and email.connected:
            sending = ("If they want it sent, send_email can send it from "
                       f"{email.address} -- they'll see it and approve it first.")
        elif channel == "email":
            sending = ("I can't send it until email is connected (Email in JARVIS's menu).")
        else:
            sending = "There's no SMS connection, so it can't be sent from here."
        return ok(
            {"draft_id": draft_id, "to": recipient, "channel": channel, "subject": subject,
             "body": body, "to_address": contact["email"] if contact else ""},
            f"Drafted a {channel} to {recipient}. It's saved as a draft -- review it in the "
            f"Communications panel. {sending}",
        )


def _slim(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": event.get("id"),
        "title": event.get("title"),
        "start_at": event.get("start_at"),
        "end_at": event.get("end_at"),
        "location": event.get("location"),
        "link": event.get("link"),
        "attendees": event.get("attendees"),
    }
