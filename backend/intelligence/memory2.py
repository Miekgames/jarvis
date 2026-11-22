"""
Memory 2.0.

Adds to the Update 1 store, without changing how it already behaves:

  * **Scope** — short-term memory expires on its own; long-term persists.
    Project- and conversation-scoped memories only surface in their context.
  * **Provenance** — every memory records where it came from, so
    "why do you think that?" has an answer.
  * **Usage tracking** — what actually gets recalled, which drives cleanup.
  * **Natural-language search** — ask in a sentence, not keywords.
  * **Cleanup** — expired short-term entries, duplicates, and long-unused
    low-importance entries, each reported before anything is removed.

Nothing here hides memory from the user: every entry is a row they can read,
edit and delete.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..core.util import ahead, new_id, now_iso, parse_iso, since, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.memory2")

SCOPES = ("short_term", "long_term", "project", "conversation")

# How long a short-term memory lives unless promoted.
SHORT_TERM_HOURS = 12


class MemoryManager:
    """Wraps the existing MemoryStore with scope, provenance and cleanup."""

    def __init__(self, db, store, orchestrator=None, activity=None):
        self.db = db
        self.store = store
        self.orchestrator = orchestrator
        self.activity = activity

    # ==================================================================
    # writing
    # ==================================================================
    async def remember(
        self,
        value: str,
        category: str = "general",
        key: str = "",
        importance: int = 3,
        scope: str = "long_term",
        project_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        source: str = "jarvis",
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not await self.store.enabled():
            raise PermissionError("Memory is disabled in settings.")

        scope = scope if scope in SCOPES else "long_term"
        if scope == "project" and not project_id:
            scope = "long_term"
        if scope == "conversation" and not conversation_id:
            scope = "short_term"

        record = await self.store.remember(value, category, key, importance, source)

        expires = ahead(SHORT_TERM_HOURS * 3600) if scope == "short_term" else None
        await self.db.execute(
            "UPDATE memory SET scope = ?, project_id = ?, conversation_id = ?, expires_at = ?,"
            " provenance = ? WHERE id = ?",
            (scope, project_id, conversation_id, expires,
             dumps(provenance or {"source": source, "recorded_at": now_iso()}), record["id"]),
        )
        return await self.get(record["id"])

    async def promote(self, memory_id: str) -> Dict[str, Any]:
        """Short-term to long-term -- 'remember that permanently'."""
        await self.db.execute(
            "UPDATE memory SET scope = 'long_term', expires_at = NULL, updated_at = ?"
            " WHERE id = ?", (now_iso(), memory_id),
        )
        return await self.get(memory_id)

    # ==================================================================
    # reading
    # ==================================================================
    async def get(self, memory_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        return _decode(row) if row else {}

    async def recall(
        self,
        query: str,
        limit: int = 8,
        project_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Scope-aware recall: global memories always, plus the ones belonging
        to the project or conversation in play."""
        await self.expire()
        hits = await self.store.search(query, limit=limit * 3)

        scoped: List[Dict[str, Any]] = []
        for hit in hits:
            record = await self.get(hit["id"])
            if not record:
                continue
            scope = record.get("scope", "long_term")
            if scope == "project" and record.get("project_id") != project_id:
                continue
            if scope == "conversation" and record.get("conversation_id") != conversation_id:
                continue
            scoped.append(record)
            if len(scoped) >= limit:
                break

        for record in scoped:
            await self.db.execute(
                "UPDATE memory SET use_count = use_count + 1, last_used = ? WHERE id = ?",
                (now_iso(), record["id"]),
            )
        return scoped

    async def search_natural(self, question: str, limit: int = 10) -> Dict[str, Any]:
        """Ask in a sentence. The model turns it into search terms; the store
        does the retrieval, so results are still real rows."""
        terms = question
        if self.orchestrator:
            try:
                terms = await self.orchestrator.ask(
                    "Extract 2-6 search keywords from this question about stored personal "
                    "memories. Return only the keywords, space separated, no punctuation.\n\n"
                    f"Question: {question}",
                    purpose="fast", max_tokens=60,
                )
            except Exception:
                logger.debug("keyword extraction unavailable", exc_info=True)

        results = await self.recall(terms or question, limit=limit)
        return {"question": question, "search_terms": truncate(terms, 120),
                "results": results, "count": len(results)}

    async def explain(self, memory_id: str) -> Dict[str, Any]:
        """Where did this come from?"""
        record = await self.get(memory_id)
        if not record:
            raise KeyError(memory_id)

        provenance = record.get("provenance") or {}
        created = parse_iso(record.get("created_at"))
        parts = []
        if provenance.get("source"):
            parts.append(f"recorded by {provenance['source']}")
        if provenance.get("conversation_id"):
            parts.append("from a conversation")
        if provenance.get("task_id"):
            parts.append("while running a background task")
        if created:
            parts.append(f"on {created.astimezone().strftime('%d %B %Y at %H:%M')}")

        return {
            "id": memory_id,
            "value": record["value"],
            "scope": record.get("scope"),
            "origin": ", ".join(parts) or "origin not recorded",
            "provenance": provenance,
            "times_recalled": record.get("use_count", 0),
            "last_recalled": record.get("last_used"),
            "importance": record.get("importance"),
        }

    async def list(
        self,
        scope: Optional[str] = None,
        category: Optional[str] = None,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory WHERE 1=1"
        params: List[Any] = []
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if search:
            sql += " AND (value LIKE ? OR key LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        return [_decode(r) for r in await self.db.fetch_all(sql, params)]

    # ==================================================================
    # cleanup
    # ==================================================================
    async def expire(self) -> int:
        """Short-term memories that have aged out."""
        return await self.db.execute(
            "DELETE FROM memory WHERE scope = 'short_term' AND expires_at IS NOT NULL"
            " AND expires_at < ? AND pinned = 0", (now_iso(),),
        )

    async def find_duplicates(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT category, key, value, COUNT(*) AS n, GROUP_CONCAT(id) AS ids"
            " FROM memory GROUP BY category, LOWER(TRIM(value)) HAVING n > 1")
        return [{"category": r["category"], "value": truncate(r["value"], 120),
                 "count": r["n"], "ids": (r["ids"] or "").split(",")} for r in rows]

    async def cleanup_report(self) -> Dict[str, Any]:
        """What cleanup *would* remove. Shown before anything is deleted."""
        expired = await self.db.fetch_all(
            "SELECT id, value FROM memory WHERE scope = 'short_term'"
            " AND expires_at IS NOT NULL AND expires_at < ? AND pinned = 0", (now_iso(),))
        duplicates = await self.find_duplicates()
        unused = await self.db.fetch_all(
            "SELECT id, value, importance, created_at FROM memory"
            " WHERE pinned = 0 AND importance <= 2 AND use_count = 0 AND created_at < ?",
            (since(60 * 86400),))

        return {
            "expired": [{"id": r["id"], "value": truncate(r["value"], 90)} for r in expired],
            "duplicate_groups": duplicates,
            "never_recalled": [{"id": r["id"], "value": truncate(r["value"], 90)}
                               for r in unused],
            "total_removable": len(expired) + sum(d["count"] - 1 for d in duplicates) + len(unused),
            "note": "Pinned memories are never removed by cleanup.",
        }

    async def cleanup(self, expired: bool = True, duplicates: bool = True,
                      unused: bool = False) -> Dict[str, Any]:
        removed = {"expired": 0, "duplicates": 0, "unused": 0}

        if expired:
            removed["expired"] = await self.expire()

        if duplicates:
            for group in await self.find_duplicates():
                # Keep the first, drop the rest.
                for memory_id in group["ids"][1:]:
                    if await self.db.execute(
                        "DELETE FROM memory WHERE id = ? AND pinned = 0", (memory_id,)
                    ):
                        removed["duplicates"] += 1

        if unused:
            removed["unused"] = await self.db.execute(
                "DELETE FROM memory WHERE pinned = 0 AND importance <= 2 AND use_count = 0"
                " AND created_at < ?", (since(60 * 86400),))

        total = sum(removed.values())
        if total and self.activity:
            await self.activity.record(
                f"Memory cleanup removed {total} entr{'y' if total == 1 else 'ies'}.",
                source="memory",
            )
        return {**removed, "total": total}

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        by_scope = await self.db.fetch_all(
            "SELECT scope, COUNT(*) AS n FROM memory GROUP BY scope")
        by_category = await self.db.fetch_all(
            "SELECT category, COUNT(*) AS n FROM memory GROUP BY category")
        return {
            "enabled": await self.store.enabled(),
            "total": int(await self.db.fetch_value("SELECT COUNT(*) FROM memory", default=0)),
            "by_scope": {r["scope"]: r["n"] for r in by_scope},
            "by_category": {r["category"]: r["n"] for r in by_category},
            "never_recalled": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM memory WHERE use_count = 0", default=0)),
            "removable": (await self.cleanup_report())["total_removable"],
        }


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["pinned"] = bool(item.get("pinned"))
    item["provenance"] = loads(item.get("provenance"), {})
    item["scope"] = item.get("scope") or "long_term"
    return item
