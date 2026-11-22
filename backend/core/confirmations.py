"""
Risk-tiered confirmation flow.

"Open Discord" runs immediately. "Send this email" shows the draft and waits.
"Delete these files" spells out exactly what will happen and waits.

A pending confirmation is a real row in the database plus an awaited future, so
the tool call genuinely blocks until the user answers -- it never proceeds
optimistically and it never claims success it didn't get.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .events import Events, bus
from .permissions import Risk
from .util import new_id, now_iso

logger = logging.getLogger("jarvis.confirm")

DEFAULT_TIMEOUT = 180.0

# Which risk tiers require an explicit yes. Overridable per tier in settings.
DEFAULT_POLICY = {
    Risk.LOW: False,
    Risk.MEDIUM: False,
    Risk.HIGH: True,
    Risk.CRITICAL: True,
}


class ConfirmationTimeout(Exception):
    pass


class ConfirmationService:
    def __init__(self, db, audit=None, settings_store=None):
        self.db = db
        self.audit = audit
        self.settings_store = settings_store
        self._pending: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    async def policy(self) -> Dict[str, bool]:
        if self.settings_store is None:
            return dict(DEFAULT_POLICY)
        stored = await self.settings_store.get("confirmation_policy", None)
        policy = dict(DEFAULT_POLICY)
        if isinstance(stored, dict):
            for tier, value in stored.items():
                if tier in policy:
                    policy[tier] = bool(value)
        return policy

    async def needs_confirmation(self, risk: str) -> bool:
        return (await self.policy()).get(risk, True)

    # ------------------------------------------------------------------
    async def request(
        self,
        tool: str,
        summary: str,
        detail: Optional[Dict[str, Any]] = None,
        risk: str = Risk.MEDIUM,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bool:
        """Block until the user approves or rejects. Returns the decision."""
        if not await self.needs_confirmation(risk):
            return True

        confirmation_id = new_id("cfm")
        await self.db.execute(
            "INSERT INTO confirmations (id, ts, tool, summary, detail, risk, status, conversation_id, task_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                confirmation_id,
                now_iso(),
                tool,
                summary,
                json.dumps(detail or {}, default=str),
                risk,
                "pending",
                conversation_id,
                task_id,
            ),
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[confirmation_id] = future

        bus.publish(
            Events.CONFIRMATION_REQUESTED,
            {
                "id": confirmation_id,
                "tool": tool,
                "summary": summary,
                "detail": detail or {},
                "risk": risk,
                "conversation_id": conversation_id,
                "task_id": task_id,
                "timeout": timeout,
            },
            source="confirm",
        )
        logger.info("awaiting confirmation %s for %s (%s)", confirmation_id, tool, risk)

        try:
            approved = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self._finalise(confirmation_id, "expired")
            logger.info("confirmation %s expired", confirmation_id)
            return False
        except asyncio.CancelledError:
            await self._finalise(confirmation_id, "cancelled")
            raise
        finally:
            self._pending.pop(confirmation_id, None)

        return bool(approved)

    # ------------------------------------------------------------------
    async def resolve(self, confirmation_id: str, approved: bool, actor: str = "user") -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,))
        if row is None:
            raise KeyError(f"unknown confirmation '{confirmation_id}'")
        if row["status"] != "pending":
            return dict(row)

        status = "approved" if approved else "rejected"
        await self._finalise(confirmation_id, status)

        if self.audit:
            await self.audit.record(
                f"confirmation.{status}",
                subject=row["tool"],
                detail={"summary": row["summary"], "risk": row["risk"], "actor": actor},
            )

        future = self._pending.get(confirmation_id)
        if future is not None and not future.done():
            future.set_result(approved)

        bus.publish(
            Events.CONFIRMATION_RESOLVED,
            {"id": confirmation_id, "status": status, "tool": row["tool"]},
            source="confirm",
        )
        return await self.get(confirmation_id)

    async def _finalise(self, confirmation_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE confirmations SET status = ?, resolved_at = ? WHERE id = ?",
            (status, now_iso(), confirmation_id),
        )

    # ------------------------------------------------------------------
    async def get(self, confirmation_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,))
        return _decode(row) if row else {}

    async def pending(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM confirmations WHERE status = 'pending' ORDER BY ts DESC"
        )
        return [_decode(r) for r in rows]

    async def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM confirmations ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [_decode(r) for r in rows]

    async def expire_stale(self) -> int:
        """On boot, nothing can still be waiting -- the futures died with the
        old process, so leaving rows 'pending' would be a lie."""
        count = await self.db.execute(
            "UPDATE confirmations SET status = 'expired', resolved_at = ? WHERE status = 'pending'",
            (now_iso(),),
        )
        if count:
            logger.info("expired %s stale confirmations from a previous run", count)
        return count

    def cancel_all(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(False)
        self._pending.clear()


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["detail"] = json.loads(item.get("detail") or "{}")
    except Exception:
        item["detail"] = {}
    return item
