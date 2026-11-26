"""Update 3 tools: structured goals, the agent orchestrator, deep research and
scoped memory."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import truncate
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    # ==================================================================
    # goals with milestones
    # ==================================================================
    @registry.add(
        "set_goal",
        "Take an objective with a deadline and turn it into a structured plan: estimate its size, "
        "ask anything genuinely blocking, generate milestones with dates, and create the tasks. "
        "Use for 'I need to get ready for X by Friday', 'help me ship Y', anything spanning days.",
        {"type": "object", "properties": {
            "objective": {"type": "string", "description": "What the user wants to be true"},
            "deadline": {"type": "string", "description": "ISO datetime, if there is one"},
            "start_now": {"type": "boolean", "description": "Create the tasks immediately"},
        }, "required": ["objective"]},
        permission="tasks.create",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Plan and start work towards: \"{truncate(a.get('objective'), 150)}\"?"
            if a.get("start_now") else
            f"Plan (but not start) work towards: \"{truncate(a.get('objective'), 150)}\"?"
        ),
        category="goals",
        timeout=240.0,
    )
    async def set_goal(ctx, objective: str, deadline: str = "", start_now: bool = False):
        planner = ctx.get("goal_planner")
        if planner is None:
            return fail("Goal planner unavailable.")
        try:
            record = await planner.create(objective, deadline or None, auto_start=start_now)
        except Exception as exc:
            return fail(f"Couldn't plan that: {exc}")

        if record.get("awaiting_answers"):
            questions = "; ".join(record["awaiting_answers"])
            return ok(
                {"goal_id": record["id"], "questions": record["awaiting_answers"],
                 "assessment": record.get("assessment")},
                f"Before I plan this I need to know: {questions}",
            )

        state = await planner.progress(record["id"] if "id" in record else record["goal_id"])
        milestones = "; ".join(f"{m['n']}. {m['title']}" for m in state["milestones"])
        complexity = state.get("complexity", {})
        return ok(
            {"goal_id": state["goal_id"], "status": state["status"],
             "milestones": state["milestones"], "complexity": complexity},
            f"Planned as {complexity.get('size', 'medium')} "
            f"(~{complexity.get('estimated_hours', '?')}h): {milestones}."
            + (" Tasks are running." if start_now else " Say the word and I'll start."),
        )

    @registry.add(
        "goal_status",
        "Check how a structured goal is going: milestone by milestone, with what's overdue.",
        {"type": "object", "properties": {
            "goal_id": {"type": "string", "description": "Omit for the most recent active goal"},
        }},
        category="goals",
    )
    async def goal_status(ctx, goal_id: str = ""):
        planner = ctx.get("goal_planner")
        if not goal_id:
            active = await planner.list(active_only=True, limit=1)
            if not active:
                return ok({"goals": []}, "No goals are active.")
            goal_id = active[0]["id"]
        try:
            state = await planner.progress(goal_id)
        except KeyError:
            return fail(f"No goal with id '{goal_id}'.")

        overdue = [m["title"] for m in state["milestones"] if m["overdue"]]
        return ok(state,
                  f"{state['objective']}: {state['completed_milestones']}/"
                  f"{state['total_milestones']} milestones ({state['percent']}%), "
                  f"status {state['status']}."
                  + (f" Overdue: {', '.join(overdue)}." if overdue else ""))

    @registry.add(
        "goal_control",
        "Start, adapt, report on, or cancel a structured goal. 'adapt' re-plans the remaining "
        "milestones when circumstances change.",
        {"type": "object", "properties": {
            "goal_id": {"type": "string"},
            "action": {"type": "string", "enum": ["start", "adapt", "report", "cancel"]},
            "reason": {"type": "string", "description": "Why, for 'adapt'"},
        }, "required": ["goal_id", "action"]},
        permission="tasks.create",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"{a.get('action', '').capitalize()} goal {a.get('goal_id')}?",
        category="goals",
        timeout=180.0,
    )
    async def goal_control(ctx, goal_id: str, action: str, reason: str = ""):
        planner = ctx.get("goal_planner")
        try:
            if action == "start":
                state = await planner.start(goal_id)
                return ok(state, f"Started: {state['total_milestones']} milestone(s) under way.")
            if action == "adapt":
                result = await planner.adapt(goal_id, reason)
                return ok(result, result.get("advice") or result.get("message", "Adapted."))
            if action == "report":
                result = await planner.report(goal_id)
                return ok(result, result["report"])
            if action == "cancel":
                result = await planner.cancel(goal_id)
                return ok(result, f"Cancelled, stopping {result['cancelled_tasks']} task(s).")
        except KeyError:
            return fail(f"No goal with id '{goal_id}'.")
        except PermissionError as exc:
            return fail(str(exc))
        return fail(f"Unknown action '{action}'.")

    @registry.add(
        "answer_goal_questions",
        "Supply the clarifications a goal was waiting for, then plan it.",
        {"type": "object", "properties": {
            "goal_id": {"type": "string"},
            "answers": {"type": "object", "description": "question -> answer"},
        }, "required": ["goal_id", "answers"]},
        permission="tasks.create",
        category="goals",
        timeout=180.0,
    )
    async def answer_goal_questions(ctx, goal_id: str, answers: Dict[str, str]):
        planner = ctx.get("goal_planner")
        try:
            state = await planner.answer(goal_id, answers)
        except KeyError:
            return fail(f"No goal with id '{goal_id}'.")
        return ok(state, f"Planned as {state['total_milestones']} milestone(s).")

    # ==================================================================
    # agents
    # ==================================================================
    @registry.add(
        "run_agent",
        "Hand work to a specialist agent that runs independently: researcher, coder, planner, "
        "files, system, comms or meeting. Omit 'agent' and the best one is chosen automatically. "
        "Each agent has its own tool boundary and time limit, and can be stopped on its own.",
        {"type": "object", "properties": {
            "objective": {"type": "string"},
            "agent": {"type": "string",
                      "enum": ["researcher", "coder", "planner", "files", "system", "comms",
                               "meeting", "general"]},
            "time_limit_minutes": {"type": "integer"},
        }, "required": ["objective"]},
        permission="tasks.create",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Run the {a.get('agent') or 'best-matched'} agent on: "
            f"\"{truncate(a.get('objective'), 140)}\"?"
        ),
        category="agents",
    )
    async def run_agent(ctx, objective: str, agent: str = "", time_limit_minutes: int = 0):
        orchestrator = ctx.get("agent_orchestrator")
        if orchestrator is None:
            return fail("Agent orchestrator unavailable.")
        result = await orchestrator.dispatch(
            objective, agent=agent or None,
            time_limit_s=time_limit_minutes * 60 if time_limit_minutes else None,
        )
        selection = result["selection"]
        return ok(result,
                  f"{result['label']} started ({selection['why']}). "
                  f"It has {result['boundaries']['tool_count']} tools and "
                  f"{result['boundaries']['time_limit']}. Run id {result['run_id']}.")

    @registry.add(
        "agent_status",
        "See which agents are running, what they're doing, and what each is allowed to touch.",
        {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Omit for all agents"},
        }},
        category="agents",
    )
    async def agent_status(ctx, run_id: str = ""):
        orchestrator = ctx.get("agent_orchestrator")
        if run_id:
            record = await orchestrator.get(run_id)
            if not record:
                return fail(f"No agent run '{run_id}'.")
            return ok(record, f"{record['label']}: {record['status']} "
                              f"({round(record['progress'] * 100)}%) — {record['current_step']}")
        stats = await orchestrator.stats()
        active = stats["active"]
        return ok({**stats, "catalogue": orchestrator.catalogue()},
                  f"{len(active)} agent(s) running." if active else "No agents running.")

    @registry.add(
        "stop_agent",
        "Stop one agent without touching anything else JARVIS is doing.",
        {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Omit with all=true to stop every agent"},
            "all": {"type": "boolean"},
        }},
        permission="tasks.create",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: ("Stop every running agent?" if a.get("all")
                                   else f"Stop agent {a.get('run_id')}?"),
        category="agents",
    )
    async def stop_agent(ctx, run_id: str = "", all: bool = False):
        orchestrator = ctx.get("agent_orchestrator")
        if all:
            result = await orchestrator.stop_all()
            return ok(result, f"Stopped {result['count']} agent(s).")
        if not run_id:
            return fail("Which agent? Give a run_id, or set all=true.")
        try:
            result = await orchestrator.stop(run_id)
        except KeyError:
            return fail(f"No agent run '{run_id}'.")
        return ok(result, f"Stopped {run_id}.")

    # ==================================================================
    # research that takes its time
    # ==================================================================
    @registry.add(
        "research_topic",
        "Research a topic properly in the background for 15 to 20 minutes -- many searches, pages "
        "actually read, follow-up searches on what turns up -- then report 3 or 4 paragraphs of what "
        "was found and what's next. Use when the owner asks to research, dig into, look into or find "
        "out about something, or when a quick web search found nothing useful.",
        {"type": "object", "properties": {
            "topic": {"type": "string", "description": "What to research, in the owner's words"},
            "minutes": {"type": "integer", "description": "15 to 20; default 17"},
        }, "required": ["topic"]},
        permission="web.search",
        category="research investigate dig deep look into find out emulator report",
    )
    async def research_topic(ctx, topic: str, minutes: int = 17):
        return await _research_long(ctx, topic, minutes)

    # "Deep research" is what the owner calls the long kind.
    @registry.add(
        "deep_research",
        "Deep research: research a topic properly in the background for 15 to 20 minutes, then "
        "report 3 or 4 paragraphs of what was found and what's next. Use for 'research X', "
        "'research it properly', 'deep dive into X', 'look into X', or when a quick search "
        "found nothing useful.",
        {"type": "object", "properties": {
            "question": {"type": "string", "description": "What to research, in the owner's words"},
        }, "required": ["question"]},
        permission="web.search",
        category="research investigate dig deep look into find out report",
    )
    async def deep_research(ctx, question: str, depth: str = "", background: bool = True):
        return await _research_long(ctx, question, 17)

    async def _research_long(ctx, topic: str, minutes: int = 17):
        topic = " ".join((topic or "").split())
        if len(topic) < 3:
            return fail("Ask the owner what to research.")
        minutes = max(15, min(int(minutes or 17), 20))
        engine = ctx.get("tasks")
        if engine is None:
            return fail("Background tasks are unavailable.")
        task = await engine.create(
            name=f"Researching: {truncate(topic, 60)}",
            task_type="long_research",
            params={"question": topic, "minutes": minutes,
                    "conversation_id": ctx.get("conversation_id")},
            priority="normal",
        )
        return ok({"task_id": task["id"], "minutes": minutes},
                  f"Started researching “{topic}”. It takes about {minutes} minutes; the report "
                  "(3-4 paragraphs: what I found and what's next) will appear here and as a "
                  "notification. The owner can watch or stop it in Tasks. Tell them that, briefly, "
                  "and do NOT answer the question yourself now.")

    # ==================================================================
    # deep research
    # ==================================================================
    @registry.add(
        "quick_research",
        "A QUICK research pass of a minute or two: a few sources, citations, disagreements "
        "surfaced. Only when the owner wants a fast answer; for deep research, digging into or "
        "researching something properly, use deep_research.",
        {"type": "object", "properties": {
            "question": {"type": "string"},
            "depth": {"type": "string", "enum": ["quick", "standard", "deep"]},
            "background": {"type": "boolean",
                           "description": "true = run as a background task (recommended for deep)"},
        }, "required": ["question"]},
        permission="web.search",
        category="research",
        timeout=280.0,
    )
    async def deep_research(ctx, question: str, depth: str = "standard", background: bool = False):
        service = ctx.get("research")
        if service is None:
            return fail("Research service unavailable.")

        project = await service.create(question, depth)

        if background or depth == "deep":
            engine = ctx.get("tasks")
            task = await engine.create(
                name=f"Research: {truncate(question, 60)}",
                task_type="deep_research",
                params={"project_id": project["id"]},
                priority="normal",
            )
            return ok({"project_id": project["id"], "task_id": task["id"]},
                      f"Deep research started in the background (project {project['id']}). "
                      f"I'll report when it's done.")

        try:
            await service.gather(project["id"])
            result = await service.synthesise(project["id"])
        except Exception as exc:
            return fail(f"Research failed: {exc}")

        notes = []
        if result["conflicts"]:
            notes.append(f"{len(result['conflicts'])} source disagreement(s)")
        if result["outdated"]:
            notes.append(f"{len(result['outdated'])} possibly outdated")
        if result["unreachable"]:
            notes.append(f"{len(result['unreachable'])} unreachable")

        return ok(
            {"project_id": result["project_id"], "summary": result["summary"],
             "report": result["report"], "citations": result["citations"],
             "conflicts": result["conflicts"], "outdated": result["outdated"]},
            result["summary"] + (f" ({'; '.join(notes)})" if notes else ""),
        )

    @registry.add(
        "research_projects",
        "List saved research projects, or reopen one to read or continue it.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "read", "continue"]},
            "project_id": {"type": "string"},
        }},
        permission="web.search",
        category="research",
        timeout=280.0,
    )
    async def research_projects(ctx, action: str = "list", project_id: str = ""):
        service = ctx.get("research")
        if action == "list":
            projects = await service.list(limit=15)
            if not projects:
                return ok({"projects": []}, "No research projects yet.")
            return ok({"projects": [
                {"id": p["id"], "question": p["question"], "status": p["status"],
                 "sources": p.get("source_count", 0), "updated": p["updated_at"]}
                for p in projects
            ]}, f"{len(projects)} research project(s).")

        if not project_id:
            return fail("Which project? Use action='list' to see them.")

        if action == "read":
            project = await service.get(project_id)
            if not project:
                return fail(f"No project '{project_id}'.")
            return ok({"question": project["question"], "summary": project["summary"],
                       "report": project["report"], "conflicts": project["conflicts"],
                       "citations": service.citations(project["sources"])},
                      project["summary"] or "That project has no report yet.")

        if action == "continue":
            try:
                result = await service.continue_project(project_id)
            except KeyError:
                return fail(f"No project '{project_id}'.")
            return ok(result, f"Continued: {result['new_sources']} new source(s). "
                              f"{result['summary']}")
        return fail(f"Unknown action '{action}'.")

    # ==================================================================
    # memory 2.0
    # ==================================================================
    @registry.add(
        "remember_scoped",
        "Remember something with a scope: short_term expires in 12 hours, long_term persists, "
        "project ties it to a project, conversation ties it to this conversation.",
        {"type": "object", "properties": {
            "value": {"type": "string"},
            "scope": {"type": "string", "enum": ["short_term", "long_term", "project", "conversation"]},
            "category": {"type": "string",
                         "enum": ["preference", "project", "person", "command", "fact", "routine",
                                  "general"]},
            "project_id": {"type": "string"},
            "importance": {"type": "integer"},
        }, "required": ["value"]},
        permission="memory.write",
        category="memory",
    )
    async def remember_scoped(ctx, value: str, scope: str = "long_term", category: str = "general",
                              project_id: str = "", importance: int = 3):
        manager = ctx.get("memory2")
        try:
            record = await manager.remember(
                value, category=category, importance=importance, scope=scope,
                project_id=project_id or None,
                conversation_id=ctx.get("conversation_id"),
                source="conversation",
                provenance={"source": "conversation", "conversation_id": ctx.get("conversation_id")},
            )
        except PermissionError as exc:
            return fail(str(exc))
        return ok({"id": record["id"], "scope": record["scope"]},
                  f"Noted ({record['scope'].replace('_', ' ')}).")

    @registry.add(
        "memory_explain",
        "Explain where a remembered fact came from and how often it's been used.",
        {"type": "object", "properties": {"memory_id": {"type": "string"}},
         "required": ["memory_id"]},
        category="memory",
    )
    async def memory_explain(ctx, memory_id: str):
        try:
            result = await ctx.get("memory2").explain(memory_id)
        except KeyError:
            return fail(f"No memory '{memory_id}'.")
        return ok(result,
                  f"\"{truncate(result['value'], 90)}\" — {result['origin']}, "
                  f"recalled {result['times_recalled']} time(s).")

    @registry.add(
        "memory_cleanup",
        "Report what memory cleanup would remove, or actually run it. Always reports first "
        "unless apply is true. Pinned memories are never touched.",
        {"type": "object", "properties": {
            "apply": {"type": "boolean"},
            "include_unused": {"type": "boolean",
                               "description": "Also remove low-importance entries never recalled"},
        }},
        permission="memory.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: ("Permanently delete the expired, duplicate"
                                   + (" and never-recalled" if a.get("include_unused") else "")
                                   + " memories?") if a.get("apply") else
                                  "Show what memory cleanup would remove?",
        category="memory",
    )
    async def memory_cleanup(ctx, apply: bool = False, include_unused: bool = False):
        manager = ctx.get("memory2")
        if not apply:
            report = await manager.cleanup_report()
            return ok(report,
                      f"{report['total_removable']} entr{'y' if report['total_removable'] == 1 else 'ies'} "
                      f"could be removed: {len(report['expired'])} expired, "
                      f"{len(report['duplicate_groups'])} duplicate group(s), "
                      f"{len(report['never_recalled'])} never recalled.")
        result = await manager.cleanup(expired=True, duplicates=True, unused=include_unused)
        return ok(result, f"Removed {result['total']} memory entr"
                          f"{'y' if result['total'] == 1 else 'ies'}.")

    # ==================================================================
    # command resolution
    # ==================================================================
    @registry.add(
        "resolve_reference",
        "Work out what an ambiguous reference points to ('this', 'that project', 'what I was "
        "doing yesterday'). Returns the candidates JARVIS can actually see. Use before acting on "
        "a vague command.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "The user's exact words"},
        }, "required": ["command"]},
        category="awareness",
    )
    async def resolve_reference(ctx, command: str):
        resolver = ctx.get("resolver")
        if resolver is None:
            return fail("Resolver unavailable.")
        result = await resolver.resolve(command)
        if not result.get("vague"):
            return ok(result, "That command isn't ambiguous — act on it directly.")
        if result.get("resolved"):
            return ok(result, f"Most likely: {result['resolved']['label']} "
                              f"({result['resolved']['why']}).")
        return ok(result, result.get("question") or "Not enough context to resolve that.")
