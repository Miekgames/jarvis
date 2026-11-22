"""Security audit trail -- separate from the activity feed on purpose.

The activity feed is chatty and user-facing. This is the tamper-evident record
of anything security-relevant: logins, permission changes, integration
changes, sensitive tool calls and confirmation decisions.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .util import now_iso, redact

logger = logging.getLogger("jarvis.audit")

SENSITIVE_EVENTS = {
    "auth.login",
    "auth.logout",
    "auth.logout_all",
    "permission.changed",
    "permission.revoke_all",
    "integration.connected",
    "integration.disconnected",
    "confirmation.approved",
    "confirmation.rejected",
    "tool.sensitive",
    "data.deleted",
    "data.exported",
    "settings.changed",
}


class AuditLog:
    def __init__(self, db):
        self.db = db

    async def record(
        self,
        event: str,
        subject: str = "",
        detail: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
        result: str = "ok",
        ip: str = "",
    ) -> None:
        payload = json.dumps(_scrub(detail or {}), default=str)
        await self.db.execute(
            "INSERT INTO audit_log (ts, user_id, event, subject, detail, result, ip)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_iso(), user_id, event, str(subject)[:200], payload, result, ip),
        )
        logger.info("audit %s %s -> %s", event, subject, result)

    async def recent(
        self, limit: int = 200, event: Optional[str] = None, result: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: List[Any] = []
        if event:
            sql += " AND event LIKE ?"
            params.append(f"{event}%")
        if result:
            sql += " AND result = ?"
            params.append(result)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(limit, 2000))
        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            try:
                row["detail"] = json.loads(row.get("detail") or "{}")
            except Exception:
                row["detail"] = {}
        return rows

    async def count(self) -> int:
        return int(await self.db.fetch_value("SELECT COUNT(*) FROM audit_log", default=0))


_SECRET_KEYS = ("password", "token", "secret", "api_key", "apikey", "authorization", "client_secret")


def _scrub(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Never let a credential into the audit table."""
    clean: Dict[str, Any] = {}
    for key, value in detail.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SECRET_KEYS):
            clean[key] = "<redacted>"
        elif isinstance(value, dict):
            clean[key] = _scrub(value)
        elif isinstance(value, str):
            clean[key] = redact(value)[:2000]
        else:
            clean[key] = value
    return clean
