"""
The developer workspace.

Git state, diff review, commit messages written from the real diff, a test
runner that parses actual failures, build status, CI, and a timeline of what
happened.

Two boundaries that matter:

  * **Reading is cheap, running is not.** Git status and diffs need only
    `dev.workspace`. Running tests or builds executes whatever the project
    defines in `package.json` or `pytest.ini`, so it sits behind `dev.execute`
    (high risk, off by default) and asks for confirmation.
  * **Commit messages are generated, commits are not made silently.**
    `dev.git_write` is separate, and committing is a confirmed action showing
    exactly which files are staged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.proc import run as _run, popen as _popen
from ..core.events import bus
from ..core.util import now_iso, since, truncate
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.devtools")

GIT_TIMEOUT = 30
RUN_TIMEOUT = 900          # a full test suite can legitimately take minutes
MAX_DIFF_CHARS = 60_000

# Test/build commands, discovered from the project rather than assumed.
_RUNNERS = {
    "python": {"test": ["python", "-m", "pytest", "-q"], "marker": ("pytest.ini", "pyproject.toml",
                                                                    "setup.cfg", "tests")},
    "node": {"test": ["npm", "test", "--silent"], "build": ["npm", "run", "build"],
             "marker": ("package.json",)},
    "rust": {"test": ["cargo", "test"], "build": ["cargo", "build"], "marker": ("Cargo.toml",)},
    "go": {"test": ["go", "test", "./..."], "build": ["go", "build", "./..."],
           "marker": ("go.mod",)},
}


class DevTools:
    def __init__(self, db, settings, orchestrator, activity, code, audit=None,
                 integrations=None):
        self.db = db
        self.settings = settings
        self.orchestrator = orchestrator
        self.activity = activity
        self.code = code                  # CodeIntelligence, for workspace resolution
        self.audit = audit
        self.integrations = integrations

    # ==================================================================
    async def _root(self, workspace: str = "") -> Tuple[Path, Dict[str, Any]]:
        record = await self.code.resolve_workspace(workspace)
        return Path(record["path"]), record

    async def _run(self, command: List[str], cwd: Path,
                   timeout: int = GIT_TIMEOUT) -> Dict[str, Any]:
        """Run a command and return its real output. Never raises on failure --
        a non-zero exit is information, not an exception.

        Deliberately not through a shell. It used to pass `shell=True` on
        Windows so that `.cmd` launchers like npm could be found, and that
        quietly broke anything whose arguments contained a shell
        metacharacter. `git log --pretty=format:%H|%h|%an|%aI|%s` is the
        obvious casualty: cmd.exe reads those pipes as pipes, tries to run a
        program called `%h`, and the whole thing blocks until the timeout --
        thirty seconds of nothing, for a command git answers instantly.

        It is also the kind of thing that turns an argument into an
        instruction. Resolving the executable up front gets the `.cmd` lookup
        without handing anything to a command interpreter.
        """
        executable = shutil.which(command[0])
        if not executable:
            return {"ok": False, "code": -1, "stdout": "",
                    "stderr": f"'{command[0]}' is not installed or not on PATH."}
        command = [executable] + list(command[1:])

        def _execute() -> Dict[str, Any]:
            import subprocess

            try:
                result = _run(
                    command, cwd=str(cwd), capture_output=True, text=True,
                    timeout=timeout, shell=False,
                )
                return {"ok": result.returncode == 0, "code": result.returncode,
                        "stdout": result.stdout or "", "stderr": result.stderr or ""}
            except FileNotFoundError:
                return {"ok": False, "code": -1, "stdout": "",
                        "stderr": f"'{command[0]}' is not installed or not on PATH."}
            except Exception as exc:
                import subprocess as sp
                if isinstance(exc, sp.TimeoutExpired):
                    return {"ok": False, "code": -1, "stdout": "",
                            "stderr": f"Timed out after {timeout}s."}
                return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}

        return await asyncio.get_event_loop().run_in_executor(None, _execute)

    async def _record(self, kind: str, summary: str, workspace_id: str = "",
                      detail: Optional[Dict[str, Any]] = None, ok: bool = True) -> None:
        await self.db.execute(
            "INSERT INTO dev_events (ts, workspace_id, kind, summary, detail, ok)"
            " VALUES (?,?,?,?,?,?)",
            (now_iso(), workspace_id, kind, truncate(summary, 400),
             dumps(detail or {}), 1 if ok else 0),
        )
        bus.publish("dev.event", {"kind": kind, "summary": truncate(summary, 200), "ok": ok},
                    source="devtools")

    # ==================================================================
    # git — reading
    # ==================================================================
    async def status(self, workspace: str = "") -> Dict[str, Any]:
        root, record = await self._root(workspace)
        if not (root / ".git").exists():
            return {"repository": False, "workspace": record["name"],
                    "message": f"{record['name']} is not a git repository."}

        branch = await self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
        porcelain = await self._run(["git", "status", "--porcelain=v1", "--branch"], root)
        if not porcelain["ok"]:
            return {"repository": True, "error": porcelain["stderr"].strip()}

        changed, ahead, behind = _parse_status(porcelain["stdout"])
        stashes = await self._run(["git", "stash", "list"], root)
        last = await self._run(
            ["git", "log", "-1", "--pretty=format:%h|%an|%ar|%s"], root)

        commit = {}
        if last["ok"] and last["stdout"]:
            parts = last["stdout"].split("|", 3)
            if len(parts) == 4:
                commit = {"sha": parts[0], "author": parts[1], "when": parts[2],
                          "subject": parts[3]}

        return {
            "repository": True,
            "workspace": record["name"],
            "workspace_id": record["id"],
            "branch": branch["stdout"].strip() if branch["ok"] else "?",
            "ahead": ahead, "behind": behind,
            "changed_files": changed,
            "clean": not changed,
            "staged": [f for f in changed if f["staged"]],
            "unstaged": [f for f in changed if not f["staged"] and not f["untracked"]],
            "untracked": [f for f in changed if f["untracked"]],
            "stashes": len([l for l in (stashes["stdout"] or "").splitlines() if l.strip()]),
            "last_commit": commit,
        }

    async def diff(self, workspace: str = "", path: str = "",
                   staged: bool = False) -> Dict[str, Any]:
        root, record = await self._root(workspace)
        command = ["git", "diff"]
        if staged:
            command.append("--staged")
        if path:
            command += ["--", path]

        result = await self._run(command, root)
        if not result["ok"] and result["stderr"]:
            return {"ok": False, "error": result["stderr"].strip()}

        text = result["stdout"]
        added, removed = _diff_stats(text)
        return {
            "ok": True, "workspace": record["name"], "path": path or "(all)",
            "staged": staged,
            "diff": truncate(text, MAX_DIFF_CHARS),
            "truncated": len(text) > MAX_DIFF_CHARS,
            "lines_added": added, "lines_removed": removed,
            "files": _diff_files(text),
            "empty": not text.strip(),
        }

    async def log(self, workspace: str = "", limit: int = 20,
                  path: str = "") -> List[Dict[str, Any]]:
        root, _ = await self._root(workspace)
        command = ["git", "log", f"-{min(limit, 100)}",
                   "--pretty=format:%H|%h|%an|%aI|%s"]
        if path:
            command += ["--", path]
        result = await self._run(command, root)
        if not result["ok"]:
            return []

        commits = []
        for line in result["stdout"].splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5:
                commits.append({"sha": parts[0], "short": parts[1], "author": parts[2],
                                "date": parts[3], "subject": parts[4]})
        return commits

    async def branches(self, workspace: str = "") -> Dict[str, Any]:
        root, _ = await self._root(workspace)
        result = await self._run(
            ["git", "branch", "--format=%(refname:short)|%(HEAD)|%(committerdate:relative)"], root)
        branches = []
        for line in (result["stdout"] or "").splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                branches.append({"name": parts[0], "current": parts[1].strip() == "*",
                                 "last_commit": parts[2]})
        return {"branches": branches, "count": len(branches)}

    # ==================================================================
    # git — review and writing
    # ==================================================================
    async def review_diff(self, workspace: str = "", staged: bool = False) -> Dict[str, Any]:
        """Explain what a diff actually does, and flag anything risky in it."""
        diff = await self.diff(workspace, staged=staged)
        if not diff.get("ok"):
            return diff
        if diff["empty"]:
            return {"ok": True, "empty": True,
                    "review": "There are no changes to review."
                              + (" Nothing is staged." if staged else "")}

        review = await self.orchestrator.ask(
            "Review this diff. Cover: what it changes and why that appears to be the intent; "
            "anything that looks like a bug, a regression, or an accidental inclusion "
            "(debug prints, commented-out code, secrets, unrelated churn); and anything worth "
            "testing before it lands. Be specific and cite file and hunk. If it looks fine, "
            "say so briefly rather than inventing concerns.\n\n"
            f"{diff['diff']}",
            purpose="coding", max_tokens=2000,
        )
        await self._record("review", f"Reviewed a diff ({diff['lines_added']}+/"
                                     f"{diff['lines_removed']}-)")
        return {"ok": True, "empty": False, "files": diff["files"],
                "lines_added": diff["lines_added"], "lines_removed": diff["lines_removed"],
                "review": review}

    async def commit_message(self, workspace: str = "",
                             staged_only: bool = True) -> Dict[str, Any]:
        """Write a commit message from the real diff, not from a description."""
        diff = await self.diff(workspace, staged=staged_only)
        if not diff.get("ok"):
            return diff
        if diff["empty"]:
            return {"ok": False,
                    "message": "Nothing is staged." if staged_only
                               else "There are no uncommitted changes."}

        recent = await self.log(workspace, limit=8)
        style = "\n".join(f"- {c['subject']}" for c in recent) or "(no history to match)"

        generated = await self.orchestrator.ask(
            "Write a git commit message for this diff.\n"
            "- First line: imperative mood, under 72 characters, no trailing full stop.\n"
            "- Then a blank line, then 1-3 short lines on *why*, only if the diff isn't "
            "self-explanatory.\n"
            "- Describe what the diff actually does. Do not invent motivation.\n"
            "- Match the style of the recent commits below.\n"
            "Return only the message.\n\n"
            f"RECENT COMMITS (for style):\n{style}\n\nDIFF:\n{diff['diff']}",
            purpose="coding", max_tokens=500,
        )
        message = generated.strip().strip("`").strip()
        return {"ok": True, "message": message, "files": diff["files"],
                "lines_added": diff["lines_added"], "lines_removed": diff["lines_removed"],
                "staged_only": staged_only}

    async def stage(self, paths: List[str], workspace: str = "") -> Dict[str, Any]:
        root, record = await self._root(workspace)
        result = await self._run(["git", "add", "--"] + (paths or ["."]), root)
        if not result["ok"]:
            return {"ok": False, "error": result["stderr"].strip()}
        status = await self.status(workspace)
        return {"ok": True, "staged": [f["path"] for f in status.get("staged", [])]}

    async def commit(self, message: str, workspace: str = "") -> Dict[str, Any]:
        """Make a commit. Never signs, never pushes, never amends."""
        root, record = await self._root(workspace)
        status = await self.status(workspace)
        if not status.get("staged"):
            return {"ok": False, "message": "Nothing is staged to commit."}

        result = await self._run(["git", "commit", "-m", message], root, timeout=60)
        if not result["ok"]:
            return {"ok": False, "error": (result["stderr"] or result["stdout"]).strip()}

        sha = await self._run(["git", "rev-parse", "--short", "HEAD"], root)
        short = sha["stdout"].strip()

        if self.audit:
            await self.audit.record("dev.commit", subject=record["name"],
                                    detail={"sha": short, "message": truncate(message, 200),
                                            "files": len(status["staged"])})
        await self._record("commit", f"Committed {short}: {truncate(message.splitlines()[0], 80)}",
                           record["id"], {"files": len(status["staged"])})
        await self.activity.record(f"Committed {short} in {record['name']}.", source="devtools")
        return {"ok": True, "sha": short, "files": len(status["staged"]),
                "message": message.splitlines()[0],
                "note": "Committed locally. Nothing has been pushed."}

    # ==================================================================
    # tests and builds
    # ==================================================================
    def _discover(self, root: Path) -> Dict[str, Any]:
        """Work out how this project is actually tested and built."""
        found: Dict[str, Any] = {"kinds": [], "test": None, "build": None}
        for kind, spec in _RUNNERS.items():
            if any((root / marker).exists() for marker in spec["marker"]):
                found["kinds"].append(kind)
                # setdefault would not replace the pre-initialised None.
                if spec.get("test") and not found["test"]:
                    found["test"] = spec["test"]
                if spec.get("build") and not found["build"]:
                    found["build"] = spec["build"]

        # package.json is authoritative about which scripts really exist.
        package = root / "package.json"
        if package.exists():
            try:
                scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
                found["scripts"] = list(scripts)
                if "test" not in scripts:
                    if "python" in found["kinds"]:
                        found["test"] = _RUNNERS["python"]["test"]
                    else:
                        found["test"] = None
                if "build" not in scripts:
                    found["build"] = None
            except (json.JSONDecodeError, OSError):
                pass
        return found

    async def run_tests(self, workspace: str = "", pattern: str = "") -> Dict[str, Any]:
        root, record = await self._root(workspace)
        discovered = self._discover(root)
        command = list(discovered.get("test") or [])
        if not command:
            return {"ok": False,
                    "message": f"I can't tell how {record['name']} runs its tests. "
                               f"Detected: {', '.join(discovered['kinds']) or 'nothing'}."}
        if pattern:
            command += ["-k", pattern] if "pytest" in " ".join(command) else [pattern]

        await self.activity.record(f"Running tests in {record['name']}…", source="devtools")
        result = await self._run(command, root, timeout=RUN_TIMEOUT)
        parsed = _parse_test_output(result["stdout"] + "\n" + result["stderr"])

        await self._record(
            "test", f"Tests {'passed' if result['ok'] else 'failed'} in {record['name']}"
                    f" ({parsed['passed']} passed, {parsed['failed']} failed)",
            record["id"], {"command": " ".join(command)}, ok=result["ok"],
        )
        return {
            "ok": result["ok"],
            "command": " ".join(command),
            "exit_code": result["code"],
            **parsed,
            "output": truncate(result["stdout"] or result["stderr"], 8000),
        }

    async def run_build(self, workspace: str = "") -> Dict[str, Any]:
        root, record = await self._root(workspace)
        command = list(self._discover(root).get("build") or [])
        if not command:
            return {"ok": False, "message": f"No build script found for {record['name']}."}

        result = await self._run(command, root, timeout=RUN_TIMEOUT)
        errors = _parse_build_errors(result["stdout"] + "\n" + result["stderr"])
        await self._record("build", f"Build {'succeeded' if result['ok'] else 'failed'} in "
                                    f"{record['name']}", record["id"], ok=result["ok"])
        return {"ok": result["ok"], "command": " ".join(command), "exit_code": result["code"],
                "errors": errors[:20], "error_count": len(errors),
                "output": truncate(result["stdout"] or result["stderr"], 8000)}

    async def analyse_failures(self, workspace: str = "") -> Dict[str, Any]:
        """Run the tests, then explain what actually broke and how to fix it."""
        run = await self.run_tests(workspace)
        if run.get("ok"):
            return {**run, "analysis": "All tests passed — nothing to analyse."}
        if not run.get("failures"):
            return {**run, "analysis": "The test run failed but produced no parseable failures. "
                                       "The raw output is included above."}

        workspace_record = await self.code.resolve_workspace(workspace)
        grounded = []
        for failure in run["failures"][:5]:
            try:
                explained = await self.code.explain_error(failure["detail"] or failure["name"],
                                                          workspace_record["id"])
                grounded.append({"test": failure["name"],
                                 "frames": explained["resolved_frames"],
                                 "explanation": explained["explanation"]})
            except Exception:
                grounded.append({"test": failure["name"], "frames": 0,
                                 "explanation": "Could not resolve source for this failure."})

        return {**run, "analysis": grounded}

    async def debug_plan(self, problem: str, workspace: str = "") -> Dict[str, Any]:
        """A concrete plan, grounded in the repo's real state."""
        status = await self.status(workspace)
        record = await self.code.resolve_workspace(workspace)
        overview = await self.code.overview(record["id"])
        recent = await self.log(workspace, limit=5)

        plan = await self.orchestrator.ask(
            "Produce a debugging plan for this problem. Give ordered, concrete steps: what to "
            "check, what command to run, and what each result would tell you. Use the repository "
            "state shown. Do not suggest checking files that don't exist here.\n\n"
            f"PROBLEM: {problem}\n\n"
            f"REPO: {record['name']} ({overview['total_lines']} lines, "
            f"{', '.join(l['language'] for l in overview['by_language'][:4])})\n"
            f"BRANCH: {status.get('branch')} | changed files: "
            f"{len(status.get('changed_files', []))}\n"
            f"RECENT COMMITS: {'; '.join(c['subject'] for c in recent)}\n"
            f"LARGEST FILES: {', '.join(f['path'] for f in overview['largest_files'][:5])}",
            purpose="coding", max_tokens=1600,
        )
        return {"problem": problem, "workspace": record["name"], "plan": plan,
                "branch": status.get("branch"),
                "uncommitted_changes": len(status.get("changed_files", []))}

    # ==================================================================
    # CI
    # ==================================================================
    async def ci_status(self, repo: str = "") -> Dict[str, Any]:
        if self.integrations is None or not self.integrations.is_connected("github"):
            return {"ok": False,
                    "message": "CI status needs the GitHub integration. Add GITHUB_TOKEN to .env."}
        connector = self.integrations.get("github")
        if not repo:
            return {"ok": False, "message": "Which repository? Give it as owner/name."}
        try:
            runs = await connector.workflow_runs(repo, limit=10)
        except Exception as exc:
            return {"ok": False, "message": f"GitHub request failed: {exc}"}
        failing = [r for r in runs if r.get("conclusion") == "failure"]
        return {"ok": True, "repo": repo, "runs": runs, "failing": len(failing),
                "latest": runs[0] if runs else None}

    # ==================================================================
    # timeline
    # ==================================================================
    async def timeline(self, workspace: str = "", hours: int = 72,
                       limit: int = 60) -> List[Dict[str, Any]]:
        """What has actually happened in development recently: commits, test
        runs, builds, reviews and indexing, in one stream."""
        events: List[Dict[str, Any]] = []

        rows = await self.db.fetch_all(
            "SELECT * FROM dev_events WHERE ts > ? ORDER BY ts DESC LIMIT ?",
            (since(hours * 3600), limit),
        )
        for row in rows:
            events.append({"ts": row["ts"], "kind": row["kind"], "summary": row["summary"],
                           "ok": bool(row["ok"]), "detail": loads(row["detail"], {})})

        try:
            for commit in await self.log(workspace, limit=15):
                events.append({"ts": commit["date"], "kind": "commit",
                               "summary": f"{commit['short']} {commit['subject']}",
                               "ok": True, "detail": {"author": commit["author"]}})
        except LookupError:
            pass

        events.sort(key=lambda e: e["ts"], reverse=True)
        return events[:limit]

    async def summary(self, workspace: str = "") -> Dict[str, Any]:
        """Everything the developer panel shows, in one call."""
        try:
            status = await self.status(workspace)
        except LookupError as exc:
            return {"ok": False, "message": str(exc)}

        record = await self.code.resolve_workspace(workspace)
        root = Path(record["path"])
        return {
            "ok": True,
            "workspace": record,
            "git": status,
            "runners": self._discover(root),
            "recent_commits": await self.log(workspace, limit=8),
            "timeline": await self.timeline(workspace, limit=25),
            "indexed": bool(record.get("indexed_at")),
        }


# ---------------------------------------------------------------------------
def _parse_status(text: str) -> Tuple[List[Dict[str, Any]], int, int]:
    changed: List[Dict[str, Any]] = []
    ahead = behind = 0

    for line in (text or "").splitlines():
        if line.startswith("##"):
            ahead_match = re.search(r"ahead (\d+)", line)
            behind_match = re.search(r"behind (\d+)", line)
            ahead = int(ahead_match.group(1)) if ahead_match else 0
            behind = int(behind_match.group(1)) if behind_match else 0
            continue
        if len(line) < 3:
            continue
        index_state, tree_state, path = line[0], line[1], line[3:].strip()
        changed.append({
            "path": path,
            "staged": index_state not in (" ", "?"),
            "untracked": index_state == "?",
            "status": _status_word(index_state, tree_state),
        })
    return changed, ahead, behind


def _status_word(index_state: str, tree_state: str) -> str:
    if index_state == "?":
        return "untracked"
    code = index_state if index_state != " " else tree_state
    return {"A": "added", "M": "modified", "D": "deleted", "R": "renamed",
            "C": "copied", "U": "conflicted"}.get(code, "changed")


def _diff_stats(diff: str) -> Tuple[int, int]:
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return added, removed


def _diff_files(diff: str) -> List[str]:
    return sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M)})


def _parse_test_output(output: str) -> Dict[str, Any]:
    """Extract counts and individual failures from pytest, jest or vitest."""
    passed = failed = skipped = 0
    failures: List[Dict[str, str]] = []

    # Each count is searched independently. A single combined pattern with all
    # groups optional matches the empty string and silently reports zero.
    def _count(label: str) -> int:
        match = re.search(rf"\b(\d+)\s+{label}\b", output)
        return int(match.group(1)) if match else 0

    passed, failed, skipped = _count("passed"), _count("failed"), _count("skipped")

    jest = re.search(r"Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(\d+) passed", output)
    if jest:
        failed = int(jest.group(1) or 0)
        skipped = int(jest.group(2) or 0)
        passed = int(jest.group(3) or 0)

    # pytest short summary lines
    for match in re.finditer(r"^FAILED\s+(\S+)\s*(?:-\s*(.*))?$", output, re.M):
        failures.append({"name": match.group(1), "detail": (match.group(2) or "").strip()})

    # pytest traceback blocks give richer detail
    for match in re.finditer(r"^_{5,}\s+(.+?)\s+_{5,}$", output, re.M):
        name = match.group(1).strip()
        if not any(f["name"].endswith(name) for f in failures):
            failures.append({"name": name, "detail": ""})

    # vitest / jest
    for match in re.finditer(r"^\s*(?:×|✕|FAIL)\s+(.+)$", output, re.M):
        failures.append({"name": match.group(1).strip()[:200], "detail": ""})

    # Attach surrounding context to failures that have none.
    if failures:
        for failure in failures:
            if failure["detail"]:
                continue
            index = output.find(failure["name"])
            if index >= 0:
                failure["detail"] = truncate(output[index:index + 1500], 1500)

    return {"passed": passed, "failed": failed or len(failures), "skipped": skipped,
            "failures": failures[:20]}


def _parse_build_errors(output: str) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    patterns = [
        re.compile(r"^(.+?)\((\d+),(\d+)\):\s*error\s+(\w+):\s*(.+)$", re.M),   # tsc
        re.compile(r"^(.+?):(\d+):(\d+):\s*error:\s*(.+)$", re.M),              # gcc/rust style
    ]
    for pattern in patterns:
        for match in pattern.finditer(output or ""):
            groups = match.groups()
            errors.append({
                "file": groups[0], "line": int(groups[1]),
                "message": truncate(groups[-1], 300),
            })
    return errors
