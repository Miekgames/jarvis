"""
Command interpretation 2.0.

    "JARVIS, take care of this."
    "Finish what I was doing yesterday."
    "Continue that project."
    "Find out why this isn't working."
    "Handle the boring stuff."

These have no object until you look at the world. The resolver assembles the
candidates -- what's on screen, what was interrupted, which project is warm,
what failed recently -- scores them, and either resolves the reference or hands
back a clarifying question naming the actual options.

It never guesses on a destructive command. That is the whole point: a wrong
guess about "take care of this" could delete the wrong thing.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..core.util import humanize_duration, iso, now_iso, parse_iso, since, truncate, utcnow

logger = logging.getLogger("jarvis.resolver")

# Phrases that mean "you work out what I mean".
# Order matters: the most specific pattern must win. "finish what I was doing
# yesterday" is historical, not merely interrupted, so the time-anchored
# patterns are tested first.
_VAGUE_PATTERNS = [
    (re.compile(r"\bwhat i was doing (yesterday|earlier|last night|this morning|last week)\b", re.I),
     "historical"),
    (re.compile(r"\b(yesterday|last night|earlier today|this morning)\b", re.I), "historical"),
    (re.compile(r"\btake care of (this|that|it|them)\b", re.I), "current"),
    (re.compile(r"\bhandle (this|that|it|them)\b", re.I), "current"),
    (re.compile(r"\bdeal with (this|that|it|them)\b", re.I), "current"),
    (re.compile(r"\bfinish (what i was doing|that|it|this)\b", re.I), "interrupted"),
    (re.compile(r"\bcontinue (that|the|my) ?(project|work|research|task)?\b", re.I), "interrupted"),
    (re.compile(r"\bpick up where (i|we) left off\b", re.I), "interrupted"),
    (re.compile(r"\bwhy (is|isn'?t|won'?t|does|doesn'?t) (this|that|it)\b", re.I), "problem"),
    (re.compile(r"\b(fix|debug|sort out) (this|that|it)\b", re.I), "problem"),
    (re.compile(r"\bthe boring stuff\b", re.I), "chores"),
    (re.compile(r"\bprepare everything for (tomorrow|today|the week|monday)\b", re.I),
     "preparation"),
]

# A short command built around a bare pronoun is referential even when it
# matches none of the phrasings above — "delete that", "open it", "send this".
# This is exactly the dangerous case, so it must not fall through.
_BARE_REFERENT = re.compile(r"\b(this|that|it|those|these|them)\b", re.I)

_DESTRUCTIVE = ("delete", "remove", "wipe", "erase", "clear", "shut down", "shutdown",
                "restart", "send", "call", "cancel", "revoke", "uninstall")


class ContextResolver:
    def __init__(self, db, awareness=None, tasks=None, goals=None, calendar=None,
                 planning=None, research=None):
        self.db = db
        self.awareness = awareness
        self.tasks = tasks
        self.goals = goals
        self.calendar = calendar
        self.planning = planning
        self.research = research

    # ==================================================================
    def classify(self, text: str) -> Optional[str]:
        """Which kind of vagueness is this, if any."""
        for pattern, kind in _VAGUE_PATTERNS:
            if pattern.search(text or ""):
                return kind

        # Fallback: a short command hanging on a bare pronoun. "delete that"
        # carries no phrasing cue but is precisely what must not be guessed.
        words = (text or "").split()
        if len(words) <= 6 and _BARE_REFERENT.search(text or ""):
            return "problem" if self.is_destructive(text) is False and \
                any(w in (text or "").lower() for w in ("why", "broken", "failing")) else "current"
        return None

    @staticmethod
    def is_destructive(text: str) -> bool:
        lowered = f" {(text or '').lower()} "
        return any(f" {word} " in lowered or lowered.strip().startswith(word)
                   for word in _DESTRUCTIVE)

    # ==================================================================
    async def resolve(self, text: str) -> Dict[str, Any]:
        """Return candidates and, when confident enough, a resolution."""
        kind = self.classify(text)
        if kind is None:
            return {"vague": False}

        candidates = await self._candidates(kind)
        destructive = self.is_destructive(text)

        if not candidates:
            return {
                "vague": True, "kind": kind, "candidates": [], "resolved": None,
                "destructive": destructive,
                "question": _NOTHING_FOUND.get(kind, "I'm not sure what you're referring to. "
                                                     "What should I look at?"),
            }

        best = candidates[0]
        runner_up = candidates[1]["score"] if len(candidates) > 1 else 0
        # Confident only when the leader is clearly ahead.
        decisive = best["score"] >= 3 and best["score"] >= runner_up * 1.6

        if destructive or not decisive:
            options = "; ".join(f"{i + 1}) {c['label']}" for i, c in enumerate(candidates[:3]))
            question = (
                f"I want to be sure before I do something irreversible — did you mean {options}?"
                if destructive else
                f"A few things could be 'this' — did you mean {options}?"
            )
            return {"vague": True, "kind": kind, "candidates": candidates[:4],
                    "resolved": None, "destructive": destructive, "question": question}

        return {
            "vague": True, "kind": kind, "candidates": candidates[:4],
            "resolved": best, "destructive": False,
            "question": None,
        }

    # ==================================================================
    async def _candidates(self, kind: str) -> List[Dict[str, Any]]:
        gatherers = {
            "current": self._current_context,
            "interrupted": self._interrupted_work,
            "historical": self._yesterday,
            "problem": self._problems,
            "chores": self._chores,
            "preparation": self._preparation,
        }
        gather = gatherers.get(kind, self._current_context)
        candidates = await gather()
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    # -- what's in front of them right now -------------------------------
    async def _current_context(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        if self.awareness:
            snapshot = self.awareness.snapshot() or {}
            if snapshot.get("active_app"):
                focused = int(snapshot.get("focused_for_seconds") or 0)
                out.append({
                    "type": "screen",
                    "label": f"what's on screen ({snapshot['active_app']} — "
                             f"{truncate(snapshot.get('window_title'), 50)})",
                    "reference": snapshot.get("window_title"),
                    "score": 4 if focused > 60 else 2,
                    "why": f"focused for {humanize_duration(focused)}",
                })

        running = await self.db.fetch_all(
            "SELECT id, name, status FROM tasks WHERE status = 'running'"
            " ORDER BY started_at DESC LIMIT 3")
        for task in running:
            out.append({"type": "task", "label": f"the running task '{task['name']}'",
                        "reference": task["id"], "score": 3, "why": "currently running"})

        failed = await self.db.fetch_all(
            "SELECT id, name, error FROM tasks WHERE status = 'failed' AND finished_at > ?"
            " ORDER BY finished_at DESC LIMIT 2", (since(3600),))
        for task in failed:
            out.append({"type": "task", "label": f"the failed task '{task['name']}'",
                        "reference": task["id"], "score": 3,
                        "why": truncate(task["error"], 80)})

        recent = await self.db.fetch_all(
            "SELECT text FROM commands WHERE ts > ? ORDER BY id DESC LIMIT 3", (since(900),))
        if recent:
            out.append({"type": "conversation",
                        "label": f"what we were just discussing ('{truncate(recent[0]['text'], 40)}')",
                        "reference": recent[0]["text"], "score": 2,
                        "why": "mentioned in the last few minutes"})
        return out

    # -- work that was left unfinished -----------------------------------
    async def _interrupted_work(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        paused = await self.db.fetch_all(
            "SELECT id, name, current_step, progress FROM tasks"
            " WHERE status IN ('paused','queued') ORDER BY created_at DESC LIMIT 4")
        for task in paused:
            percent = round(float(task["progress"] or 0) * 100)
            out.append({"type": "task", "label": f"the unfinished task '{task['name']}' ({percent}%)",
                        "reference": task["id"], "score": 4 if percent else 3,
                        "why": task["current_step"] or "not finished"})

        goals = await self.db.fetch_all(
            "SELECT id, objective, status FROM goals"
            " WHERE status IN ('running','planned','blocked','needs_input')"
            " ORDER BY updated_at DESC LIMIT 3")
        for goal in goals:
            out.append({"type": "goal", "label": f"the goal '{truncate(goal['objective'], 50)}'",
                        "reference": goal["id"],
                        "score": 5 if goal["status"] == "running" else 3,
                        "why": f"goal is {goal['status']}"})

        projects = await self.db.fetch_all(
            "SELECT id, question FROM research_projects WHERE status = 'active'"
            " ORDER BY updated_at DESC LIMIT 2")
        for project in projects:
            out.append({"type": "research",
                        "label": f"the research on '{truncate(project['question'], 45)}'",
                        "reference": project["id"], "score": 3, "why": "research still open"})
        return out

    # -- yesterday --------------------------------------------------------
    async def _yesterday(self) -> List[Dict[str, Any]]:
        start = (utcnow() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        out: List[Dict[str, Any]] = []

        tasks = await self.db.fetch_all(
            "SELECT id, name, status FROM tasks WHERE created_at BETWEEN ? AND ?"
            " ORDER BY created_at DESC LIMIT 5", (iso(start), iso(end)))
        for task in tasks:
            finished = task["status"] == "completed"
            out.append({"type": "task", "label": f"'{task['name']}' from yesterday",
                        "reference": task["id"], "score": 2 if finished else 4,
                        "why": f"yesterday, {task['status']}"})

        if self.awareness:
            usage = await self.awareness.usage(hours=36, limit=3)
            for entry in usage:
                out.append({"type": "app", "label": f"what you were doing in {entry['app']}",
                            "reference": entry["app"], "score": 2,
                            "why": f"{entry['minutes']} minutes yesterday"})

        commands = await self.db.fetch_all(
            "SELECT text FROM commands WHERE ts BETWEEN ? AND ? ORDER BY id DESC LIMIT 3",
            (iso(start), iso(end)))
        for command in commands:
            out.append({"type": "conversation",
                        "label": f"'{truncate(command['text'], 45)}'",
                        "reference": command["text"], "score": 2, "why": "you asked this yesterday"})
        return out

    # -- something is broken ----------------------------------------------
    async def _problems(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        failed = await self.db.fetch_all(
            "SELECT id, name, error FROM tasks WHERE status = 'failed' AND finished_at > ?"
            " ORDER BY finished_at DESC LIMIT 3", (since(24 * 3600),))
        for task in failed:
            out.append({"type": "task", "label": f"the failed task '{task['name']}'",
                        "reference": task["id"], "score": 5,
                        "why": truncate(task["error"], 90)})

        errors = await self.db.fetch_all(
            "SELECT message FROM activity_log WHERE level = 'error' AND ts > ?"
            " ORDER BY id DESC LIMIT 3", (since(6 * 3600),))
        for entry in errors:
            out.append({"type": "error", "label": truncate(entry["message"], 60),
                        "reference": entry["message"], "score": 3, "why": "recent error"})

        if self.awareness:
            snapshot = self.awareness.snapshot() or {}
            if snapshot.get("active_app"):
                out.append({"type": "screen",
                            "label": f"whatever is wrong in {snapshot['active_app']}",
                            "reference": snapshot.get("window_title"), "score": 4,
                            "why": "the app you're currently in — I can look at the screen"})
        return out

    # -- the boring stuff --------------------------------------------------
    async def _chores(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        unread = int(await self.db.fetch_value(
            "SELECT COUNT(*) FROM notifications WHERE read = 0 AND dismissed = 0", default=0))
        if unread >= 3:
            out.append({"type": "chore", "label": f"triaging your {unread} unread notifications",
                        "reference": "notifications", "score": 4, "why": f"{unread} unread"})

        failed = int(await self.db.fetch_value(
            "SELECT COUNT(*) FROM tasks WHERE status = 'failed'", default=0))
        if failed:
            out.append({"type": "chore", "label": f"retrying {failed} failed task(s)",
                        "reference": "retry_failed", "score": 3, "why": f"{failed} failed"})

        stale = int(await self.db.fetch_value(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('completed','cancelled')"
            " AND created_at < ?", (since(30 * 86400),), default=0))
        if stale > 20:
            out.append({"type": "chore", "label": f"clearing {stale} old task records",
                        "reference": "housekeeping", "score": 2, "why": "housekeeping"})

        out.append({"type": "chore", "label": "running a verified backup",
                    "reference": "backup", "score": 2, "why": "always safe to do"})
        return out

    # -- prepare for what's coming ------------------------------------------
    async def _preparation(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if self.calendar:
            events = await self.calendar.upcoming(limit=4, hours=36)
            for event in events:
                start = parse_iso(event["start_at"])
                when = start.astimezone().strftime("%H:%M on %a") if start else "soon"
                out.append({"type": "event", "label": f"'{event['title']}' at {when}",
                            "reference": event["id"], "score": 5, "why": "upcoming event"})

        deadlines = await self.db.fetch_all(
            "SELECT id, objective, deadline FROM goals WHERE deadline IS NOT NULL"
            " AND deadline > ? AND status NOT IN ('completed','cancelled')"
            " ORDER BY deadline LIMIT 3", (now_iso(),))
        for goal in deadlines:
            out.append({"type": "goal", "label": f"the goal '{truncate(goal['objective'], 45)}'",
                        "reference": goal["id"], "score": 4, "why": "has an approaching deadline"})
        return out

    # ==================================================================
    async def context_block(self, text: str) -> str:
        """Rendered for the system prompt so the model resolves the reference
        the same way, with the same evidence."""
        resolution = await self.resolve(text)
        if not resolution.get("vague"):
            return ""

        lines = [f"The user's request is referential ({resolution['kind']}). "
                 f"Candidates JARVIS can see right now:"]
        for candidate in resolution.get("candidates", [])[:4]:
            lines.append(f"- {candidate['label']} ({candidate['why']})")

        if resolution.get("resolved"):
            lines.append(
                f"Most likely: {resolution['resolved']['label']}. Proceed with that, but say "
                f"which one you picked so the user can correct you."
            )
        elif resolution.get("question"):
            lines.append(
                "Do NOT guess. Ask exactly this, or a close paraphrase: "
                f"\"{resolution['question']}\""
            )
        return "\n".join(lines)


_NOTHING_FOUND = {
    "current": "I can't see anything obvious you'd be pointing at. What should I take care of?",
    "interrupted": "I don't have any unfinished work on record. What were you working on?",
    "historical": "I don't have anything logged from yesterday. What were you doing?",
    "problem": "Nothing's failed recently that I can see. What isn't working?",
    "chores": "Nothing routine is outstanding — no unread notifications, no failed tasks.",
    "preparation": "Nothing is scheduled and no deadlines are close. What should I prepare for?",
}
