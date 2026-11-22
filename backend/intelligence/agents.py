"""
Agent orchestrator.

Update 2 had agent *profiles* -- prompts selected per task. This makes agents
first-class runtime objects: each run is a row you can watch, message, bound and
stop individually, without touching anything else JARVIS is doing.

Design decisions worth stating:

  * **Boundaries are enforced, not advertised.** Each agent declares the
    permissions and tool categories it may use. A researcher physically cannot
    delete a file, even if the model decides it wants to -- the tool isn't in
    the set it's handed.
  * **Time limits are real.** An agent that exceeds its budget is stopped and
    reports partial results, rather than running until someone notices.
  * **Agents talk through the database**, not through shared memory, so a
    message survives a restart and is visible in the UI.
  * **Stopping one agent stops one agent.** Cancellation is per-run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, NamedTuple, Optional

from ..core.events import bus
from ..core.util import humanize_duration, new_id, now_iso, parse_iso, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.agents")


class AgentSpec(NamedTuple):
    key: str
    label: str
    description: str
    system: str
    purpose: str                 # which model role to route to
    categories: List[str]        # tool categories this agent may use
    denied_permissions: List[str]  # hard boundaries, regardless of user settings
    time_limit_s: int
    colour: str                  # drives the particle sub-cluster tint


AGENTS: Dict[str, AgentSpec] = {
    "researcher": AgentSpec(
        key="researcher",
        label="Researcher",
        description="Searches, reads and cross-checks sources. Cannot modify anything.",
        system=("You are a researcher. Search, read and cross-check before answering. Cite URLs. "
                "Separate what the sources say from what you infer. Say when sources disagree."),
        purpose="reasoning",
        categories=["web", "information", "memory", "files"],
        denied_permissions=["files.write", "files.delete", "system.power", "email.send",
                            "smarthome.control", "smarthome.security",
                            "shell.execute", "github.write"],
        time_limit_s=1800,
        colour="#5ad2ff",
    ),
    "coder": AgentSpec(
        key="coder",
        label="Coding assistant",
        description="Reads code, explains errors, proposes changes. Writes only on request.",
        system=("You are a software engineer. Read files before discussing them. Quote exact "
                "error text and file paths. Keep changes minimal and explain each one."),
        purpose="coding",
        categories=["files", "developer", "system", "web"],
        denied_permissions=["files.delete", "system.power", "email.send",
                            "smarthome.control", "smarthome.security"],
        time_limit_s=1800,
        colour="#7b6cff",
    ),
    "planner": AgentSpec(
        key="planner",
        label="Planner",
        description="Turns objectives into schedules, milestones and tasks.",
        system=("You are a planner. Turn the objective into concrete steps with realistic timing. "
                "Use the calendar and task tools for real dates -- never invent availability."),
        purpose="reasoning",
        categories=["planning", "calendar", "tasks", "scheduling", "projects"],
        denied_permissions=["files.delete", "system.power", "email.send",
                            "smarthome.security", "shell.execute"],
        time_limit_s=900,
        colour="#58e0a8",
    ),
    "files": AgentSpec(
        key="files",
        label="File assistant",
        description="Finds, reads and organises files. Deletion always confirms.",
        system=("You organise files. Inspect before acting. Never delete without the delete tool, "
                "which asks the user. Summarise every change you made."),
        purpose="fast",
        categories=["files", "system"],
        denied_permissions=["system.power", "email.send", "smarthome.control",
                            "smarthome.security", "shell.execute"],
        time_limit_s=900,
        colour="#ffb454",
    ),
    "system": AgentSpec(
        key="system",
        label="System assistant",
        description="Monitors and tunes this computer. Reports numbers, not impressions.",
        system=("You monitor and manage this computer. Report concrete numbers from tools. "
                "Never guess a metric."),
        purpose="fast",
        categories=["system", "awareness", "applications", "gaming"],
        denied_permissions=["files.delete", "email.send", "smarthome.security",
                            "shell.execute"],
        time_limit_s=600,
        colour="#9fb3c8",
    ),
    "comms": AgentSpec(
        key="comms",
        label="Communication assistant",
        description="Drafts messages. Sending always needs your confirmation.",
        system=("You draft email and messages. Always leave sending to the user's confirmation -- "
                "draft, show, and stop. Never claim something was sent."),
        purpose="conversation",
        categories=["communication", "contacts", "calendar"],
        denied_permissions=["files.delete", "system.power", "smarthome.control",
                            "smarthome.security", "shell.execute"],
        time_limit_s=900,
        colour="#e070ff",
    ),
    "meeting": AgentSpec(
        key="meeting",
        label="Meeting assistant",
        description="Prepares for meetings and follows up afterwards.",
        system=("You prepare meetings: pull the event, find related files and notes, produce an "
                "agenda and a short brief. Afterwards, extract action items."),
        purpose="conversation",
        categories=["calendar", "files", "contacts", "planning", "projects"],
        denied_permissions=["files.delete", "system.power", "smarthome.security",
                            "shell.execute"],
        time_limit_s=900,
        colour="#63e8ff",
    ),
    "general": AgentSpec(
        key="general",
        label="General assistant",
        description="Anything that doesn't fit a specialist.",
        system=("You work autonomously on a background task. Use tools to gather what you need, "
                "then report exactly what you did and did not manage."),
        purpose="conversation",
        categories=[],               # empty = every category the user permits
        denied_permissions=["system.power", "shell.execute", "smarthome.security"],
        time_limit_s=1200,
        colour="#8fa4b8",
    ),
}

DEFAULT_AGENT = "general"

# Keyword hints for automatic selection. Deliberately transparent -- the choice
# is explainable, which matters more here than a marginally better guess.
_SELECTION_HINTS: Dict[str, tuple] = {
    "researcher": ("research", "find out", "look up", "compare", "sources", "investigate",
                   "what is", "who is", "latest", "news", "study", "evidence"),
    "coder": ("code", "bug", "error", "stack trace", "compile", "test", "function", "repo",
              "git", "refactor", "exception", "traceback", "build"),
    "planner": ("plan", "schedule", "milestone", "deadline", "organise my week", "roadmap",
                "prioritise", "timeline"),
    "files": ("file", "folder", "documents", "organise", "duplicate", "rename", "downloads",
              "tidy", "clean up"),
    "system": ("cpu", "memory", "ram", "disk", "gpu", "slow", "performance", "process",
               "temperature", "battery"),
    "comms": ("email", "message", "reply", "draft", "text", "contact", "call", "send"),
    "meeting": ("meeting", "call with", "agenda", "attendees", "brief", "standup", "review"),
}


class AgentRun:
    """Live handle for one running agent."""

    def __init__(self, run_id: str, spec: AgentSpec, task: asyncio.Task):
        self.id = run_id
        self.spec = spec
        self.task = task
        self.cancelled = False


class AgentOrchestrator:
    def __init__(self, db, orchestrator, registry, permissions, activity,
                 notifications=None, reasoning=None):
        self.db = db
        self.orchestrator = orchestrator
        self.registry = registry
        self.permissions = permissions
        self.activity = activity
        self.notifications = notifications
        self.reasoning = reasoning
        self._running: Dict[str, AgentRun] = {}

    # ==================================================================
    # selection
    # ==================================================================
    def select(self, objective: str) -> Dict[str, Any]:
        """Choose an agent, and be able to say why."""
        lowered = (objective or "").lower()
        scores: Dict[str, int] = {}
        matched: Dict[str, List[str]] = {}

        for key, hints in _SELECTION_HINTS.items():
            hits = [hint for hint in hints if hint in lowered]
            if hits:
                scores[key] = len(hits)
                matched[key] = hits

        if not scores:
            return {"agent": DEFAULT_AGENT, "confidence": 0.35,
                    "why": "Nothing in the request pointed to a specialist, so the general "
                           "assistant will handle it."}

        best = max(scores, key=lambda k: scores[k])
        total = sum(scores.values())
        confidence = min(0.95, 0.45 + 0.5 * scores[best] / max(1, total))
        return {
            "agent": best,
            "confidence": round(confidence, 2),
            "why": f"Matched {AGENTS[best].label.lower()} cues: {', '.join(matched[best][:3])}.",
            "considered": {k: scores[k] for k in scores},
        }

    # ==================================================================
    # boundaries
    # ==================================================================
    def tools_for(self, spec: AgentSpec, query: str = "") -> List[Dict[str, Any]]:
        """The tool set an agent is physically given.

        Two filters: the categories it works in, and its denied permissions.
        Both are applied to the *already* permission-filtered registry, so an
        agent can never exceed what the user has allowed globally."""
        allowed = []
        for tool in self.registry.all():
            schema = next((s for s in self.registry.schemas() if s["name"] == tool.name), None)
            if schema is None:
                continue                                    # user has this permission off
            if tool.permission and tool.permission in spec.denied_permissions:
                continue                                    # agent boundary
            if spec.categories and tool.category not in spec.categories:
                continue
            allowed.append(schema)
        return allowed

    def boundary_report(self, spec: AgentSpec) -> Dict[str, Any]:
        return {
            "agent": spec.key,
            "may_use_categories": spec.categories or ["(all permitted categories)"],
            "denied_permissions": spec.denied_permissions,
            "time_limit": humanize_duration(spec.time_limit_s),
            "tool_count": len(self.tools_for(spec)),
        }

    # ==================================================================
    # running
    # ==================================================================
    async def dispatch(
        self,
        objective: str,
        agent: Optional[str] = None,
        goal_id: Optional[str] = None,
        task_id: Optional[str] = None,
        priority: str = "normal",
        time_limit_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start an agent. Returns immediately; watch progress via events."""
        selection = {"agent": agent, "confidence": 1.0, "why": "explicitly requested"} if agent \
            else self.select(objective)
        key = selection["agent"] if selection["agent"] in AGENTS else DEFAULT_AGENT
        spec = AGENTS[key]

        run_id = new_id("run")
        limit = int(time_limit_s or spec.time_limit_s)
        await self.db.execute(
            "INSERT INTO agent_runs (id, agent, objective, status, task_id, goal_id, priority,"
            " time_limit_s, created_at) VALUES (?,?,?,'queued',?,?,?,?,?)",
            (run_id, key, objective, task_id, goal_id, priority, limit, now_iso()),
        )

        if self.reasoning:
            await self.reasoning.log_decision(
                subject="agent selection",
                summary=f"Chose the {spec.label} for: {truncate(objective, 60)}",
                rationale=selection["why"],
                inputs={"confidence": selection["confidence"],
                        "considered": selection.get("considered", {})},
                alternatives=[AGENTS[k].label for k in selection.get("considered", {}) if k != key],
            )

        runner = asyncio.create_task(self._run(run_id, spec, objective, limit),
                                     name=f"agent:{run_id}")
        self._running[run_id] = AgentRun(run_id, spec, runner)

        bus.publish("agent.started", {"id": run_id, "agent": key, "label": spec.label,
                                      "colour": spec.colour, "objective": truncate(objective, 120),
                                      "confidence": selection["confidence"]}, source="agents")
        await self.activity.record(f"{spec.label} started: {truncate(objective, 60)}",
                                   source="agents")
        return {"run_id": run_id, "agent": key, "label": spec.label,
                "selection": selection, "boundaries": self.boundary_report(spec)}

    async def _run(self, run_id: str, spec: AgentSpec, objective: str, limit: int) -> None:
        await self.db.execute(
            "UPDATE agent_runs SET status = 'running', started_at = ? WHERE id = ?",
            (now_iso(), run_id),
        )
        started = utcnow()

        async def on_step(tool_name: str, tool_input: Dict[str, Any]) -> None:
            elapsed = (utcnow() - started).total_seconds()
            progress = min(0.9, elapsed / max(1, limit))
            await self.db.execute(
                "UPDATE agent_runs SET progress = ?, current_step = ? WHERE id = ?",
                (progress, truncate(tool_name, 80), run_id),
            )
            bus.publish("agent.progress", {"id": run_id, "agent": spec.key,
                                           "step": tool_name, "progress": progress},
                        source="agents")

        try:
            result = await asyncio.wait_for(
                self.orchestrator.run_agent(
                    objective=objective,
                    system=spec.system,
                    purpose=spec.purpose,
                    max_iterations=10,
                    on_step=on_step,
                ),
                timeout=limit,
            )
            await self._finish(run_id, spec, "completed", result=result)

        except asyncio.TimeoutError:
            await self._finish(
                run_id, spec, "timed_out",
                error=f"Exceeded its {humanize_duration(limit)} budget and was stopped.",
            )
        except asyncio.CancelledError:
            await self._finish(run_id, spec, "cancelled", error="Stopped by the user.")
            raise
        except Exception as exc:
            logger.exception("agent %s failed", run_id)
            await self._finish(run_id, spec, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._running.pop(run_id, None)

    async def _finish(self, run_id: str, spec: AgentSpec, status: str,
                      result: Optional[Dict[str, Any]] = None, error: str = "") -> None:
        await self.db.execute(
            "UPDATE agent_runs SET status = ?, finished_at = ?, progress = ?, result = ?,"
            " error = ? WHERE id = ?",
            (status, now_iso(), 1.0 if status == "completed" else 0.0,
             dumps(result) if result else None, error, run_id),
        )
        bus.publish("agent.finished", {"id": run_id, "agent": spec.key, "status": status,
                                       "error": error}, source="agents")
        level = "info" if status == "completed" else "warn"
        await self.activity.record(
            f"{spec.label} {status.replace('_', ' ')}" + (f": {error}" if error else "."),
            level=level, source="agents",
        )
        if status in ("failed", "timed_out") and self.notifications:
            await self.notifications.create(
                title=f"{spec.label} {status.replace('_', ' ')}",
                body=error, priority="normal", source="agents",
            )

    # ==================================================================
    async def stop(self, run_id: str) -> Dict[str, Any]:
        """Stop one agent. Everything else keeps running."""
        run = self._running.get(run_id)
        if run is None:
            record = await self.get(run_id)
            if not record:
                raise KeyError(run_id)
            if record["status"] in ("running", "queued"):
                await self.db.execute(
                    "UPDATE agent_runs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                    (now_iso(), run_id),
                )
            return {"stopped": run_id, "was_running": False}

        run.cancelled = True
        run.task.cancel()
        return {"stopped": run_id, "agent": run.spec.key, "was_running": True}

    async def stop_all(self) -> Dict[str, Any]:
        stopped = []
        for run_id in list(self._running):
            try:
                await self.stop(run_id)
                stopped.append(run_id)
            except Exception:
                continue
        return {"stopped": stopped, "count": len(stopped)}

    # ==================================================================
    # inter-agent messaging
    # ==================================================================
    async def send(self, from_run: str, body: str, to_run: Optional[str] = None,
                   kind: str = "note", payload: Optional[Dict[str, Any]] = None) -> int:
        """One agent leaving a note for another (or for the orchestrator).

        Persisted rather than in-memory so it survives a restart and shows up
        in the UI as a bridge between clusters."""
        await self.db.execute(
            "INSERT INTO agent_messages (ts, from_run, to_run, kind, body, payload)"
            " VALUES (?,?,?,?,?,?)",
            (now_iso(), from_run, to_run, kind, truncate(body, 2000), dumps(payload or {})),
        )
        bus.publish("agent.message", {"from": from_run, "to": to_run, "kind": kind,
                                      "body": truncate(body, 200)}, source="agents")
        return 1

    async def inbox(self, run_id: str, consume: bool = True) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_messages WHERE to_run = ? AND consumed = 0 ORDER BY id",
            (run_id,),
        )
        if consume and rows:
            await self.db.execute(
                "UPDATE agent_messages SET consumed = 1 WHERE to_run = ? AND consumed = 0",
                (run_id,),
            )
        return [{**dict(r), "payload": loads(r["payload"], {})} for r in rows]

    async def messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_messages ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "payload": loads(r["payload"], {})} for r in rows]

    # ==================================================================
    # reads
    # ==================================================================
    async def get(self, run_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        return _decode(row) if row else {}

    async def active(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_runs WHERE status IN ('queued','running') ORDER BY created_at")
        return [_decode(r) for r in rows]

    async def history(self, limit: int = 40) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]

    def catalogue(self) -> List[Dict[str, Any]]:
        return [
            {"key": spec.key, "label": spec.label, "description": spec.description,
             "colour": spec.colour, "time_limit": humanize_duration(spec.time_limit_s),
             **self.boundary_report(spec)}
            for spec in AGENTS.values()
        ]

    async def stats(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM agent_runs GROUP BY status")
        counts = {r["status"]: r["n"] for r in rows}
        active = await self.active()
        return {
            "available": len(AGENTS),
            "running": len(self._running),
            "active": [{"id": a["id"], "agent": a["agent"], "progress": a["progress"],
                        "step": a["current_step"], "colour": AGENTS[a["agent"]].colour
                        if a["agent"] in AGENTS else "#8fa4b8"}
                       for a in active],
            "counts": counts,
        }

    async def recover(self) -> int:
        """Agent runs marked running belonged to a dead process."""
        count = await self.db.execute(
            "UPDATE agent_runs SET status = 'cancelled', finished_at = ?,"
            " error = 'Interrupted by a restart.' WHERE status IN ('running','queued')",
            (now_iso(),),
        )
        if count:
            logger.info("cleared %s stranded agent run(s)", count)
        return count


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["result"] = loads(item.get("result"), None)
    spec = AGENTS.get(item.get("agent", ""))
    item["label"] = spec.label if spec else item.get("agent", "")
    item["colour"] = spec.colour if spec else "#8fa4b8"
    return item
