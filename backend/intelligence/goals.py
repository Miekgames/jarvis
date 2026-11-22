"""
Goal-based execution — agent 2.0.

You state an outcome ("get me ready for the product review on Thursday") and
JARVIS decomposes it into concrete steps, works out which steps depend on which,
picks the right specialist agent for each, and runs them through the existing
task engine — so every step inherits checkpointing, retries, cancellation and
crash recovery for free.

The decomposition is written to the goal record *before* anything runs, so you
can see the whole plan and cancel it before work starts.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import new_id, now_iso, truncate
from ..db.database import dumps, loads
from ..tasks.handlers.agent import AGENT_PROFILES

logger = logging.getLogger("jarvis.goals")

MAX_STEPS = 8

_PLANNER_SYSTEM = """You decompose an objective into concrete executable steps.

Return ONLY a JSON array. Each element:
{
  "title":     short imperative step name,
  "detail":    precisely what this step must produce,
  "agent":     one of: general, researcher, coder, planner, files, system, comms, meeting,
  "task_type": one of: research, agent, meeting_prep, briefing, diagnostics, organise_files,
  "depends_on": array of zero-based indices of earlier steps this needs,
  "params":    object of parameters for the task type (e.g. {"query": "..."} for research)
}

Rules:
- Between 1 and %d steps. Fewer is better; do not pad.
- Only include steps that can be done with tools: searching the web, reading and writing
  files, reading the calendar, checking the system, drafting text. Never invent a step
  that needs a capability that does not exist.
- Use depends_on only for genuine ordering requirements, so independent steps run in
  parallel.
- If the objective is a single action, return exactly one step.
""" % MAX_STEPS


class GoalService:
    def __init__(self, db, orchestrator, tasks, activity, reasoning=None):
        self.db = db
        self.orchestrator = orchestrator
        self.tasks = tasks
        self.activity = activity
        self.reasoning = reasoning

    # ==================================================================
    async def plan(self, objective: str) -> Dict[str, Any]:
        """Decompose without executing. This is what 'figure it out' shows you
        before it commits to anything."""
        raw = await self.orchestrator.ask(
            f"Objective: {objective}", system=_PLANNER_SYSTEM, purpose="reasoning", max_tokens=1800
        )
        steps = _parse_steps(raw)
        if not steps:
            steps = [{
                "title": truncate(objective, 60),
                "detail": objective,
                "agent": "general",
                "task_type": "agent",
                "depends_on": [],
                "params": {},
            }]
        return {"objective": objective, "steps": steps[:MAX_STEPS], "raw": truncate(raw, 2000)}

    # ==================================================================
    async def create(self, objective: str, auto_start: bool = True) -> Dict[str, Any]:
        goal_id = new_id("goal")
        await self.db.execute(
            "INSERT INTO goals (id, objective, status, plan, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (goal_id, objective, "planning", dumps([]), now_iso(), now_iso()),
        )
        await self.activity.record(f"New goal: {truncate(objective, 80)}", source="goals")

        try:
            planned = await self.plan(objective)
        except Exception as exc:
            await self.db.execute(
                "UPDATE goals SET status = 'failed', result = ?, updated_at = ?, finished_at = ?"
                " WHERE id = ?",
                (dumps({"error": str(exc)}), now_iso(), now_iso(), goal_id),
            )
            raise

        steps = planned["steps"]
        await self.db.execute(
            "UPDATE goals SET plan = ?, status = ?, updated_at = ? WHERE id = ?",
            (dumps(steps), "planned", now_iso(), goal_id),
        )

        if self.reasoning:
            await self.reasoning.log_decision(
                subject="goal decomposition",
                summary=f"Split '{truncate(objective, 60)}' into {len(steps)} step(s).",
                rationale="; ".join(f"{i + 1}. {s['title']}" for i, s in enumerate(steps)),
                inputs={"objective": objective},
                alternatives=["run it as a single opaque agent task"],
            )

        bus.publish("goal.planned", {"id": goal_id, "objective": objective, "steps": steps},
                    source="goals")

        if auto_start:
            return await self.start(goal_id)
        return await self.get(goal_id)

    # ==================================================================
    async def start(self, goal_id: str) -> Dict[str, Any]:
        """Turn the plan into real tasks, wiring dependencies so the engine
        handles ordering and parallelism."""
        goal = await self.get(goal_id)
        if not goal:
            raise KeyError(goal_id)
        if goal["status"] not in ("planned", "planning"):
            return goal

        steps: List[Dict[str, Any]] = goal["plan"]
        created_ids: List[str] = []

        for index, step in enumerate(steps):
            depends = [
                created_ids[i] for i in step.get("depends_on", [])
                if isinstance(i, int) and 0 <= i < len(created_ids)
            ]
            task_type = step.get("task_type", "agent")
            if task_type not in self.tasks.handler_types():
                task_type = "agent"

            params = dict(step.get("params") or {})
            if task_type == "agent":
                params.setdefault("objective", step.get("detail") or step.get("title", ""))
                params.setdefault("agent", step.get("agent", "general")
                                  if step.get("agent") in AGENT_PROFILES else "general")
            elif task_type == "research":
                params.setdefault("query", step.get("detail") or step.get("title", ""))

            record = await self.tasks.create(
                name=truncate(step.get("title", f"Step {index + 1}"), 80),
                task_type=task_type,
                params=params,
                description=step.get("detail", ""),
                priority="normal",
                depends_on=depends or None,
            )
            created_ids.append(record["id"])
            step["task_id"] = record["id"]

        await self.db.execute(
            "UPDATE goals SET status = 'running', plan = ?, root_task = ?, updated_at = ?"
            " WHERE id = ?",
            (dumps(steps), created_ids[0] if created_ids else None, now_iso(), goal_id),
        )
        await self.activity.record(
            f"Goal running: {len(created_ids)} task(s) queued for "
            f"'{truncate(goal['objective'], 60)}'.",
            source="goals",
        )
        bus.publish("goal.started", {"id": goal_id, "tasks": created_ids}, source="goals")
        return await self.get(goal_id)

    # ==================================================================
    async def refresh(self, goal_id: str) -> Dict[str, Any]:
        """Recompute goal status from the state of its tasks."""
        goal = await self.get(goal_id)
        if not goal or goal["status"] in ("completed", "failed", "cancelled"):
            return goal

        task_ids = [s.get("task_id") for s in goal["plan"] if s.get("task_id")]
        if not task_ids:
            return goal

        placeholders = ",".join("?" for _ in task_ids)
        rows = await self.db.fetch_all(
            f"SELECT id, status, result, error FROM tasks WHERE id IN ({placeholders})", task_ids
        )
        statuses = [r["status"] for r in rows]

        if all(s == "completed" for s in statuses):
            outputs = {r["id"]: loads(r["result"], None) for r in rows}
            await self.db.execute(
                "UPDATE goals SET status = 'completed', result = ?, updated_at = ?,"
                " finished_at = ? WHERE id = ?",
                (dumps({"outputs": outputs}), now_iso(), now_iso(), goal_id),
            )
            await self.activity.record(
                f"Goal complete: {truncate(goal['objective'], 70)}", source="goals")
            bus.publish("goal.completed", {"id": goal_id}, source="goals")
        elif any(s == "failed" for s in statuses):
            failures = [f"{r['id']}: {truncate(r['error'], 120)}"
                        for r in rows if r["status"] == "failed"]
            await self.db.execute(
                "UPDATE goals SET status = 'failed', result = ?, updated_at = ?, finished_at = ?"
                " WHERE id = ?",
                (dumps({"failures": failures}), now_iso(), now_iso(), goal_id),
            )
            bus.publish("goal.failed", {"id": goal_id, "failures": failures}, source="goals")
        elif all(s == "cancelled" for s in statuses):
            await self.db.execute(
                "UPDATE goals SET status = 'cancelled', updated_at = ?, finished_at = ?"
                " WHERE id = ?",
                (now_iso(), now_iso(), goal_id),
            )
        return await self.get(goal_id)

    # ==================================================================
    async def progress(self, goal_id: str) -> Dict[str, Any]:
        """A human-readable progress report -- the autonomous status update."""
        goal = await self.refresh(goal_id)
        if not goal:
            raise KeyError(goal_id)

        steps = []
        done = 0
        for index, step in enumerate(goal["plan"]):
            task = await self.tasks.get(step["task_id"]) if step.get("task_id") else {}
            status = task.get("status", "not started")
            if status == "completed":
                done += 1
            steps.append({
                "n": index + 1,
                "title": step.get("title"),
                "agent": step.get("agent", "general"),
                "status": status,
                "progress": round(float(task.get("progress") or 0) * 100),
                "current_step": task.get("current_step", ""),
                "error": task.get("error"),
            })

        current = next((s for s in steps if s["status"] == "running"), None)
        return {
            "goal_id": goal_id,
            "objective": goal["objective"],
            "status": goal["status"],
            "completed_steps": done,
            "total_steps": len(steps),
            "percent": round(100 * done / max(1, len(steps))),
            "current": current,
            "steps": steps,
            "result": goal.get("result"),
        }

    async def cancel(self, goal_id: str) -> Dict[str, Any]:
        goal = await self.get(goal_id)
        if not goal:
            raise KeyError(goal_id)
        cancelled = 0
        for step in goal["plan"]:
            if step.get("task_id"):
                try:
                    await self.tasks.cancel(step["task_id"])
                    cancelled += 1
                except Exception:
                    continue
        await self.db.execute(
            "UPDATE goals SET status = 'cancelled', updated_at = ?, finished_at = ? WHERE id = ?",
            (now_iso(), now_iso(), goal_id),
        )
        await self.activity.record(
            f"Goal cancelled: {truncate(goal['objective'], 70)}", level="warn", source="goals")
        return {"goal_id": goal_id, "cancelled_tasks": cancelled}

    # ==================================================================
    async def get(self, goal_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
        return _decode(row) if row else {}

    async def list(self, limit: int = 25, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM goals"
        if active_only:
            sql += " WHERE status IN ('planning','planned','running')"
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [_decode(r) for r in await self.db.fetch_all(sql, (limit,))]

    async def refresh_all(self) -> int:
        """Called periodically so goal status tracks task status without the
        goal service needing to subscribe to every task event."""
        active = await self.list(active_only=True, limit=50)
        for goal in active:
            if goal["status"] == "running":
                await self.refresh(goal["id"])
        return len(active)


# ---------------------------------------------------------------------------
def _parse_steps(raw: str) -> List[Dict[str, Any]]:
    """Models wrap JSON in prose and fences often enough that this needs to be
    forgiving rather than strict."""
    import json
    import re

    text = (raw or "").strip()

    # Local reasoning models (deepseek-r1, qwen3) emit their scratchpad in
    # <think> tags. That text often contains brackets, which would derail the
    # bracket scan below, so remove it before looking for JSON.
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", " ", text, flags=re.S | re.I)   # unterminated block

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    parsed = None
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            parsed = None

    # Some models return a single step as a bare object rather than a list.
    if parsed is None:
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            try:
                single = json.loads(text[obj_start:obj_end + 1])
                if isinstance(single, dict):
                    parsed = [single]
            except json.JSONDecodeError:
                parsed = None

    if not isinstance(parsed, list):
        return []

    steps = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        depends = item.get("depends_on") or []
        steps.append({
            "title": str(item["title"])[:120],
            "detail": str(item.get("detail", ""))[:1000],
            "agent": str(item.get("agent", "general")),
            "task_type": str(item.get("task_type", "agent")),
            "depends_on": [i for i in depends if isinstance(i, int)],
            "params": item.get("params") if isinstance(item.get("params"), dict) else {},
        })
    return steps


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["plan"] = loads(item.get("plan"), [])
    item["result"] = loads(item.get("result"), None)
    return item
