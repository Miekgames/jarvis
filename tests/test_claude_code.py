"""Big coding jobs go to Claude Code; the local coder steps in when it can't.

A stand-in for the `claude` process emits the stream-json events the real one
does, so nothing here starts Claude Code or uses the owner's Claude usage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.autonomy import claude_code as cc
from backend.ai.tools.project_tools import project_folder, project_said


def _events(result="Built it. Run `npm start`.", is_error=False, session="s1", files=("index.html",)):
    lines = [b"Warning: no stdin data received\n",
             json.dumps({"type": "system", "subtype": "init", "session_id": session}).encode() + b"\n"]
    for name in files:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"Writing {name} now."},
            {"type": "tool_use", "name": "Write", "input": {"file_path": name}}]}}).encode() + b"\n")
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}}).encode() + b"\n")
    lines.append(json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                             "result": result, "session_id": session, "num_turns": 7}).encode() + b"\n")
    return lines


class Process:
    def __init__(self, lines):
        self._lines = list(lines)
        self.returncode = None
        self.killed = False
        outer = self

        class Out:
            async def readline(self):
                return outer._lines.pop(0) if outer._lines else b""
        self.stdout = Out()

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class Spawner:
    """Hands out one stand-in process per `claude -p` run, recording the commands."""

    def __init__(self, *runs):
        self.runs = list(runs)
        self.commands = []
        self.processes = []

    async def __call__(self, *args, **kwargs):
        self.commands.append(list(args))
        process = Process(self.runs.pop(0) if self.runs else _events())
        self.processes.append(process)
        return process


async def _no_log(line):
    return None


async def test_a_job_runs_and_reports(tmp_path):
    seen = []

    async def log(line):
        seen.append(line)

    spawn = Spawner(_events(files=("index.html", "app.js")))
    result = await cc.run_job("build me a habit tracker", tmp_path / "habit", 0, log, lambda: False,
                              exe=Path("claude.exe"), spawn=spawn)
    assert result["engine"] == "claude-code" and result["passes"] == 1
    assert result["report"] == "Built it. Run `npm start`."
    assert result["files"] == ["index.html", "app.js"]
    assert (tmp_path / "habit").is_dir()
    assert any(line.startswith("Writing index.html") for line in seen)
    assert any("Running npm test" in line for line in seen)


def test_what_claude_code_may_and_may_not_do():
    args = cc.command(Path("claude.exe"), "build it")
    assert args[:3] == ["claude.exe", "-p", "build it"]
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    allowed = args[args.index("--allowedTools") + 1]
    denied = args[args.index("--disallowedTools") + 1]
    assert "Bash(npm:*)" in allowed and "Bash(git push:*)" not in allowed
    assert "Bash(git push:*)" in denied and "Bash(shutdown:*)" in denied
    assert "--resume" not in args
    assert cc.command(Path("claude.exe"), "more", resume="s1")[-2:] == ["--resume", "s1"]


async def test_not_signed_in_means_unavailable(tmp_path):
    spawn = Spawner(_events(result="Not logged in · Please run /login", is_error=True, files=()))
    with pytest.raises(cc.ClaudeUnavailable, match="Not logged in"):
        await cc.run_job("build it", tmp_path, 0, _no_log, lambda: False, exe=Path("c.exe"), spawn=spawn)


async def test_a_usage_limit_means_unavailable(tmp_path):
    spawn = Spawner(_events(result="Claude usage limit reached. Your limit resets at 6pm.", is_error=True, files=()))
    with pytest.raises(cc.ClaudeUnavailable):
        await cc.run_job("build it", tmp_path, 0, _no_log, lambda: False, exe=Path("c.exe"), spawn=spawn)


async def test_not_installed_means_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "find_claude", lambda: None)
    with pytest.raises(cc.ClaudeUnavailable, match="isn't installed"):
        await cc.run_job("build it", tmp_path, 0, _no_log, lambda: False)


async def test_stopping_kills_claude_code(tmp_path):
    spawn = Spawner(_events())
    stop = {"now": False}

    async def log(line):
        stop["now"] = True                   # the owner says "stop the project" mid-run

    with pytest.raises(cc.JobStopped):
        await cc.run_job("build it", tmp_path, 0, log, lambda: stop["now"], exe=Path("c.exe"), spawn=spawn)
    assert spawn.processes[0].killed


async def test_given_time_it_keeps_testing_and_improving(tmp_path):
    now = {"t": 0.0}

    def clock():
        now["t"] += 20 * 60                  # each look at the clock: twenty minutes later
        return now["t"]

    spawn = Spawner(_events(result="v1"), _events(result="v2"), _events(result="v3"))
    result = await cc.run_job("build it", tmp_path, 60, _no_log, lambda: False, exe=Path("c.exe"),
                              spawn=spawn, clock=clock)
    assert result["passes"] == 2 and result["report"] == "v2"
    assert "--resume" in spawn.commands[1] and "s1" in spawn.commands[1]
    assert "minutes left" in spawn.commands[1][2]


def test_the_newest_bundled_claude_code_is_used(tmp_path, monkeypatch):
    for version in ("2.1.9", "2.1.330", "2.0.77"):
        target = tmp_path / "Claude" / "claude-code" / version / "abc"
        target.mkdir(parents=True)
        (target / "claude.exe").write_bytes(b"")
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert cc.find_claude().parent.parent.name == "2.1.330"


# -- saying it ---------------------------------------------------------------
@pytest.mark.parametrize("said, task, minutes", [
    ("build me a habit tracker app", "build me a habit tracker app", 0),
    ("Jarvis, make me a Discord bot that rolls dice", "make me a Discord bot that rolls dice", 0),
    ("while I'm away for two hours, build me a budgeting website", "build me a budgeting website", 120),
    ("work on my snake game for an hour", "Work on my snake game", 60),
    ("can you build me a website?", "build me a website", 0),
    ("start a project: a weather dashboard with charts", "a weather dashboard with charts", 0),
])
def test_asking_for_something_to_be_made(said, task, minutes):
    tool, args = project_said(said)
    assert tool == "start_project" and args["task"] == task and args["minutes"] == minutes


@pytest.mark.parametrize("said", [
    "how do I build a website?", "what's the best way to make a game", "explain this python script",
    "make a grocery list with eggs", "write me a poem", "make me a sandwich", "write an email to Sam",
    "make your answers shorter", "create a reminder for 5pm", "make a countdown to christmas",
    "can you explain how apps work?", "what can you do",
])
def test_questions_stay_with_the_local_model(said):
    assert project_said(said) is None


def test_project_folders_are_named_after_the_job(tmp_path):
    from datetime import datetime

    folder = project_folder(tmp_path, "build me a habit tracker app", datetime(2026, 11, 27, 14, 5))
    assert folder == tmp_path / "habit-tracker-app-20261127-1405"


# -- the task ------------------------------------------------------------------
class Ctx:
    def __init__(self, params, **services):
        self.params = params
        self.services = services
        self.lines = []
        self.cancelled = False

    async def log(self, line, level="info", **meta):
        self.lines.append(line)

    def checkpoint_now(self):
        if self.cancelled:
            raise RuntimeError("cancelled")


class Recorder:
    def __init__(self):
        self.saved, self.notes, self.started = [], [], []

    async def _save_message(self, conversation_id, role, text, blocks, *rest):
        self.saved.append((conversation_id, text))

    async def create(self, title, body="", priority="normal", source="", action=None):
        self.notes.append((title, body, priority))

    async def start(self, prompt, away, work, allow_shell=False):
        self.started.append((prompt, away, work))
        return {"phase": "planning"}


async def test_the_report_comes_back(monkeypatch, tmp_path):
    from backend.tasks.handlers import project

    async def done(task, folder, minutes, log, cancelled):
        return {"engine": "claude-code", "report": "Built a habit tracker. Open index.html.",
                "folder": str(folder), "files": ["index.html"], "passes": 1, "minutes": 12.5}

    monkeypatch.setattr(cc, "run_job", done)
    seen = Recorder()
    ctx = Ctx({"task": "build me a habit tracker", "folder": str(tmp_path), "conversation_id": "c1"},
              orchestrator=seen, notifications=seen, autonomy=seen)
    result = await project.project_handler(ctx)
    assert result["engine"] == "claude-code"
    conversation, text = seen.saved[0]
    assert conversation == "c1" and "Project done" in text and "Built a habit tracker" in text
    assert str(tmp_path) in text
    assert seen.notes[0][2] == "high" and seen.started == []


async def test_without_claude_code_the_local_coder_takes_over(monkeypatch, tmp_path):
    from backend.tasks.handlers import project

    async def refused(task, folder, minutes, log, cancelled):
        raise cc.ClaudeUnavailable("Not logged in · Please run /login")

    monkeypatch.setattr(cc, "run_job", refused)
    seen = Recorder()
    ctx = Ctx({"task": "build me a habit tracker", "minutes": 30, "folder": str(tmp_path),
               "conversation_id": "c1"}, orchestrator=seen, notifications=seen, autonomy=seen)
    result = await project.project_handler(ctx)
    assert result["engine"] == "local"
    assert seen.started == [("build me a habit tracker", 30.0, 30.0)]
    assert "isn't signed in on this PC yet" in seen.saved[0][1], "the reason is said plainly"
    assert "local coder" in seen.saved[0][1]


async def test_start_project_creates_the_job(registry, permissions, tmp_path, monkeypatch):
    from backend.ai.tools import project_tools

    made = []

    class Engine:
        async def create(self, **record):
            made.append(record)
            return {"id": "t1", **record}

    project_tools.register(registry)
    registry.context._services.update(tasks=Engine(), settings=SimpleNamespace(DATA_DIR=tmp_path))
    monkeypatch.setattr(cc, "find_claude", lambda: Path("claude.exe"))
    from backend.core import permissions as gate
    if not gate.ALL_GRANTED:
        # Off until the owner turns it on: nothing is started.
        refused = await registry.dispatch("start_project", {"task": "build me a habit tracker app"})
        assert not refused.ok and not made
    await permissions.set("code.delegate", True)
    result = await registry.dispatch("start_project", {"task": "build me a habit tracker app", "minutes": 30})
    assert result.ok and "Claude Code is building that for about 30 minutes" in result.message
    assert made[0]["task_type"] == "project"
    assert Path(made[0]["params"]["folder"]).parent == tmp_path / "projects"

    monkeypatch.setattr(cc, "find_claude", lambda: None)
    result = await registry.dispatch("start_project", {"task": "build me a snake game"})
    assert "the local coder will build what it can" in result.message


def test_output_in_the_windows_code_page_still_reads():
    from backend.autonomy.claude_code import _decode

    assert _decode("Not logged in · Please run /login".encode("cp1252")).startswith("Not logged in ·")
    assert _decode("✅ done".encode("utf-8")) == "✅ done"


# -- the 5-hour limit: hand the project to the local coder -------------------
LIMIT_TEXT = "You've hit your 5-hour limit · resets 5:40pm (America/New_York)"


def test_the_real_limit_wording_means_unavailable():
    from backend.autonomy.claude_code import _UNAVAILABLE
    from backend.tasks.handlers.project import _plain_reason

    assert _UNAVAILABLE.search(LIMIT_TEXT)
    assert _plain_reason(LIMIT_TEXT) == "your Claude usage limit has been reached (it resets 5:40pm)"


async def test_a_limit_reply_is_not_a_report(tmp_path):
    """It came back as a normal result, not flagged as an error, with nothing built."""
    import json as _json

    from backend.autonomy import claude_code

    class Out:
        def __init__(self, lines):
            self.lines = [(_json.dumps(l) + "\n").encode() for l in lines]

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class Proc:
        returncode = 0

        def __init__(self, lines):
            self.stdout = Out(lines)

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def spawn(*args, **kwargs):
        return Proc([{"type": "result", "subtype": "success", "is_error": False, "result": LIMIT_TEXT,
                      "num_turns": 1, "session_id": "s"}])

    async def log(line):
        pass

    with pytest.raises(claude_code.ClaudeUnavailable, match="5-hour limit"):
        await claude_code.run_pass(Path("claude.exe"), "build it", tmp_path, log, lambda: False, spawn=spawn)
