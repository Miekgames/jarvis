"""Developer workspace and code-understanding tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import truncate
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    # ==================================================================
    # workspaces & indexing
    # ==================================================================
    @registry.add(
        "workspaces",
        "List, add or index code workspaces JARVIS is authorised to read. Index a project before "
        "asking questions about it.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "index", "overview", "remove"]},
            "path": {"type": "string", "description": "Folder to add"},
            "workspace": {"type": "string", "description": "Name or id; omit for the current one"},
        }, "required": ["action"]},
        permission="dev.workspace",
        category="developer",
        timeout=300.0,
    )
    async def workspaces(ctx, action: str, path: str = "", workspace: str = ""):
        code = ctx.get("code")
        if code is None:
            return fail("Code intelligence unavailable.")

        if action == "list":
            listing = await code.workspaces()
            if not listing:
                return ok({"workspaces": []},
                          "No workspaces registered. Add one with action='add' and a folder path.")
            return ok({"workspaces": [
                {"id": w["id"], "name": w["name"], "path": w["path"], "kind": w["kind"],
                 "indexed": bool(w["indexed_at"]), "files": w["file_count"],
                 "symbols": w["symbol_count"]}
                for w in listing
            ]}, f"{len(listing)} workspace(s).")

        if action == "add":
            if not path:
                return fail("Which folder? Give a path.")
            try:
                record = await code.add_workspace(path)
            except (PermissionError, NotADirectoryError) as exc:
                return fail(str(exc))
            return ok({"workspace": record},
                      f"Registered '{record['name']}' ({record['kind']}). Index it to make it "
                      f"searchable.")

        try:
            record = await code.resolve_workspace(workspace)
        except LookupError as exc:
            return fail(str(exc))

        if action == "index":
            result = await code.index(record["id"])
            return ok(result, f"Indexed {result['workspace']}: {result['files']} files, "
                              f"{result['symbols']} symbols.")
        if action == "overview":
            result = await code.overview(record["id"])
            languages = ", ".join(f"{l['language']} ({l['files']} files)"
                                  for l in result["by_language"][:5])
            return ok(result, f"{record['name']}: {result['total_lines']} lines. {languages}.")
        if action == "remove":
            return ok({"removed": await code.remove_workspace(record["id"])},
                      f"Removed '{record['name']}' from the index.")
        return fail(f"Unknown action '{action}'.")

    # ==================================================================
    # code questions
    # ==================================================================
    @registry.add(
        "find_in_code",
        "Find where something is defined or used across an indexed codebase. Returns exact file "
        "paths and line numbers.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Function, class, type or variable name"},
            "mode": {"type": "string", "enum": ["definition", "references", "outline"]},
            "path": {"type": "string", "description": "For mode='outline', the file to outline"},
            "workspace": {"type": "string"},
        }, "required": ["name"]},
        permission="dev.workspace",
        category="developer",
        timeout=120.0,
    )
    async def find_in_code(ctx, name: str, mode: str = "definition", path: str = "",
                           workspace: str = ""):
        code = ctx.get("code")
        try:
            record = await code.resolve_workspace(workspace)
        except LookupError as exc:
            return fail(str(exc))

        if mode == "outline":
            try:
                return ok(await code.outline(path or name, record["id"]))
            except FileNotFoundError as exc:
                return fail(str(exc))

        if mode == "references":
            hits = await code.find_references(name, record["id"])
            if not hits:
                return ok({"references": []}, f"No references to '{name}' found.")
            return ok({"count": len(hits), "references": hits[:50]},
                      f"{len(hits)} reference(s) to '{name}'.")

        symbols = await code.find_symbol(name, record["id"])
        if not symbols:
            return ok({"symbols": []},
                      f"'{name}' isn't in the index. It may be new — re-index, or try "
                      f"mode='references'.")
        first = symbols[0]
        return ok({"symbols": symbols},
                  f"{first['kind']} {first['name']} at {first['path']}:{first['line']}"
                  + (f" (+{len(symbols) - 1} more)" if len(symbols) > 1 else ""))

    @registry.add(
        "ask_codebase",
        "Answer a question about an indexed codebase. The index picks which files to read, so "
        "answers are grounded in real code with file and line references.",
        {"type": "object", "properties": {
            "question": {"type": "string"},
            "workspace": {"type": "string"},
        }, "required": ["question"]},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def ask_codebase(ctx, question: str, workspace: str = ""):
        code = ctx.get("code")
        try:
            record = await code.resolve_workspace(workspace)
        except LookupError as exc:
            return fail(str(exc))
        if not record.get("indexed_at"):
            return fail(f"'{record['name']}' isn't indexed yet. Index it first.")
        result = await code.ask_about_codebase(question, record["id"])
        return ok(result, result["answer"])

    @registry.add(
        "explain_error",
        "Explain a compiler error, runtime error or stack trace against the real source. Resolves "
        "each frame to actual code before explaining.",
        {"type": "object", "properties": {
            "error": {"type": "string", "description": "The full error text or stack trace"},
            "workspace": {"type": "string"},
        }, "required": ["error"]},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def explain_error(ctx, error: str, workspace: str = ""):
        code = ctx.get("code")
        try:
            record = await code.resolve_workspace(workspace)
        except LookupError as exc:
            return fail(str(exc))
        result = await code.explain_error(error, record["id"])
        return ok(result, f"Resolved {result['resolved_frames']}/{result['total_frames']} "
                          f"frame(s). {truncate(result['explanation'], 400)}")

    @registry.add(
        "review_code",
        "Review a file for likely bugs, suggest improvements, or propose tests.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root"},
            "focus": {"type": "string", "enum": ["bugs", "improve", "tests"]},
            "workspace": {"type": "string"},
        }, "required": ["path"]},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def review_code(ctx, path: str, focus: str = "bugs", workspace: str = ""):
        code = ctx.get("code")
        try:
            record = await code.resolve_workspace(workspace)
            result = await code.review_file(path, record["id"], focus)
        except (LookupError, FileNotFoundError, ValueError) as exc:
            return fail(str(exc))
        static = result["static_findings"]
        return ok(result, truncate(result["review"], 500)
                  + (f" Plus {len(static)} static finding(s)." if static else ""))

    # ==================================================================
    # git
    # ==================================================================
    @registry.add(
        "git_status",
        "Show the repository state: branch, changed files, staged vs unstaged, ahead/behind, and "
        "the last commit.",
        {"type": "object", "properties": {"workspace": {"type": "string"}}},
        permission="dev.workspace",
        category="developer",
    )
    async def git_status(ctx, workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            status = await dev.status(workspace)
        except LookupError as exc:
            return fail(str(exc))
        if not status.get("repository"):
            return ok(status, status.get("message", "Not a git repository."))
        if status.get("clean"):
            return ok(status, f"{status['workspace']} is clean on {status['branch']}.")
        return ok(status,
                  f"{status['branch']}: {len(status['changed_files'])} changed "
                  f"({len(status['staged'])} staged, {len(status['unstaged'])} unstaged, "
                  f"{len(status['untracked'])} untracked).")

    @registry.add(
        "git_diff",
        "Show or review the current diff. Set review=true to have it explained and checked for "
        "anything risky.",
        {"type": "object", "properties": {
            "staged": {"type": "boolean"},
            "path": {"type": "string"},
            "review": {"type": "boolean"},
            "workspace": {"type": "string"},
        }},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def git_diff(ctx, staged: bool = False, path: str = "", review: bool = False,
                       workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            if review:
                result = await dev.review_diff(workspace, staged)
                return ok(result, result.get("review", "Nothing to review."))
            result = await dev.diff(workspace, path, staged)
        except LookupError as exc:
            return fail(str(exc))
        if not result.get("ok"):
            return fail(result.get("error", "Could not read the diff."))
        if result["empty"]:
            return ok(result, "No changes.")
        return ok(result, f"{len(result['files'])} file(s), +{result['lines_added']}/"
                          f"-{result['lines_removed']}.")

    @registry.add(
        "git_log",
        "Recent commits, optionally for one file.",
        {"type": "object", "properties": {
            "limit": {"type": "integer"}, "path": {"type": "string"},
            "workspace": {"type": "string"},
        }},
        permission="dev.workspace",
        category="developer",
    )
    async def git_log(ctx, limit: int = 15, path: str = "", workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            commits = await dev.log(workspace, limit, path)
        except LookupError as exc:
            return fail(str(exc))
        if not commits:
            return ok({"commits": []}, "No commits found.")
        return ok({"commits": commits}, f"{len(commits)} commit(s), latest: "
                                        f"{commits[0]['short']} {commits[0]['subject']}")

    @registry.add(
        "write_commit_message",
        "Generate a commit message from the actual staged diff, matching the repo's existing "
        "style. Does not commit.",
        {"type": "object", "properties": {
            "staged_only": {"type": "boolean"}, "workspace": {"type": "string"},
        }},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def write_commit_message(ctx, staged_only: bool = True, workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            result = await dev.commit_message(workspace, staged_only)
        except LookupError as exc:
            return fail(str(exc))
        if not result.get("ok"):
            return fail(result["message"])
        return ok(result, result["message"])

    @registry.add(
        "git_commit",
        "Stage files and make a commit. Local only — never pushes.",
        {"type": "object", "properties": {
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Files to stage; omit if already staged"},
            "workspace": {"type": "string"},
        }, "required": ["message"]},
        permission="dev.git_write",
        risk=Risk.HIGH,
        confirm_summary=lambda a: (
            f"Commit with this message?\n\n\"{truncate(a.get('message'), 300)}\"\n\n"
            + (f"Staging first: {', '.join(a.get('paths') or [])}\n" if a.get("paths") else "")
            + "This commits locally. Nothing will be pushed."
        ),
        category="developer",
        timeout=120.0,
    )
    async def git_commit(ctx, message: str, paths: Optional[List[str]] = None,
                         workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            if paths:
                staged = await dev.stage(paths, workspace)
                if not staged.get("ok"):
                    return fail(staged.get("error", "Staging failed."))
            result = await dev.commit(message, workspace)
        except LookupError as exc:
            return fail(str(exc))
        if not result.get("ok"):
            return fail(result.get("error") or result.get("message", "Commit failed."))
        return ok(result, f"Committed {result['sha']} ({result['files']} file(s)). "
                          f"{result['note']}")

    # ==================================================================
    # tests and builds
    # ==================================================================
    @registry.add(
        "run_tests",
        "Run the project's own test suite and report what passed and failed. Set analyse=true to "
        "also explain each failure against the real source.",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Only run tests matching this"},
            "analyse": {"type": "boolean"},
            "workspace": {"type": "string"},
        }},
        permission="dev.execute",
        risk=Risk.HIGH,
        confirm_summary=lambda a: (
            "Run this project's test suite? It executes the test and setup code defined in the "
            "project itself."
        ),
        category="developer",
        timeout=960.0,
    )
    async def run_tests(ctx, pattern: str = "", analyse: bool = False, workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            result = await (dev.analyse_failures(workspace) if analyse
                            else dev.run_tests(workspace, pattern))
        except LookupError as exc:
            return fail(str(exc))
        if "message" in result and not result.get("ok") and "passed" not in result:
            return fail(result["message"])

        headline = (f"{result['passed']} passed, {result['failed']} failed"
                    + (f", {result['skipped']} skipped" if result.get("skipped") else "") + ".")
        if result.get("failures"):
            headline += " Failing: " + ", ".join(f["name"] for f in result["failures"][:4])
        return ok(result, headline)

    @registry.add(
        "run_build",
        "Run the project's build and report any compiler errors with file and line.",
        {"type": "object", "properties": {"workspace": {"type": "string"}}},
        permission="dev.execute",
        risk=Risk.HIGH,
        confirm_summary=lambda a: "Run this project's build script?",
        category="developer",
        timeout=960.0,
    )
    async def run_build(ctx, workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            result = await dev.run_build(workspace)
        except LookupError as exc:
            return fail(str(exc))
        if not result.get("ok") and "message" in result:
            return fail(result["message"])
        return ok(result, "Build succeeded." if result["ok"]
                  else f"Build failed with {result['error_count']} error(s).")

    @registry.add(
        "debug_plan",
        "Produce a concrete debugging plan for a problem, grounded in the repository's real state.",
        {"type": "object", "properties": {
            "problem": {"type": "string"}, "workspace": {"type": "string"},
        }, "required": ["problem"]},
        permission="dev.workspace",
        category="developer",
        timeout=280.0,
    )
    async def debug_plan(ctx, problem: str, workspace: str = ""):
        dev = ctx.get("devtools")
        try:
            result = await dev.debug_plan(problem, workspace)
        except LookupError as exc:
            return fail(str(exc))
        return ok(result, result["plan"])

    @registry.add(
        "dev_timeline",
        "What has happened in development recently: commits, test runs, builds and reviews.",
        {"type": "object", "properties": {
            "hours": {"type": "integer"}, "workspace": {"type": "string"},
        }},
        permission="dev.workspace",
        category="developer",
    )
    async def dev_timeline(ctx, hours: int = 72, workspace: str = ""):
        dev = ctx.get("devtools")
        events = await dev.timeline(workspace, hours)
        if not events:
            return ok({"events": []}, "No development activity recorded.")
        return ok({"events": events}, f"{len(events)} event(s) in the last {hours}h.")

    @registry.add(
        "ci_status",
        "Check CI workflow runs for a GitHub repository.",
        {"type": "object", "properties": {
            "repo": {"type": "string", "description": "owner/name"},
        }, "required": ["repo"]},
        permission="github.read",
        requires_integration="github",
        category="developer",
        timeout=60.0,
    )
    async def ci_status(ctx, repo: str):
        dev = ctx.get("devtools")
        result = await dev.ci_status(repo)
        if not result.get("ok"):
            return fail(result["message"])
        latest = result.get("latest")
        return ok(result,
                  f"{repo}: {result['failing']} failing run(s)."
                  + (f" Latest: {latest['name']} — {latest.get('conclusion') or latest['status']}."
                     if latest else ""))
