"""
The proactive engine — the part that makes JARVIS "always there" rather than
"something you open".

Rules it holds itself to, because a proactive assistant that gets this wrong is
worse than a passive one:

  * Off by default. `awareness.proactive` must be switched on.
  * Suggests, never acts. A suggestion is an offer with a button, not an
    action already taken.
  * Rate limited, and silent in quiet modes and during sleep.
  * Never repeats a suggestion the user dismissed.
  * Always carries its evidence -- every suggestion says why.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import ahead, new_id, now_iso, parse_iso, since, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.proactive")

CHECK_INTERVAL_SECONDS = 120
DEFAULT_MAX_PER_HOUR = 3
DISMISSAL_COOLDOWN_HOURS = 20


class ProactiveEngine:
    def __init__(self, db, settings_store, activity, notifications, services: Dict[str, Any]):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self.notifications = notifications
        self.services = services

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._offered_at: List[float] = []

    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="proactive")
        logger.info("proactive engine started")

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

    async def enabled(self) -> bool:
        awareness = await self.settings_store.get("awareness", {})
        return bool(awareness.get("proactive", False))

    # ==================================================================
    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                if await self._may_speak():
                    await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("proactive sweep failed")

    async def _may_speak(self) -> bool:
        if not await self.enabled():
            return False
        if await self.settings_store.get("sleeping", False):
            return False

        notifications = await self.settings_store.get("notifications", {})
        mode = await self.settings_store.get("active_mode", "normal")
        if mode in set(notifications.get("quiet_modes", [])):
            return False

        awareness = await self.settings_store.get("awareness", {})
        limit = int(awareness.get("max_suggestions_per_hour", DEFAULT_MAX_PER_HOUR))
        cutoff = time.monotonic() - 3600
        self._offered_at = [t for t in self._offered_at if t > cutoff]
        return len(self._offered_at) < limit

    # ==================================================================
    async def sweep(self) -> List[Dict[str, Any]]:
        """Run every generator, offer the single best candidate. Offering one
        good suggestion beats offering four mediocre ones."""
        candidates: List[Dict[str, Any]] = []
        for generator in (
            self._from_routine,
            self._from_system_pressure,
            self._from_calendar,
            self._from_stalled_work,
        ):
            try:
                candidates.extend(await generator())
            except Exception:
                logger.exception("suggestion generator %s failed", generator.__name__)

        fresh = [c for c in candidates if not await self._recently_dismissed(c["kind"], c["title"])]
        if not fresh:
            return []

        fresh.sort(key=lambda c: c.get("confidence", 0), reverse=True)
        best = fresh[0]
        if best.get("confidence", 0) < 0.45:
            return []

        return [await self.offer(**best)]

    # ==================================================================
    # generators
    # ==================================================================
    async def _from_routine(self) -> List[Dict[str, Any]]:
        patterns = self.services.get("patterns")
        if patterns is None:
            return []
        out = []
        for prediction in await patterns.due_now():
            out.append({
                "kind": "routine",
                "title": f"Run \"{truncate(prediction['command'], 60)}\"?",
                "body": "You usually do this around now.",
                "action": {"kind": "command", "text": prediction["command"]},
                "confidence": prediction["confidence"],
                "reason": prediction["reason"],
            })
        return out

    async def _from_system_pressure(self) -> List[Dict[str, Any]]:
        awareness = self.services.get("awareness")
        if awareness is None:
            return []
        out = []
        for advice in (await awareness.recommendations())[:1]:
            action = {"kind": "none"}
            if advice.get("tool"):
                action = {"kind": "tool", **advice["tool"]}
            out.append({
                "kind": "performance",
                "title": advice["issue"],
                "body": f"{advice['cause']}. {advice['action']}.",
                "action": action,
                "confidence": 0.7,
                "reason": advice["cause"],
            })
        return out

    async def _from_calendar(self) -> List[Dict[str, Any]]:
        calendar = self.services.get("calendar")
        if calendar is None:
            return []
        upcoming = await calendar.upcoming(limit=1, hours=2)
        if not upcoming:
            return []

        event = upcoming[0]
        start = parse_iso(event["start_at"])
        if start is None:
            return []
        minutes = (start - utcnow()).total_seconds() / 60
        if not (12 <= minutes <= 75):
            return []

        # Don't offer if preparation is already under way or done.
        existing = await self.db.fetch_value(
            "SELECT COUNT(*) FROM tasks WHERE type = 'meeting_prep' AND created_at > ?",
            (since(7200),), default=0,
        )
        if existing:
            return []

        return [{
            "kind": "meeting",
            "title": f"Prepare for {truncate(event['title'], 50)}?",
            "body": f"It starts in {int(minutes)} minutes. I can gather context and draft an agenda.",
            "action": {"kind": "task", "type": "meeting_prep",
                       "name": f"Prep: {event['title']}", "params": {"event_id": event["id"]}},
            "confidence": 0.8,
            "reason": f"'{event['title']}' starts in {int(minutes)} minutes and has no prep task.",
        }]

    async def _from_stalled_work(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, name, error FROM tasks WHERE status = 'failed' AND finished_at > ?"
            " ORDER BY finished_at DESC LIMIT 1",
            (since(3600),),
        )
        if not rows:
            return []
        task = rows[0]
        return [{
            "kind": "recovery",
            "title": f"Retry \"{truncate(task['name'], 50)}\"?",
            "body": truncate(task["error"] or "It failed earlier.", 160),
            "action": {"kind": "retry_task", "task_id": task["id"]},
            "confidence": 0.55,
            "reason": f"Task failed within the last hour: {truncate(task['error'], 100)}",
        }]

    # ==================================================================
    async def offer(
        self,
        kind: str,
        title: str,
        body: str = "",
        action: Optional[Dict[str, Any]] = None,
        confidence: float = 0.5,
        reason: str = "",
    ) -> Dict[str, Any]:
        suggestion_id = new_id("sug")
        await self.db.execute(
            "INSERT INTO suggestions (id, ts, kind, title, body, action, confidence, reason)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (suggestion_id, now_iso(), kind, title, body, dumps(action or {}), confidence, reason),
        )
        self._offered_at.append(time.monotonic())

        record = await self.get(suggestion_id)
        bus.publish("suggestion.offered", record, source="proactive")
        await self.activity.record(f"Suggested: {title}", source="proactive")
        logger.info("suggested (%s, %.2f): %s", kind, confidence, title)
        return record

    async def _recently_dismissed(self, kind: str, title: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM suggestions WHERE kind = ? AND title = ?"
            " AND status IN ('dismissed','accepted') AND ts > ?",
            (kind, title, since(DISMISSAL_COOLDOWN_HOURS * 3600)),
        )
        if row:
            return True
        # Also skip anything identical still sitting unanswered.
        pending = await self.db.fetch_one(
            "SELECT id FROM suggestions WHERE kind = ? AND title = ? AND status = 'offered'",
            (kind, title),
        )
        return pending is not None

    # ==================================================================
    async def resolve(self, suggestion_id: str, accepted: bool) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
        if not row:
            raise KeyError(suggestion_id)
        if row["status"] != "offered":
            return _decode(row)

        status = "accepted" if accepted else "dismissed"
        await self.db.execute(
            "UPDATE suggestions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, now_iso(), suggestion_id),
        )

        result: Dict[str, Any] = {}
        if accepted:
            result = await self._execute(loads(row["action"], {}) or {})

        record = await self.get(suggestion_id)
        bus.publish("suggestion.resolved", {**record, "result": result}, source="proactive")
        await self.activity.record(
            f"Suggestion {status}: {row['title']}",
            source="proactive",
        )
        return {**record, "result": result}

    async def _execute(self, action: Dict[str, Any]) -> Dict[str, Any]:
        kind = action.get("kind")
        try:
            if kind == "task":
                engine = self.services["tasks"]
                record = await engine.create(
                    name=action.get("name", "Suggested task"),
                    task_type=action.get("type", "agent"),
                    params=action.get("params", {}),
                    priority="high",
                )
                return {"task_id": record["id"]}

            if kind == "retry_task":
                await self.services["tasks"].retry(action["task_id"])
                return {"retried": action["task_id"]}

            if kind == "tool":
                registry = self.services["registry"]
                result = await registry.dispatch(action.get("name", ""),
                                                 action.get("arguments", {}) or {})
                return {"ok": result.ok, "message": truncate(result.message, 300)}

            if kind == "command":
                # Hand the text back to the UI so it goes through the normal
                # conversation path, tools, confirmations and all.
                bus.publish("suggestion.run_command", {"text": action.get("text", "")},
                            source="proactive")
                return {"dispatched": action.get("text", "")}
        except Exception as exc:
            logger.exception("suggestion action failed")
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"note": "Nothing to run for this suggestion."}

    # ==================================================================
    async def get(self, suggestion_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
        return _decode(row) if row else {}

    async def pending(self, limit: int = 5) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM suggestions WHERE status = 'offered' ORDER BY confidence DESC,"
            " ts DESC LIMIT ?",
            (limit,),
        )
        return [_decode(r) for r in rows]

    async def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM suggestions ORDER BY ts DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]

    async def stats(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status")
        counts = {r["status"]: r["n"] for r in rows}
        offered = sum(counts.values()) or 1
        return {
            "enabled": await self.enabled(),
            "running": self.running,
            "counts": counts,
            "acceptance_rate": round(counts.get("accepted", 0) / offered, 2),
            "offered_last_hour": len(self._offered_at),
        }

    async def expire_stale(self, hours: int = 6) -> int:
        """A suggestion nobody answered stops being relevant."""
        return await self.db.execute(
            "UPDATE suggestions SET status = 'expired', resolved_at = ?"
            " WHERE status = 'offered' AND ts < ?",
            (now_iso(), since(hours * 3600)),
        )


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["action"] = loads(item.get("action"), {})
    return item
