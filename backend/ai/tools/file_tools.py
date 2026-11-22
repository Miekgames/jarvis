"""File management tools. All paths are confined to the configured roots."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import truncate
from ...services.files import PathNotAllowed
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    def files(ctx):
        service = ctx.get("files")
        if service is None:
            raise RuntimeError("File service unavailable.")
        return service

    # ==================================================================
    @registry.add(
        "find_files",
        "Search for files by name, and optionally by content. Use this for 'find my maths homework', "
        "'where is that invoice', 'which file mentions the API key rotation'.",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Text to match in the filename (or content)"},
            "root": {"type": "string", "description": "Folder to search from; defaults to the home folder"},
            "search_content": {"type": "boolean", "description": "Also search inside text files (slower)"},
            "limit": {"type": "integer"},
        }, "required": ["pattern"]},
        permission="files.read",
        category="files",
        timeout=90.0,
    )
    async def find_files(ctx, pattern: str, root: str = "", search_content: bool = False, limit: int = 25):
        try:
            hits = await files(ctx).search(pattern, root or None, min(limit, 60), search_content)
        except PathNotAllowed as exc:
            return fail(str(exc))
        if not hits:
            return ok({"matches": []}, f"No files matched '{pattern}'.")
        return ok(
            {"count": len(hits), "matches": [
                {k: v for k, v in h.items() if k in ("path", "name", "size_human", "modified", "snippet")}
                for h in hits
            ]},
            f"Found {len(hits)} match(es).",
        )

    # ==================================================================
    @registry.add(
        "read_file",
        "Read a file's contents. Handles text, code, PDF, Word and Excel files. Also lists a folder if "
        "given a folder path.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Truncate long files (default 20000)"},
        }, "required": ["path"]},
        permission="files.read",
        category="files",
    )
    async def read_file(ctx, path: str, max_chars: int = 20000):
        try:
            result = await files(ctx).read(path, min(max_chars, 60000))
        except (PathNotAllowed, FileNotFoundError, OSError) as exc:
            return fail(str(exc))
        return ok(result)

    @registry.add(
        "list_folder",
        "List the contents of a folder.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        permission="files.read",
        category="files",
    )
    async def list_folder(ctx, path: str):
        try:
            return ok(await files(ctx).list_dir(path))
        except (PathNotAllowed, NotADirectoryError, OSError) as exc:
            return fail(str(exc))

    # ==================================================================
    @registry.add(
        "write_file",
        "Create a file or overwrite an existing one. The previous version is kept in JARVIS's trash.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean", "description": "Append instead of overwriting"},
        }, "required": ["path", "content"]},
        permission="files.write",
        category="files",
    )
    async def write_file(ctx, path: str, content: str, append: bool = False):
        try:
            result = await files(ctx).write(path, content, append)
        except (PathNotAllowed, OSError) as exc:
            return fail(str(exc))
        return ok(result, f"Wrote {result['bytes_written']} characters to {result['path']}.")

    @registry.add(
        "create_folder",
        "Create a folder (including any missing parents).",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        permission="files.write",
        category="files",
    )
    async def create_folder(ctx, path: str):
        try:
            return ok(await files(ctx).mkdir(path), "Folder created.")
        except (PathNotAllowed, OSError) as exc:
            return fail(str(exc))

    @registry.add(
        "move_file",
        "Move or rename a file or folder.",
        {"type": "object", "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
        }, "required": ["source", "destination"]},
        permission="files.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Move '{a.get('source')}' to '{a.get('destination')}'?",
        category="files",
    )
    async def move_file(ctx, source: str, destination: str):
        try:
            result = await files(ctx).move(source, destination)
        except (PathNotAllowed, FileNotFoundError, FileExistsError, OSError) as exc:
            return fail(str(exc))
        return ok(result, f"Moved to {result['to']}.")

    @registry.add(
        "copy_file",
        "Copy a file or folder.",
        {"type": "object", "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
        }, "required": ["source", "destination"]},
        permission="files.write",
        category="files",
    )
    async def copy_file(ctx, source: str, destination: str):
        try:
            result = await files(ctx).copy(source, destination)
        except (PathNotAllowed, FileNotFoundError, OSError) as exc:
            return fail(str(exc))
        return ok(result, f"Copied to {result['to']}.")

    # ==================================================================
    @registry.add(
        "delete_file",
        "Delete a file or folder. It is moved to JARVIS's recoverable trash, not erased. Always "
        "asks for confirmation.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        permission="files.delete",
        risk=Risk.HIGH,
        confirm_summary=lambda a: (
            f"Delete '{a.get('path')}'? It will be moved to JARVIS's trash folder inside the data "
            f"directory, so it can be recovered -- but it will disappear from where it is now."
        ),
        category="files",
    )
    async def delete_file(ctx, path: str):
        try:
            result = await files(ctx).delete(path)
        except (PathNotAllowed, FileNotFoundError, OSError) as exc:
            return fail(str(exc))
        return ok(result, result["message"])

    @registry.add(
        "find_duplicate_files",
        "Find duplicate files (same size and content hash) under a folder.",
        {"type": "object", "properties": {"root": {"type": "string"}}},
        permission="files.read",
        category="files",
        timeout=180.0,
    )
    async def find_duplicate_files(ctx, root: str = ""):
        try:
            groups = await files(ctx).find_duplicates(root or None)
        except PathNotAllowed as exc:
            return fail(str(exc))
        wasted = sum(g["size"] * (g["count"] - 1) for g in groups)
        return ok(
            {"groups": groups[:20], "group_count": len(groups), "reclaimable_bytes": wasted},
            f"{len(groups)} duplicate group(s); about {wasted / 1_048_576:.0f} MB reclaimable.",
        )

    # ==================================================================
    @registry.add(
        "summarise_document",
        "Read a document (PDF, Word, text, spreadsheet) and answer a question about it or summarise it.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "question": {"type": "string", "description": "Optional question; omit for a summary"},
        }, "required": ["path"]},
        permission="files.read",
        category="files",
        timeout=180.0,
    )
    async def summarise_document(ctx, path: str, question: str = ""):
        try:
            document = await files(ctx).read(path, max_chars=60000)
        except (PathNotAllowed, FileNotFoundError, OSError) as exc:
            return fail(str(exc))

        text = document.get("text", "")
        if not text:
            return fail(document.get("message") or "No readable text found in that file.")

        orchestrator = ctx.get("orchestrator")
        prompt = (
            f"Answer this question using only the document below. If it doesn't contain the answer, "
            f"say so.\n\nQUESTION: {question}\n\nDOCUMENT ({path}):\n{text}"
            if question
            else f"Summarise this document. Cover what it is, the key points, and anything that looks "
                 f"like an action or deadline.\n\nDOCUMENT ({path}):\n{text}"
        )
        answer = await orchestrator.ask(prompt, purpose="conversation", max_tokens=2000)
        return ok({"path": path, "answer": answer, "truncated": document.get("truncated", False)}, answer)

    # ==================================================================
    @registry.add(
        "trash",
        "Inspect or restore items in JARVIS's recoverable trash.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "restore"]},
            "path": {"type": "string", "description": "Trash path to restore"},
        }, "required": ["action"]},
        permission="files.read",
        category="files",
    )
    async def trash(ctx, action: str, path: str = ""):
        service = files(ctx)
        if action == "list":
            items = await service.trash_contents()
            return ok({"items": items[:60], "count": len(items)})
        if not path:
            return fail("Restoring needs the trash path (get it from action='list').")
        try:
            return ok(await service.restore(path), "Restored.")
        except (ValueError, OSError) as exc:
            return fail(str(exc))
