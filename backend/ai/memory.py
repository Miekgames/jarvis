"""
Long-term memory.

Deliberately simple and fully inspectable: rows in a table the user can read,
edit and delete from the UI. Nothing is hidden, nothing is implicit -- if
JARVIS remembers something, it is visible in Settings -> Memory.

Retrieval is keyword scoring rather than embeddings so it works offline and
with no extra model. Given the scale of a personal memory store (hundreds to a
few thousand rows), that is both fast enough and easier to reason about.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from datetime import timedelta

from ..core.util import new_id, now_iso, truncate, utcnow

logger = logging.getLogger("jarvis.memory")

CATEGORIES = [
    "preference",
    "project",
    "person",
    "command",
    "fact",
    "routine",
    "general",
]

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "to", "of", "in", "on", "at", "for", "with", "my", "me", "i", "you", "it",
    "that", "this", "what", "when", "how", "do", "does", "did", "can", "could",
    "would", "should", "please", "jarvis",
}


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower()) if t not in _STOPWORDS and len(t) > 2]


# A thread needs real back-and-forth before it counts as "what we are
# working on". Set high on purpose.
#
# The first attempt used six, and the list it produced was "What is my CPU
# usage?", "name two colours" and "hello jarvis this is a test" -- because a
# single question plus a tool call and a reply already makes several rows.
# Noise in a prompt is worse than an empty section: the model treats it as
# context and reasons from it. Better for this to return nothing most of the
# time and be right when it does.
MIN_THREAD_TURNS = 20


class MemoryStore:
    def __init__(self, db, settings_store=None):
        self.db = db
        self.settings_store = settings_store

    # ------------------------------------------------------------------
    async def enabled(self) -> bool:
        if self.settings_store is None:
            return True
        privacy = await self.settings_store.get("privacy", {})
        ai = await self.settings_store.get("ai", {})
        return bool(privacy.get("memory", True)) and bool(ai.get("memory_enabled", True))

    # ------------------------------------------------------------------
    async def remember(
        self,
        value: str,
        category: str = "general",
        key: str = "",
        importance: int = 3,
        source: str = "jarvis",
        pinned: bool = False,
    ) -> Dict[str, Any]:
        if not await self.enabled():
            raise PermissionError("Memory is disabled in settings.")
        category = category if category in CATEGORIES else "general"
        importance = max(1, min(5, int(importance)))

        # Updating an existing keyed fact beats accumulating near-duplicates.
        if key:
            existing = await self.db.fetch_one(
                "SELECT * FROM memory WHERE category = ? AND key = ?", (category, key)
            )
            if existing:
                await self.db.execute(
                    "UPDATE memory SET value = ?, importance = ?, updated_at = ?, source = ? WHERE id = ?",
                    (value, importance, now_iso(), source, existing["id"]),
                )
                logger.info("memory updated: %s/%s", category, key)
                return await self.get(existing["id"]) or {}

        memory_id = new_id("mem")
        await self.db.execute(
            "INSERT INTO memory (id, category, key, value, importance, source, pinned, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (memory_id, category, key, value, importance, source, 1 if pinned else 0, now_iso(), now_iso()),
        )
        logger.info("memory stored: %s %s", category, truncate(value, 60))
        return await self.get(memory_id) or {}

    # ------------------------------------------------------------------
    async def search(self, query: str, limit: int = 8, category: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = await self.list(category=category, limit=2000)
        terms = set(_tokens(query))
        if not terms:
            return sorted(rows, key=lambda r: (-r["pinned"], -r["importance"]))[:limit]

        scored = []
        for row in rows:
            haystack = set(_tokens(f"{row['key']} {row['value']} {row['category']}"))
            overlap = len(terms & haystack)
            if overlap == 0 and not row["pinned"]:
                continue
            score = overlap * 2 + row["importance"] + (5 if row["pinned"] else 0)
            scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in scored[:limit]]

    async def core(self, limit: int = 12) -> List[Dict[str, Any]]:
        """The facts that should be in every prompt regardless of the query.

        Who the user is, what they are building, how they like to be dealt
        with. These were competing with search results for the same handful
        of slots, which meant a question about anything specific pushed out
        the things that should never leave.
        """
        rows = await self.list(limit=200)
        return [r for r in rows if r["pinned"] or r["importance"] >= 4][:limit]

    async def recent_threads(self, limit: int = 5, days: int = 14
                             ) -> List[Dict[str, Any]]:
        """What has actually been under discussion lately.

        Read from the conversation table rather than remembered facts,
        because "what are we working on" is not something anybody thinks to
        write down -- and it is the first thing lost when a page reloads or
        a new conversation starts.
        """
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        try:
            # Count the turns, and keep only threads that actually went
            # somewhere.
            #
            # Titles come from the first message, so most conversations are
            # named things like "hello jarvis this is a test" -- which is
            # exactly what the first attempt put in front of the model as
            # "recently under discussion". A length check does not separate
            # those from real work, but the number of exchanges does: a test
            # is one message and a stray reply, whereas anything genuinely
            # being worked on has a conversation in it.
            rows = await self.db.fetch_all(
                "SELECT c.id, c.title, c.updated_at, COUNT(m.id) AS turns"
                " FROM conversations c"
                " LEFT JOIN messages m ON m.conversation_id = c.id"
                " WHERE c.archived = 0 AND c.updated_at > ?"
                " GROUP BY c.id HAVING turns >= ?"
                " ORDER BY c.updated_at DESC LIMIT ?",
                (cutoff, MIN_THREAD_TURNS, limit * 3),
            )
        except Exception:
            return []

        out: List[Dict[str, Any]] = []
        seen: List[str] = []
        for row in rows:
            title = _clean(row["title"])
            if len(title) < 8:
                continue

            # Collapse by prefix rather than by a fixed-length fingerprint.
            # Titles come from first messages, so the same subject picked up
            # again reads as "working on the earnings counter" and "working
            # on the earnings counter again" -- the same thread to any
            # reader, and two separate entries to a 40-character hash.
            lowered = title.lower()
            if any(lowered.startswith(other) or other.startswith(lowered)
                   for other in seen):
                continue
            seen.append(lowered)

            out.append({"id": row["id"], "title": truncate(title, 90),
                        "updated_at": row["updated_at"]})
            if len(out) >= limit:
                break
        return out

    async def context_block(self, query: str, limit: int = 8) -> str:
        """Everything worth telling the model, in sections.

        Three separate things, because they answer different questions and
        one must not crowd out another:

          * what is permanently true about the user
          * what is relevant to what they just said
          * what has been going on lately

        The last of these is what makes the assistant survive a reload: the
        conversation history is in the database either way, but without this
        nothing tells the model it exists.
        """
        if not await self.enabled():
            return ""

        sections: List[str] = []

        core = await self.core()
        if core:
            sections.append("What you know about the user:\n"
                            + "\n".join(_bullet(hit) for hit in core))

        core_ids = {hit["id"] for hit in core}
        hits = [h for h in await self.search(query, limit=limit + len(core_ids))
                if h["id"] not in core_ids][:limit]
        if hits:
            sections.append("Possibly relevant to what they just said:\n"
                            + "\n".join(_bullet(hit) for hit in hits))

        threads = await self.recent_threads()
        if threads:
            sections.append(
                "Recently under discussion (earlier conversations, still "
                "available if they refer back):\n"
                + "\n".join(f"- {t['title']}" for t in threads))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    async def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        return _decode(row) if row else None

    async def list(
        self, category: Optional[str] = None, limit: int = 500, search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory WHERE 1=1"
        params: List[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if search:
            sql += " AND (value LIKE ? OR key LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        return [_decode(r) for r in await self.db.fetch_all(sql, params)]

    async def update(self, memory_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"value", "category", "key", "importance", "pinned"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get(memory_id)
        if "pinned" in updates:
            updates["pinned"] = 1 if updates["pinned"] else 0
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(
            f"UPDATE memory SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now_iso(), memory_id),
        )
        return await self.get(memory_id)

    async def forget(self, memory_id: str) -> bool:
        return await self.db.execute("DELETE FROM memory WHERE id = ?", (memory_id,)) > 0

    async def clear(self, category: Optional[str] = None) -> int:
        if category:
            return await self.db.execute("DELETE FROM memory WHERE category = ?", (category,))
        return await self.db.execute("DELETE FROM memory")

    async def stats(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all("SELECT category, COUNT(*) AS n FROM memory GROUP BY category")
        return {
            "total": int(await self.db.fetch_value("SELECT COUNT(*) FROM memory", default=0)),
            "by_category": {r["category"]: r["n"] for r in rows},
            "enabled": await self.enabled(),
        }


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["pinned"] = bool(item.get("pinned"))
    item["importance"] = int(item.get("importance") or 3)
    return item

def _bullet(hit: Dict[str, Any]) -> str:
    label = f"[{hit['category']}]"
    if hit["key"]:
        label += f" {hit['key']}:"
    return f"- {label} {hit['value']}"

def _clean(title: str) -> str:
    """Tidy a conversation title for use in a prompt.

    Titles are lifted from the first message, so they arrive with whatever
    was in it: replacement characters from a bad decode, runs of whitespace,
    trailing punctuation from a truncation.
    """
    text = (title or "").replace("�", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(" .,:;-—")
