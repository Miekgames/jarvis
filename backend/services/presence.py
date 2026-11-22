"""
Sleep / wake.

Sleep is not a black overlay on a still-rendering dashboard. The frontend tears
down its WebGL context and stops its animation loop; the backend slows its
sampling and stops pushing chatty events. What keeps running is exactly what
should: the scheduler, task workers, reminders, calendar watching and critical
alerts.

Wake restores the previous state, replays what happened, and reports it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import humanize_duration, now_iso, parse_iso, utcnow

logger = logging.getLogger("jarvis.presence")


class PresenceService:
    def __init__(self, db, settings, settings_store, activity, orchestrator=None, metrics=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.activity = activity
        self.orchestrator = orchestrator
        self.metrics = metrics
        self._slept_at: Optional[str] = None
        self._previous_interval: Optional[int] = None

    # ==================================================================
    async def is_sleeping(self) -> bool:
        return bool(await self.settings_store.get("sleeping", False))

    # ==================================================================
    async def sleep(self, reason: str = "user", greeting: bool = True) -> Dict[str, Any]:
        if await self.is_sleeping():
            return {"sleeping": True, "message": "Already asleep."}

        self._slept_at = now_iso()
        await self.settings_store.set("sleeping", True, actor=reason)
        await self.settings_store.set("slept_at", self._slept_at, actor=reason)

        # Back off sampling: still monitoring, just not 3-second granularity.
        if self.metrics is not None:
            self._previous_interval = self.settings.METRICS_INTERVAL_SECONDS
            self.settings.METRICS_INTERVAL_SECONDS = max(30, self._previous_interval * 10)

        if self.orchestrator is not None:
            self.orchestrator.set_state("sleep")

        message = "Going to sleep. Background services stay running -- say the wake word, press the wake key, or click anywhere to bring me back."
        bus.publish(
            Events.SLEEP,
            {"reason": reason, "at": self._slept_at, "message": message if greeting else ""},
            source="presence",
        )
        await self.activity.record(f"Entered sleep mode ({reason}).", source="presence")
        logger.info("sleep mode entered (%s)", reason)
        return {"sleeping": True, "at": self._slept_at, "message": message}

    # ==================================================================
    async def wake(self, reason: str = "user", greeting: bool = True) -> Dict[str, Any]:
        if not await self.is_sleeping():
            return {"sleeping": False, "message": "Already awake."}

        slept_at = await self.settings_store.get("slept_at", self._slept_at)
        await self.settings_store.set("sleeping", False, actor=reason)

        if self.metrics is not None and self._previous_interval:
            self.settings.METRICS_INTERVAL_SECONDS = self._previous_interval
            self._previous_interval = None

        summary = await self._while_you_were_out(slept_at)
        if self.orchestrator is not None:
            self.orchestrator.set_state("idle")

        greeting_text = self._greeting(summary)
        bus.publish(
            Events.WAKE,
            {"reason": reason, "at": now_iso(), "summary": summary,
             "message": greeting_text if greeting else ""},
            source="presence",
        )
        await self.activity.record(f"Woke from sleep ({reason}).", source="presence")
        logger.info("woke from sleep (%s)", reason)
        return {"sleeping": False, "summary": summary, "message": greeting_text}

    async def toggle(self, reason: str = "user") -> Dict[str, Any]:
        return await self.wake(reason) if await self.is_sleeping() else await self.sleep(reason)

    # ==================================================================
    async def _while_you_were_out(self, slept_at: Optional[str]) -> Dict[str, Any]:
        if not slept_at:
            return {}
        completed = await self.db.fetch_all(
            "SELECT name, status FROM tasks WHERE finished_at > ? ORDER BY finished_at DESC LIMIT 10",
            (slept_at,),
        )
        notifications = await self.db.fetch_all(
            "SELECT title, priority FROM notifications WHERE ts > ? AND dismissed = 0"
            " ORDER BY ts DESC LIMIT 20",
            (slept_at,),
        )
        events = await self.db.fetch_all(
            "SELECT title, start_at FROM calendar_events WHERE start_at >= ? ORDER BY start_at LIMIT 3",
            (now_iso(),),
        )
        slept = parse_iso(slept_at)
        duration = (utcnow() - slept).total_seconds() if slept else 0

        return {
            "asleep_for": humanize_duration(duration),
            "asleep_seconds": int(duration),
            "tasks_finished": [dict(t) for t in completed],
            "notifications": [dict(n) for n in notifications],
            "next_events": [dict(e) for e in events],
            "critical_count": sum(1 for n in notifications if n["priority"] == "critical"),
        }

    def _greeting(self, summary: Dict[str, Any]) -> str:
        hour = utcnow().astimezone().hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        owner = self.settings.OWNER_NAME
        line = f"Good {part}, {owner}. I'm back online."

        pieces: List[str] = []
        finished = summary.get("tasks_finished") or []
        if finished:
            pieces.append(f"{len(finished)} task{'s' if len(finished) != 1 else ''} finished while you were away")
        notifications = summary.get("notifications") or []
        if notifications:
            critical = summary.get("critical_count", 0)
            text = f"{len(notifications)} notification{'s' if len(notifications) != 1 else ''}"
            if critical:
                text += f", {critical} critical"
            pieces.append(text)
        events = summary.get("next_events") or []
        if events:
            pieces.append(f"next up: {events[0]['title']}")

        return line + (" " + ". ".join(p.capitalize() for p in pieces) + "." if pieces else "")

    # ==================================================================
    async def status(self) -> Dict[str, Any]:
        sleeping = await self.is_sleeping()
        return {
            "sleeping": sleeping,
            "since": await self.settings_store.get("slept_at", None) if sleeping else None,
            "state": self.orchestrator.state if self.orchestrator else "unknown",
            "mode": await self.settings_store.get("active_mode", "normal"),
        }
