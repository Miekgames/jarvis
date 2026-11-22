"""
Scheduler.

Handles one-time, interval and cron schedules. Every schedule fires an *action*
-- the same action vocabulary the automation engine uses -- so "remind me at
7pm", "every Monday at 8am give me my schedule" and "run housekeeping nightly"
are all the same mechanism.

Missed fires (machine asleep, backend down) are handled by a misfire grace
window: inside the window it runs late, beyond it the occurrence is skipped and
the next one is scheduled, which is logged rather than hidden.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import iso, new_id, now_iso, parse_iso, utcnow
from ..db.database import dumps, loads
from .cron import CronError, CronExpression

logger = logging.getLogger("jarvis.scheduler")


class Kind:
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class SchedulerService:
    def __init__(self, db, settings, activity, actions, notifications=None):
        self.db = db
        self.settings = settings
        self.activity = activity
        self.actions = actions          # ActionRunner, shared with automations
        self.notifications = notifications
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._recompute_all()
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        logger.info("scheduler started (%ss tick)", self.settings.SCHEDULER_TICK_SECONDS)

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

    # ==================================================================
    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self.settings.SCHEDULER_TICK_SECONDS)

    async def tick(self, at: Optional[datetime] = None) -> int:
        """Fire everything due. Exposed for tests and manual runs."""
        moment = at or utcnow()
        due = await self.db.fetch_all(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ?"
            " ORDER BY next_run ASC",
            (iso(moment),),
        )
        fired = 0
        for row in due:
            try:
                if await self._fire(row, moment):
                    fired += 1
            except Exception:
                logger.exception("schedule %s failed to fire", row["id"])
                await self.activity.record(
                    f"Schedule '{row['name']}' failed to run.", level="error", source="scheduler"
                )
                await self._advance(row, moment)
        return fired

    async def _fire(self, row: Dict[str, Any], moment: datetime) -> bool:
        scheduled_for = parse_iso(row["next_run"])
        grace = int(row.get("misfire_grace") or 300)

        if scheduled_for and (moment - scheduled_for).total_seconds() > grace and grace > 0:
            logger.info("skipping missed occurrence of '%s' (%ss late)", row["name"],
                        int((moment - scheduled_for).total_seconds()))
            await self.activity.record(
                f"Skipped a missed run of '{row['name']}' -- it was more than "
                f"{grace}s late (the machine was likely asleep).",
                level="warn",
                source="scheduler",
            )
            # The occurrence is consumed even though it was skipped, otherwise a
            # one-shot would be retried on every subsequent tick forever.
            await self.db.execute(
                "UPDATE schedules SET last_run = ? WHERE id = ?", (iso(moment), row["id"])
            )
            row = {**row, "last_run": iso(moment)}
            await self._advance(row, moment)
            return False

        action = loads(row["action"], {}) or {}
        await self.activity.record(f"Schedule fired: {row['name']}", source="scheduler")
        bus.publish(
            Events.SCHEDULE_FIRED,
            {"id": row["id"], "name": row["name"], "action": action},
            source="scheduler",
        )

        result = await self.actions.run(action, context={"schedule_id": row["id"], "schedule_name": row["name"]})

        await self.db.execute(
            "UPDATE schedules SET last_run = ?, fire_count = fire_count + 1 WHERE id = ?",
            (iso(moment), row["id"]),
        )
        # _advance reads last_run to decide what comes next, so hand it the
        # updated view rather than the pre-fire snapshot.
        await self._advance({**row, "last_run": iso(moment)}, moment)
        logger.info("schedule '%s' fired -> %s", row["name"], result.get("status"))
        return True

    async def _advance(self, row: Dict[str, Any], moment: datetime) -> None:
        following = self._next_run(row, after=moment)
        if following is None:
            await self.db.execute(
                "UPDATE schedules SET next_run = NULL, enabled = 0 WHERE id = ?", (row["id"],)
            )
        else:
            await self.db.execute(
                "UPDATE schedules SET next_run = ? WHERE id = ?", (iso(following), row["id"])
            )

    # ==================================================================
    def _next_run(self, row: Dict[str, Any], after: Optional[datetime] = None) -> Optional[datetime]:
        after = after or utcnow()
        kind = row.get("kind", Kind.ONCE)

        if kind == Kind.ONCE:
            run_at = parse_iso(row.get("run_at"))
            if run_at is None or row.get("last_run"):
                return None  # never scheduled, or already consumed
            # A one-shot whose time has just passed is still due: the misfire
            # grace window in _fire decides whether to run it late or skip it.
            # Dropping it here would silently lose reminders created while the
            # backend was down.
            return run_at

        if kind == Kind.INTERVAL:
            seconds = int(row.get("interval_seconds") or 0)
            if seconds <= 0:
                return None
            last = parse_iso(row.get("last_run"))
            if last is None:
                # `run_at` is the first occurrence, not the moment the interval
                # starts counting from -- "every hour from 9am" should fire at
                # 9am, and "every 5 minutes" starting now should fire now.
                return parse_iso(row.get("run_at")) or after
            following = last + timedelta(seconds=seconds)
            while following <= after:
                following += timedelta(seconds=seconds)
            return following

        if kind == Kind.CRON:
            try:
                expression = CronExpression(row.get("cron") or "")
            except CronError as exc:
                logger.error("schedule %s has an invalid cron expression: %s", row.get("id"), exc)
                return None
            # Cron is evaluated in local time -- that's what users mean by "8am".
            local_after = after.astimezone()
            following = expression.next_after(local_after.replace(tzinfo=None))
            if following is None:
                return None
            return following.replace(tzinfo=local_after.tzinfo).astimezone(after.tzinfo)

        return None

    async def _recompute_all(self) -> None:
        rows = await self.db.fetch_all("SELECT * FROM schedules WHERE enabled = 1")
        for row in rows:
            following = self._next_run(row)
            await self.db.execute(
                "UPDATE schedules SET next_run = ? WHERE id = ?",
                (iso(following) if following else None, row["id"]),
            )
        logger.info("recomputed next-run times for %s schedule(s)", len(rows))

    # ==================================================================
    # creation
    # ==================================================================
    async def create(
        self,
        name: str,
        action: Dict[str, Any],
        kind: str = Kind.ONCE,
        run_at: Optional[str] = None,
        cron: str = "",
        interval_seconds: int = 0,
        enabled: bool = True,
        misfire_grace: int = 300,
    ) -> Dict[str, Any]:
        if kind == Kind.CRON:
            error = None
            try:
                CronExpression(cron)
            except CronError as exc:
                error = str(exc)
            if error:
                raise ValueError(error)
        elif kind == Kind.ONCE and not run_at:
            raise ValueError("A one-time schedule needs 'run_at'.")
        elif kind == Kind.INTERVAL and interval_seconds <= 0:
            raise ValueError("An interval schedule needs a positive 'interval_seconds'.")

        schedule_id = new_id("sch")
        row = {
            "id": schedule_id, "kind": kind, "cron": cron,
            "interval_seconds": interval_seconds, "run_at": run_at, "last_run": None,
        }
        following = self._next_run(row)

        await self.db.execute(
            "INSERT INTO schedules (id, name, kind, cron, interval_seconds, run_at, next_run,"
            " action, enabled, created_at, misfire_grace) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                schedule_id, name, kind, cron, interval_seconds, run_at,
                iso(following) if following else None, dumps(action),
                1 if enabled else 0, now_iso(), misfire_grace,
            ),
        )
        record = await self.get(schedule_id)
        await self.activity.record(
            f"Schedule created: {name}" + (f" (next: {record.get('next_run')})" if following else ""),
            source="scheduler",
        )
        return record

    async def create_once(self, name: str, run_at: str, action: Dict[str, Any]) -> Dict[str, Any]:
        return await self.create(name, action, kind=Kind.ONCE, run_at=run_at)

    async def create_reminder(self, text: str, run_at: str, priority: str = "normal") -> Dict[str, Any]:
        return await self.create_once(
            name=f"Reminder: {text[:60]}",
            run_at=run_at,
            action={"kind": "notify", "title": "Reminder", "body": text, "priority": priority, "speak": True},
        )

    # ==================================================================
    # CRUD
    # ==================================================================
    async def get(self, schedule_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return _decode(row) if row else {}

    async def list(self, enabled_only: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY CASE WHEN next_run IS NULL THEN 1 ELSE 0 END, next_run ASC LIMIT ?"
        return [_decode(r) for r in await self.db.fetch_all(sql, (limit,))]

    async def upcoming(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL"
            " ORDER BY next_run ASC LIMIT ?",
            (limit,),
        )
        return [_decode(r) for r in rows]

    async def update(self, schedule_id: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"name", "cron", "interval_seconds", "run_at", "enabled", "action", "kind", "misfire_grace"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get(schedule_id)
        if "action" in updates:
            updates["action"] = dumps(updates["action"])
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0

        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self.db.execute(
            f"UPDATE schedules SET {assignments} WHERE id = ?", (*updates.values(), schedule_id)
        )

        row = await self.db.fetch_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if row and row["enabled"]:
            following = self._next_run(row)
            await self.db.execute(
                "UPDATE schedules SET next_run = ? WHERE id = ?",
                (iso(following) if following else None, schedule_id),
            )
        return await self.get(schedule_id)

    async def delete(self, schedule_id: str) -> bool:
        return await self.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)) > 0

    async def run_now(self, schedule_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if not row:
            raise KeyError(schedule_id)
        action = loads(row["action"], {}) or {}
        result = await self.actions.run(action, context={"schedule_id": schedule_id, "manual": True})
        await self.db.execute(
            "UPDATE schedules SET last_run = ?, fire_count = fire_count + 1 WHERE id = ?",
            (now_iso(), schedule_id),
        )
        return result

    async def stats(self) -> Dict[str, Any]:
        total = int(await self.db.fetch_value("SELECT COUNT(*) FROM schedules", default=0))
        enabled = int(await self.db.fetch_value("SELECT COUNT(*) FROM schedules WHERE enabled = 1", default=0))
        upcoming = await self.upcoming(limit=1)
        return {
            "total": total,
            "enabled": enabled,
            "running": self.running,
            "next": upcoming[0] if upcoming else None,
        }


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["action"] = loads(item.get("action"), {})
    item["enabled"] = bool(item.get("enabled"))
    return item
