"""Keeping the gist of a conversation that has outgrown the model's window.

The model sees a conversation through a fixed window. History is trimmed
from the front to fit (see `Orchestrator.history`), and until now what was
trimmed was simply gone: forty messages in, the plan agreed at the start
had never been said as far as the model knew.

Now the trimmed part is summarised. After a reply, if the conversation was
too long to send whole, the messages that fell off the front -- and were not
already summarised -- are folded into a running summary for that
conversation, which then rides along in the system prompt of every later
turn.

Done after the reply, never before it: summarising is a model call, and the
user is waiting on the answer, not on the bookkeeping. The cost is that the
first turn after something falls off goes without its summary; every turn
after that has it.

Ported idea: OpenJarvis ``sessions/compression.py`` (Stanford Hazy Research,
Apache-2.0) -- consolidate the oldest turns, keep the recent ones whole.
Theirs falls back to cutting each old message to a hundred characters; this
asks the model for an actual summary, updated incrementally.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.util import now_iso, truncate
from ..db.database import loads

logger = logging.getLogger("jarvis.compression")

SUMMARY_WORDS = 220
# How much of the fallen-off conversation to read per update. A day-long
# conversation is folded in over several updates rather than one giant call.
MAX_CHARS_PER_UPDATE = 12000

_SYSTEM = (
    "You keep a running summary of the earlier part of a conversation between "
    "a user and their assistant, JARVIS. Keep what later turns may depend on: "
    "decisions, agreed plans, names, numbers, preferences stated, open "
    "questions and promises made. Drop greetings and filler. Write plain "
    f"prose or short bullets, at most {SUMMARY_WORDS} words, and reply with "
    "the summary only."
)


def _text_of(row) -> str:
    """The words of one stored message, without tool plumbing."""
    if row.get("text"):
        return str(row["text"])
    parts = []
    for block in loads(row.get("blocks"), []) or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return " ".join(parts)


class ConversationSummaries:
    def __init__(self, db, orchestrator) -> None:
        self.db = db
        self.orchestrator = orchestrator

    async def get(self, conversation_id: Optional[str]) -> str:
        if not conversation_id:
            return ""
        row = await self.db.fetch_one(
            "SELECT summary FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,))
        return (row or {}).get("summary", "") or ""

    async def refresh(self, conversation_id: str, oldest_kept: str) -> bool:
        """Fold messages older than `oldest_kept` into the summary. Never raises."""
        try:
            current = await self.db.fetch_one(
                "SELECT summary, covers_until, messages FROM conversation_summaries"
                " WHERE conversation_id = ?", (conversation_id,)) or {}
            since = current.get("covers_until") or ""
            rows = await self.db.fetch_all(
                "SELECT role, text, blocks, created_at FROM messages"
                " WHERE conversation_id = ? AND created_at > ? AND created_at < ?"
                " ORDER BY created_at ASC, rowid ASC",
                (conversation_id, since, oldest_kept))
            if not rows:
                return False

            lines, used, last = [], 0, since
            for row in rows:
                words = " ".join(_text_of(row).split())
                last = row["created_at"]
                if not words:
                    continue
                who = "User" if row["role"] == "user" else "JARVIS"
                line = f"{who}: {truncate(words, 1500)}"
                if used + len(line) > MAX_CHARS_PER_UPDATE and lines:
                    break
                lines.append(line)
                used += len(line)
            if not lines:
                # Only tool plumbing fell off; nothing to say, but move on.
                await self._store(conversation_id, current.get("summary", ""), last,
                                  int(current.get("messages") or 0))
                return False

            previous = current.get("summary") or "(nothing yet)"
            summary = await self.orchestrator.ask(
                f"Summary so far:\n{previous}\n\n"
                f"More of the conversation, continuing from there:\n"
                + "\n".join(lines)
                + "\n\nWrite the updated summary.",
                system=_SYSTEM, purpose="fast", max_tokens=420, temperature=0.2)
            summary = (summary or "").strip()
            if not summary:
                return False
            await self._store(conversation_id, summary, last,
                              int(current.get("messages") or 0) + len(lines))
            logger.info("conversation %s: summarised %d more message(s)",
                        conversation_id, len(lines))
            return True
        except Exception:
            logger.debug("summarising conversation %s failed", conversation_id,
                         exc_info=True)
            return False

    async def _store(self, conversation_id: str, summary: str, covers_until: str,
                     messages: int) -> None:
        await self.db.execute(
            "INSERT INTO conversation_summaries (conversation_id, summary, covers_until,"
            " messages, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(conversation_id) DO UPDATE SET summary = excluded.summary,"
            " covers_until = excluded.covers_until, messages = excluded.messages,"
            " updated_at = excluded.updated_at",
            (conversation_id, summary, covers_until, messages, now_iso()))
