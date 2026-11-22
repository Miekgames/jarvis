"""
Personal organisation.

Daily and weekly plans, schedule-conflict detection, free-time blocks,
"what should I do next", focus sessions, end-of-day summaries and a weekly
productivity report.

The arithmetic (conflicts, gaps, workload) is computed deterministically from
the calendar and task data; the model is used only to phrase things. That
matters -- a planner that hallucinates a free slot you don't have is worse than
no planner.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..core.util import (
    ahead, humanize_duration, iso, new_id, now_iso, parse_iso, since, truncate, utcnow,
)

logger = logging.getLogger("jarvis.planning")

WORK_START_HOUR = 9
WORK_END_HOUR = 18
MIN_USEFUL_BLOCK_MINUTES = 25

PRIORITY_WEIGHT = {"critical": 0, "high": 1, "normal": 2, "low": 3, "background": 4}


class PlanningService:
    def __init__(self, db, settings_store, calendar, tasks, orchestrator, awareness=None,
                 notifications=None):
        self.db = db
        self.settings_store = settings_store
        self.calendar = calendar
        self.tasks = tasks
        self.orchestrator = orchestrator
        self.awareness = awareness
        self.notifications = notifications

    # ==================================================================
    # conflicts
    # ==================================================================
    async def conflicts(self, days: int = 7) -> List[Dict[str, Any]]:
        """Overlapping calendar events. Pure arithmetic, no model involved."""
        events = await self.calendar.upcoming(limit=200, hours=days * 24)
        spans: List[Tuple[datetime, datetime, Dict[str, Any]]] = []
        for event in events:
            start = parse_iso(event["start_at"])
            end = parse_iso(event.get("end_at")) or (start + timedelta(hours=1) if start else None)
            if start and end:
                spans.append((start, end, event))
        spans.sort(key=lambda s: s[0])

        found = []
        for i in range(len(spans) - 1):
            start_a, end_a, event_a = spans[i]
            for j in range(i + 1, len(spans)):
                start_b, end_b, event_b = spans[j]
                if start_b >= end_a:
                    break
                overlap = (min(end_a, end_b) - start_b).total_seconds() / 60
                if overlap > 0:
                    found.append({
                        "overlap_minutes": int(overlap),
                        "a": {"id": event_a["id"], "title": event_a["title"],
                              "start": event_a["start_at"]},
                        "b": {"id": event_b["id"], "title": event_b["title"],
                              "start": event_b["start_at"]},
                        "severity": "high" if overlap > 20 else "low",
                    })
        return found

    # ==================================================================
    # free time
    # ==================================================================
    async def free_blocks(self, day_offset: int = 0,
                          minimum_minutes: int = MIN_USEFUL_BLOCK_MINUTES) -> List[Dict[str, Any]]:
        """Gaps in the working day that are actually long enough to use."""
        local_now = utcnow().astimezone()
        day = (local_now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        window_start = day.replace(hour=WORK_START_HOUR)
        window_end = day.replace(hour=WORK_END_HOUR)
        if day_offset == 0 and local_now > window_start:
            window_start = local_now

        if window_start >= window_end:
            return []

        events = await self.calendar.range(iso(day), iso(day + timedelta(days=1)))
        busy: List[Tuple[datetime, datetime]] = []
        for event in events:
            start = parse_iso(event["start_at"])
            end = parse_iso(event.get("end_at")) or (start + timedelta(hours=1) if start else None)
            if start and end:
                busy.append((start.astimezone(), end.astimezone()))
        busy.sort()

        # Merge overlapping meetings so a double-booking doesn't create a phantom gap.
        merged: List[List[datetime]] = []
        for start, end in busy:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        blocks = []
        cursor = window_start
        for start, end in merged:
            if start > cursor:
                gap = (min(start, window_end) - cursor).total_seconds() / 60
                if gap >= minimum_minutes:
                    blocks.append({"start": iso(cursor), "end": iso(min(start, window_end)),
                                   "minutes": int(gap)})
            cursor = max(cursor, end)
            if cursor >= window_end:
                break

        if cursor < window_end:
            gap = (window_end - cursor).total_seconds() / 60
            if gap >= minimum_minutes:
                blocks.append({"start": iso(cursor), "end": iso(window_end), "minutes": int(gap)})

        return blocks

    # ==================================================================
    # what next
    # ==================================================================
    async def next_action(self) -> Dict[str, Any]:
        """A single concrete recommendation, with the reasoning shown."""
        local_now = utcnow().astimezone()

        # 1. Anything imminent beats everything else.
        upcoming = await self.calendar.upcoming(limit=1, hours=3)
        if upcoming:
            event = upcoming[0]
            start = parse_iso(event["start_at"])
            minutes = (start - utcnow()).total_seconds() / 60 if start else 999
            if minutes <= 20:
                return {
                    "recommendation": f"Get ready for '{event['title']}'",
                    "why": f"It starts in {int(minutes)} minutes.",
                    "kind": "meeting",
                    "reference": event["id"],
                    "urgency": "now",
                }

        # 2. A failed task is blocking something.
        failed = await self.db.fetch_one(
            "SELECT id, name, error FROM tasks WHERE status = 'failed' AND finished_at > ?"
            " ORDER BY finished_at DESC LIMIT 1",
            (since(6 * 3600),),
        )
        if failed:
            return {
                "recommendation": f"Deal with the failed task '{failed['name']}'",
                "why": truncate(failed["error"] or "It failed and nothing has retried it.", 160),
                "kind": "task",
                "reference": failed["id"],
                "urgency": "soon",
            }

        # 3. Project deadlines.
        deadline = await self.db.fetch_one(
            "SELECT id, name, deadline FROM projects WHERE status = 'active' AND deadline IS NOT NULL"
            " AND deadline > ? ORDER BY deadline ASC LIMIT 1",
            (now_iso(),),
        )
        if deadline:
            due = parse_iso(deadline["deadline"])
            hours_left = (due - utcnow()).total_seconds() / 3600 if due else 999
            if hours_left < 72:
                return {
                    "recommendation": f"Work on '{deadline['name']}'",
                    "why": f"Its deadline is in {humanize_duration(hours_left * 3600)}.",
                    "kind": "project",
                    "reference": deadline["id"],
                    "urgency": "soon" if hours_left < 24 else "today",
                }

        # 4. Use the next free block.
        blocks = await self.free_blocks()
        if blocks:
            block = blocks[0]
            start = parse_iso(block["start"])
            return {
                "recommendation": f"You have a free {block['minutes']}-minute block"
                                  f" from {start.astimezone().strftime('%H:%M') if start else '?'}",
                "why": "Nothing is scheduled in it.",
                "kind": "focus",
                "reference": None,
                "urgency": "today",
                "suggested_focus_minutes": min(50, block["minutes"]),
            }

        return {
            "recommendation": "Nothing is pressing",
            "why": "No imminent events, no failed tasks, no near deadlines.",
            "kind": "idle",
            "urgency": "none",
        }

    # ==================================================================
    # plans
    # ==================================================================
    async def daily_plan(self, day_offset: int = 0) -> Dict[str, Any]:
        local_now = utcnow().astimezone()
        day = (local_now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)

        events = await self.calendar.range(iso(day), iso(day + timedelta(days=1)))
        blocks = await self.free_blocks(day_offset)
        conflicts = [c for c in await self.conflicts(days=max(1, day_offset + 1))
                     if (parse_iso(c["a"]["start"]) or utcnow()).date() == day.date()]
        active_tasks = await self.tasks.list(active_only=True, limit=20)

        facts = {
            "date": day.strftime("%A %d %B %Y"),
            "events": [
                {"title": e["title"],
                 "start": (parse_iso(e["start_at"]) or utcnow()).astimezone().strftime("%H:%M"),
                 "location": e.get("location") or e.get("link") or ""}
                for e in events
            ],
            "free_blocks": [
                {"from": (parse_iso(b["start"]) or utcnow()).astimezone().strftime("%H:%M"),
                 "to": (parse_iso(b["end"]) or utcnow()).astimezone().strftime("%H:%M"),
                 "minutes": b["minutes"]}
                for b in blocks
            ],
            "conflicts": [
                f"{c['a']['title']} overlaps {c['b']['title']} by {c['overlap_minutes']} min"
                for c in conflicts
            ],
            "active_tasks": [{"name": t["name"], "status": t["status"]} for t in active_tasks],
            "total_free_minutes": sum(b["minutes"] for b in blocks),
            "meeting_count": len(events),
        }

        narrative = await self._narrate(
            "Write a short plan for this day from the facts below. Ground everything in the "
            "actual times given -- never invent a slot or a meeting. Call out conflicts first "
            "if there are any. Suggest what to put in the free blocks based on the active tasks. "
            "Under 140 words, plain prose, no markdown.",
            facts,
        )
        return {"facts": facts, "plan": narrative}

    async def weekly_plan(self) -> Dict[str, Any]:
        events = await self.calendar.upcoming(limit=100, hours=24 * 7)
        by_day: Dict[str, List[str]] = {}
        for event in events:
            start = parse_iso(event["start_at"])
            if not start:
                continue
            key = start.astimezone().strftime("%a %d %b")
            by_day.setdefault(key, []).append(
                f"{start.astimezone().strftime('%H:%M')} {event['title']}")

        free_by_day = {}
        for offset in range(7):
            blocks = await self.free_blocks(offset)
            label = (utcnow().astimezone() + timedelta(days=offset)).strftime("%a %d %b")
            free_by_day[label] = sum(b["minutes"] for b in blocks)

        deadlines = await self.db.fetch_all(
            "SELECT name, deadline FROM projects WHERE status = 'active' AND deadline IS NOT NULL"
            " AND deadline BETWEEN ? AND ? ORDER BY deadline",
            (now_iso(), ahead(7 * 86400)),
        )

        facts = {
            "week_of": utcnow().astimezone().strftime("%d %B %Y"),
            "schedule": by_day,
            "free_minutes_per_day": free_by_day,
            "conflicts": len(await self.conflicts(days=7)),
            "deadlines": [{"project": d["name"], "due": d["deadline"]} for d in deadlines],
            "busiest_day": max(free_by_day, key=lambda k: -free_by_day[k]) if free_by_day else None,
        }

        narrative = await self._narrate(
            "Write a brief plan for the week from these facts. Identify the busiest day, where "
            "the real free capacity is, and what deserves attention given the deadlines. Use only "
            "the days and times given. Under 160 words, plain prose, no markdown.",
            facts,
        )
        return {"facts": facts, "plan": narrative}

    # ==================================================================
    # deadline prediction
    # ==================================================================
    async def deadline_risk(self) -> List[Dict[str, Any]]:
        """Flag projects likely to overrun, based on remaining free capacity
        rather than a vibe."""
        projects = await self.db.fetch_all(
            "SELECT id, name, deadline FROM projects WHERE status = 'active'"
            " AND deadline IS NOT NULL ORDER BY deadline"
        )
        assessments = []
        for project in projects:
            due = parse_iso(project["deadline"])
            if not due:
                continue
            days_left = max(0, (due - utcnow()).days)

            open_tasks = int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ?"
                " AND status IN ('queued','running','paused','waiting')",
                (project["id"],), default=0,
            ))
            done = int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'completed'",
                (project["id"],), default=0,
            ))

            capacity = 0
            for offset in range(min(days_left + 1, 7)):
                capacity += sum(b["minutes"] for b in await self.free_blocks(offset))

            risk = "low"
            if days_left <= 1 and open_tasks:
                risk = "high"
            elif open_tasks and capacity < open_tasks * 45:
                risk = "high"
            elif days_left <= 3 and open_tasks > 2:
                risk = "medium"

            assessments.append({
                "project": project["name"],
                "project_id": project["id"],
                "due": project["deadline"],
                "days_left": days_left,
                "open_tasks": open_tasks,
                "completed_tasks": done,
                "free_minutes_before_deadline": capacity,
                "risk": risk,
                "why": (
                    f"{open_tasks} open task(s), {days_left} day(s) left, about "
                    f"{capacity} free minutes scheduled before then."
                ),
            })
        return assessments

    # ==================================================================
    # focus sessions
    # ==================================================================
    async def start_focus_session(self, minutes: int = 50, intent: str = "") -> Dict[str, Any]:
        """Switch to focus mode, set a timer, and record the intent so the
        end-of-session summary can reference it."""
        from ..core.util import new_id as make_id

        timer_id = make_id("tmr")
        ends = ahead(minutes * 60)
        await self.db.execute(
            "INSERT INTO timers (id, label, kind, duration, started_at, ends_at, status)"
            " VALUES (?,?,?,?,?,?,'running')",
            (timer_id, intent or "Focus session", "timer", minutes * 60, now_iso(), ends),
        )
        return {
            "timer_id": timer_id,
            "minutes": minutes,
            "intent": intent,
            "ends_at": ends,
        }

    # ==================================================================
    # summaries
    # ==================================================================
    async def end_of_day(self) -> Dict[str, Any]:
        day_start = utcnow().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

        completed = await self.db.fetch_all(
            "SELECT name, type FROM tasks WHERE status = 'completed' AND finished_at > ?",
            (iso(day_start),),
        )
        failed = await self.db.fetch_all(
            "SELECT name, error FROM tasks WHERE status = 'failed' AND finished_at > ?",
            (iso(day_start),),
        )
        commands = int(await self.db.fetch_value(
            "SELECT COUNT(*) FROM commands WHERE ts > ?", (iso(day_start),), default=0))
        events = await self.calendar.range(iso(day_start), iso(day_start + timedelta(days=1)))

        usage: List[Dict[str, Any]] = []
        categories: Dict[str, float] = {}
        if self.awareness:
            usage = await self.awareness.usage(hours=16, limit=6)
            categories = await self.awareness.by_category(hours=16)

        tomorrow = await self.calendar.range(
            iso(day_start + timedelta(days=1)), iso(day_start + timedelta(days=2)))

        facts = {
            "date": day_start.strftime("%A %d %B"),
            "tasks_completed": [t["name"] for t in completed],
            "tasks_failed": [{"name": t["name"], "error": truncate(t["error"], 120)}
                             for t in failed],
            "commands_given": commands,
            "meetings_held": len(events),
            "time_by_app_minutes": {u["app"]: u["minutes"] for u in usage},
            "time_by_category_minutes": categories,
            "tomorrow": [
                f"{(parse_iso(e['start_at']) or utcnow()).astimezone().strftime('%H:%M')} {e['title']}"
                for e in tomorrow
            ],
        }

        narrative = await self._narrate(
            "Write an end-of-day summary from these facts. Say what actually got done, what "
            "didn't, where the time went, and what tomorrow looks like. Honest, not "
            "congratulatory -- if little happened, say so neutrally. Under 130 words, plain "
            "prose, no markdown.",
            facts,
        )

        if self.notifications:
            await self.notifications.create(
                title="End-of-day summary", body=truncate(narrative, 400),
                priority="low", source="planning",
            )
        return {"facts": facts, "summary": narrative}

    async def weekly_report(self) -> Dict[str, Any]:
        week_start = utcnow() - timedelta(days=7)

        rows = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE created_at > ? GROUP BY status",
            (iso(week_start),),
        )
        counts = {r["status"]: r["n"] for r in rows}
        finished = counts.get("completed", 0) + counts.get("failed", 0)

        categories: Dict[str, float] = {}
        usage: List[Dict[str, Any]] = []
        if self.awareness:
            categories = await self.awareness.by_category(hours=24 * 7)
            usage = await self.awareness.usage(hours=24 * 7, limit=8)

        busiest = await self.db.fetch_all(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM commands WHERE ts > ?"
            " GROUP BY day ORDER BY n DESC LIMIT 1",
            (iso(week_start),),
        )

        facts = {
            "period": f"{week_start.astimezone().strftime('%d %b')} to "
                      f"{utcnow().astimezone().strftime('%d %b')}",
            "tasks": counts,
            "success_rate": round(counts.get("completed", 0) / finished, 2) if finished else None,
            "commands": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM commands WHERE ts > ?", (iso(week_start),), default=0)),
            "meetings": len(await self.calendar.range(iso(week_start), now_iso())),
            "time_by_category_minutes": categories,
            "top_apps": {u["app"]: u["minutes"] for u in usage},
            "busiest_day": busiest[0]["day"] if busiest else None,
            "deadline_risks": [a for a in await self.deadline_risk() if a["risk"] != "low"],
        }

        narrative = await self._narrate(
            "Write a weekly productivity report from these facts. Cover what shipped, where time "
            "actually went, and one specific thing worth changing. No motivational filler, no "
            "invented numbers. Under 180 words, plain prose, no markdown.",
            facts,
        )
        return {"facts": facts, "report": narrative}

    # ==================================================================
    async def _narrate(self, instruction: str, facts: Dict[str, Any]) -> str:
        """Phrase pre-computed facts. If the model is unreachable, fall back to
        a plain rendering rather than returning nothing."""
        from ..db.database import dumps

        try:
            return await self.orchestrator.ask(
                f"{instruction}\n\nFACTS (these are authoritative, do not contradict them):\n"
                f"{dumps(facts)}",
                purpose="fast",
                max_tokens=700,
            )
        except Exception as exc:
            logger.warning("narration unavailable (%s) -- returning raw facts", exc)
            lines = []
            for key, value in facts.items():
                if value in ({}, [], None, ""):
                    continue
                lines.append(f"{key.replace('_', ' ')}: {value}")
            return "(AI narration unavailable.)\n" + "\n".join(lines)
