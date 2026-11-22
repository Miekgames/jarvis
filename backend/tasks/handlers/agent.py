"""
Generic autonomous agent task, plus the specialised agents the orchestrator
delegates to.

`agent_handler` is the workhorse: give it an objective and it plans, calls
tools, and reports. Specialised variants differ only in their system prompt and
the model role they route to, which keeps the multi-agent story honest -- there
is one execution path, not several half-built ones.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from ...core.util import truncate
from ..engine import TaskContext

logger = logging.getLogger("jarvis.tasks.agent")

AGENT_PROFILES: Dict[str, Dict[str, str]] = {
    "general": {
        "label": "General assistant",
        "purpose": "conversation",
        "system": "You are a capable assistant working autonomously on a background task. "
                  "Use tools to gather what you need, then report what you actually did.",
    },
    "researcher": {
        "label": "Researcher",
        "purpose": "reasoning",
        "system": "You are a researcher. Search, read and cross-check before answering. "
                  "Cite URLs. Distinguish what the sources say from what you infer.",
    },
    "coder": {
        "label": "Coding assistant",
        "purpose": "coding",
        "system": "You are a software engineer. Read files before changing them, keep changes minimal, "
                  "and explain what you changed and why.",
    },
    "planner": {
        "label": "Planning assistant",
        "purpose": "reasoning",
        "system": "You are a planner. Turn the objective into concrete steps, then execute the ones you "
                  "have tools for and clearly list what needs the user.",
    },
    "files": {
        "label": "File assistant",
        "purpose": "fast",
        "system": "You organise files. Always inspect before acting, never delete without the delete tool "
                  "(which asks for confirmation), and summarise every change you made.",
    },
    "system": {
        "label": "System assistant",
        "purpose": "fast",
        "system": "You monitor and manage this computer. Report concrete numbers, not vague impressions.",
    },
    "comms": {
        "label": "Communication assistant",
        "purpose": "conversation",
        "system": "You handle drafting for email and messages. Always leave sending to the user's "
                  "confirmation -- draft, show, and wait.",
    },
    "meeting": {
        "label": "Meeting assistant",
        "purpose": "conversation",
        "system": "You prepare for meetings: pull the event, find related files and notes, "
                  "and produce an agenda plus a short brief.",
    },
}


async def agent_handler(ctx: TaskContext) -> Dict[str, Any]:
    objective = ctx.params.get("objective") or ctx.params.get("goal") or ""
    if not objective:
        raise ValueError("Agent task needs an 'objective' parameter.")

    profile_key = ctx.params.get("agent", "general")
    profile = AGENT_PROFILES.get(profile_key, AGENT_PROFILES["general"])
    max_iterations = int(ctx.params.get("max_iterations", 10))
    orchestrator = ctx.services["orchestrator"]

    state = ctx.load_checkpoint()
    completed_steps = int(state.get("iterations", 0))

    await ctx.log(f"Running as the {profile['label']} agent.")
    await ctx.progress(0.05, "Planning")

    step_counter = {"n": completed_steps}

    async def on_step(tool_name: str, tool_input: Dict[str, Any]) -> None:
        ctx.checkpoint_now()
        step_counter["n"] += 1
        await ctx.log(f"Using {tool_name}: {truncate(tool_input, 160)}")
        await ctx.progress(
            min(0.9, 0.1 + step_counter["n"] / (max_iterations * 1.5)),
            f"{tool_name}",
        )
        await ctx.save_checkpoint({"iterations": step_counter["n"], "objective": objective})

    result = await orchestrator.run_agent(
        objective=objective,
        system=profile["system"],
        purpose=profile["purpose"],
        max_iterations=max_iterations,
        task_id=ctx.task_id,
        on_step=on_step,
    )

    await ctx.progress(1.0, "Done")
    if result.get("incomplete"):
        await ctx.log("Hit the step limit before finishing.", "warn")

    return {
        "objective": objective,
        "agent": profile_key,
        "output": result.get("text", ""),
        "tools_used": [t["tool"] for t in result.get("trace", [])],
        "trace": result.get("trace", []),
        "incomplete": bool(result.get("incomplete")),
    }


async def meeting_prep_handler(ctx: TaskContext) -> Dict[str, Any]:
    """Concrete multi-step workflow: find the meeting, gather context, produce
    a brief, attach it to the event and set a reminder."""
    orchestrator = ctx.services["orchestrator"]
    db = ctx.services["db"]
    calendar = ctx.services["calendar"]
    scheduler = ctx.services.get("scheduler")

    event_id = ctx.params.get("event_id")
    total = 6

    await ctx.step(1, total, "Finding the meeting")
    if event_id:
        event = await calendar.get(event_id)
    else:
        upcoming = await calendar.upcoming(limit=1)
        event = upcoming[0] if upcoming else None
    if not event:
        return {"prepared": False, "reason": "There are no upcoming events on the calendar to prepare for."}

    await ctx.log(f"Preparing for '{event['title']}' at {event['start_at']}")

    await ctx.step(2, total, "Gathering related notes and files")
    related_notes = await db.fetch_all(
        "SELECT title, body FROM project_items WHERE title LIKE ? OR body LIKE ? LIMIT 5",
        (f"%{event['title'][:30]}%", f"%{event['title'][:30]}%"),
    )
    files_summary = ""
    try:
        files = ctx.services["files"]
        matches = await files.search(event["title"][:40], limit=8)
        files_summary = "\n".join(f"- {m['path']}" for m in matches[:8])
    except Exception as exc:
        await ctx.log(f"File search skipped: {exc}", "warn")

    await ctx.step(3, total, "Checking attendee history")
    attendees = event.get("attendees") or []
    contact_notes = []
    for attendee in attendees[:5]:
        row = await db.fetch_one(
            "SELECT name, organization, notes FROM contacts WHERE email = ? OR name = ?",
            (attendee, attendee),
        )
        if row:
            contact_notes.append(f"{row['name']} ({row['organization']}): {row['notes'][:200]}")

    await ctx.step(4, total, "Writing the brief")
    context_block = "\n".join(
        [
            f"MEETING: {event['title']}",
            f"WHEN: {event['start_at']}",
            f"WHERE: {event.get('location') or event.get('link') or 'not specified'}",
            f"ATTENDEES: {', '.join(attendees) if attendees else 'not listed'}",
            f"DESCRIPTION: {event.get('description', '')[:1000]}",
            "RELATED NOTES:\n" + ("\n".join(f"- {n['title']}: {n['body'][:200]}" for n in related_notes) or "none"),
            "RELATED FILES:\n" + (files_summary or "none found"),
            "ATTENDEE CONTEXT:\n" + ("\n".join(contact_notes) or "none on file"),
        ]
    )
    brief = await orchestrator.ask(
        "Write a concise meeting preparation brief from the context below. Include: the purpose in one "
        "line, a suggested agenda of 3-5 items, what the user should have ready, and any open questions. "
        "Say plainly if context is thin rather than padding it out.\n\n" + context_block,
        purpose="conversation",
        max_tokens=1500,
    )

    await ctx.step(5, total, "Filing the brief")
    project_id = ctx.params.get("project_id")
    if project_id:
        from ...core.util import now_iso

        await db.execute(
            "INSERT INTO project_items (project_id, kind, ref_id, title, body, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (project_id, "note", event["id"], f"Prep: {event['title']}", brief, now_iso()),
        )

    await ctx.step(6, total, "Setting a reminder")
    reminder_set = False
    if scheduler:
        try:
            from ...core.util import parse_iso
            from datetime import timedelta

            start = parse_iso(event["start_at"])
            if start:
                await scheduler.create_once(
                    name=f"Meeting in 15 minutes: {event['title']}",
                    run_at=(start - timedelta(minutes=15)).isoformat(),
                    action={
                        "kind": "notify",
                        "title": f"{event['title']} starts in 15 minutes",
                        "body": event.get("link") or event.get("location") or "",
                        "priority": "high",
                    },
                )
                reminder_set = True
        except Exception as exc:
            await ctx.log(f"Couldn't set the reminder: {exc}", "warn")

    return {
        "prepared": True,
        "event": {"id": event["id"], "title": event["title"], "start_at": event["start_at"]},
        "brief": brief,
        "related_files": files_summary.splitlines(),
        "reminder_set": reminder_set,
    }
