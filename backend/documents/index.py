"""The owner's documents, searchable: folders they choose, read and indexed.

The owner adds a folder; everything readable in it -- notes, markdown, text,
PDFs, Word documents, code -- is cut into passages (see `chunker`) and put
in a full-text index. JARVIS's `search_documents` tool then finds the
passages that answer a question, and the model answers from them, naming
the file each part came from.

Search is SQLite FTS5 with BM25 ranking and Porter stemming: no embedding
model, nothing to download, nothing leaves the machine, and it is fast on
tens of thousands of passages. It matches words, not meaning -- "car" does
not find "vehicle" -- which is the honest trade for running on this machine
with the local model's embeddings switched off.

Indexing is incremental: a file is read again only when its size or time
changed, and files that disappeared are dropped. Folders must be inside the
locations JARVIS is allowed to read; what is read is treated as untrusted
text like any other tool result (screened before the model sees it).

Ported idea: OpenJarvis's connectors -> chunker -> store -> retrieval
pipeline for its Deep Research agent (Stanford Hazy Research, Apache-2.0).
The storage and search here are JARVIS's own.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.util import now_iso
from .chunker import chunk

logger = logging.getLogger("jarvis.documents")

TEXT_TYPES = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json", ".yaml", ".yml",
              ".toml", ".ini", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go",
              ".rs", ".c", ".cpp", ".h", ".sql", ".sh", ".ps1", ".tex", ".org"}
PAGE_TYPES = {".html", ".htm"}
SUPPORTED = TEXT_TYPES | PAGE_TYPES | {".pdf", ".docx"}

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_FOLDER = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".idea", ".vscode", "$recycle.bin", "appdata"}


def read_text(path: Path) -> str:
    """The words in a file, or "" if there are none we can read."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"## Page {number}\n\n{text}")
        return "\n\n".join(pages)
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        lines = []
        for paragraph in document.paragraphs:
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
            text = paragraph.text.strip()
            if not text:
                lines.append("")
            elif style.startswith("heading"):
                lines.append(f"\n## {text}\n")
            else:
                lines.append(text)
        for table in document.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(lines)
    raw = path.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    if suffix in PAGE_TYPES:
        from ..services.web import _TextExtractor

        parser = _TextExtractor()
        try:
            parser.feed(raw)
        except Exception:
            pass
        return parser.text()
    return raw


def _query(text: str) -> str:
    """A safe FTS5 query: each word quoted (so punctuation cannot inject
    query syntax), any of them may match, best matches ranked first."""
    words = [w for w in re.findall(r"[\w'-]+", (text or "").lower()) if len(w) > 1]
    stop = {"the", "and", "for", "what", "how", "did", "does", "was", "are", "who", "when",
            "where", "which", "about", "from", "with", "that", "this", "have", "has", "my",
            "me", "in", "on", "of", "to", "is", "it", "do", "an", "or", "be", "say", "says",
            "tell", "find", "documents", "document", "files", "file", "notes"}
    terms = [w for w in words if w not in stop][:12] or words[:6]
    return " OR ".join('"' + w.replace('"', "") + '"' for w in terms)


_STOP_TERMS = {"the", "and", "for", "what", "how", "did", "does", "was", "are", "who",
               "when", "where", "which", "about", "from", "with", "that", "this", "have",
               "has", "my", "me", "is", "it", "do", "an", "or", "be", "you", "your", "can",
               "next", "last", "one", "get", "got", "any", "will", "there", "their"}


def _terms(text: str) -> set:
    """Words that carry meaning, crudely stemmed ("services" -> "servic")."""
    return {w[:6] for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP_TERMS}


class DocumentIndex:
    def __init__(self, db, files=None, activity=None) -> None:
        self.db = db
        self.files = files
        self.activity = activity
        self._running: Dict[str, asyncio.Task] = {}

    # -- folders ------------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        if self.files is not None:
            return Path(self.files.resolve(path))
        return Path(path).expanduser().resolve()

    async def add_folder(self, path: str) -> Dict[str, Any]:
        folder = self._resolve(path)
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a folder.")
        key = str(folder)
        await self.db.execute(
            "INSERT OR IGNORE INTO doc_folders (path, added_at, status) VALUES (?,?,?)",
            (key, now_iso(), "waiting"))
        self.index_later(key)
        return await self.folder(key)

    async def remove_folder(self, path: str) -> bool:
        row = await self.db.fetch_one("SELECT path FROM doc_folders WHERE path = ?", (path,))
        if not row:
            return False
        task = self._running.pop(path, None)
        if task is not None:
            task.cancel()
        files = await self.db.fetch_all("SELECT path FROM doc_files WHERE folder = ?", (path,))
        for file in files:
            await self.db.execute("DELETE FROM doc_passages WHERE path = ?", (file["path"],))
        await self.db.execute("DELETE FROM doc_files WHERE folder = ?", (path,))
        await self.db.execute("DELETE FROM doc_folders WHERE path = ?", (path,))
        return True

    async def folder(self, path: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM doc_folders WHERE path = ?", (path,)) or {}
        if row:
            row["running"] = path in self._running
        return row

    async def folders(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM doc_folders ORDER BY added_at")
        for row in rows:
            row["running"] = row["path"] in self._running
        return rows

    # -- indexing -------------------------------------------------------------
    def index_later(self, path: str) -> None:
        if path in self._running:
            return
        task = asyncio.create_task(self.index_folder(path))
        self._running[path] = task
        task.add_done_callback(lambda _t, p=path: self._running.pop(p, None))

    async def refresh_all(self) -> None:
        for row in await self.folders():
            self.index_later(row["path"])

    async def index_folder(self, path: str) -> Dict[str, Any]:
        folder = Path(path)
        await self.db.execute("UPDATE doc_folders SET status = 'indexing', error = ''"
                              " WHERE path = ?", (path,))
        try:
            found = await asyncio.to_thread(self._walk, folder)
            known = {row["path"]: row for row in await self.db.fetch_all(
                "SELECT path, mtime, size FROM doc_files WHERE folder = ?", (path,))}

            for gone in set(known) - {str(p) for p, _, _ in found}:
                await self.db.execute("DELETE FROM doc_passages WHERE path = ?", (gone,))
                await self.db.execute("DELETE FROM doc_files WHERE path = ?", (gone,))

            changed = [(p, m, s) for p, m, s in found
                       if str(p) not in known or known[str(p)]["mtime"] != m
                       or known[str(p)]["size"] != s]
            for file, mtime, size in changed:
                await self._index_file(path, file, mtime, size)
                await asyncio.sleep(0)               # stay responsive

            totals = await self.db.fetch_one(
                "SELECT COUNT(*) AS files, COALESCE(SUM(passages), 0) AS passages"
                " FROM doc_files WHERE folder = ?", (path,)) or {}
            await self.db.execute(
                "UPDATE doc_folders SET status = 'ready', indexed_at = ?, files = ?, passages = ?"
                " WHERE path = ?", (now_iso(), totals.get("files", 0),
                                    totals.get("passages", 0), path))
            if changed:
                logger.info("indexed %s: %d file(s) read, %s passages in total",
                            path, len(changed), totals.get("passages"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("indexing %s failed: %s", path, exc)
            await self.db.execute("UPDATE doc_folders SET status = 'error', error = ?"
                                  " WHERE path = ?", (str(exc)[:300], path))
        return await self.folder(path)

    def _walk(self, folder: Path) -> List[tuple]:
        found = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                path = Path(root) / name
                if path.suffix.lower() not in SUPPORTED or name.startswith(("~$", ".")):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size == 0 or stat.st_size > MAX_FILE_BYTES:
                    continue
                found.append((path, stat.st_mtime, stat.st_size))
                if len(found) >= MAX_FILES_PER_FOLDER:
                    return found
        return found

    async def _index_file(self, folder: str, file: Path, mtime: float, size: int) -> None:
        key = str(file)
        await self.db.execute("DELETE FROM doc_passages WHERE path = ?", (key,))
        error, count = "", 0
        try:
            text = await asyncio.to_thread(read_text, file)
            for passage in chunk(text):
                await self.db.execute(
                    "INSERT INTO doc_passages (text, heading, path, position) VALUES (?,?,?,?)",
                    (passage.text, passage.heading, key, passage.position))
                count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        await self.db.execute(
            "INSERT INTO doc_files (path, folder, mtime, size, passages, indexed_at, error)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime,"
            " size = excluded.size, passages = excluded.passages,"
            " indexed_at = excluded.indexed_at, error = excluded.error",
            (key, folder, mtime, size, count, now_iso(), error))

    # -- searching --------------------------------------------------------------
    async def search(self, question: str, limit: int = 6) -> List[Dict[str, Any]]:
        query = _query(question)
        if not query:
            return []
        rows = await self.db.fetch_all(
            "SELECT path, heading, position, text,"
            " snippet(doc_passages, 0, '[', ']', ' … ', 24) AS snippet,"
            " bm25(doc_passages) AS score"
            " FROM doc_passages WHERE doc_passages MATCH ? ORDER BY score LIMIT ?",
            (query, max(1, min(limit, 20))))
        return [{
            "file": Path(row["path"]).name,
            "path": row["path"],
            "section": row["heading"] or "",
            "passage": row["text"],
            "snippet": row["snippet"],
            "score": round(-float(row["score"]), 3),
        } for row in rows]

    async def context_for(self, question: str, limit: int = 3) -> str:
        """Passages that plainly bear on a message, ready for the prompt.

        Run before every reply, so the model has the owner's own notes in
        front of it without having to think of searching -- which a small
        local model asked "when is my boiler due a service" does not.

        Only clear matches are used: a passage must share at least two of
        the message's words, or most of them for a short message. Anything
        weaker is noise in every prompt. The text is the owner's files but
        still outside text, so it is screened like a tool result.
        """
        try:
            hits = await self.search(question, limit=limit * 2)
        except Exception:
            return ""
        wanted = _terms(question)
        if not wanted or not hits:
            return ""
        chosen = []
        for hit in hits:
            # The file's name and the section heading are part of what a
            # passage is about: "Warranty" in boiler.md is the boiler's.
            about = f"{Path(hit['file']).stem} {hit['section']} {hit['passage']}"
            shared = wanted & _terms(about)
            if len(shared) >= 2 or (len(wanted) <= 2 and len(shared) == len(wanted)):
                chosen.append(hit)
            if len(chosen) >= limit:
                break
        if not chosen:
            return ""
        from ..core.screening import guard_for_model, screener

        lines = []
        for hit in chosen:
            where = hit["file"] + (f", section \"{hit['section']}\"" if hit["section"] else "")
            lines.append(f"[{where}]\n{hit['passage'][:1200]}")
        cleaned, screen = screener.screen("\n\n".join(lines))
        return ("Passages from the owner's own documents that may answer this. If they do, "
                "answer from them and name the file; if not, ignore them:\n\n"
                + guard_for_model(cleaned, "the owner's documents", screen))

    async def stats(self) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS files, COALESCE(SUM(passages), 0) AS passages,"
            " SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS unreadable FROM doc_files") or {}
        return {"files": int(row.get("files") or 0), "passages": int(row.get("passages") or 0),
                "unreadable": int(row.get("unreadable") or 0)}
