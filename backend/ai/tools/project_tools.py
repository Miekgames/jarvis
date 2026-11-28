"""Projects: the big jobs that go to Claude Code.

"Build me a habit tracker app", "make me a Discord bot that rolls dice",
"while I'm away for two hours, build me a budgeting website", "work on my
snake game for an hour". Questions -- including questions about code -- stay
with the local model; only asking for something to be *made* starts a project.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .registry import ToolRegistry, fail, ok

_THINGS = (r"apps?|applications?|web\s*sites?|sites?|web\s*apps?|games?|programs?|scripts?|tools?|"
           r"bots?|apis?|extensions?|plug-?ins?|dashboards?|trackers?|projects?|pages?|landing\s+pages?|"
           r"servers?|clis?|backends?|frontends?|services?|calculators?|utilit(?:y|ies)|widgets?|"
           r"portfolios?|blogs?|stores?|plugins?")
_BUILD = re.compile(
    r"(?:build|make|create|code|write|develop|program|design|put\s+together)\s+(?:me\s+|us\s+)?"
    r"(?:a\s+|an\s+|the\s+|my\s+|some\s+)?(?:new\s+)?(?:[\w'.+#-]+\s+){0,6}?(?:" + _THINGS + r")\b.*",
    re.I | re.S)
_AWAY = re.compile(r"(?:while|when)\s+i'?m\s+(?:away|out|gone|asleep)|i'?m\s+(?:going\s+out|heading\s+out|"
                   r"off\s+to\s+bed)", re.I)
_WORK_ON = re.compile(r"(?:work|keep\s+working)\s+on\s+(?P<what>.+?)\s+for\s+(?:about\s+|around\s+)?"
                      r"(?:\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|eight|ten|twelve|half\s+an)\s*"
                      r"(?:hours?|hrs?|minutes?|mins?)\b.*", re.I | re.S)
_START = re.compile(r"(?:start|begin|new)\s+(?:a\s+)?(?:new\s+)?(?:coding\s+)?project\s*[:,-]?\s+(?P<what>.{6,})",
                    re.I | re.S)
_FOLDER = re.compile(r"[A-Za-z]:\\[^\s\"'<>|?*]+")


_SEND_TO_DISCORD = re.compile(r"\b(?:send|share|dm)\b[^.]*?\bdiscord\b", re.I)


def project_said(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """("start_project", arguments) when the owner is asking for something to be built."""
    from .control_tools import _minutes_said

    said = " ".join((message or "").strip().split())
    said = re.sub(r"^(?:hey\s+)?jarvis[,:]?\s+", "", said, flags=re.I)
    # "Can you build me a website?" asks for one; "how do I build a website?" doesn't.
    asked = bool(re.match(r"(?:please\s+)?(?:can|could|would|will)\s+you\s+", said, re.I))
    said = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+|"
                  r"i\s+(?:want|need)\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+)+", "", said, flags=re.I)
    if said.endswith("?") and not asked and not _AWAY.search(said):
        return None                                  # a question about building stays a chat
    said = said.rstrip("?")
    words = said.rstrip(".! ")
    minutes = _minutes_said(words) or 0
    folder = next((m.group(0).rstrip(".,") for m in _FOLDER.finditer(words)
                   if Path(m.group(0).rstrip(".,")).is_dir()), "")
    # "while I'm away for an hour, build me a habit tracker"
    if _AWAY.search(words):
        build = re.search(r"(?:build|make|create|code|write|develop|program|design)\b.*", words, re.I | re.S)
        if build and _BUILD.fullmatch(build.group(0)):
            return "start_project", {"task": build.group(0), "minutes": minutes, "folder": folder}
        return None
    m = _WORK_ON.fullmatch(words)
    if m:
        return "start_project", {"task": f"Work on {m.group('what')}", "minutes": minutes, "folder": folder}
    m = _START.fullmatch(words)
    if m:
        return "start_project", {"task": m.group("what"), "minutes": minutes, "folder": folder}
    if _BUILD.fullmatch(words):
        return "start_project", {"task": words, "minutes": minutes, "folder": folder}
    return None


def project_folder(workspace: Path, task: str, when: Optional[datetime] = None) -> Path:
    """A new folder for a project: a few words of the task and the date."""
    words = [w for w in re.findall(r"[a-z0-9]+", task.lower())
             if w not in {"build", "make", "create", "code", "write", "me", "a", "an", "the", "my",
                          "for", "that", "with", "and", "to", "of", "it", "please", "while", "away"}]
    slug = "-".join(words[:5]) or "project"
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M")
    return workspace / f"{slug}-{stamp}"


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "start_project",
        "Start a coding project: build an app, website, game, script, bot or tool, or work on an "
        "existing one for a while. It's done by Claude Code in its own folder (the local coder if "
        "Claude Code can't), and the report comes back when it's finished. Not for questions "
        "about code -- answer those directly.",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "What to build or do, in the owner's words"},
            "minutes": {"type": "integer", "description": "How long to keep working, if the owner said"},
            "folder": {"type": "string", "description": "An existing project folder, if the owner named one"},
        }, "required": ["task"]},
        permission="code.delegate",
        category="project build make create app website game script bot tool code develop program",
    )
    async def start_project(ctx, task: str, minutes: int = 0, folder: str = ""):
        from ...autonomy.claude_code import MAX_MINUTES, find_claude
        from ...core.util import truncate

        task = (task or "").strip()
        if len(task) < 6:
            return fail("What should I build?")
        engine = ctx.get("tasks")
        if engine is None:
            return fail("Background tasks aren't available.")
        settings = ctx.get("settings")
        workspace = Path(getattr(settings, "DATA_DIR", ".")) / "projects"
        if folder and Path(folder).is_dir():
            where = Path(folder)
        else:
            where = project_folder(workspace, task)
        minutes = max(0, min(int(minutes or 0), MAX_MINUTES))
        record = await engine.create(
            name=f"Project: {truncate(task, 60)}",
            task_type="project",
            params={"task": task, "minutes": minutes, "folder": str(where),
                    "conversation_id": ctx.get("conversation_id"),
                    # Files go to Discord only when asked: "...and send it to my Discord".
                    "send_files": bool(_SEND_TO_DISCORD.search(task))},
            priority="normal",
        )
        how_long = f" for about {minutes} minutes" if minutes else ""
        if find_claude() is None:
            said = (f"🛠️ On it{how_long}. Claude Code isn't installed here, so the local coder will "
                    f"build what it can. I'll report back when it's done. Say “stop the project” to stop it.")
        else:
            said = (f"🛠️ On it: Claude Code is building that{how_long} in {where}. I'll put the report "
                    f"here and send a notification when it's done. Say “stop the project” to stop it.")
        return ok({"task_id": record["id"], "folder": str(where), "minutes": minutes}, said)
