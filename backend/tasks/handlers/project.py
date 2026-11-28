"""A coding project: Claude Code does it; the local coder steps in if it can't.

Started by "build me a habit tracker", "make me a Discord bot that ...",
"work on my game for two hours". Claude Code works in the project's folder
for as long as the job needs (and, with a time given, keeps testing and
improving until it's used). Its report comes back into the conversation the
job was asked in and as a notification -- which reaches Discord too, when
the owner has asked for that.

If Claude Code can't take it (not installed, not signed in, out of usage),
the reason is said plainly and the job goes to away mode, where the best
local coding model builds what it can.
"""
from __future__ import annotations

import asyncio
import re
import logging
from pathlib import Path
from typing import Any, Dict

from ...core.util import truncate
from ..engine import TaskContext

logger = logging.getLogger("jarvis.tasks.project")

# Claude Code jobs one at a time: two at once would fight over the owner's usage.
_ONE_AT_A_TIME = asyncio.Lock()


async def project_handler(ctx: TaskContext) -> Dict[str, Any]:
    from ...autonomy import claude_code

    task = (ctx.params.get("task") or "").strip()
    if not task:
        raise ValueError("No project to work on.")
    minutes = int(ctx.params.get("minutes") or 0)
    folder = Path(ctx.params.get("folder") or "")

    async def log(line: str) -> None:
        ctx.checkpoint_now()                     # stopping takes effect between steps too
        await ctx.log(line)

    await ctx.log(f"Project: {task}" + (f" (about {minutes} minutes)" if minutes else "")
                  + f" -- in {folder}")
    try:
        async with _ONE_AT_A_TIME:
            result = await claude_code.run_job(task, folder, minutes, log, lambda: ctx.cancelled)
        message = _finished(task, result)
    except claude_code.JobStopped:
        ctx.checkpoint_now()                     # raises: the task ends as cancelled
        raise
    except claude_code.ClaudeUnavailable as why:
        await ctx.log(f"Claude Code couldn't take it: {why}")
        result, message = await _local_coder(ctx, task, minutes, str(why))

    await _deliver(ctx, task, message)
    # Only when asked ("...and send it to my Discord"); otherwise "send me the
    # tip calculator on Discord" sends it later.
    if result.get("engine") == "claude-code" and ctx.params.get("send_files"):
        await _send_to_discord(ctx, task, result)
    return result


async def _send_to_discord(ctx: TaskContext, task: str, result: Dict[str, Any]) -> None:
    """The finished project to the owner's Discord DMs: a zip, and how it looks."""
    from ...autonomy.packing import pack, screenshots

    bridge = ctx.services.get("discord")
    folder = Path(result.get("folder") or "")
    if bridge is None or bridge.client is None or not folder.is_dir():
        return
    try:
        if not (await bridge.config())["owners"]:
            return
        settings = getattr(ctx.engine, "settings", None) or ctx.services.get("settings")
        out = Path(getattr(settings, "DATA_DIR", folder.parent)) / "outbox"
        files = [pack(folder, out)] + screenshots(folder)
        said = await bridge.send_files(files, f"📦 **{truncate(task, 80)}**: the files, and how it looks.")
        await ctx.log(said)
    except Exception as exc:
        await ctx.log(f"Couldn't send the files to Discord: {exc}")


async def _local_coder(ctx: TaskContext, task: str, minutes: int, why: str):
    """Hand the job to away mode, where the local coding model works on it."""
    autonomy = ctx.services.get("autonomy")
    reason = _plain_reason(why)
    if autonomy is None:
        message = (f"I couldn't start “{truncate(task, 80)}”: {reason}, and the local coder isn't "
                   "available either.")
        return {"engine": "none", "reason": why}, message
    try:
        run = await autonomy.start(task, float(minutes or 60), float(minutes or 60), False)
    except (RuntimeError, ValueError) as exc:
        message = f"I couldn't start “{truncate(task, 80)}”: {reason}, and the local coder said: {exc}"
        return {"engine": "none", "reason": why, "local_error": str(exc)}, message
    await ctx.log("Handed to the local coder (away mode).")
    message = (f"🛠️ Claude Code couldn't take “{truncate(task, 80)}” ({reason}), so the local "
               f"coder is on it instead, for about {int(minutes or 60)} minutes. It's less capable, "
               "so expect a smaller result. Its report will come when it's done.")
    return {"engine": "local", "reason": why, "away": run}, message


def _plain_reason(why: str) -> str:
    lower = why.lower()
    if "login" in lower or "logged in" in lower or "authentication" in lower:
        return "it isn't signed in on this PC yet"
    if "limit" in lower or "quota" in lower or "credit" in lower:
        resets = re.search(r"resets\s+(\d{1,2}(?::\d{2})?\s*[ap]m)", why, re.I)
        return ("your Claude usage limit has been reached"
                + (f" (it resets {resets.group(1).strip()})" if resets else ""))
    if "installed" in lower:
        return "it isn't installed on this PC"
    return truncate(why, 160)


def _finished(task: str, result: Dict[str, Any]) -> str:
    passes = result.get("passes", 1)
    head = (f"✅ **Project done: {truncate(task, 70)}** (Claude Code, {result.get('minutes', 0)} min"
            + (f", {passes} passes" if passes > 1 else "") + ")")
    report = result.get("report") or "It finished without writing a report."
    return f"{head}\n\n{report}\n\n📁 {result.get('folder', '')}"


async def _deliver(ctx: TaskContext, task: str, message: str) -> None:
    """Into the conversation it was asked in, and as a notification."""
    from ...core.events import Events, bus

    services = ctx.services
    conversation_id = ctx.params.get("conversation_id")
    orchestrator = services.get("orchestrator")
    if conversation_id and orchestrator is not None:
        try:
            await orchestrator._save_message(conversation_id, "assistant", message,
                                             [{"type": "text", "text": message}])
            bus.publish(Events.MESSAGE, {"conversation_id": conversation_id, "role": "assistant",
                                         "text": message, "late": True}, source="project")
        except Exception:
            logger.debug("could not post the project report to the conversation", exc_info=True)
    notifications = services.get("notifications")
    if notifications is not None:
        await notifications.create(title=f"Project: {truncate(task, 70)}", body=message[:900],
                                   priority="high", source="project", action={"panel": "tasks"})
