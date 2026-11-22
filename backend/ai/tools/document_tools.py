"""Searching the owner's own documents, and choosing which folders count."""
from __future__ import annotations

from ...core.permissions import Risk
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "search_documents",
        "Search the owner's own documents -- notes, PDFs, Word files and text in the "
        "folders they added to JARVIS's document index -- and return the passages "
        "that answer the question, with the file each came from. Use for questions "
        "about their files, notes, manuals, contracts or anything they have written down.",
        {"type": "object", "properties": {
            "question": {"type": "string", "description": "What to look for"},
            "limit": {"type": "integer", "description": "How many passages, default 5"},
        }, "required": ["question"]},
        permission="files.read",
        category="documents files notes search",
    )
    async def search_documents(ctx, question: str, limit: int = 5):
        index = ctx.get("documents")
        if index is None:
            return fail("The document index is not available.")
        stats = await index.stats()
        if stats["passages"] == 0:
            return fail("No documents are indexed yet. The owner can add folders under "
                        "Documents in JARVIS's menu, or ask you to index one.")
        hits = await index.search(question, limit=max(1, min(int(limit or 5), 10)))
        if not hits:
            return ok({"passages": []},
                      "Nothing in the indexed documents matched. Say so plainly rather "
                      "than answering from general knowledge as if it came from their files.")
        passages = [{"file": h["file"], "section": h["section"], "path": h["path"],
                     "text": h["passage"]} for h in hits]
        return ok({"passages": passages},
                  f"{len(passages)} passage(s) from the owner's documents. Answer from these "
                  "only, and name the file (and section, if given) for each thing you say.")

    @registry.add(
        "index_documents_folder",
        "Add a folder to JARVIS's document index so its notes, PDFs and documents can "
        "be searched. Use when the owner asks you to read, learn or index a folder.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "The folder"},
        }, "required": ["path"]},
        permission="files.read",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Index every readable document in {a.get('path')}?",
        category="documents files index",
    )
    async def index_documents_folder(ctx, path: str):
        index = ctx.get("documents")
        if index is None:
            return fail("The document index is not available.")
        try:
            folder = await index.add_folder(path)
        except Exception as exc:
            return fail(str(exc))
        return ok(folder, f"Indexing {folder.get('path')} now. It runs in the background; "
                          "searching works as soon as the first files are read.")
