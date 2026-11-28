"""Big coding jobs go to Claude Code; JARVIS hands over, watches, reports back.

Everyday questions stay on the local model. A project -- "build me a habit
tracker", "make a Discord bot that...", "work for two hours on my game" --
goes to Claude Code running headless (`claude -p`) in its own folder, for as
long as it takes; with a time given, it keeps testing and improving until the
time is used. When it finishes, its own report comes back to the owner.

What it may do, unattended: read and edit files in the project folder, and
run ordinary development commands there (npm, node, python, pip, git, tests).
Pushing anywhere, deleting wholesale, and shutting the PC down are refused.

When Claude Code can't take the job -- not installed, not signed in, out of
usage -- the reason is said plainly and the local coder (away mode, with the
best local coding model) does what it can instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("jarvis.claude_code")

ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "TodoWrite", "WebSearch", "WebFetch",
    "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(python:*)", "Bash(py:*)", "Bash(pip:*)",
    "Bash(pytest:*)", "Bash(uv:*)", "Bash(git init:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(mkdir:*)", "Bash(ls:*)",
    "Bash(dir:*)", "Bash(cat:*)", "Bash(type:*)", "Bash(echo:*)", "Bash(cargo:*)", "Bash(go:*)",
    "Bash(dotnet:*)", "Bash(java:*)", "Bash(javac:*)",
]
DISALLOWED_TOOLS = [
    "Bash(git push:*)", "Bash(rm -rf:*)", "Bash(rmdir:*)", "Bash(del:*)", "Bash(format:*)",
    "Bash(shutdown:*)", "Bash(reg:*)", "Bash(curl:*)", "Bash(powershell:*)",
]
SYSTEM = (
    "You are working for JARVIS, the owner's personal assistant, unattended. The owner is not "
    "watching and can't answer questions: make sensible choices and note them. Work only inside "
    "the current folder. Build the whole thing asked for, run it and test it as you go, and fix "
    "what fails. Finish with a short report for the owner: what you built, how to run it, what "
    "works, what doesn't yet, and the next steps you'd suggest."
)
# A page has to be looked at, not just tested: the first real project passed
# its tests and was cut off on a phone. Claude Code may run Python, not
# browsers, so this is how it looks.
BROWSER_CHECK = Path(__file__).with_name("browser_check.py")
LOOKING = (
    "\n\nTo look at a web page you built, run: python \"{script}\" index.html (add --mobile for a "
    "390-wide phone, or pass a localhost URL). It saves a screenshot under ./screenshots and prints "
    "the page's visible text after its scripts ran. Open the screenshot with Read and look at it. "
    "Check every page at desktop and phone size, and fix anything cut off, overlapping or empty "
    "before you report."
)


def system_prompt() -> str:
    return SYSTEM + (LOOKING.format(script=BROWSER_CHECK) if BROWSER_CHECK.exists() else "")
MORE = ("You have about {minutes} minutes left. Review what you built, test it thoroughly as a user "
        "would, fix anything broken, then make it better. Finish with the same kind of report.")
MAX_MINUTES = 8 * 60
# What Claude Code says when it can't take the job at all.
_UNAVAILABLE = re.compile(r"not logged in|please run /login|usage limit|limit reached|"
                          r"rate.?limit|overloaded|credit balance|quota|authentication|"
                          # "You've hit your 5-hour limit · resets 5:40pm" -- the real wording,
                          # which slipped through as a "report" the first time it happened.
                          r"hit your .{0,30}limit|\d+.hour limit|limit\s*[·•-]\s*resets|weekly limit", re.I)


class ClaudeUnavailable(RuntimeError):
    """Claude Code can't take this job: not installed, not signed in, out of usage."""


class JobStopped(Exception):
    """The owner stopped the job: Claude Code was killed mid-run."""


def _version_key(name: str) -> List[int]:
    return [int(part) for part in re.findall(r"\d+", name)] or [0]


def find_claude() -> Optional[Path]:
    """`claude` on the PATH, else the newest copy the Claude desktop app ships."""
    on_path = shutil.which("claude")
    if on_path:
        return Path(on_path)
    root = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"
    found = sorted(root.glob("*/*/claude.exe"), key=lambda p: _version_key(p.parent.parent.name),
                   reverse=True) if root.exists() else []
    return found[0] if found else None


def command(exe: Path, prompt: str, resume: str = "") -> List[str]:
    """`claude -p` with what it may and may not do, and JSON events to follow."""
    args = [str(exe), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(ALLOWED_TOOLS),
            "--disallowedTools", ",".join(DISALLOWED_TOOLS),
            "--append-system-prompt", system_prompt()]
    if resume:
        args += ["--resume", resume]
    return args


def describe(event: Dict[str, Any]) -> str:
    """One line for the task log: what Claude Code is doing right now."""
    if event.get("type") != "assistant":
        return ""
    lines = []
    for block in (event.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_use":
            name, args = block.get("name", ""), block.get("input") or {}
            target = args.get("file_path") or args.get("path") or args.get("command") or args.get("pattern") or ""
            verb = {"Write": "Writing", "Edit": "Editing", "MultiEdit": "Editing", "Read": "Reading",
                    "Bash": "Running", "Grep": "Searching for", "Glob": "Looking for",
                    "WebSearch": "Searching the web for", "WebFetch": "Reading"}.get(name, name)
            lines.append(f"{verb} {str(target)[:120]}".strip())
        elif block.get("type") == "text" and block.get("text", "").strip():
            lines.append(block["text"].strip().split("\n")[0][:160])
    return " · ".join(lines)[:300]


@dataclass
class Pass:
    result: str = ""
    session: str = ""
    is_error: bool = False
    turns: int = 0
    files: List[str] = field(default_factory=list)


Spawn = Callable[..., Awaitable[Any]]


def _decode(line: bytes) -> str:
    """UTF-8, or -- as Claude Code sometimes writes on Windows -- the system's code page."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        import locale

        try:
            return line.decode(locale.getpreferredencoding(False))
        except (UnicodeDecodeError, LookupError):
            return line.decode("utf-8", "replace")


async def run_pass(exe: Path, prompt: str, folder: Path, log: Callable[[str], Awaitable[None]],
                   cancelled: Callable[[], bool], resume: str = "",
                   spawn: Optional[Spawn] = None) -> Pass:
    """One `claude -p` run, followed event by event until its result."""
    spawn = spawn or asyncio.create_subprocess_exec
    try:
        process = await spawn(*command(exe, prompt, resume), cwd=str(folder),
                              stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                              stderr=asyncio.subprocess.STDOUT, limit=16 * 1024 * 1024)
    except (OSError, ValueError) as exc:
        raise ClaudeUnavailable(f"Claude Code wouldn't start: {exc}")
    done = Pass()
    last_said = ""
    try:
        while True:
            if cancelled():
                process.kill()
                raise JobStopped()
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            try:
                event = json.loads(_decode(line))
            except ValueError:
                continue                                  # a warning line, not an event
            if event.get("type") == "system" and event.get("subtype") == "init":
                done.session = event.get("session_id", "") or done.session
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    path = (block.get("input") or {}).get("file_path") if block.get("type") == "tool_use" \
                        and block.get("name") in ("Write", "Edit", "MultiEdit") else None
                    if path and path not in done.files:
                        done.files.append(path)
                said = describe(event)
                if said and said != last_said:           # every step, but not the same line twice
                    last_said = said
                    await log(said)
            if event.get("type") == "result":
                done.result = str(event.get("result") or "")
                done.session = event.get("session_id", "") or done.session
                done.is_error = bool(event.get("is_error"))
                done.turns = int(event.get("num_turns") or 0)
    finally:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
    # Refused outright -- flagged as an error, or a "limit" message with nothing
    # built -- is not a report. (A real report may mention a rate limit in
    # passing; it will have changed files.)
    if _UNAVAILABLE.search(done.result) and (done.is_error or not done.files):
        raise ClaudeUnavailable(done.result.strip() or "Claude Code refused the job.")
    if not done.result and not done.files:
        raise ClaudeUnavailable("Claude Code stopped without doing anything.")
    return done


async def run_job(task: str, folder: Path, minutes: int, log: Callable[[str], Awaitable[None]],
                  cancelled: Callable[[], bool], exe: Optional[Path] = None,
                  spawn: Optional[Spawn] = None, clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """The whole job: the build, then -- with time given -- passes that test and improve."""
    exe = exe or find_claude()
    if exe is None:
        raise ClaudeUnavailable("Claude Code isn't installed on this PC.")
    folder.mkdir(parents=True, exist_ok=True)
    started = clock()
    budget = min(int(minutes or 0), MAX_MINUTES) * 60
    first = await run_pass(exe, task, folder, log, cancelled, spawn=spawn)
    passes, files, report, session = 1, list(first.files), first.result, first.session
    # "Work on it for two hours": keep going while there's time worth using.
    while budget and clock() - started < budget - 10 * 60 and session:
        left = int((budget - (clock() - started)) // 60)
        await log(f"Another pass: about {left} minutes left to test and improve.")
        try:
            more = await run_pass(exe, MORE.format(minutes=left), folder, log, cancelled,
                                  resume=session, spawn=spawn)
        except ClaudeUnavailable as exc:
            await log(f"Stopped early: {exc}")
            break
        passes += 1
        files += [f for f in more.files if f not in files]
        report, session = more.result or report, more.session or session
    return {"engine": "claude-code", "report": report.strip(), "folder": str(folder), "files": files,
            "passes": passes, "minutes": round((clock() - started) / 60, 1)}
