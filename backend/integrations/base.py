"""
Integration framework.

Every connector declares what it can do and what it needs. The UI renders that
declaration directly, so an integration that lacks credentials shows exactly
which environment variables are missing and where to get them -- rather than a
button that looks live and silently does nothing.

Status vocabulary:
    connected       credentials present and the last probe succeeded
    needs_config    the connector works, but required credentials are missing
    error           credentials present but the provider rejected or failed
    not_implemented the connector is a declared surface with no working backend
"""
from __future__ import annotations

import abc
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.integrations")


class Status:
    CONNECTED = "connected"
    NEEDS_CONFIG = "needs_config"
    ERROR = "error"
    NOT_IMPLEMENTED = "not_implemented"


class Capability:
    def __init__(self, name: str, description: str, implemented: bool = True):
        self.name = name
        self.description = description
        self.implemented = implemented

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "implemented": self.implemented}


class Integration(abc.ABC):
    key: str = ""
    name: str = ""
    description: str = ""
    category: str = "general"
    docs_url: str = ""
    # Environment variables the connector needs, with where to get them.
    required_env: Dict[str, str] = {}
    optional_env: Dict[str, str] = {}
    capabilities: List[Capability] = []
    # True when the connector has a real backend implementation.
    implemented: bool = True

    def __init__(self, settings, db=None):
        self.settings = settings
        self.db = db
        self._client = None

    # ------------------------------------------------------------------
    def missing_env(self) -> List[str]:
        import os

        return [name for name in self.required_env if not os.getenv(name)]

    def configured(self) -> bool:
        return not self.missing_env()

    def http(self, **kwargs):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0, **kwargs)
        return self._client

    # ------------------------------------------------------------------
    async def test(self) -> Dict[str, Any]:
        """Probe the provider. Must never raise."""
        if not self.implemented:
            return {
                "status": Status.NOT_IMPLEMENTED,
                "message": f"{self.name} is declared but has no working backend in this build.",
            }
        missing = self.missing_env()
        if missing:
            return {
                "status": Status.NEEDS_CONFIG,
                "message": f"Missing credentials: {', '.join(missing)}.",
                "missing": missing,
            }
        try:
            return await self.probe()
        except Exception as exc:
            logger.warning("integration %s probe failed: %s", self.key, exc)
            return {"status": Status.ERROR, "message": f"{type(exc).__name__}: {exc}"}

    @abc.abstractmethod
    async def probe(self) -> Dict[str, Any]:
        """Provider-specific liveness check. Return {"status", "message", ...}."""

    async def forget(self) -> bool:
        """Delete credentials this connector stored itself (not .env).
        True if something was deleted. Called by Disconnect."""
        return False

    async def sync(self) -> Dict[str, Any]:
        """Optional: pull data into the local store."""
        return {"status": "noop", "message": f"{self.name} has nothing to sync."}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "docs_url": self.docs_url,
            "implemented": self.implemented,
            "required_env": self.required_env,
            "optional_env": self.optional_env,
            "missing_env": self.missing_env(),
            "capabilities": [c.to_dict() for c in self.capabilities],
        }
