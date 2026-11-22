"""Self-modification tools.

Proposal and application are deliberately separate tools. The model can propose
freely; applying is a distinct, confirmed, critical-risk action.
"""
from __future__ import annotations

from typing import Any, Dict

from ...core.permissions import Risk
from ...core.util import truncate
from .registry import ToolRegistry, fail, ok


def _apply_confirm(arguments: Dict[str, Any]) -> str:
    line = (
        "Write this change to JARVIS's own source code?\n\n"
        f"Proposal: {arguments.get('proposal_id')}"
    )
    if arguments.get("allow_protected"):
        line += (
            "\n\nWARNING: this edits a file that implements JARVIS's own safety controls "
            "(permissions, authentication, confirmations or auditing). Read the diff carefully "
            "— approving this can weaken the protections that gate every other action."
        )
    line += "\n\nA backup is taken first and the change can be rolled back."
    return line


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "source_files",
        "List JARVIS's own source files that can be edited, with sizes and which are protected. "
        "Use before proposing a change so the path is real.",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Filter, e.g. 'orchestrator' or 'gl/'"},
        }},
        permission="code.self_modify",
        category="selfedit",
    )
    async def source_files(ctx, pattern: str = ""):
        service = ctx.get("selfedit")
        if service is None:
            return fail("Self-edit service unavailable.")
        files = service.list_files(pattern)
        if not files:
            return ok({"files": []}, f"No editable source files match '{pattern}'.")
        return ok({"count": len(files), "files": files[:80], "stats": service.stats()},
                  f"{len(files)} editable file(s).")

    @registry.add(
        "read_source",
        "Read one of JARVIS's own source files.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Project-relative, e.g. "
                                                      "'backend/ai/orchestrator.py'"},
        }, "required": ["path"]},
        permission="code.self_modify",
        category="selfedit",
    )
    async def read_source(ctx, path: str):
        service = ctx.get("selfedit")
        try:
            record = service.read(path)
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            return fail(str(exc))
        return ok(record, f"{record['path']} — {record['lines']} lines"
                          + (" (PROTECTED)" if record["protected"] else ""))

    @registry.add(
        "propose_code_change",
        "Ask the dedicated code model to rewrite one of JARVIS's own source files. Returns a diff "
        "and a syntax check. Writes NOTHING — use apply_code_change to actually make the change.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Project-relative source path"},
            "request": {"type": "string",
                        "description": "What to change, precisely. Be specific about behaviour."},
        }, "required": ["path", "request"]},
        permission="code.self_modify",
        category="selfedit",
        timeout=300.0,
    )
    async def propose_code_change(ctx, path: str, request: str):
        service = ctx.get("selfedit")
        if service is None:
            return fail("Self-edit service unavailable.")
        try:
            result = await service.propose(path, request)
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            return fail(str(exc))
        except Exception as exc:
            return fail(f"The code model failed: {exc}")

        if not result.get("ok"):
            return fail(result["message"])

        validation = result["validation"]
        headline = (
            f"Proposed {result['lines_added']} added / {result['lines_removed']} removed in "
            f"{result['path']} using {result['model']['model']}. "
            f"Syntax: {'valid' if validation['valid'] else 'INVALID — ' + str(validation.get('error'))}."
        )
        if result["protected"]:
            headline += " This file is PROTECTED (it implements safety controls)."
        headline += f" Nothing written yet; proposal id {result['proposal_id']}."

        return ok({k: v for k, v in result.items() if k != "diff"} |
                  {"diff": truncate(result["diff"], 6000)}, headline)

    @registry.add(
        "apply_code_change",
        "Write a previously proposed change to disk. Backs up first, refuses invalid syntax, and "
        "can be rolled back. Always asks for confirmation.",
        {"type": "object", "properties": {
            "proposal_id": {"type": "string"},
            "allow_protected": {"type": "boolean",
                                "description": "Required only for files implementing JARVIS's "
                                               "own safety controls"},
        }, "required": ["proposal_id"]},
        permission="code.self_modify",
        risk=Risk.CRITICAL,
        confirm_summary=_apply_confirm,
        category="selfedit",
        timeout=180.0,
    )
    async def apply_code_change(ctx, proposal_id: str, allow_protected: bool = False):
        service = ctx.get("selfedit")
        try:
            result = await service.apply(proposal_id, allow_protected)
        except KeyError as exc:
            return fail(str(exc))
        except PermissionError as exc:
            return fail(str(exc))
        if not result.get("ok"):
            return fail(result["message"])
        return ok(result, result["message"])

    @registry.add(
        "rollback_code_change",
        "Restore one of JARVIS's source files from a backup taken before an edit.",
        {"type": "object", "properties": {
            "backup": {"type": "string", "description": "Backup filename; omit to list them"},
            "path": {"type": "string", "description": "Filter the listing to one file"},
        }},
        permission="code.self_modify",
        risk=Risk.HIGH,
        confirm_summary=lambda a: (f"Restore source from backup {a.get('backup')}?"
                                   if a.get("backup") else "List available source backups?"),
        category="selfedit",
    )
    async def rollback_code_change(ctx, backup: str = "", path: str = ""):
        service = ctx.get("selfedit")
        if not backup:
            backups = service.backups(path or None)
            if not backups:
                return ok({"backups": []}, "No source backups exist yet.")
            return ok({"backups": backups[:30]},
                      f"{len(backups)} backup(s). Pass one as 'backup' to restore it.")
        try:
            result = await service.rollback(backup)
        except (PermissionError, FileNotFoundError) as exc:
            return fail(str(exc))
        return ok(result, f"Restored {result['path']} from {backup}."
                          + (" Restart the backend to load it."
                             if result["restart_required"] else ""))
