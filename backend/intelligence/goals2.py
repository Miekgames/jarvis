"""
Goals 2.0 — give JARVIS an objective, not a command.

    "I need to get ready for this project by Friday."

becomes: understand it, ask what's genuinely unclear, estimate how big it is,
break it into milestones with dates, turn each milestone into scheduled tasks,
track progress, adapt when things change, and write a completion report.

This wraps the Update 2 GoalService rather than replacing it -- decomposition,
task creation and dependency wiring still go through the task engine, so every
step keeps checkpointing, retries, cancellation and crash recovery.

Two rules the planner holds to:
  * Complexity is estimated *before* work starts, and a large estimate is
    surfaced for approval rather than silently spawning twenty tasks.
  * Questions are asked when the objective is genuinely underspecified, not as
    a ritual. An objective with a clear deliverable and deadline gets on with it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import ahead, humanize_duration, iso, new_id, now_iso, parse_iso, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.goals2")

MAX_MILESTONES = 6

_COMPLEXITY_SYSTEM = """You estimate how much work an objective needs, before any work starts.

Return ONLY JSON:
{
  "size": "trivial" | "small" | "medium" | "large",
  "estimated_hours": number,
  "milestone_count": integer between 1 and 6,
  "confidence": number 0..1,
  "needs_clarification": [ up to 3 short questions, only if genuinely blocking ],
  "reasoning": one sentence,
  "risks": [ up to 3 short risk notes ]
}

Guidance:
- "trivial" is something doable in one step right now; it needs one milestone.
- Only ask a question if you genuinely cannot proceed without the answer.
  A clear deliverable with a date needs no questions.
- confidence is how well you understand the objective, not how likely success is.
"""

_MILESTONE_SYSTEM = """You break an objective into milestones -- meaningful checkpoints, not individual actions.

Return ONLY a JSON array, at most %d entries:
[
  {
    "title": short outcome-shaped name (e.g. "Requirements gathered", not "Do research"),
    "detail": what must be true for this milestone to be complete,
    "depends_on": array of zero-based indices of earlier milestones,
    "effort_hours": number,
    "tasks": [
      {"title": ..., "task_type": "research"|"agent"|"organise_files"|"briefing"|"meeting_prep",
       "agent": "researcher"|"coder"|"planner"|"files"|"general"|"comms"|"meeting"|"system",
       "params": {...}}
    ]
  }
]

Rules:
- Milestones are checkpoints; tasks are the work inside them. 1-3 tasks each.
- Only use task types and agents from the lists above.
- Sequence with depends_on so independent milestones can run in parallel.
""" % MAX_MILESTONES


class GoalPlanner:
    def __init__(self, db, orchestrator, tasks, activity, goals, scheduler=None,
                 notifications=None, reasoning=None, planning=None):
        self.db = db
        self.orchestrator = orchestrator
        self.tasks = tasks
        self.activity = activity
        self.goals = goals            # the Update 2 GoalService
        self.scheduler = scheduler
        self.notifications = notifications
        self.reasoning = reasoning
        self.planning = planning

    # ==================================================================
    # 1. understand + estimate
    # ==================================================================
    async def assess(self, objective: str, deadline: Optional[str] = None) -> Dict[str, Any]:
        """Estimate complexity and surface blocking questions -- before doing
        anything. Cheap, and it prevents a vague one-liner turning into twenty
        background tasks."""
        context = [f"Objective: {objective}"]
        if deadline:
            due = parse_iso(deadline)
            if due:
                context.append(
                    f"Deadline: {due.astimezone().strftime('%A %d %B, %H:%M')} "
                    f"({humanize_duration((due - utcnow()).total_seconds())} from now)"
                )
        if self.planning:
            try:
                blocks = await self.planning.free_blocks()
                context.append(f"Free time today: {sum(b['minutes'] for b in blocks)} minutes")
            except Exception:
                pass

        raw = await self.orchestrator.ask(
            "\n".join(context), system=_COMPLEXITY_SYSTEM, purpose="reasoning", max_tokens=700
        )
        estimate = _parse_object(raw) or {}

        size = str(estimate.get("size", "medium")).lower()
        if size not in ("trivial", "small", "medium", "large"):
            size = "medium"
        questions = [str(q) for q in (estimate.get("needs_clarification") or [])][:3]

        return {
            "objective": objective,
            "size": size,
            "estimated_hours": _as_float(estimate.get("estimated_hours"), _DEFAULT_HOURS[size]),
            "milestone_count": max(1, min(MAX_MILESTONES,
                                          int(_as_float(estimate.get("milestone_count"), 3)))),
            "confidence": max(0.0, min(1.0, _as_float(estimate.get("confidence"), 0.6))),
            "questions": questions,
            "reasoning": truncate(estimate.get("reasoning", ""), 300),
            "risks": [str(r) for r in (estimate.get("risks") or [])][:3],
            "needs_approval": size == "large",
        }

    # ==================================================================
    # 2. create the goal, with milestones
    # ==================================================================
    async def create(
        self,
        objective: str,
        deadline: Optional[str] = None,
        project_id: Optional[str] = None,
        auto_start: bool = False,
        answers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        assessment = await self.assess(objective, deadline)

        enriched = objective
        if answers:
            enriched += "\n\nClarifications from the user:\n" + "\n".join(
                f"- {q}: {a}" for q, a in answers.items()
            )

        goal_id = new_id("goal")
        await self.db.execute(
            "INSERT INTO goals (id, objective, status, plan, created_at, updated_at, project_id,"
            " deadline, complexity, confidence, questions) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (goal_id, enriched, "planning", dumps([]), now_iso(), now_iso(), project_id,
             deadline, dumps(assessment), assessment["confidence"],
             dumps(assessment["questions"])),
        )
        await self.activity.record(
            f"Goal created: {truncate(objective, 70)} "
            f"({assessment['size']}, ~{assessment['estimated_hours']:.1f}h)",
            source="goals",
        )

        # Unanswered blocking questions stop here. Guessing at an ambiguous
        # objective is exactly what the spec says not to do.
        if assessment["questions"] and not answers:
            await self.db.execute(
                "UPDATE goals SET status = 'needs_input', updated_at = ? WHERE id = ?",
                (now_iso(), goal_id),
            )
            bus.publish("goal.needs_input", {"id": goal_id, "questions": assessment["questions"]},
                        source="goals")
            return {**await self.get(goal_id), "assessment": assessment,
                    "awaiting_answers": assessment["questions"]}

        milestones = await self._generate_milestones(goal_id, enriched, assessment, deadline)

        await self.db.execute(
            "UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
            ("awaiting_approval" if assessment["needs_approval"] else "planned", now_iso(), goal_id),
        )

        if self.reasoning:
            await self.reasoning.log_decision(
                subject="goal plan",
                summary=f"Planned '{truncate(objective, 60)}' as {len(milestones)} milestone(s).",
                rationale=assessment["reasoning"] or "Decomposed by estimated complexity.",
                inputs={"size": assessment["size"], "hours": assessment["estimated_hours"],
                        "deadline": deadline},
                alternatives=["run as a single agent task", "ask more questions first"],
            )

        record = await self.get(goal_id)
        bus.publish("goal.planned", {"id": goal_id, "milestones": len(milestones),
                                     "size": assessment["size"]}, source="goals")

        if auto_start and not assessment["needs_approval"]:
            return await self.start(goal_id)
        return record

    # ------------------------------------------------------------------
    async def _generate_milestones(
        self, goal_id: str, objective: str, assessment: Dict[str, Any], deadline: Optional[str]
    ) -> List[Dict[str, Any]]:
        prompt = (
            f"Objective: {objective}\n"
            f"Estimated size: {assessment['size']} (~{assessment['estimated_hours']:.1f} hours)\n"
            f"Produce about {assessment['milestone_count']} milestone(s)."
        )
        raw = await self.orchestrator.ask(
            prompt, system=_MILESTONE_SYSTEM, purpose="reasoning", max_tokens=2200
        )
        parsed = _parse_array(raw)

        if not parsed:
            # A goal with no plan is useless; one honest milestone beats none.
            parsed = [{
                "title": truncate(objective, 70),
                "detail": objective,
                "depends_on": [],
                "effort_hours": assessment["estimated_hours"],
                "tasks": [{"title": truncate(objective, 70), "task_type": "agent",
                           "agent": "general", "params": {}}],
            }]

        parsed = parsed[:MAX_MILESTONES]
        schedule = _spread_deadlines(parsed, deadline)
        created: List[Dict[str, Any]] = []

        for index, milestone in enumerate(parsed):
            milestone_id = new_id("mst")
            await self.db.execute(
                "INSERT INTO milestones (id, goal_id, title, detail, position, due_at, status,"
                " depends_on, task_ids, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (milestone_id, goal_id, truncate(milestone.get("title", f"Milestone {index + 1}"), 120),
                 truncate(milestone.get("detail", ""), 1000), index, schedule[index], "pending",
                 dumps([i for i in (milestone.get("depends_on") or []) if isinstance(i, int)]),
                 dumps([]), now_iso()),
            )
            created.append({**milestone, "id": milestone_id, "due_at": schedule[index]})

        # The plan column keeps the raw shape so the goal reads standalone.
        await self.db.execute(
            "UPDATE goals SET plan = ? WHERE id = ?",
            (dumps([{"title": m.get("title"), "detail": m.get("detail"),
                     "milestone_id": m["id"], "tasks": m.get("tasks", [])} for m in created]),
             goal_id),
        )
        return created

    # ==================================================================
    # 3. execute
    # ==================================================================
    async def start(self, goal_id: str) -> Dict[str, Any]:
        """Turn milestones into real tasks. Dependencies between milestones
        become dependencies between their tasks, so the engine handles ordering
        and parallelism."""
        goal = await self.get(goal_id)
        if not goal:
            raise KeyError(goal_id)
        if goal["status"] == "running":
            return goal
        if goal["status"] == "needs_input":
            raise PermissionError("This goal is waiting for answers before it can start.")

        milestones = await self.milestones(goal_id)
        milestone_tasks: Dict[int, List[str]] = {}
        plan = goal.get("plan") or []

        for milestone in milestones:
            spec = next((p for p in plan if p.get("milestone_id") == milestone["id"]), {})
            depends_on: List[str] = []
            for index in milestone["depends_on"]:
                depends_on.extend(milestone_tasks.get(index, []))

            created: List[str] = []
            for task_spec in (spec.get("tasks") or [{"title": milestone["title"],
                                                     "task_type": "agent", "agent": "general"}]):
                task_type = task_spec.get("task_type", "agent")
                if task_type not in self.tasks.handler_types():
                    task_type = "agent"
                params = dict(task_spec.get("params") or {})
                if task_type == "agent":
                    params.setdefault("objective", task_spec.get("title") or milestone["detail"])
                    params.setdefault("agent", task_spec.get("agent", "general"))
                elif task_type == "research":
                    params.setdefault("query", task_spec.get("title") or milestone["detail"])

                record = await self.tasks.create(
                    name=truncate(task_spec.get("title", milestone["title"]), 80),
                    task_type=task_type,
                    params=params,
                    description=milestone["detail"],
                    priority="normal",
                    project_id=goal.get("project_id"),
                    depends_on=depends_on or None,
                )
                created.append(record["id"])

            milestone_tasks[milestone["position"]] = created
            await self.db.execute(
                "UPDATE milestones SET task_ids = ?, status = 'active' WHERE id = ?",
                (dumps(created), milestone["id"]),
            )

        await self.db.execute(
            "UPDATE goals SET status = 'running', updated_at = ? WHERE id = ?",
            (now_iso(), goal_id),
        )
        await self._schedule_reminders(goal_id)

        total = sum(len(v) for v in milestone_tasks.values())
        await self.activity.record(
            f"Goal started: {len(milestones)} milestone(s), {total} task(s).", source="goals")
        bus.publish("goal.started", {"id": goal_id, "milestones": len(milestones), "tasks": total},
                    source="goals")
        return await self.progress(goal_id)

    async def _schedule_reminders(self, goal_id: str) -> None:
        """A deadline nobody is reminded about is decoration."""
        if self.scheduler is None:
            return
        goal = await self.get(goal_id)
        for milestone in await self.milestones(goal_id):
            due = parse_iso(milestone["due_at"])
            if not due:
                continue
            remind_at = due - timedelta(hours=12)
            if remind_at <= utcnow():
                continue
            try:
                await self.scheduler.create_once(
                    name=f"Milestone due: {truncate(milestone['title'], 50)}",
                    run_at=iso(remind_at),
                    action={"kind": "notify",
                            "title": f"Milestone due tomorrow: {truncate(milestone['title'], 50)}",
                            "body": truncate(goal.get("objective", ""), 160),
                            "priority": "high"},
                )
            except Exception:
                logger.debug("could not schedule milestone reminder", exc_info=True)

    # ==================================================================
    # 4. track
    # ==================================================================
    async def progress(self, goal_id: str) -> Dict[str, Any]:
        goal = await self.get(goal_id)
        if not goal:
            raise KeyError(goal_id)

        milestones = await self.milestones(goal_id)
        rendered: List[Dict[str, Any]] = []
        done_count = 0

        for milestone in milestones:
            task_states = []
            for task_id in milestone["task_ids"]:
                task = await self.tasks.get(task_id)
                if task:
                    task_states.append({
                        "id": task_id, "name": task["name"], "status": task["status"],
                        "progress": round(float(task.get("progress") or 0) * 100),
                        "error": task.get("error"),
                    })

            statuses = [t["status"] for t in task_states]
            if statuses and all(s == "completed" for s in statuses):
                status = "complete"
                done_count += 1
                if milestone["status"] != "complete":
                    await self.db.execute(
                        "UPDATE milestones SET status = 'complete', completed_at = ? WHERE id = ?",
                        (now_iso(), milestone["id"]),
                    )
                    bus.publish("goal.milestone", {"goal_id": goal_id, "title": milestone["title"]},
                                source="goals")
            elif any(s == "failed" for s in statuses):
                status = "blocked"
            elif any(s == "running" for s in statuses):
                status = "active"
            else:
                status = milestone["status"]

            due = parse_iso(milestone["due_at"])
            rendered.append({
                "id": milestone["id"],
                "n": milestone["position"] + 1,
                "title": milestone["title"],
                "detail": milestone["detail"],
                "status": status,
                "due_at": milestone["due_at"],
                "overdue": bool(due and due < utcnow() and status != "complete"),
                "tasks": task_states,
            })

        percent = round(100 * done_count / max(1, len(rendered)))
        overall = goal["status"]
        if rendered and done_count == len(rendered) and overall == "running":
            overall = "completed"
            # Mark complete first, then write the report from the state we
            # already have. Calling report() here would re-enter progress().
            await self.db.execute(
                "UPDATE goals SET status = 'completed', finished_at = ?, updated_at = ?"
                " WHERE id = ?", (now_iso(), now_iso(), goal_id),
            )
            await self._write_report(goal_id, goal, rendered, done_count)
        elif any(m["status"] == "blocked" for m in rendered) and overall == "running":
            overall = "blocked"

        return {
            "goal_id": goal_id,
            "objective": goal["objective"],
            "status": overall,
            "deadline": goal.get("deadline"),
            "complexity": goal.get("complexity", {}),
            "confidence": goal.get("confidence"),
            "questions": goal.get("questions", []),
            "milestones": rendered,
            "completed_milestones": done_count,
            "total_milestones": len(rendered),
            "percent": percent,
            "adaptations": goal.get("adaptations", []),
            "report": goal.get("report"),
        }

    # ==================================================================
    # 5. adapt
    # ==================================================================
    async def adapt(self, goal_id: str, reason: str = "") -> Dict[str, Any]:
        """Re-plan the remaining milestones when circumstances change -- a
        blocked task, a moved deadline, a new constraint. Completed work is
        never rewritten."""
        state = await self.progress(goal_id)
        remaining = [m for m in state["milestones"] if m["status"] != "complete"]
        if not remaining:
            return {"adapted": False, "message": "Every milestone is already complete."}

        blocked = [m for m in remaining if m["status"] == "blocked"]
        detail = reason or (
            f"{len(blocked)} milestone(s) blocked by failed tasks" if blocked
            else "requested re-plan"
        )

        advice = await self.orchestrator.ask(
            "A goal is in progress and needs its remaining plan revised.\n\n"
            f"Objective: {state['objective']}\n"
            f"Reason for revision: {detail}\n"
            f"Completed: {[m['title'] for m in state['milestones'] if m['status'] == 'complete']}\n"
            f"Remaining: {[{'title': m['title'], 'status': m['status']} for m in remaining]}\n\n"
            "In three sentences: what should change, what should be dropped, and what should be "
            "done differently. Be specific and do not restate the plan.",
            purpose="reasoning", max_tokens=500,
        )

        adaptations = state.get("adaptations") or []
        adaptations.append({"at": now_iso(), "reason": detail, "advice": truncate(advice, 900)})
        await self.db.execute(
            "UPDATE goals SET adaptations = ?, updated_at = ? WHERE id = ?",
            (dumps(adaptations), now_iso(), goal_id),
        )
        await self.activity.record(
            f"Goal plan adapted: {truncate(detail, 70)}", level="warn" if blocked else "info",
            source="goals",
        )
        bus.publish("goal.adapted", {"id": goal_id, "reason": detail}, source="goals")

        if self.notifications and blocked:
            await self.notifications.create(
                title="A goal needs attention",
                body=truncate(advice, 300), priority="high", source="goals",
                action={"kind": "open_goal", "goal_id": goal_id},
            )
        return {"adapted": True, "reason": detail, "advice": advice}

    # ==================================================================
    # 6. report
    # ==================================================================
    async def _write_report(self, goal_id: str, goal: Dict[str, Any],
                            milestones: List[Dict[str, Any]], done_count: int) -> None:
        narrative = await self._compose_report(goal, milestones, done_count)
        await self.db.execute(
            "UPDATE goals SET report = ? WHERE id = ?", (narrative, goal_id))
        await self.activity.record(
            f"Goal complete: {truncate(goal['objective'], 70)}", source="goals")
        bus.publish("goal.completed", {"id": goal_id}, source="goals")
        if self.notifications:
            await self.notifications.create(
                title="Goal complete", body=truncate(narrative, 400),
                priority="normal", source="goals",
            )

    async def report(self, goal_id: str) -> Dict[str, Any]:
        """Final completion report, built from what actually happened."""
        state = await self.progress(goal_id)
        goal = await self.get(goal_id)
        narrative = await self._compose_report(
            goal, state["milestones"], state["completed_milestones"])
        return {"goal_id": goal_id, "objective": goal["objective"],
                "facts": {"milestones": state["milestones"]}, "report": narrative}

    async def _compose_report(self, goal: Dict[str, Any], milestones: List[Dict[str, Any]],
                              done_count: int) -> str:
        outputs = []
        for milestone in milestones:
            for task in milestone["tasks"]:
                record = await self.tasks.get(task["id"])
                result = record.get("result") if record else None
                if result:
                    outputs.append({"milestone": milestone["title"], "task": task["name"],
                                    "result": truncate(dumps(result), 900)})

        created = parse_iso(goal.get("created_at"))
        elapsed = (utcnow() - created).total_seconds() if created else 0
        estimate = (goal.get("complexity") or {}).get("estimated_hours")

        facts = {
            "objective": goal["objective"],
            "milestones_completed": f"{done_count}/{len(milestones)}",
            "milestones": [{"title": m["title"], "status": m["status"]} for m in milestones],
            "elapsed": humanize_duration(elapsed),
            "estimated_hours": estimate,
            "adaptations": len(goal.get("adaptations") or []),
            "outputs": outputs[:8],
        }

        try:
            return await self.orchestrator.ask(
                "Write a completion report for this goal. Say what was achieved, what each "
                "milestone produced, anything that did not get done, and whether it took longer "
                "than estimated. Be factual, not congratulatory. Under 180 words, plain prose, "
                f"no markdown.\n\nFACTS (authoritative):\n{dumps(facts)}",
                purpose="conversation", max_tokens=900,
            )
        except Exception as exc:
            logger.warning("report narration unavailable: %s", exc)
            return (f"Goal '{truncate(goal['objective'], 80)}' finished "
                    f"{facts['milestones_completed']} milestones in {facts['elapsed']}. "
                    "(AI narration unavailable.)")

    # ==================================================================
    # reads / control
    # ==================================================================
    async def get(self, goal_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
        return _decode_goal(row) if row else {}

    async def milestones(self, goal_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM milestones WHERE goal_id = ? ORDER BY position", (goal_id,))
        out = []
        for row in rows:
            item = dict(row)
            item["depends_on"] = loads(item.get("depends_on"), [])
            item["task_ids"] = loads(item.get("task_ids"), [])
            out.append(item)
        return out

    async def list(self, active_only: bool = False, limit: int = 25) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM goals"
        if active_only:
            sql += " WHERE status IN ('planning','planned','running','needs_input','awaiting_approval','blocked')"
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [_decode_goal(r) for r in await self.db.fetch_all(sql, (limit,))]

    async def answer(self, goal_id: str, answers: Dict[str, str]) -> Dict[str, Any]:
        """Supply the clarifications a goal was waiting for, then plan it."""
        goal = await self.get(goal_id)
        if not goal:
            raise KeyError(goal_id)

        enriched = goal["objective"] + "\n\nClarifications from the user:\n" + "\n".join(
            f"- {q}: {a}" for q, a in answers.items()
        )
        await self.db.execute(
            "UPDATE goals SET objective = ?, status = 'planning', updated_at = ? WHERE id = ?",
            (enriched, now_iso(), goal_id),
        )
        assessment = loads(goal.get("complexity"), {}) or {}
        assessment.setdefault("estimated_hours", 2.0)
        assessment.setdefault("milestone_count", 3)
        assessment.setdefault("size", "medium")

        await self._generate_milestones(goal_id, enriched, assessment, goal.get("deadline"))
        await self.db.execute(
            "UPDATE goals SET status = 'planned', updated_at = ? WHERE id = ?",
            (now_iso(), goal_id),
        )
        return await self.progress(goal_id)

    async def cancel(self, goal_id: str) -> Dict[str, Any]:
        cancelled = 0
        for milestone in await self.milestones(goal_id):
            for task_id in milestone["task_ids"]:
                try:
                    await self.tasks.cancel(task_id)
                    cancelled += 1
                except Exception:
                    continue
        await self.db.execute(
            "UPDATE goals SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), goal_id),
        )
        await self.activity.record(f"Goal cancelled ({cancelled} task(s) stopped).",
                                   level="warn", source="goals")
        return {"goal_id": goal_id, "cancelled_tasks": cancelled}

    async def refresh_all(self) -> int:
        """Keeps goal status in step with task status without subscribing to
        every task event."""
        active = await self.list(active_only=True, limit=50)
        for goal in active:
            if goal["status"] in ("running", "blocked"):
                try:
                    await self.progress(goal["id"])
                except Exception:
                    logger.debug("goal refresh failed for %s", goal["id"], exc_info=True)
        return len(active)


# ---------------------------------------------------------------------------
_DEFAULT_HOURS = {"trivial": 0.25, "small": 1.0, "medium": 4.0, "large": 16.0}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _spread_deadlines(milestones: List[Dict[str, Any]], deadline: Optional[str]) -> List[Optional[str]]:
    """Distribute milestone dates by effort share, ending at the deadline.

    Deterministic arithmetic rather than asking a model for dates -- a planner
    that invents a date you can't meet is worse than one that gives none."""
    due = parse_iso(deadline)
    if due is None:
        return [None] * len(milestones)

    start = utcnow()
    span = (due - start).total_seconds()
    if span <= 0:
        return [iso(due)] * len(milestones)

    efforts = [max(0.1, _as_float(m.get("effort_hours"), 1.0)) for m in milestones]
    total = sum(efforts) or 1.0

    dates: List[Optional[str]] = []
    consumed = 0.0
    for effort in efforts:
        consumed += effort
        dates.append(iso(start + timedelta(seconds=span * (consumed / total))))
    return dates


def _decode_goal(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["plan"] = loads(item.get("plan"), [])
    item["result"] = loads(item.get("result"), None)
    item["complexity"] = loads(item.get("complexity"), {})
    item["questions"] = loads(item.get("questions"), [])
    item["adaptations"] = loads(item.get("adaptations"), [])
    return item


def _parse_object(raw: str) -> Optional[Dict[str, Any]]:
    import json
    import re

    text = re.sub(r"<think>.*?</think>", " ", raw or "", flags=re.S | re.I)
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_array(raw: str) -> List[Dict[str, Any]]:
    import json
    import re

    text = re.sub(r"<think>.*?</think>", " ", raw or "", flags=re.S | re.I)
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict) and item.get("title")]
