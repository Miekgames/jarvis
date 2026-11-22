"""
Calendar service.

Local-first: events live in JARVIS's own database, so the calendar works with
no accounts connected. External providers (Google, Microsoft) sync *into* this
store through the integration layer, which keeps one query path for everything
downstream -- the briefing, meeting prep, automations and the AI's context.

The watcher publishes `calendar.upcoming` as events approach, which is what
"15 minutes before my next meeting" automations trigger on.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import ahead, iso, new_id, now_iso, parse_iso, since, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.calendar")

LEAD_TIMES = [60, 30, 15, 5, 1]  # minutes before an event that we announce


class CalendarService:
    def __init__(self, db, activity, notifications=None):
        self.db = db
        self.activity = activity
        self.notifications = notifications
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._announced: Dict[str, set] = {}

    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._watch(), name="calendar-watcher")
        logger.info("calendar watcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def _watch(self) -> None:
        while self._running:
            try:
                await self._check_upcoming()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("calendar watch failed")
            await asyncio.sleep(60)

    async def _check_upcoming(self) -> None:
        now = utcnow()
        events = await self.db.fetch_all(
            "SELECT * FROM calendar_events WHERE start_at >= ? AND start_at <= ? ORDER BY start_at",
            (iso(now), ahead(3600)),
        )
        for row in events:
            start = parse_iso(row["start_at"])
            if start is None:
                continue
            minutes_until = int((start - now).total_seconds() // 60)
            announced = self._announced.setdefault(row["id"], set())

            for lead in LEAD_TIMES:
                if minutes_until <= lead and lead not in announced:
                    announced.add(lead)
                    event = _decode(row)
                    bus.publish(
                        "calendar.upcoming",
                        {**event, "minutes_until": minutes_until, "lead": lead},
                        source="calendar",
                    )
                    if lead in (15, 5) and self.notifications:
                        await self.notifications.create(
                            title=f"{event['title']} in {minutes_until} minute"
                                  f"{'s' if minutes_until != 1 else ''}",
                            body=event.get("link") or event.get("location") or "",
                            priority="high" if lead <= 5 else "normal",
                            source="calendar",
                            action={"kind": "open_event", "event_id": event["id"], "link": event.get("link")},
                        )
                    break

        # forget events that have passed
        for event_id in list(self._announced):
            if not any(r["id"] == event_id for r in events):
                self._announced.pop(event_id, None)

    # ==================================================================
    async def create(
        self,
        title: str,
        start_at: str,
        end_at: Optional[str] = None,
        location: str = "",
        link: str = "",
        description: str = "",
        attendees: Optional[List[str]] = None,
        all_day: bool = False,
        source: str = "local",
        external_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start = parse_iso(start_at)
        if start is None:
            raise ValueError(f"'{start_at}' is not a valid date/time.")
        if end_at is None:
            end_at = iso(start + timedelta(hours=1))

        event_id = new_id("evt")
        await self.db.execute(
            "INSERT INTO calendar_events (id, title, start_at, end_at, all_day, location, link,"
            " description, attendees, source, external_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, title, iso(start), end_at, 1 if all_day else 0, location, link,
                description, dumps(attendees or []), source, external_id, now_iso(), now_iso(),
            ),
        )
        record = await self.get(event_id)
        bus.publish(Events.CALENDAR_CHANGED, {"action": "created", "event": record}, source="calendar")
        await self.activity.record(f"Event created: {title}", source="calendar")
        return record

    async def update(self, event_id: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"title", "start_at", "end_at", "location", "link", "description", "attendees", "all_day"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return await self.get(event_id)
        if "attendees" in updates:
            updates["attendees"] = dumps(updates["attendees"])
        if "all_day" in updates:
            updates["all_day"] = 1 if updates["all_day"] else 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self.db.execute(
            f"UPDATE calendar_events SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_iso(), event_id),
        )
        record = await self.get(event_id)
        bus.publish(Events.CALENDAR_CHANGED, {"action": "updated", "event": record}, source="calendar")
        return record

    async def delete(self, event_id: str) -> bool:
        record = await self.get(event_id)
        deleted = await self.db.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,)) > 0
        if deleted:
            bus.publish(Events.CALENDAR_CHANGED, {"action": "deleted", "event": record}, source="calendar")
            await self.activity.record(f"Event cancelled: {record.get('title')}", source="calendar")
        return deleted

    # ==================================================================
    async def get(self, event_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM calendar_events WHERE id = ?", (event_id,))
        return _decode(row) if row else {}

    async def upcoming(self, limit: int = 10, hours: int = 24 * 7) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM calendar_events WHERE start_at >= ? AND start_at <= ?"
            " ORDER BY start_at ASC LIMIT ?",
            (now_iso(), ahead(hours * 3600), limit),
        )
        return [_decode(r) for r in rows]

    async def next_event(self) -> Optional[Dict[str, Any]]:
        events = await self.upcoming(limit=1)
        return events[0] if events else None

    async def range(self, start: str, end: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM calendar_events WHERE start_at >= ? AND start_at <= ? ORDER BY start_at ASC",
            (start, end),
        )
        return [_decode(r) for r in rows]

    async def today(self) -> List[Dict[str, Any]]:
        now = utcnow().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.range(iso(start), iso(start + timedelta(days=1)))

    async def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM calendar_events WHERE title LIKE ? OR description LIKE ? OR location LIKE ?"
            " ORDER BY start_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        )
        return [_decode(r) for r in rows]

    async def stats(self) -> Dict[str, Any]:
        return {
            "total": int(await self.db.fetch_value("SELECT COUNT(*) FROM calendar_events", default=0)),
            "upcoming": len(await self.upcoming(limit=100)),
            "next": await self.next_event(),
            "watcher_running": self.running,
        }

    # ==================================================================
    async def sync_external(self, events: List[Dict[str, Any]], source: str) -> Dict[str, int]:
        """Upsert events pulled from an external provider by external_id."""
        created = updated = 0
        for event in events:
            external_id = event.get("external_id") or event.get("id")
            existing = await self.db.fetch_one(
                "SELECT id FROM calendar_events WHERE source = ? AND external_id = ?",
                (source, external_id),
            )
            if existing:
                await self.update(
                    existing["id"],
                    title=event.get("title"),
                    start_at=event.get("start_at"),
                    end_at=event.get("end_at"),
                    location=event.get("location"),
                    link=event.get("link"),
                    description=event.get("description"),
                    attendees=event.get("attendees"),
                )
                updated += 1
            else:
                await self.create(
                    title=event.get("title", "Untitled"),
                    start_at=event.get("start_at", now_iso()),
                    end_at=event.get("end_at"),
                    location=event.get("location", ""),
                    link=event.get("link", ""),
                    description=event.get("description", ""),
                    attendees=event.get("attendees"),
                    source=source,
                    external_id=external_id,
                )
                created += 1
        await self.activity.record(
            f"Calendar synchronised from {source}: {created} new, {updated} updated.", source="calendar"
        )
        return {"created": created, "updated": updated}


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["attendees"] = loads(item.get("attendees"), [])
    item["all_day"] = bool(item.get("all_day"))
    return item
