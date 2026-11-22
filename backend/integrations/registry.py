"""Integration registry: discovery, status persistence and health rollup."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso
from ..db.database import dumps, loads
from .base import Integration, Status
from .connectors import (
    EmailIntegration,
    GitHubIntegration,
    GoogleCalendarIntegration,
    HomeAssistantIntegration,
    SearchIntegration,
    SpotifyIntegration,
    TeamsIntegration,
    WeatherIntegration,
    ZoomIntegration,
)

logger = logging.getLogger("jarvis.integrations")


class IntegrationRegistry:
    def __init__(self, db, settings, activity, audit=None):
        self.db = db
        self.settings = settings
        self.activity = activity
        self.audit = audit
        self._integrations: Dict[str, Integration] = {}
        self._status: Dict[str, Dict[str, Any]] = {}

    # ==================================================================
    def build(self, weather_service=None, web_service=None, email_account=None) -> None:
        connectors: List[Integration] = [
            WeatherIntegration(self.settings, self.db, weather_service),
            SearchIntegration(self.settings, self.db, web_service),
            GitHubIntegration(self.settings, self.db),
            HomeAssistantIntegration(self.settings, self.db),
            GoogleCalendarIntegration(self.settings, self.db),
            EmailIntegration(self.settings, self.db, email_account),
            SpotifyIntegration(self.settings, self.db),
            ZoomIntegration(self.settings, self.db),
            TeamsIntegration(self.settings, self.db),
        ]
        self._integrations = {c.key: c for c in connectors}
        logger.info("registered %s integrations", len(self._integrations))

    def get(self, key: str) -> Optional[Integration]:
        return self._integrations.get(key)

    def keys(self) -> List[str]:
        return sorted(self._integrations)

    # ==================================================================
    async def bootstrap(self, probe: bool = True) -> Dict[str, Any]:
        """Ensure a row exists for every connector and seed status from the last
        run. Live probes hit the network, so by default they are kicked off in
        the background rather than holding up startup."""
        existing = {r["key"] for r in await self.db.fetch_all("SELECT key FROM integrations")}
        rows = [
            (key, 0, "", dumps({}), None, Status.NEEDS_CONFIG, "", now_iso())
            for key in self._integrations
            if key not in existing
        ]
        if rows:
            await self.db.execute_many(
                "INSERT INTO integrations (key, connected, account, config, last_sync, status, error,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )

        # Seed from persisted state so the UI has something truthful immediately.
        for row in await self.db.fetch_all("SELECT key, status, error, account FROM integrations"):
            if row["key"] in self._integrations:
                self._status[row["key"]] = {
                    "status": row["status"],
                    "message": row["error"] or "Last known state; re-probing.",
                    "account": row["account"],
                    "stale": True,
                }
        # Connectors with missing credentials can be judged with no network at all.
        for key, integration in self._integrations.items():
            if not integration.implemented:
                self._status[key] = {
                    "status": Status.NOT_IMPLEMENTED,
                    "message": f"{integration.name} needs an OAuth application you register yourself.",
                }
            elif integration.missing_env():
                self._status[key] = {
                    "status": Status.NEEDS_CONFIG,
                    "message": f"Missing credentials: {', '.join(integration.missing_env())}.",
                    "missing": integration.missing_env(),
                }

        if probe:
            asyncio.create_task(self._background_probe(), name="integration-probe")
        return await self.health()

    async def _background_probe(self) -> None:
        try:
            await self.refresh_all()
            summary = await self.health()
            logger.info(
                "integration probe complete: %s connected, %s need credentials",
                summary["connected"], summary["available"],
            )
            bus.publish(Events.INTEGRATION_CHANGED, {"key": "*", **summary}, source="integrations")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background integration probe failed")

    async def refresh_all(self) -> Dict[str, Dict[str, Any]]:
        results = await asyncio.gather(
            *(self._refresh(key) for key in self._integrations), return_exceptions=True
        )
        for key, result in zip(self._integrations, results):
            if isinstance(result, Exception):
                logger.warning("integration %s refresh raised: %s", key, result)
        return self._status

    async def _refresh(self, key: str) -> Dict[str, Any]:
        integration = self._integrations[key]
        try:
            result = await asyncio.wait_for(integration.test(), timeout=12.0)
        except asyncio.TimeoutError:
            result = {"status": Status.ERROR, "message": "Probe timed out."}
        except Exception as exc:
            result = {"status": Status.ERROR, "message": f"{type(exc).__name__}: {exc}"}

        status = result.get("status", Status.ERROR)
        await self.db.execute(
            "UPDATE integrations SET connected = ?, status = ?, error = ?, account = ?, updated_at = ?"
            " WHERE key = ?",
            (
                1 if status == Status.CONNECTED else 0,
                status,
                "" if status == Status.CONNECTED else result.get("message", "")[:500],
                result.get("account", ""),
                now_iso(),
                key,
            ),
        )
        self._status[key] = result
        return result

    async def refresh(self, key: str) -> Dict[str, Any]:
        if key not in self._integrations:
            raise KeyError(f"Unknown integration '{key}'.")
        result = await self._refresh(key)
        bus.publish(Events.INTEGRATION_CHANGED, {"key": key, **result}, source="integrations")
        return result

    # ==================================================================
    def is_connected(self, key: str) -> bool:
        return self._status.get(key, {}).get("status") == Status.CONNECTED

    def status_of(self, key: str) -> Dict[str, Any]:
        return self._status.get(key, {"status": Status.NEEDS_CONFIG, "message": "Not probed yet."})

    async def list(self) -> List[Dict[str, Any]]:
        rows = {r["key"]: r for r in await self.db.fetch_all("SELECT * FROM integrations")}
        out = []
        for key, integration in sorted(self._integrations.items()):
            row = rows.get(key, {})
            live = self._status.get(key, {})
            out.append(
                {
                    **integration.describe(),
                    "connected": live.get("status") == Status.CONNECTED,
                    "status": live.get("status", row.get("status", Status.NEEDS_CONFIG)),
                    "message": live.get("message", row.get("error", "")),
                    "account": live.get("account", row.get("account", "")),
                    "last_sync": row.get("last_sync"),
                    "config": loads(row.get("config"), {}),
                }
            )
        return out

    async def sync(self, key: str) -> Dict[str, Any]:
        integration = self._integrations.get(key)
        if integration is None:
            raise KeyError(f"Unknown integration '{key}'.")
        if not self.is_connected(key):
            return {"status": "skipped", "message": f"{integration.name} is not connected."}
        result = await integration.sync()
        await self.db.execute(
            "UPDATE integrations SET last_sync = ? WHERE key = ?", (now_iso(), key)
        )
        await self.activity.record(f"{integration.name} synchronised.", source="integrations")
        return result

    async def disconnect(self, key: str) -> Dict[str, Any]:
        """Clears the stored config. Credentials in .env are the user's to
        remove -- we say so rather than pretending we deleted them."""
        integration = self._integrations.get(key)
        if integration is None:
            raise KeyError(f"Unknown integration '{key}'.")
        # Credentials the connector keeps itself (the email password) go too:
        # "Disconnect" that leaves them working would only look disconnected.
        forgot = await integration.forget()
        await self.db.execute(
            "UPDATE integrations SET connected = 0, config = '{}', account = '', status = ?,"
            " updated_at = ? WHERE key = ?",
            (Status.NEEDS_CONFIG, now_iso(), key),
        )
        self._status[key] = {"status": Status.NEEDS_CONFIG, "message": "Disconnected locally."}
        if self.audit:
            await self.audit.record("integration.disconnected", subject=key)
        bus.publish(Events.INTEGRATION_CHANGED, {"key": key, "status": Status.NEEDS_CONFIG},
                    source="integrations")
        note = " The saved credentials were deleted." if forgot else ""
        if integration.required_env:
            note = (
                f" The credentials themselves live in your .env file "
                f"({', '.join(integration.required_env)}) -- remove them there to revoke access fully."
            )
        return {"key": key, "disconnected": True, "message": f"{integration.name} disconnected.{note}"}

    # ==================================================================
    async def health(self) -> Dict[str, Any]:
        by_key = {}
        connected = available = broken = 0
        for key, integration in self._integrations.items():
            status = self._status.get(key, {}).get("status", Status.NEEDS_CONFIG)
            by_key[key] = status
            if status == Status.CONNECTED:
                connected += 1
            elif status == Status.ERROR:
                broken += 1
            elif status == Status.NEEDS_CONFIG:
                available += 1
        return {
            "connected": connected,
            "available": available,
            "errors": broken,
            "total": len(self._integrations),
            "by_key": by_key,
        }

    async def close(self) -> None:
        for integration in self._integrations.values():
            await integration.close()
