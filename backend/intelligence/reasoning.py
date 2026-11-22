"""
Adaptive personality and transparency.

Three jobs:

  * **Adapt.** Shape how JARVIS answers based on real context -- what you are
    doing, what mode you are in, the time, and whether the last few exchanges
    went badly.
  * **Notice confusion.** If you re-ask, contradict, or say "that's not what I
    meant", stop repeating the same explanation and change the approach.
  * **Explain itself.** Every non-trivial decision is logged with its inputs
    and the alternatives considered, so "why did you do that?" has a real
    answer rather than a plausible-sounding reconstruction.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.util import new_id, now_iso, parse_iso, since, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.reasoning")


# ---------------------------------------------------------------------------
# confusion signals
# ---------------------------------------------------------------------------
_CONFUSION_MARKERS = (
    "i don't understand", "i dont understand", "what do you mean", "that's not what",
    "thats not what", "no i meant", "i meant", "you misunderstood", "wrong", "not right",
    "that didn't work", "that didnt work", "still not", "try again", "huh", "what?",
    "confused", "unclear", "makes no sense", "doesn't make sense", "explain again",
    "simpler", "in plain english", "eli5", "i don't get it", "i dont get it",
)

_FRUSTRATION_MARKERS = ("seriously", "come on", "again?", "ffs", "for god", "useless", "stop")

# Referential words that need conversation or UI context to resolve.
_AMBIGUOUS = (
    "it", "that", "this", "those", "these", "them", "there", "the same", "again",
    "next", "the other one", "that one", "the last one",
)


class ReasoningService:
    def __init__(self, db, settings_store, activity, awareness=None, patterns=None):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self.awareness = awareness
        self.patterns = patterns

    # ==================================================================
    # transparency: the decision log
    # ==================================================================
    async def log_decision(
        self,
        subject: str,
        summary: str,
        rationale: str = "",
        inputs: Optional[Dict[str, Any]] = None,
        alternatives: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> str:
        decision_id = new_id("dec")
        await self.db.execute(
            "INSERT INTO decisions (id, ts, subject, summary, rationale, inputs, alternatives,"
            " conversation_id, task_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (decision_id, now_iso(), truncate(subject, 120), truncate(summary, 500),
             truncate(rationale, 1000), dumps(inputs or {}), dumps(alternatives or []),
             conversation_id, task_id),
        )
        return decision_id

    async def decisions(self, limit: int = 20, subject: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM decisions"
        params: List[Any] = []
        if subject:
            sql += " WHERE subject LIKE ?"
            params.append(f"%{subject}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            row["inputs"] = loads(row.get("inputs"), {})
            row["alternatives"] = loads(row.get("alternatives"), [])
        return rows

    async def explain_recent(self, limit: int = 5) -> Dict[str, Any]:
        """Answers 'why did you do that?' from the record, not from memory.

        Combines logged decisions with what actually happened -- tool calls and
        task activity -- so the explanation is grounded in events.
        """
        decisions = await self.decisions(limit=limit)
        tools = await self.db.fetch_all(
            "SELECT message, ts, meta FROM activity_log WHERE source = 'tools' AND ts > ?"
            " ORDER BY id DESC LIMIT 10",
            (since(1800),),
        )
        tasks = await self.db.fetch_all(
            "SELECT name, type, status, current_step, error FROM tasks WHERE created_at > ?"
            " ORDER BY created_at DESC LIMIT 5",
            (since(3600),),
        )

        if not decisions and not tasks and not tools:
            return {
                "explained": False,
                "message": "I haven't made any decisions worth explaining in the last while.",
            }

        return {
            "explained": True,
            "decisions": [
                {
                    "when": d["ts"],
                    "what": d["summary"],
                    "why": d["rationale"],
                    "considered": d["alternatives"],
                    "based_on": d["inputs"],
                }
                for d in decisions
            ],
            "recent_tasks": [dict(t) for t in tasks],
            "recent_tool_activity": [
                {"when": t["ts"], "what": t["message"]} for t in tools
            ],
        }

    # ==================================================================
    # confusion detection
    # ==================================================================
    async def read_the_room(self, conversation_id: Optional[str], latest: str) -> Dict[str, Any]:
        """Look at the last few turns and decide whether to change approach."""
        lowered = (latest or "").lower()
        signals: List[str] = []

        if any(marker in lowered for marker in _CONFUSION_MARKERS):
            signals.append("said something indicating the last answer missed")
        if any(marker in lowered for marker in _FRUSTRATION_MARKERS):
            signals.append("sounds frustrated")
        if lowered.count("?") >= 2:
            signals.append("asked several questions at once")

        repeated = False
        if conversation_id:
            rows = await self.db.fetch_all(
                "SELECT text FROM messages WHERE conversation_id = ? AND role = 'user'"
                " AND text != '' ORDER BY created_at DESC LIMIT 4",
                (conversation_id,),
            )
            previous = [r["text"] for r in rows[1:]]
            if previous and self.patterns is not None:
                from .patterns import normalise

                fingerprint = normalise(latest)
                if fingerprint and any(normalise(p) == fingerprint for p in previous):
                    repeated = True
                    signals.append("asked essentially the same thing again")

        confused = bool(signals)
        guidance = ""
        if confused:
            guidance = (
                "The user does not seem to have got what they needed from your last answer. "
                "Do NOT repeat the same explanation in the same shape. Change approach: be more "
                "concrete, use a worked example with their actual data, or state plainly what you "
                "are uncertain about and ask one focused question. Keep it short."
            )
            if repeated:
                guidance += (
                    " They have asked this before, so acknowledge that briefly and try a "
                    "genuinely different angle."
                )

        return {"confused": confused, "signals": signals, "repeated": repeated, "guidance": guidance}

    # ==================================================================
    # ambiguity
    # ==================================================================
    async def ambiguity(self, text: str) -> Dict[str, Any]:
        """Flag commands that cannot be resolved without context, and supply the
        context that would resolve them."""
        lowered = f" {(text or '').lower().strip()} "
        words = lowered.split()

        # A short command built mostly from referential words is the risky case;
        # "open that file in vscode" is fine because it carries other anchors.
        referential = [w for w in _AMBIGUOUS if f" {w} " in lowered]
        vague = bool(referential) and len(words) <= 6

        destructive = any(
            marker in lowered
            for marker in (" delete ", " remove ", " wipe ", " clear ", " shut down ",
                           " restart ", " send ", " call ", " cancel ")
        )

        context: Dict[str, Any] = {}
        if vague or destructive:
            if self.awareness:
                snapshot = self.awareness.snapshot()
                if snapshot.get("active_app"):
                    context["on_screen"] = f"{snapshot['active_app']} — {snapshot.get('window_title', '')}"
            active = await self.db.fetch_all(
                "SELECT id, name, status FROM tasks WHERE status IN ('running','queued','paused')"
                " ORDER BY created_at DESC LIMIT 3"
            )
            if active:
                context["active_tasks"] = [dict(t) for t in active]
            events = await self.db.fetch_all(
                "SELECT id, title, start_at FROM calendar_events WHERE start_at >= ?"
                " ORDER BY start_at LIMIT 2",
                (now_iso(),),
            )
            if events:
                context["next_events"] = [dict(e) for e in events]

        guidance = ""
        if vague and destructive:
            guidance = (
                "This command is both vague and potentially destructive. Do not guess the target. "
                "Ask exactly one clarifying question naming the candidates you can see."
            )
        elif vague:
            guidance = (
                "This command relies on context. Resolve it from the conversation, what is on "
                "screen, or the active task list. If you genuinely cannot, ask one short "
                "clarifying question rather than guessing."
            )

        return {
            "ambiguous": vague,
            "destructive": destructive,
            "referential_words": referential,
            "resolution_context": context,
            "guidance": guidance,
        }

    # ==================================================================
    # adaptive style
    # ==================================================================
    async def style_directives(self, conversation_id: Optional[str], latest: str) -> str:
        """Assemble the per-turn personality instructions appended to the system
        prompt. Everything here is derived from observable state."""
        parts: List[str] = []
        moment = utcnow().astimezone()

        # -- time of day ---------------------------------------------------
        hour = moment.hour
        if 5 <= hour < 9:
            parts.append("It is early morning; lead with what matters today and keep it brisk.")
        elif 22 <= hour or hour < 5:
            parts.append(
                "It is late at night. Be calm and low-key, keep answers short, and avoid "
                "starting anything long-running unless asked."
            )

        # -- what they're doing --------------------------------------------
        if self.awareness:
            snapshot = self.awareness.snapshot()
            category = snapshot.get("category")
            focused = snapshot.get("focused_for_seconds", 0) or 0
            app = snapshot.get("active_app")

            if app:
                parts.append(f"They are currently in {app} ({category}).")
            if category == "development":
                parts.append(
                    "They are coding: be precise and technical, prefer exact names, paths and "
                    "commands over prose."
                )
            elif category == "gaming":
                parts.append(
                    "They are gaming: be extremely brief, avoid anything that would pull focus, "
                    "and don't suggest long tasks."
                )
            elif category == "communication":
                parts.append("They are in a conversation tool: keep interruptions minimal.")
            elif category == "creative":
                parts.append("They are in a creative tool: avoid breaking their flow with detail.")

            if focused > 45 * 60 and category in ("development", "creative", "productivity"):
                parts.append(
                    f"They have been focused in the same app for {int(focused / 60)} minutes -- "
                    "do not volunteer distractions."
                )

        # -- mode -----------------------------------------------------------
        mode = await self.settings_store.get("active_mode", "normal")
        if mode == "focus":
            parts.append("Focus mode: answer in one or two sentences, nothing extra.")
        elif mode == "meeting":
            parts.append("Meeting mode: they may be on a call. Be silent-friendly and terse.")
        elif mode == "developer":
            parts.append("Developer mode: include exact commands, file paths and error text.")

        # -- how the conversation is going -----------------------------------
        mood = await self.read_the_room(conversation_id, latest)
        if mood["confused"]:
            parts.append(mood["guidance"])

        ambiguity = await self.ambiguity(latest)
        if ambiguity["guidance"]:
            parts.append(ambiguity["guidance"])
            if ambiguity["resolution_context"]:
                parts.append(
                    "Context you can use to resolve the reference: "
                    + truncate(dumps(ambiguity["resolution_context"]), 700)
                )

        # -- their own shorthand ---------------------------------------------
        if self.patterns:
            _, phrases = await self.patterns.expand(latest)
            if phrases:
                parts.append("Their personal shorthand in this message: " + "; ".join(phrases))

        # -- recent failures --------------------------------------------------
        failures = int(await self.db.fetch_value(
            "SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND finished_at > ?",
            (since(1800),), default=0,
        ))
        if failures >= 2:
            parts.append(
                f"{failures} tasks have failed in the last half hour. Be upfront about what is "
                "broken rather than optimistic."
            )

        if not parts:
            return ""
        return "Right now:\n" + "\n".join(f"- {p}" for p in parts)

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        return {
            "decisions_logged": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM decisions", default=0)),
            "last_decision": await self.db.fetch_value(
                "SELECT ts FROM decisions ORDER BY ts DESC LIMIT 1", default=None),
        }
