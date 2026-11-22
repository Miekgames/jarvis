"""
Capability permissions.

Every tool declares the permission it needs, and the gate is checked before
the tool runs.

**These are all granted.** The owner asked for every capability on and for
the switches to go, so `allowed()` returns True and the per-permission
toggles are no longer in the interface. The registry below stays, because it
is still what tells the rest of the system how dangerous each capability is
-- `risk_of()` drives the confirmation tier, and removing the definitions
would quietly downgrade every risky action to medium.

What that means for safety, stated plainly rather than left implied: the
first of the three layers is gone. Confirmations and the audit log remain,
and unattended work has its own hold queue in `backend.autonomy.guard`,
which is now the principal safeguard rather than a backstop. The riskiest
consequence is `shell.execute`, described below as "developer mode only" and
now always available.

`revoke_all()` is kept and still works. It is the one route back if this
turns out to have been the wrong call.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional

from .util import now_iso

logger = logging.getLogger("jarvis.permissions")


# When True every capability is granted regardless of the switches below.
# Off in this edition: each capability has its own switch in Settings, the
# defaults are the conservative ones in REGISTRY, and setup offers presets.
ALL_GRANTED = False


class Risk:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PermissionDef(NamedTuple):
    key: str
    label: str
    description: str
    risk: str
    default: bool


REGISTRY: List[PermissionDef] = [
    # -- filesystem ----------------------------------------------------------
    PermissionDef("files.read", "Read files", "Search, preview and read files inside the allowed roots.", Risk.LOW, True),
    PermissionDef("files.write", "Write files", "Create, rename, move and copy files.", Risk.MEDIUM, True),
    PermissionDef("files.delete", "Delete files", "Send files to a recoverable trash folder.", Risk.HIGH, False),

    # -- applications & processes -------------------------------------------
    PermissionDef("apps.launch", "Launch applications", "Open installed applications.", Risk.LOW, True),
    PermissionDef("apps.close", "Close applications", "Terminate a running application.", Risk.MEDIUM, False),

    # -- system --------------------------------------------------------------
    PermissionDef("system.info", "Read system state", "CPU, memory, disk, network, battery and process list.", Risk.LOW, True),
    PermissionDef("system.control", "Control the machine", "Volume, brightness and media keys.", Risk.MEDIUM, True),
    PermissionDef("system.screenshot", "Take screenshots", "Capture the screen to the local data folder.", Risk.MEDIUM, False),
    PermissionDef("system.power", "Power control", "Lock, sleep, restart or shut down the computer.", Risk.CRITICAL, False),
    PermissionDef("shell.execute", "Run shell commands", "Execute arbitrary commands. Developer mode only.", Risk.CRITICAL, False),
    PermissionDef("code.self_modify", "Edit its own code",
                  "Let JARVIS read and rewrite its own source using a dedicated code model. "
                  "Every change is diffed, syntax-checked, confirmed and backed up first.",
                  Risk.CRITICAL, False),

    # -- web -----------------------------------------------------------------
    PermissionDef("web.search", "Search the web", "Run web searches and read result snippets.", Risk.LOW, True),
    PermissionDef("web.fetch", "Open web pages", "Fetch and extract the text of a page.", Risk.LOW, True),

    # -- personal data -------------------------------------------------------
    PermissionDef("calendar.read", "Read calendar", "See events, meetings and availability.", Risk.LOW, True),
    PermissionDef("calendar.write", "Modify calendar", "Create, edit and cancel events.", Risk.MEDIUM, False),
    PermissionDef("email.read", "Read email", "Read, search and summarise messages.", Risk.MEDIUM, False),
    PermissionDef("email.send", "Send email", "Send messages on your behalf.", Risk.HIGH, False),
    PermissionDef("contacts.read", "Read contacts", "Look up people and their details.", Risk.LOW, True),
    PermissionDef("contacts.write", "Modify contacts", "Add and edit contact records.", Risk.MEDIUM, True),

    # -- communications ------------------------------------------------------
    PermissionDef("meetings.join", "Join meetings", "Open or join a meeting on your behalf.", Risk.HIGH, False),

    # -- home ----------------------------------------------------------------
    PermissionDef("smarthome.control", "Control smart home", "Lights, plugs, thermostats and scenes.", Risk.HIGH, False),
    PermissionDef("smarthome.security", "Locks and cameras", "Doors, locks and camera feeds.", Risk.CRITICAL, False),

    # -- assistant internals -------------------------------------------------
    PermissionDef("tasks.create", "Create background tasks", "Queue long-running work.", Risk.LOW, True),
    PermissionDef("automation.manage", "Manage automations", "Create, edit and run automations.", Risk.MEDIUM, True),
    PermissionDef("memory.write", "Write to memory", "Remember preferences and facts about you.", Risk.LOW, True),
    PermissionDef("notifications.read", "Read desktop notifications",
                  "Read the notifications Windows itself receives -- messages, "
                  "reminders, alerts from other applications. Their text is held "
                  "in memory only: never written to disk, never logged. Off by "
                  "default.",
                  Risk.HIGH, False),
    PermissionDef("media.control", "Control media", "Play, pause and skip playback.", Risk.LOW, True),
    PermissionDef("dev.workspace", "Read your code",
                  "Index and read authorised project folders: symbols, imports, git status.",
                  Risk.LOW, True),
    PermissionDef("dev.execute", "Run builds and tests",
                  "Execute the project's own test and build scripts. These run arbitrary "
                  "code defined in the project.", Risk.HIGH, False),
    PermissionDef("dev.git_write", "Write to git",
                  "Stage and commit changes in an authorised repository.", Risk.HIGH, False),
    PermissionDef("github.read", "Read GitHub", "Browse repositories, issues and pull requests.", Risk.LOW, True),
    PermissionDef("github.write", "Write to GitHub", "Create issues, comments and branches.", Risk.HIGH, False),
]

BY_KEY: Dict[str, PermissionDef] = {p.key: p for p in REGISTRY}


class PermissionDenied(Exception):
    def __init__(self, key: str):
        self.key = key
        definition = BY_KEY.get(key)
        label = definition.label if definition else key
        super().__init__(
            f"Permission '{label}' ({key}) is disabled. Enable it in Settings -> Permissions "
            f"if you want me to do this."
        )


class PermissionService:
    def __init__(self, db, audit=None):
        self.db = db
        self.audit = audit
        self._cache: Dict[str, bool] = {}

    async def bootstrap(self) -> None:
        """Insert any permission introduced since the last run, without
        clobbering choices the user has already made."""
        existing = {r["key"] for r in await self.db.fetch_all("SELECT key FROM permissions")}
        rows = [
            (p.key, 1 if p.default else 0, p.label, p.description, p.risk, now_iso())
            for p in REGISTRY
            if p.key not in existing
        ]
        if rows:
            await self.db.execute_many(
                "INSERT INTO permissions (key, enabled, label, description, risk, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                rows,
            )
            logger.info("registered %s new permissions", len(rows))
        # Refresh labels/descriptions so wording changes ship without a migration.
        await self.db.execute_many(
            "UPDATE permissions SET label = ?, description = ?, risk = ? WHERE key = ?",
            [(p.label, p.description, p.risk, p.key) for p in REGISTRY],
        )
        await self.refresh()

    async def refresh(self) -> None:
        rows = await self.db.fetch_all("SELECT key, enabled FROM permissions")
        self._cache = {r["key"]: bool(r["enabled"]) for r in rows}

    # ------------------------------------------------------------------
    def allowed(self, key: str) -> bool:
        """Whether a capability may be used.

        Always True while ALL_GRANTED stands. The cache is still maintained
        and `revoke_all()` still writes to it, so flipping this constant
        back restores the original behaviour without any other change.
        """
        if not key:
            return True
        if ALL_GRANTED:
            return True
        return self._cache.get(key, False)

    def require(self, key: str) -> None:
        if not self.allowed(key):
            raise PermissionDenied(key)

    def risk_of(self, key: str) -> str:
        definition = BY_KEY.get(key)
        return definition.risk if definition else Risk.MEDIUM

    # ------------------------------------------------------------------
    async def set(self, key: str, enabled: bool, actor: str = "user") -> Dict[str, Any]:
        if key not in BY_KEY:
            raise KeyError(f"unknown permission '{key}'")
        await self.db.execute(
            "UPDATE permissions SET enabled = ?, updated_at = ? WHERE key = ?",
            (1 if enabled else 0, now_iso(), key),
        )
        self._cache[key] = enabled
        if self.audit:
            await self.audit.record(
                "permission.changed",
                subject=key,
                detail={"enabled": enabled, "actor": actor, "risk": self.risk_of(key)},
            )
        logger.info("permission %s -> %s", key, "enabled" if enabled else "disabled")
        return await self.get(key)

    async def set_many(self, values: Dict[str, bool], actor: str = "user") -> List[Dict[str, Any]]:
        out = []
        for key, enabled in values.items():
            if key in BY_KEY:
                out.append(await self.set(key, bool(enabled), actor))
        return out

    async def revoke_all(self, actor: str = "user") -> int:
        await self.db.execute("UPDATE permissions SET enabled = 0, updated_at = ?", (now_iso(),))
        await self.refresh()
        if self.audit:
            await self.audit.record("permission.revoke_all", subject="*", detail={"actor": actor})
        return len(REGISTRY)

    async def get(self, key: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM permissions WHERE key = ?", (key,))
        return dict(row) if row else {}

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM permissions ORDER BY key")
        out = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["group"] = item["key"].split(".")[0]
            out.append(item)
        return out
