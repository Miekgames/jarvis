"""
JARVIS modes.

A mode is a named bundle of overrides applied on top of the base settings:
notification rules, voice, AI routing, permissions to soften or tighten,
applications to launch, automations to run. Switching modes is a single action
that everything else -- notifications, the particle field, the AI's context --
already respects.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.modes")


BUILTIN_MODES: Dict[str, Dict[str, Any]] = {
    "normal": {
        "name": "Normal",
        "icon": "circle",
        "config": {"description": "Everything on, nothing suppressed."},
    },
    "focus": {
        "name": "Focus",
        "icon": "target",
        "config": {
            "description": "Only critical interruptions get through.",
            "settings": {"notifications": {"min_priority": "critical"}},
            "particles": {"quality": "balanced"},
        },
    },
    "work": {
        "name": "Work",
        "icon": "briefcase",
        "config": {
            "description": "Normal notifications, calendar-aware, primary model.",
            "settings": {"notifications": {"min_priority": "normal"}, "ai": {"route": {"conversation": "primary"}}},
        },
    },
    "gaming": {
        "name": "Gaming",
        "icon": "gamepad",
        "config": {
            "description": "Low visual overhead, high-priority alerts only.",
            "settings": {
                "notifications": {"min_priority": "high"},
                "particles": {"count": 1800, "quality": "low", "connections": False},
            },
        },
    },
    "study": {
        "name": "Study",
        "icon": "book",
        "config": {
            "description": "Quiet, reasoning-first, timers encouraged.",
            "settings": {
                "notifications": {"min_priority": "high"},
                "ai": {"route": {"conversation": "reasoning"}},
            },
        },
    },
    "meeting": {
        "name": "Meeting",
        "icon": "users",
        "config": {
            "description": "Voice muted, notifications held, nothing spoken aloud.",
            "settings": {
                "notifications": {"min_priority": "critical"},
                "voice": {"enabled": False, "wake_word_enabled": False},
            },
        },
    },
    "away": {
        "name": "Away",
        "icon": "moon",
        "config": {
            "description": "Background work continues, nothing is spoken.",
            "settings": {"voice": {"enabled": False}},
        },
    },
    "developer": {
        "name": "Developer",
        "icon": "terminal",
        "config": {
            "description": "Coding model, verbose logs, developer panels visible.",
            "settings": {"ai": {"route": {"conversation": "coding"}}},
        },
    },
    "privacy": {
        "name": "Privacy",
        "icon": "shield",
        "config": {
            "description": "Local model only, memory paused, no external integrations.",
            "settings": {
                "privacy": {"privacy_mode": True, "cloud_ai": False, "memory": False,
                            "activity_tracking": False},
            },
        },
    },
    "sleep": {
        "name": "Sleep",
        "icon": "power",
        "config": {
            "description": "Display black, rendering paused, background services running.",
            "settings": {"notifications": {"min_priority": "critical"}},
        },
    },
}


class ModeService:
    def __init__(self, db, settings_store, activity, actions=None):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self.actions = actions

    # ------------------------------------------------------------------
    async def bootstrap(self) -> None:
        existing = {r["key"] for r in await self.db.fetch_all("SELECT key FROM modes")}
        rows = [
            (key, spec["name"], spec.get("icon", ""), dumps(spec["config"]), 1, now_iso())
            for key, spec in BUILTIN_MODES.items()
            if key not in existing
        ]
        if rows:
            await self.db.execute_many(
                "INSERT INTO modes (key, name, icon, config, builtin, created_at) VALUES (?,?,?,?,?,?)",
                rows,
            )
            logger.info("registered %s built-in modes", len(rows))

    # ------------------------------------------------------------------
    async def active(self) -> str:
        return await self.settings_store.get("active_mode", "normal")

    async def activate(self, key: str) -> Dict[str, Any]:
        mode = await self.get(key)
        if not mode:
            raise KeyError(f"Unknown mode '{key}'.")

        previous = await self.active()
        await self.settings_store.set("active_mode", key, actor="mode")

        # Apply the mode's setting overrides.
        overrides = (mode.get("config") or {}).get("settings") or {}
        for setting_key, values in overrides.items():
            if isinstance(values, dict):
                await self.settings_store.patch(setting_key, values, actor=f"mode:{key}")
            else:
                await self.settings_store.set(setting_key, values, actor=f"mode:{key}")

        # Run any actions the mode defines (launch apps, start automations...).
        action_list = (mode.get("config") or {}).get("actions") or []
        results = []
        if action_list and self.actions:
            results = await self.actions.run_all(action_list, context={"mode": key, "source": "mode"})

        bus.publish(Events.MODE_CHANGED, {"mode": key, "previous": previous, "name": mode["name"]},
                    source="modes")
        await self.activity.record(f"Mode changed: {previous} -> {mode['name']}", source="modes")
        logger.info("mode %s -> %s", previous, key)
        return {**mode, "previous": previous, "action_results": results}

    # ------------------------------------------------------------------
    async def get(self, key: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM modes WHERE key = ?", (key,))
        return _decode(row) if row else {}

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM modes ORDER BY builtin DESC, name")
        active = await self.active()
        return [{**_decode(r), "active": r["key"] == active} for r in rows]

    async def create(self, key: str, name: str, config: Dict[str, Any], icon: str = "") -> Dict[str, Any]:
        await self.db.execute(
            "INSERT INTO modes (key, name, icon, config, builtin, created_at) VALUES (?,?,?,?,0,?)"
            " ON CONFLICT(key) DO UPDATE SET name = excluded.name, icon = excluded.icon,"
            " config = excluded.config",
            (key, name, icon, dumps(config), now_iso()),
        )
        return await self.get(key)

    async def update(self, key: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"name", "icon", "config"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "config" in updates:
            updates["config"] = dumps(updates["config"])
        if not updates:
            return await self.get(key)
        assignments = ", ".join(f"{field} = ?" for field in updates)
        await self.db.execute(f"UPDATE modes SET {assignments} WHERE key = ?", (*updates.values(), key))
        return await self.get(key)

    async def delete(self, key: str) -> bool:
        mode = await self.get(key)
        if mode.get("builtin"):
            raise PermissionError(f"'{key}' is a built-in mode and can't be deleted.")
        if await self.active() == key:
            await self.activate("normal")
        return await self.db.execute("DELETE FROM modes WHERE key = ?", (key,)) > 0


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["config"] = loads(item.get("config"), {})
    item["builtin"] = bool(item.get("builtin"))
    return item
