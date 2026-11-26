"""Scheduled notifications: "remind me at 7pm to...", "every weekday at 7:30...".

A reminder is a schedule whose action is a notification. When it goes off it
shows in JARVIS, is spoken if a JARVIS tab is open, and appears as a Windows
notification -- so it reaches the owner with no tab open at all.

When it goes off is worked out from the owner's words by `scheduler.phrases`,
never by the model.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import iso, parse_iso
from ..scheduler import phrases

logger = logging.getLogger("jarvis.reminders")

MAX_MESSAGE = 200


class ReminderError(ValueError):
    """Something the owner can act on, in words they can act on."""


def _is_reminder(schedule: Dict[str, Any]) -> bool:
    action = schedule.get("action") or {}
    return action.get("kind") == "notify" and (
        action.get("source") == "reminder" or str(schedule.get("name", "")).startswith("Reminder:"))


def _local(value: Optional[str]) -> Optional[datetime]:
    moment = parse_iso(value) if value else None
    return moment.astimezone() if moment else None


class Reminders:
    def __init__(self, scheduler) -> None:
        self.scheduler = scheduler

    def understand(self, when: str, now: Optional[datetime] = None) -> phrases.When:
        found = phrases.parse(when, now)
        if found is None:
            raise ReminderError(
                f"I couldn't tell when “{when}” is. Say it like “at 7pm”, “in 20 minutes”, "
                "“tomorrow at 9”, “every Monday at 8am” or “every 2 hours”.")
        return found

    async def add(self, message: str, when: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        message = " ".join((message or "").split()).strip(" .")[:MAX_MESSAGE]
        if not message:
            raise ReminderError("What should the reminder say?")
        found = self.understand(when, now)
        action = {"kind": "notify", "title": "Reminder", "body": message, "priority": "high",
                  "speak": True, "desktop": True, "source": "reminder", "says": found.says}
        first = found.run_at.astimezone().astimezone(timezone.utc) if found.run_at else None
        try:
            record = await self.scheduler.create(
                name=f"Reminder: {message[:60]}", action=action, kind=found.kind,
                run_at=iso(first) if first else None, cron=found.cron,
                interval_seconds=found.interval_seconds)
        except ValueError as exc:
            raise ReminderError(str(exc))
        bus.publish("schedule.changed", {"id": record["id"]}, source="reminders")
        return {**self._shape(record), "assumed": found.assumed}

    def _shape(self, schedule: Dict[str, Any]) -> Dict[str, Any]:
        action = schedule.get("action") or {}
        nxt = _local(schedule.get("next_run"))
        return {
            "id": schedule["id"],
            "message": action.get("body") or str(schedule.get("name", "")).replace("Reminder: ", ""),
            "says": action.get("says") or "",
            "repeats": schedule.get("kind") in ("cron", "interval"),
            "next": nxt.isoformat() if nxt else None,
            "next_said": _when_said(nxt) if nxt else "no further times",
            "enabled": bool(schedule.get("enabled")),
        }

    async def list(self) -> List[Dict[str, Any]]:
        schedules = await self.scheduler.list(enabled_only=True, limit=500)
        items = [self._shape(s) for s in schedules if _is_reminder(s) and s.get("next_run")]
        return sorted(items, key=lambda i: i["next"] or "")

    async def find(self, words: str) -> Optional[Dict[str, Any]]:
        items = await self.list()
        words = (words or "").strip().lower()
        for item in items:
            if item["id"] == words:
                return item
        if words.isdigit() and 1 <= int(words) <= len(items):
            return items[int(words) - 1]
        wanted = {w for w in words.replace(",", " ").split() if len(w) > 2}
        scored = [(len(wanted & set(i["message"].lower().split())), i) for i in items]
        scored = sorted([s for s in scored if s[0]], key=lambda s: s[0], reverse=True)
        if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            return None
        return scored[0][1]

    async def cancel(self, reminder_id: str) -> bool:
        done = bool(await self.scheduler.delete(reminder_id))
        if done:
            bus.publish("schedule.changed", {"id": reminder_id}, source="reminders")
        return done


def _when_said(moment: datetime) -> str:
    today = datetime.now().astimezone().date()
    day = moment.date()
    if day == today:
        label = "today"
    elif (day - today).days == 1:
        label = "tomorrow"
    elif 0 < (day - today).days < 7:
        label = moment.strftime("%A")
    else:
        label = moment.strftime("%a %d %b").replace(" 0", " ")
    return f"{label} at {moment.strftime('%H:%M')}"
