"""
Unified notification centre.

One place for meetings, task completions, errors, system warnings, reminders
and automation events. Notification rules are honoured here rather than in the
UI, so "don't interrupt me during Focus mode unless it's critical" actually
suppresses delivery instead of merely hiding a badge.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..db.database import dumps, loads
from .events import Events, bus
from .util import ahead, new_id, now_iso, parse_iso, utcnow

logger = logging.getLogger("jarvis.notifications")

PRIORITIES = ["low", "normal", "high", "critical"]
_RANK = {name: index for index, name in enumerate(PRIORITIES)}


class NotificationService:
    def __init__(self, db, settings_store):
        self.db = db
        self.settings_store = settings_store

    # ------------------------------------------------------------------
    async def create(
        self,
        title: str,
        body: str = "",
        priority: str = "normal",
        source: str = "jarvis",
        action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        priority = priority if priority in _RANK else "normal"
        notification_id = new_id("ntf")
        await self.db.execute(
            "INSERT INTO notifications (id, ts, priority, title, body, source, action)"
            " VALUES (?,?,?,?,?,?,?)",
            (notification_id, now_iso(), priority, title, body, source, dumps(action or {})),
        )
        record = await self.get(notification_id)

        if await self._should_deliver(priority):
            bus.publish(Events.NOTIFICATION, record, source="notifications")
        else:
            logger.info("notification '%s' held back by the current mode's rules", title)
        return record

    async def _should_deliver(self, priority: str) -> bool:
        rules = await self.settings_store.get("notifications", {})
        if not rules.get("enabled", True):
            return False
        if _RANK[priority] < _RANK.get(rules.get("min_priority", "low"), 0):
            return False

        mode = await self.settings_store.get("active_mode", "normal")
        sleeping = await self.settings_store.get("sleeping", False)
        quiet_modes = set(rules.get("quiet_modes", []))
        allowed_in_quiet = set(rules.get("quiet_allows", ["critical"]))

        if (mode in quiet_modes or sleeping) and priority not in allowed_in_quiet:
            return False
        return True

    # ------------------------------------------------------------------
    async def get(self, notification_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM notifications WHERE id = ?", (notification_id,))
        return _decode(row) if row else {}

    async def list(
        self,
        include_dismissed: bool = False,
        limit: int = 100,
        priority: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM notifications WHERE 1=1"
        params: List[Any] = []
        if not include_dismissed:
            sql += " AND dismissed = 0"
            sql += " AND (snoozed_until IS NULL OR snoozed_until <= ?)"
            params.append(now_iso())
        if priority:
            sql += " AND priority = ?"
            params.append(priority)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [_decode(r) for r in await self.db.fetch_all(sql, params)]

    async def unread_count(self) -> int:
        return int(
            await self.db.fetch_value(
                "SELECT COUNT(*) FROM notifications WHERE read = 0 AND dismissed = 0", default=0
            )
        )

    # ------------------------------------------------------------------
    async def mark_read(self, notification_id: str) -> Dict[str, Any]:
        await self.db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
        return await self.get(notification_id)

    async def mark_all_read(self) -> int:
        return await self.db.execute("UPDATE notifications SET read = 1 WHERE read = 0")

    async def dismiss(self, notification_id: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE notifications SET dismissed = 1, read = 1 WHERE id = ?", (notification_id,)
        )
        return await self.get(notification_id)

    async def snooze(self, notification_id: str, minutes: int = 15) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE notifications SET snoozed_until = ? WHERE id = ?",
            (ahead(minutes * 60), notification_id),
        )
        return await self.get(notification_id)

    async def clear(self, before_iso: Optional[str] = None) -> int:
        if before_iso:
            return await self.db.execute("DELETE FROM notifications WHERE ts < ?", (before_iso,))
        return await self.db.execute("DELETE FROM notifications")

    # ------------------------------------------------------------------
    async def summarise(self, orchestrator=None) -> Dict[str, Any]:
        """'You have 12 notifications. 2 are important, 7 routine, 3 promotional.'"""
        items = await self.list(limit=50)
        if not items:
            return {"count": 0, "summary": "No pending notifications."}

        buckets: Dict[str, int] = {}
        for item in items:
            buckets[item["priority"]] = buckets.get(item["priority"], 0) + 1

        parts = [f"{count} {priority}" for priority, count in
                 sorted(buckets.items(), key=lambda kv: -_RANK[kv[0]])]
        summary = f"You have {len(items)} notification{'s' if len(items) != 1 else ''}: " + ", ".join(parts) + "."

        if orchestrator and len(items) >= 5:
            listing = "\n".join(f"- [{i['priority']}] {i['title']}: {i['body'][:120]}" for i in items[:20])
            try:
                summary = await orchestrator.ask(
                    f"Summarise these notifications for a spoken briefing in two or three sentences. "
                    f"Lead with anything urgent.\n\n{listing}",
                    purpose="fast",
                )
            except Exception:
                logger.debug("AI notification summary unavailable, using the counted summary")

        return {"count": len(items), "summary": summary, "by_priority": buckets, "items": items[:20]}


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["action"] = loads(item.get("action"), {})
    item["read"] = bool(item.get("read"))
    item["dismissed"] = bool(item.get("dismissed"))
    return item
