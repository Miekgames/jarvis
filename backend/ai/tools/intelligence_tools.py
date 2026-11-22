"""Tools for the Intelligence Layer: screen awareness, vision, goals, planning,
suggestions, learning and the device mesh."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import humanize_duration, truncate
from .registry import ToolRegistry, fail, ok

_VISION_INTENT = {
    "describe": "describe what you are looking at",
    "error": "explain the error shown",
    "ocr": "read the text on it",
    "elements": "list the buttons and fields on it",
    "diagram": "interpret the diagram or chart",
    "troubleshoot": "diagnose what has gone wrong",
}


def _vision_confirm(arguments: Dict[str, Any]) -> str:
    intent = _VISION_INTENT.get(arguments.get("task", "describe"), "analyse it")
    question = arguments.get("question")
    line = f"Capture your screen and send the image to the vision model to {intent}?"
    if question:
        line += f'\n\nYour question: "{truncate(question, 200)}"'
    return line


def register(registry: ToolRegistry) -> None:

    # ==================================================================
    # awareness
    # ==================================================================
    @registry.add(
        "what_am_i_doing",
        "Report what the user is currently doing on this computer: the focused application, its "
        "window title, how long they've been in it, and whether they're idle. Use this to ground "
        "answers about 'this', 'here' or 'what I'm working on'.",
        {"type": "object", "properties": {}},
        permission="system.info",
        category="awareness",
    )
    async def what_am_i_doing(ctx):
        awareness = ctx.get("awareness")
        if awareness is None or not awareness.running:
            return fail("Ambient awareness isn't running. Enable it in Settings -> Awareness.")
        snapshot = awareness.snapshot()
        if not snapshot.get("active_app"):
            return ok(snapshot, "I can't see a focused window right now.")
        return ok(
            {**snapshot,
             "focused_for": humanize_duration(snapshot.get("focused_for_seconds", 0))},
            f"{snapshot['active_app']} — {truncate(snapshot.get('window_title'), 90)}",
        )

    @registry.add(
        "app_usage",
        "How the user's time has actually been spent across applications and categories. Use for "
        "'where did my time go', 'what have I been doing today'.",
        {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "Look-back window, default 24"},
        }},
        permission="system.info",
        category="awareness",
    )
    async def app_usage(ctx, hours: int = 24):
        awareness = ctx.get("awareness")
        if awareness is None:
            return fail("Awareness service unavailable.")
        usage = await awareness.usage(hours=min(hours, 24 * 14))
        categories = await awareness.by_category(hours=min(hours, 24 * 14))
        if not usage:
            return ok({"apps": []}, "No application usage recorded for that window yet.")
        return ok({"hours": hours, "apps": usage, "by_category_minutes": categories})

    @registry.add(
        "performance_advice",
        "Identify what is actually slowing this computer down right now and what to do about it. "
        "Returns concrete evidence, not generic tips.",
        {"type": "object", "properties": {}},
        permission="system.info",
        category="awareness",
    )
    async def performance_advice(ctx):
        awareness = ctx.get("awareness")
        if awareness is None:
            return fail("Awareness service unavailable.")
        advice = await awareness.recommendations()
        hogs = await awareness.resource_hogs(limit=5)
        if not advice:
            return ok({"advice": [], "heaviest_processes": hogs},
                      "Nothing is under pressure right now.")
        return ok({"advice": advice, "heaviest_processes": hogs})

    # ==================================================================
    # vision
    # ==================================================================
    @registry.add(
        "look_at_screen",
        "Take a screenshot and analyse it with a vision model. Use for 'what am I looking at', "
        "'what does this error mean', 'read this', 'explain this diagram', 'why isn't this "
        "working'. Choose the task that matches the question.",
        {"type": "object", "properties": {
            "task": {"type": "string",
                     "enum": ["describe", "error", "ocr", "elements", "diagram", "troubleshoot"],
                     "description": "describe=what's on screen, error=explain an error, ocr=read "
                                    "all text, elements=list buttons and fields, diagram=interpret "
                                    "a chart, troubleshoot=diagnose a problem"},
            "question": {"type": "string", "description": "A specific question about the screen"},
        }},
        permission="system.screenshot",
        risk=Risk.MEDIUM,
        confirm_summary=_vision_confirm,
        category="vision",
        timeout=120.0,
    )
    async def look_at_screen(ctx, task: str = "describe", question: str = ""):
        vision = ctx.get("vision")
        if vision is None:
            return fail("Vision service unavailable.")
        settings_store = ctx.get("settings_store")
        awareness_settings = await settings_store.get("awareness", {})
        if not awareness_settings.get("vision", True):
            return fail("Screen vision is switched off in Settings -> Awareness.")

        result = await vision.look(task=task, question=question)
        if not result.get("ok"):
            return fail(result.get("message", "Vision failed."))
        return ok(
            {"answer": result["answer"], "active_app": result.get("active_app"),
             "image": result["image"]},
            result["answer"],
        )

    @registry.add(
        "compare_screens",
        "Compare a previously captured screenshot with the current screen (or with another saved "
        "image) and report what changed.",
        {"type": "object", "properties": {
            "before": {"type": "string", "description": "Path to the earlier screenshot"},
            "after": {"type": "string", "description": "Optional; omit to capture the screen now"},
            "question": {"type": "string"},
        }, "required": ["before"]},
        permission="system.screenshot",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: "Capture the screen and compare it with the earlier screenshot?",
        category="vision",
        timeout=120.0,
    )
    async def compare_screens(ctx, before: str, after: str = "", question: str = ""):
        vision = ctx.get("vision")
        result = await vision.compare(before, after or None, question)
        if not result.get("ok"):
            return fail(result.get("message", "Comparison failed."))
        return ok({"answer": result["answer"]}, result["answer"])

    # ==================================================================
    # goals (agent 2.0)
    # ==================================================================
    @registry.add(
        "pursue_goal",
        "Take a complex objective, break it into concrete steps with dependencies, and run them "
        "as background tasks. Use this for open-ended requests like 'figure out X', 'prepare "
        "everything for Y', 'research and write up Z' — anything needing several steps. Returns "
        "the plan, and the work continues in the background.",
        {"type": "object", "properties": {
            "objective": {"type": "string", "description": "The outcome the user wants"},
            "plan_only": {"type": "boolean",
                          "description": "true = show the plan without running it"},
        }, "required": ["objective"]},
        permission="tasks.create",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Work autonomously towards: \"{truncate(a.get('objective'), 160)}\"? "
            "I'll break it into steps and run them as background tasks."
        ),
        category="agents",
        timeout=180.0,
    )
    async def pursue_goal(ctx, objective: str, plan_only: bool = False):
        goals = ctx.get("goals")
        if goals is None:
            return fail("Goal service unavailable.")
        try:
            if plan_only:
                planned = await goals.plan(objective)
                return ok(
                    {"steps": [{"n": i + 1, "title": s["title"], "agent": s["agent"],
                                "depends_on": s["depends_on"]}
                               for i, s in enumerate(planned["steps"])]},
                    f"Plan for '{truncate(objective, 60)}': "
                    + "; ".join(f"{i + 1}. {s['title']}" for i, s in enumerate(planned["steps"])),
                )
            record = await goals.create(objective, auto_start=True)
        except Exception as exc:
            return fail(f"Couldn't plan that: {exc}")

        steps = record.get("plan", [])
        return ok(
            {"goal_id": record["id"], "status": record["status"],
             "steps": [{"n": i + 1, "title": s["title"], "task_id": s.get("task_id")}
                       for i, s in enumerate(steps)]},
            f"Working on it: {len(steps)} step(s) queued. "
            + "; ".join(f"{i + 1}. {s['title']}" for i, s in enumerate(steps)),
        )

    @registry.add(
        "goal_progress",
        "Check how an autonomous goal is progressing, step by step.",
        {"type": "object", "properties": {
            "goal_id": {"type": "string", "description": "Omit for the most recent active goal"},
        }},
        permission="tasks.create",
        category="agents",
    )
    async def goal_progress(ctx, goal_id: str = ""):
        goals = ctx.get("goals")
        if not goal_id:
            active = await goals.list(active_only=True, limit=1)
            if not active:
                return ok({"goals": []}, "No goals are running.")
            goal_id = active[0]["id"]
        try:
            report = await goals.progress(goal_id)
        except KeyError:
            return fail(f"No goal with id '{goal_id}'.")
        current = report.get("current")
        return ok(report,
                  f"{report['objective']}: {report['completed_steps']}/{report['total_steps']} "
                  f"steps done"
                  + (f", currently {current['title']}" if current else "")
                  + f" ({report['status']}).")

    # ==================================================================
    # planning
    # ==================================================================
    @registry.add(
        "plan_my_day",
        "Produce a plan for today or this week, grounded in the real calendar, free time and "
        "active tasks. Also flags schedule conflicts.",
        {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["today", "tomorrow", "week"]},
        }},
        permission="calendar.read",
        category="planning",
        timeout=120.0,
    )
    async def plan_my_day(ctx, scope: str = "today"):
        planning = ctx.get("planning")
        if planning is None:
            return fail("Planning service unavailable.")
        if scope == "week":
            result = await planning.weekly_plan()
            return ok(result["facts"], result["plan"])
        result = await planning.daily_plan(day_offset=1 if scope == "tomorrow" else 0)
        return ok(result["facts"], result["plan"])

    @registry.add(
        "what_should_i_do_next",
        "Recommend the single most sensible next thing to do, with the reasoning. Considers "
        "imminent meetings, failed tasks, approaching deadlines and free time.",
        {"type": "object", "properties": {}},
        permission="calendar.read",
        category="planning",
    )
    async def what_should_i_do_next(ctx):
        planning = ctx.get("planning")
        if planning is None:
            return fail("Planning service unavailable.")
        result = await planning.next_action()
        return ok(result, f"{result['recommendation']} — {result['why']}")

    @registry.add(
        "find_free_time",
        "Find genuinely free blocks in the working day, and flag any schedule conflicts.",
        {"type": "object", "properties": {
            "day_offset": {"type": "integer", "description": "0 = today, 1 = tomorrow"},
            "minimum_minutes": {"type": "integer"},
        }},
        permission="calendar.read",
        category="planning",
    )
    async def find_free_time(ctx, day_offset: int = 0, minimum_minutes: int = 25):
        planning = ctx.get("planning")
        blocks = await planning.free_blocks(day_offset, minimum_minutes)
        conflicts = await planning.conflicts(days=max(1, day_offset + 1))
        if not blocks:
            return ok({"blocks": [], "conflicts": conflicts},
                      "No free blocks of that length in the working day.")
        total = sum(b["minutes"] for b in blocks)
        return ok({"blocks": blocks, "conflicts": conflicts, "total_free_minutes": total},
                  f"{len(blocks)} free block(s), {total} minutes total."
                  + (f" {len(conflicts)} scheduling conflict(s)." if conflicts else ""))

    @registry.add(
        "start_focus_session",
        "Start a focus session: a timer plus a stated intent, so the end-of-session summary can "
        "reference what it was for.",
        {"type": "object", "properties": {
            "minutes": {"type": "integer"},
            "intent": {"type": "string", "description": "What the session is for"},
        }},
        permission="tasks.create",
        category="planning",
    )
    async def start_focus_session(ctx, minutes: int = 50, intent: str = ""):
        planning = ctx.get("planning")
        result = await planning.start_focus_session(max(5, min(minutes, 180)), intent)
        modes = ctx.get("modes")
        if modes:
            try:
                await modes.activate("focus")
            except Exception:
                pass
        return ok(result, f"Focus session started: {result['minutes']} minutes"
                          + (f" on {intent}." if intent else "."))

    @registry.add(
        "day_summary",
        "Summarise what actually happened today, or produce the weekly productivity report.",
        {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["day", "week"]},
        }},
        permission="system.info",
        category="planning",
        timeout=120.0,
    )
    async def day_summary(ctx, scope: str = "day"):
        planning = ctx.get("planning")
        if scope == "week":
            result = await planning.weekly_report()
            return ok(result["facts"], result["report"])
        result = await planning.end_of_day()
        return ok(result["facts"], result["summary"])

    @registry.add(
        "deadline_check",
        "Assess which projects are at risk of missing their deadline, based on remaining free "
        "capacity versus open work.",
        {"type": "object", "properties": {}},
        permission="calendar.read",
        category="planning",
    )
    async def deadline_check(ctx):
        planning = ctx.get("planning")
        assessments = await planning.deadline_risk()
        if not assessments:
            return ok({"projects": []}, "No active projects have deadlines set.")
        at_risk = [a for a in assessments if a["risk"] != "low"]
        return ok({"projects": assessments},
                  f"{len(at_risk)} of {len(assessments)} project(s) at risk."
                  if at_risk else "All projects look achievable.")

    # ==================================================================
    # learning & transparency
    # ==================================================================
    @registry.add(
        "why_did_you_do_that",
        "Explain JARVIS's recent decisions from its own decision log: what it did, why, what it "
        "considered instead, and what it based that on.",
        {"type": "object", "properties": {
            "about": {"type": "string", "description": "Optional subject to filter on"},
        }},
        category="transparency",
    )
    async def why_did_you_do_that(ctx, about: str = ""):
        reasoning = ctx.get("reasoning")
        if reasoning is None:
            return fail("Reasoning service unavailable.")
        if about:
            decisions = await reasoning.decisions(limit=8, subject=about)
            if not decisions:
                return ok({"decisions": []}, f"Nothing logged about '{about}'.")
            return ok({"decisions": decisions})
        result = await reasoning.explain_recent()
        if not result["explained"]:
            return ok(result, result["message"])
        return ok(result)

    @registry.add(
        "my_habits",
        "Show what JARVIS has learned about the user's command habits: most-used commands, what "
        "they usually do around now, and what tends to follow what.",
        {"type": "object", "properties": {}},
        category="awareness",
    )
    async def my_habits(ctx):
        patterns = ctx.get("patterns")
        if patterns is None:
            return fail("Pattern learning unavailable.")
        stats = await patterns.stats()
        if not stats["enabled"]:
            return ok(stats, "Learning is switched off in Settings -> Privacy.")
        due = await patterns.due_now()
        return ok({**stats, "due_now": due})

    @registry.add(
        "learn_phrase",
        "Record that a personal phrase or piece of shorthand means something specific, so future "
        "commands using it are understood. Use when the user explains their own terminology.",
        {"type": "object", "properties": {
            "phrase": {"type": "string", "description": "The shorthand, e.g. 'the site'"},
            "means": {"type": "string", "description": "What it refers to"},
        }, "required": ["phrase", "means"]},
        permission="memory.write",
        category="awareness",
    )
    async def learn_phrase(ctx, phrase: str, means: str):
        patterns = ctx.get("patterns")
        await patterns.learn_phrase(phrase, means, source="conversation")
        return ok({"phrase": phrase, "means": means},
                  f"Got it — '{phrase}' means {truncate(means, 80)}.")

    @registry.add(
        "suggestions",
        "List suggestions JARVIS has offered, or generate a fresh one now.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "generate"]},
        }},
        category="awareness",
    )
    async def suggestions(ctx, action: str = "list"):
        proactive = ctx.get("proactive")
        if proactive is None:
            return fail("Proactive engine unavailable.")
        if action == "generate":
            offered = await proactive.sweep()
            if not offered:
                return ok({"suggestions": []}, "Nothing worth suggesting right now.")
            return ok({"suggestions": offered},
                      "; ".join(s["title"] for s in offered))
        pending = await proactive.pending()
        return ok({"suggestions": pending, "stats": await proactive.stats()})

    # ==================================================================
    # gaming
    # ==================================================================
    @registry.add(
        "gaming_status",
        "Report gaming activity: what's running now, playtime totals per game, and recent "
        "sessions. Note that frame rate is not measured by this build.",
        {"type": "object", "properties": {
            "game": {"type": "string", "description": "Optional: a specific game"},
        }},
        permission="system.info",
        category="gaming",
    )
    async def gaming_status(ctx, game: str = ""):
        gaming = ctx.get("gaming")
        if gaming is None:
            return fail("Gaming service unavailable.")
        current = await gaming.current()
        playtime = await gaming.playtime(game or None)
        sessions = await gaming.sessions(limit=5, game=game or None)
        message = (
            f"Currently playing {current['game']} ({current['elapsed']})."
            if current else "No game running."
        )
        return ok({"current": current, "playtime": playtime, "recent_sessions": sessions},
                  message)

    # ==================================================================
    # devices
    # ==================================================================
    @registry.add(
        "devices",
        "List the user's paired devices and which are online.",
        {"type": "object", "properties": {}},
        category="devices",
    )
    async def devices(ctx):
        service = ctx.get("devices")
        if service is None:
            return fail("Device service unavailable.")
        listing = await service.list()
        online = [d["name"] for d in listing if d.get("online")]
        return ok({"devices": listing},
                  f"{len(listing)} device(s) paired, online: {', '.join(online) or 'none'}.")

    @registry.add(
        "send_to_device",
        "Hand something off to another paired device — a command to run, a link to open, or a "
        "conversation to continue there.",
        {"type": "object", "properties": {
            "device": {"type": "string", "description": "Device name or id"},
            "text": {"type": "string", "description": "The command or message to send"},
            "kind": {"type": "string", "enum": ["command", "link", "conversation", "note"]},
        }, "required": ["device", "text"]},
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Send \"{truncate(a.get('text'), 80)}\" to {a.get('device')}?",
        category="devices",
    )
    async def send_to_device(ctx, device: str, text: str, kind: str = "command"):
        service = ctx.get("devices")
        listing = await service.list()
        target = next(
            (d for d in listing
             if d["id"] == device or d["name"].lower() == device.lower()),
            None,
        )
        if target is None:
            names = ", ".join(d["name"] for d in listing) or "none paired"
            return fail(f"No device called '{device}'. Paired devices: {names}.")
        if target.get("is_self"):
            return fail("That's this machine — no handoff needed.")

        result = await service.handoff(target["id"], {"kind": kind, "text": text})
        return ok(result,
                  f"Sent to {target['name']}. It'll pick it up "
                  + ("straight away." if target.get("online") else "when it next connects."))
