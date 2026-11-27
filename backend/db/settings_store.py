"""
Runtime preferences (as opposed to process configuration in backend/config.py).

Anything the user can change from the UI without restarting lives here: theme,
voice preferences, confirmation policy, notification rules, dashboard layout,
active mode. Values are JSON so callers get real types back.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import now_iso
from .database import dumps, loads

logger = logging.getLogger("jarvis.settings")


DEFAULTS: Dict[str, Any] = {
    # -- presentation --------------------------------------------------------
    "theme": {
        "preset": "arc_reactor",
        "accent": "#5ad2ff",
        "accent_secondary": "#7b6cff",
        "particle_near": "#3890d9",
        "particle_far": "#17336b",
        "backdrop": "#071322",
        "density": "comfortable",
        "reduced_motion": False,
        "font_scale": 1.0,
        "high_contrast": False,
        # The screen fades to a skill's own colour while it's in use.
        "skill_colors": True,
    },
    # Which local model rewrites JARVIS's own code ("use mistral for
    # self-editing"). Empty: the best installed coder that isn't the chat model.
    "selfedit": {
        "model": "",
        "provider": "",
    },
    "particles": {
        "count": 4200,
        "quality": "auto",          # auto | high | balanced | low
        "connections": True,
        "glow": 1.0,
        "trail": 0.88,
    },
    # -- assistant behaviour -------------------------------------------------
    "personality": {
        "name": "JARVIS",
        "address_user_as": "",
        "tone": "composed",         # composed | warm | terse | playful
        "formality": "formal",
        "humour": 0.2,
        "response_length": "concise",
        "custom_instructions": "",
        "emojis": True,             # an emoji now and then where it adds meaning
    },
    "ai": {
        "route": {
            "conversation": "primary",
            "fast": "fast",
            "coding": "coding",
            "reasoning": "reasoning",
            "vision": "vision",
        },
        "fallback_enabled": True,
        "streaming": True,
        "temperature": 0.7,
        "memory_enabled": True,
        # Keep durable facts the owner states, without being asked to.
        "learn_automatically": True,
        # An OpenAI-compatible local server (LM Studio, llama.cpp, vLLM...)
        # used ahead of Ollama when enabled.
        "local_server": {"enabled": False, "base_url": "", "model": "", "label": ""},
        "max_tool_iterations": 12,
    },
    "voice": {
        "enabled": True,
        "tts_engine": "edge",        # edge | browser | off
        "voice": "en-GB-RyanNeural",
        "rate": 1.0,
        "volume": 1.0,
        "stt_engine": "browser",     # browser | whisper
        "wake_word_enabled": False,
        "push_to_talk_key": "Space",
        "interruptible": True,
        "continuous": False,
    },
    # -- safety --------------------------------------------------------------
    "confirmation_policy": {"low": False, "medium": False, "high": True, "critical": True},
    "privacy": {
        "privacy_mode": False,
        "cloud_ai": True,
        "memory": True,
        "activity_tracking": True,
        "voice": True,
    },
    # -- notifications -------------------------------------------------------
    "notifications": {
        "enabled": True,
        "sound": False,
        "min_priority": "low",
        "quiet_modes": ["sleep", "focus", "meeting"],
        "quiet_allows": ["critical"],
    },
    # -- system --------------------------------------------------------------
    "monitoring": {
        "enabled": True,
        "cpu_alert": 92,
        "mem_alert": 92,
        "disk_alert": 94,
        "gpu_temp_alert": 84,
        "sustained_seconds": 300,
    },
    "briefing": {
        "enabled": False,
        "time": "08:00",
        "include": ["time", "weather", "calendar", "tasks", "notifications", "system"],
    },
    "cost": {"monthly_limit_usd": 0, "warn_at_percent": 80},
    "shortcuts": {
        "command_bar": "ctrl+space",
        "push_to_talk": "ctrl+shift+space",
        "sleep": "ctrl+shift+s",
        "stop_all": "ctrl+shift+x",
        "history": "ctrl+h",
    },
    # -- the intelligence layer ---------------------------------------------
    "awareness": {
        # Ambient tracking of the focused app and idle state. Local only.
        "enabled": True,
        # Learn command frequency and sequences to predict what you'll want.
        "learning": True,
        # Offer suggestions unprompted. Off by default -- this one is opinionated,
        # so it must be chosen deliberately.
        "proactive": False,
        "max_suggestions_per_hour": 3,
        # Folders whose changes fire the file.changed automation trigger.
        "watch_folders": [],
        # Let JARVIS look at the screen when asked. Still gated by the
        # system.screenshot permission and a confirmation prompt.
        "vision": True,
        "screenshot_retention_days": 7,
    },
    "gaming": {
        "enabled": True,
        "auto_mode": False,          # switch to gaming mode when a game starts
        "heuristic_detection": True,  # spot unlisted games by resource profile
        "known_games": [],
        "ignore": [],
    },
    "active_mode": "normal",
    "sleeping": False,
}


class SettingsStore:
    def __init__(self, db):
        self.db = db
        self._cache: Dict[str, Any] = {}

    async def bootstrap(self) -> None:
        rows = await self.db.fetch_all("SELECT key, value FROM settings")
        stored = {r["key"]: loads(r["value"]) for r in rows}
        missing = []
        for key, value in DEFAULTS.items():
            if key not in stored:
                missing.append((key, dumps(value), now_iso()))
                stored[key] = value
            elif isinstance(value, dict) and isinstance(stored[key], dict):
                # Merge in keys added by a newer version without discarding
                # anything the user has already set.
                merged = {**value, **stored[key]}
                if merged != stored[key]:
                    await self.db.execute(
                        "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
                        (dumps(merged), now_iso(), key),
                    )
                stored[key] = merged
        if missing:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?,?,?)", missing
            )
        self._cache = stored
        logger.info("settings loaded (%s keys)", len(self._cache))

    # ------------------------------------------------------------------
    async def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        row = await self.db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return DEFAULTS.get(key, default)
        value = loads(row["value"], default)
        self._cache[key] = value
        return value

    def get_cached(self, key: str, default: Any = None) -> Any:
        """Synchronous read for hot paths (tool gating, particle state)."""
        return self._cache.get(key, DEFAULTS.get(key, default))

    async def set(self, key: str, value: Any, actor: str = "user") -> Any:
        await self.db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, dumps(value), now_iso()),
        )
        self._cache[key] = value
        bus.publish("settings.changed", {"key": key, "value": value, "actor": actor}, source="settings")
        return value

    async def patch(self, key: str, updates: Dict[str, Any], actor: str = "user") -> Any:
        """Shallow-merge into a dict setting."""
        current = await self.get(key, {})
        if not isinstance(current, dict):
            return await self.set(key, updates, actor)
        merged = {**current, **updates}
        return await self.set(key, merged, actor)

    async def all(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all("SELECT key, value FROM settings ORDER BY key")
        out = {**DEFAULTS}
        for row in rows:
            out[row["key"]] = loads(row["value"])
        self._cache = out
        return out

    async def reset(self, key: Optional[str] = None) -> Dict[str, Any]:
        if key:
            value = DEFAULTS.get(key)
            await self.set(key, value, actor="reset")
            return {key: value}
        await self.db.execute("DELETE FROM settings")
        self._cache = {}
        await self.bootstrap()
        return dict(self._cache)

    async def keys(self) -> List[str]:
        return sorted(set(DEFAULTS) | {r["key"] for r in await self.db.fetch_all("SELECT key FROM settings")})
