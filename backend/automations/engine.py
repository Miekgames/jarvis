"""
Automation engine: TRIGGER -> CONDITIONS -> ACTIONS.

Automations subscribe to the event bus, so anything the system publishes can be
a trigger. Conditions are evaluated against both the event payload and live
state (time of day, active mode, metrics), and every run -- fired, skipped or
failed -- is recorded so the user can see why something did or didn't happen.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.events import Event, Events, bus
from ..core.util import as_float, new_id, now_iso, parse_iso, utcnow
from ..db.database import dumps, loads
from .actions import describe_action

logger = logging.getLogger("jarvis.automations")


TRIGGER_KINDS = {
    "event": "Any system event (task.completed, notification.new, system.alert, ...)",
    "metric": "A system metric crossing a threshold",
    "calendar_before": "N minutes before a calendar event starts",
    "mode_change": "When the active mode changes",
    "manual": "Only when run by hand",
}

CONDITION_KINDS = {
    "time_between": "Local time is between start and end (HH:MM)",
    "day_of_week": "Today is one of the listed days (0=Monday)",
    "mode_is": "The active mode is one of the listed modes",
    "metric_above": "A live metric is above a value",
    "metric_below": "A live metric is below a value",
    "field": "A field of the triggering event compares as specified",
    "not_sleeping": "JARVIS is awake",
}


class AutomationEngine:
    def __init__(self, db, activity, actions, settings_store, metrics=None):
        self.db = db
        self.activity = activity
        self.actions = actions
        self.settings_store = settings_store
        self.metrics = metrics
        self._unsubscribe = None
        self._last_fired: Dict[str, float] = {}
        self._running = False

    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._unsubscribe = bus.subscribe("*", self._on_event)
        self._running = True
        count = int(await self.db.fetch_value("SELECT COUNT(*) FROM automations WHERE enabled = 1", default=0))
        logger.info("automation engine started (%s enabled)", count)

    async def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # ==================================================================
    async def _on_event(self, event: Event) -> None:
        # Ignore our own events to avoid feedback loops.
        if event.type in (Events.AUTOMATION_FIRED, Events.ACTIVITY, Events.METRICS):
            if event.type != Events.METRICS:
                return

        rows = await self.db.fetch_all("SELECT * FROM automations WHERE enabled = 1")
        for row in rows:
            automation = _decode(row)
            try:
                if not self._trigger_matches(automation["trigger"], event):
                    continue
                if not await self._conditions_pass(automation["conditions"], event):
                    await self._record_run(automation["id"], "skipped",
                                           {"reason": "conditions not met", "event": event.type})
                    continue
                cooldown = int(automation.get("cooldown") or 0)
                if cooldown and time.monotonic() - self._last_fired.get(automation["id"], 0) < cooldown:
                    continue
                self._last_fired[automation["id"]] = time.monotonic()
                await self.fire(automation, event)
            except Exception:
                logger.exception("automation %s failed while handling %s", automation["id"], event.type)

    # ==================================================================
    def _trigger_matches(self, trigger: Dict[str, Any], event: Event) -> bool:
        kind = trigger.get("kind", "event")

        if kind == "manual":
            return False

        if kind == "event":
            pattern = trigger.get("event", "")
            if not pattern:
                return False
            if pattern.endswith(".*"):
                return event.type.startswith(pattern[:-1])
            return event.type == pattern

        if kind == "metric":
            if event.type != Events.METRICS:
                return False
            metric = trigger.get("metric", "cpu")
            value = event.data.get(metric)
            if value is None:
                return False
            threshold = as_float(trigger.get("value"), 0)
            return (
                float(value) > threshold if trigger.get("op", "above") == "above" else float(value) < threshold
            )

        if kind == "mode_change":
            return event.type == Events.MODE_CHANGED

        if kind == "calendar_before":
            # Driven by the calendar watcher, which publishes calendar.upcoming
            # with the lead time already computed.
            if event.type != "calendar.upcoming":
                return False
            lead = int(trigger.get("minutes", 15))
            return int(event.data.get("minutes_until", -1)) <= lead

        return False

    async def _conditions_pass(self, conditions: List[Dict[str, Any]], event: Event) -> bool:
        for condition in conditions or []:
            if not await self._evaluate(condition, event):
                return False
        return True

    async def _evaluate(self, condition: Dict[str, Any], event: Event) -> bool:
        kind = condition.get("kind", "")
        negate = bool(condition.get("negate"))
        result = await self._evaluate_inner(kind, condition, event)
        return (not result) if negate else result

    async def _evaluate_inner(self, kind: str, condition: Dict[str, Any], event: Event) -> bool:
        if kind == "time_between":
            now = datetime.now().time()
            start = _parse_time(condition.get("start", "00:00"))
            end = _parse_time(condition.get("end", "23:59"))
            if start <= end:
                return start <= now <= end
            return now >= start or now <= end  # window crosses midnight

        if kind == "day_of_week":
            days = condition.get("days") or []
            return datetime.now().weekday() in [int(d) for d in days]

        if kind == "mode_is":
            modes = condition.get("modes") or ([condition["mode"]] if condition.get("mode") else [])
            active = await self.settings_store.get("active_mode", "normal")
            return active in modes

        if kind in ("metric_above", "metric_below"):
            if self.metrics is None:
                return False
            latest = self.metrics.latest()
            value = latest.get(condition.get("metric", "cpu"))
            if value is None:
                return False
            threshold = as_float(condition.get("value"), 0)
            return float(value) > threshold if kind == "metric_above" else float(value) < threshold

        if kind == "field":
            value = event.data.get(condition.get("field", ""))
            expected = condition.get("value")
            operator = condition.get("op", "eq")
            return _compare(value, operator, expected)

        if kind == "not_sleeping":
            return not bool(await self.settings_store.get("sleeping", False))

        logger.warning("unknown condition kind '%s'", kind)
        return False

    # ==================================================================
    async def fire(self, automation: Dict[str, Any], event: Optional[Event] = None) -> Dict[str, Any]:
        context = {
            "automation_id": automation["id"],
            "automation_name": automation["name"],
            "event": event.type if event else "manual",
            "source": "automation",
        }
        logger.info("automation '%s' firing", automation["name"])
        bus.publish(
            Events.AUTOMATION_FIRED,
            {"id": automation["id"], "name": automation["name"], "trigger": event.type if event else "manual"},
            source="automations",
        )

        results = await self.actions.run_all(automation["actions"], context)
        failures = [r for r in results if r["status"] == "error"]
        status = "failed" if failures and len(failures) == len(results) else (
            "partial" if failures else "fired"
        )

        await self.db.execute(
            "UPDATE automations SET last_fired = ?, fire_count = fire_count + 1 WHERE id = ?",
            (now_iso(), automation["id"]),
        )
        await self._record_run(automation["id"], status, {"results": results})
        await self.activity.record(
            f"Automation '{automation['name']}' {status}"
            + (f" ({len(failures)} action(s) failed)" if failures else ""),
            level="error" if status == "failed" else "info",
            source="automations",
        )
        return {"status": status, "results": results}

    async def run_manually(self, automation_id: str) -> Dict[str, Any]:
        record = await self.get(automation_id)
        if not record:
            raise KeyError(automation_id)
        return await self.fire(record, None)

    async def _record_run(self, automation_id: str, status: str, detail: Dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO automation_runs (automation_id, ts, status, detail) VALUES (?,?,?,?)",
            (automation_id, now_iso(), status, dumps(detail)),
        )

    # ==================================================================
    # CRUD
    # ==================================================================
    async def create(
        self,
        name: str,
        trigger: Dict[str, Any],
        actions: List[Dict[str, Any]],
        conditions: Optional[List[Dict[str, Any]]] = None,
        description: str = "",
        enabled: bool = True,
        cooldown: int = 0,
    ) -> Dict[str, Any]:
        if not actions:
            raise ValueError("An automation needs at least one action.")
        automation_id = new_id("auto")
        await self.db.execute(
            "INSERT INTO automations (id, name, description, enabled, trigger, conditions, actions,"
            " created_at, cooldown) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                automation_id, name, description, 1 if enabled else 0,
                dumps(trigger), dumps(conditions or []), dumps(actions), now_iso(), cooldown,
            ),
        )
        await self.activity.record(f"Automation created: {name}", source="automations")
        return await self.get(automation_id)

    async def get(self, automation_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM automations WHERE id = ?", (automation_id,))
        return _decode(row) if row else {}

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM automations ORDER BY created_at DESC")
        return [_decode(r) for r in rows]

    async def update(self, automation_id: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"name", "description", "enabled", "trigger", "conditions", "actions", "cooldown"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get(automation_id)
        for json_field in ("trigger", "conditions", "actions"):
            if json_field in updates:
                updates[json_field] = dumps(updates[json_field])
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self.db.execute(
            f"UPDATE automations SET {assignments} WHERE id = ?", (*updates.values(), automation_id)
        )
        return await self.get(automation_id)

    async def delete(self, automation_id: str) -> bool:
        return await self.db.execute("DELETE FROM automations WHERE id = ?", (automation_id,)) > 0

    async def runs(self, automation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM automation_runs WHERE automation_id = ? ORDER BY id DESC LIMIT ?",
            (automation_id, limit),
        )
        for row in rows:
            row["detail"] = loads(row.get("detail"), {})
        return rows

    # ==================================================================
    @staticmethod
    def preview(automation: Dict[str, Any]) -> Dict[str, Any]:
        """Plain-English rendering shown before an automation is enabled."""
        trigger = automation.get("trigger", {})
        kind = trigger.get("kind", "event")
        if kind == "event":
            when = f"When the event '{trigger.get('event', '?')}' occurs"
        elif kind == "metric":
            when = f"When {trigger.get('metric', 'cpu')} goes {trigger.get('op', 'above')} {trigger.get('value')}"
        elif kind == "calendar_before":
            when = f"{trigger.get('minutes', 15)} minutes before a calendar event"
        elif kind == "mode_change":
            when = "When the active mode changes"
        else:
            when = "Only when run manually"

        conditions = automation.get("conditions") or []
        condition_text = [
            {
                "time_between": lambda c: f"the time is between {c.get('start')} and {c.get('end')}",
                "day_of_week": lambda c: f"it is a {', '.join(str(d) for d in c.get('days', []))} (0=Monday)",
                "mode_is": lambda c: f"the mode is {', '.join(c.get('modes', []) or [c.get('mode', '')])}",
                "metric_above": lambda c: f"{c.get('metric')} is above {c.get('value')}",
                "metric_below": lambda c: f"{c.get('metric')} is below {c.get('value')}",
                "field": lambda c: f"{c.get('field')} {c.get('op', 'eq')} {c.get('value')}",
                "not_sleeping": lambda c: "JARVIS is awake",
            }.get(c.get("kind", ""), lambda c: f"condition '{c.get('kind')}'")(c)
            for c in conditions
        ]

        return {
            "when": when,
            "conditions": condition_text,
            "actions": [describe_action(a) for a in automation.get("actions", [])],
            "summary": (
                f"{when}"
                + (f", and {' and '.join(condition_text)}" if condition_text else "")
                + ", then: "
                + "; ".join(describe_action(a) for a in automation.get("actions", []))
            ),
        }


# ---------------------------------------------------------------------------
def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["trigger"] = loads(item.get("trigger"), {})
    item["conditions"] = loads(item.get("conditions"), [])
    item["actions"] = loads(item.get("actions"), [])
    item["enabled"] = bool(item.get("enabled"))
    return item


def _parse_time(text: str):
    from datetime import time as time_cls

    try:
        hour, _, minute = str(text).partition(":")
        return time_cls(int(hour), int(minute or 0))
    except (ValueError, TypeError):
        return time_cls(0, 0)


def _compare(value: Any, operator: str, expected: Any) -> bool:
    try:
        if operator == "eq":
            return str(value) == str(expected)
        if operator == "ne":
            return str(value) != str(expected)
        if operator == "contains":
            return str(expected).lower() in str(value).lower()
        if operator == "gt":
            return float(value) > float(expected)
        if operator == "lt":
            return float(value) < float(expected)
        if operator == "in":
            return value in (expected or [])
    except (TypeError, ValueError):
        return False
    return False
