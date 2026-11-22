"""
Sandboxed file management.

Every path is resolved and checked against the configured roots before anything
happens, so a prompt-injected "read C:\\Windows\\System32\\config\\SAM" fails at
the boundary rather than at the tool. Deletion moves to a recoverable trash
folder inside the data directory -- nothing is destroyed outright.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.util import humanize_duration, new_id, now_iso, truncate

logger = logging.getLogger("jarvis.files")

TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".html", ".css", ".csv", ".log", ".ini", ".cfg", ".toml", ".xml", ".sql",
    ".sh", ".bat", ".ps1", ".java", ".c", ".cpp", ".h", ".rs", ".go", ".rb", ".php",
}

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "AppData", ".cache",
    "$RECYCLE.BIN", "System Volume Information", ".next", "dist", "build",
}

MAX_READ_BYTES = 2_000_000


class PathNotAllowed(PermissionError):
    pass


class FileService:
    def __init__(self, settings):
        self.settings = settings
        self.roots = [Path(r).expanduser().resolve() for r in settings.FILE_ROOTS]
        self.trash = Path(settings.DATA_DIR) / "trash"
        self.trash.mkdir(parents=True, exist_ok=True)

    # ==================================================================
    def resolve(self, path: str) -> Path:
        """Resolve and confine. Raises PathNotAllowed outside the roots."""
        if not path:
            raise PathNotAllowed("No path given.")
        if not self.roots:
            # Belt and braces behind the config fix: say what is wrong rather
            # than dying on roots[0]. An IndexError here surfaced to the model
            # as "IndexError: list index out of range", which it dutifully
            # reported to the user and could do nothing with -- it has no way
            # to guess that the real problem is an empty line in a .env file.
            raise PathNotAllowed(
                "No file roots are configured, so I'm not allowed to touch any "
                "folder at all. Set JARVIS_FILE_ROOTS in .env to the folders I "
                "may use (it defaults to your home folder when left unset)."
            )

        candidate = Path(os.path.expandvars(str(path))).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathNotAllowed(f"Could not resolve '{path}': {exc}")

        for root in self.roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise PathNotAllowed(
            f"'{resolved}' is outside the folders I'm allowed to touch "
            f"({', '.join(str(r) for r in self.roots)}). Add it to JARVIS_FILE_ROOTS if that's intended."
        )

    def is_allowed(self, path: str) -> bool:
        try:
            self.resolve(path)
            return True
        except PathNotAllowed:
            return False

    # ==================================================================
    async def search(
        self,
        pattern: str,
        root: Optional[str] = None,
        limit: int = 40,
        content: bool = False,
        max_depth: int = 6,
    ) -> List[Dict[str, Any]]:
        """Filename (and optionally content) search. Runs in a thread -- a deep
        walk must never block the event loop."""
        base = self.resolve(root) if root else self.roots[0]

        def _walk() -> List[Dict[str, Any]]:
            needle = pattern.lower()
            hits: List[Dict[str, Any]] = []
            base_depth = len(base.parts)

            for current, dirnames, filenames in os.walk(base):
                current_path = Path(current)
                if len(current_path.parts) - base_depth >= max_depth:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

                for filename in filenames:
                    if len(hits) >= limit:
                        return hits
                    full = current_path / filename
                    matched = needle in filename.lower()
                    snippet = ""

                    if not matched and content and full.suffix.lower() in TEXT_SUFFIXES:
                        try:
                            if full.stat().st_size <= 1_000_000:
                                text = full.read_text(encoding="utf-8", errors="ignore")
                                index = text.lower().find(needle)
                                if index >= 0:
                                    matched = True
                                    snippet = text[max(0, index - 80): index + 160].replace("\n", " ")
                        except OSError:
                            continue

                    if matched:
                        try:
                            stat = full.stat()
                            hits.append(
                                {
                                    "path": str(full),
                                    "name": filename,
                                    "size": stat.st_size,
                                    "size_human": _human_size(stat.st_size),
                                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                    "suffix": full.suffix.lower(),
                                    "snippet": snippet,
                                }
                            )
                        except OSError:
                            continue
            return hits

        return await asyncio.get_event_loop().run_in_executor(None, _walk)

    # ==================================================================
    async def read(self, path: str, max_chars: int = 20000) -> Dict[str, Any]:
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"'{target}' does not exist.")
        if target.is_dir():
            return await self.list_dir(str(target))

        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return {
                "path": str(target),
                "too_large": True,
                "size": size,
                "message": f"File is {_human_size(size)} -- too large to read in full.",
            }

        suffix = target.suffix.lower()
        if suffix == ".pdf":
            return await self._read_pdf(target, max_chars)
        if suffix == ".docx":
            return await self._read_docx(target, max_chars)
        if suffix in (".xlsx", ".xlsm"):
            return await self._read_xlsx(target, max_chars)

        def _read() -> str:
            return target.read_text(encoding="utf-8", errors="replace")

        try:
            text = await asyncio.get_event_loop().run_in_executor(None, _read)
        except OSError as exc:
            raise OSError(f"Could not read '{target}': {exc}")

        return {
            "path": str(target),
            "size": size,
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
            "lines": text.count("\n") + 1,
        }

    async def _read_pdf(self, target: Path, max_chars: int) -> Dict[str, Any]:
        def _extract() -> Dict[str, Any]:
            try:
                from pypdf import PdfReader
            except ImportError:
                try:
                    from PyPDF2 import PdfReader  # type: ignore
                except ImportError:
                    return {
                        "path": str(target),
                        "text": "",
                        "message": "PDF text extraction needs the optional 'pypdf' package "
                                   "(pip install pypdf).",
                    }
            reader = PdfReader(str(target))
            pages = [page.extract_text() or "" for page in reader.pages[:60]]
            return {"path": str(target), "pages": len(reader.pages), "text": "\n\n".join(pages)[:max_chars]}

        return await asyncio.get_event_loop().run_in_executor(None, _extract)

    async def _read_docx(self, target: Path, max_chars: int) -> Dict[str, Any]:
        def _extract() -> Dict[str, Any]:
            # .docx is a zip of XML -- readable with the standard library.
            import re
            import zipfile

            try:
                with zipfile.ZipFile(target) as archive:
                    xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
            except (KeyError, zipfile.BadZipFile, OSError) as exc:
                return {"path": str(target), "text": "", "message": f"Could not read the document: {exc}"}
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
            return {"path": str(target), "text": text[:max_chars]}

        return await asyncio.get_event_loop().run_in_executor(None, _extract)

    async def _read_xlsx(self, target: Path, max_chars: int) -> Dict[str, Any]:
        def _extract() -> Dict[str, Any]:
            try:
                import openpyxl
            except ImportError:
                return {
                    "path": str(target),
                    "text": "",
                    "message": "Spreadsheet reading needs the optional 'openpyxl' package "
                               "(pip install openpyxl).",
                }
            workbook = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
            chunks = []
            for sheet in workbook.worksheets[:5]:
                chunks.append(f"--- {sheet.title} ---")
                for row in sheet.iter_rows(max_row=200, values_only=True):
                    chunks.append("\t".join("" if c is None else str(c) for c in row))
            workbook.close()
            return {"path": str(target), "text": "\n".join(chunks)[:max_chars]}

        return await asyncio.get_event_loop().run_in_executor(None, _extract)

    # ==================================================================
    async def list_dir(self, path: str, limit: int = 300) -> Dict[str, Any]:
        target = self.resolve(path)
        if not target.is_dir():
            raise NotADirectoryError(f"'{target}' is not a folder.")

        def _list() -> Dict[str, Any]:
            entries = []
            for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:limit]:
                try:
                    stat = entry.stat()
                    entries.append(
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "is_dir": entry.is_dir(),
                            "size": stat.st_size if entry.is_file() else None,
                            "size_human": _human_size(stat.st_size) if entry.is_file() else "",
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        }
                    )
                except OSError:
                    continue
            return {"path": str(target), "entries": entries, "count": len(entries)}

        return await asyncio.get_event_loop().run_in_executor(None, _list)

    # ==================================================================
    async def write(self, path: str, content: str, append: bool = False) -> Dict[str, Any]:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        backup = None
        if target.exists() and not append:
            backup = self.trash / f"{target.stem}_{new_id()}{target.suffix}"
            shutil.copy2(target, backup)

        def _write() -> int:
            with open(target, "a" if append else "w", encoding="utf-8") as handle:
                return handle.write(content)

        written = await asyncio.get_event_loop().run_in_executor(None, _write)
        return {
            "path": str(target),
            "bytes_written": written,
            "appended": append,
            "previous_version": str(backup) if backup else None,
        }

    async def mkdir(self, path: str) -> Dict[str, Any]:
        target = self.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": str(target), "created": True}

    async def move(self, source: str, destination: str) -> Dict[str, Any]:
        source_path = self.resolve(source)
        destination_path = self.resolve(destination)
        if not source_path.exists():
            raise FileNotFoundError(f"'{source_path}' does not exist.")
        if destination_path.is_dir():
            destination_path = destination_path / source_path.name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            raise FileExistsError(f"'{destination_path}' already exists.")
        shutil.move(str(source_path), str(destination_path))
        return {"from": str(source_path), "to": str(destination_path)}

    async def copy(self, source: str, destination: str) -> Dict[str, Any]:
        source_path = self.resolve(source)
        destination_path = self.resolve(destination)
        if destination_path.is_dir():
            destination_path = destination_path / source_path.name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(str(source_path), str(destination_path))
        else:
            shutil.copy2(str(source_path), str(destination_path))
        return {"from": str(source_path), "to": str(destination_path)}

    async def rename(self, path: str, new_name: str) -> Dict[str, Any]:
        target = self.resolve(path)
        if "/" in new_name or "\\" in new_name:
            raise ValueError("New name must not contain a path separator.")
        destination = target.parent / new_name
        if destination.exists():
            raise FileExistsError(f"'{destination}' already exists.")
        target.rename(destination)
        return {"from": str(target), "to": str(destination)}

    async def delete(self, path: str) -> Dict[str, Any]:
        """Move to JARVIS's own trash rather than unlinking, so 'delete' is
        always recoverable."""
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"'{target}' does not exist.")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = self.trash / f"{stamp}_{target.name}"
        counter = 1
        while destination.exists():
            destination = self.trash / f"{stamp}_{counter}_{target.name}"
            counter += 1
        shutil.move(str(target), str(destination))

        return {
            "deleted": str(target),
            "recoverable_at": str(destination),
            "message": f"Moved to JARVIS trash. Recover it from {destination} if that was wrong.",
        }

    async def restore(self, trash_path: str) -> Dict[str, Any]:
        source = Path(trash_path).resolve()
        source.relative_to(self.trash.resolve())  # raises ValueError if outside trash
        original_name = source.name.split("_", 2)[-1]
        destination = self.roots[0] / original_name
        shutil.move(str(source), str(destination))
        return {"restored_to": str(destination)}

    async def trash_contents(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": str(p),
                "name": p.name,
                "size_human": _human_size(p.stat().st_size) if p.is_file() else "folder",
                "deleted_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            }
            for p in sorted(self.trash.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        ]

    async def empty_trash(self) -> int:
        count = 0
        for entry in self.trash.iterdir():
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                count += 1
            except OSError:
                continue
        return count

    # ==================================================================
    async def find_duplicates(self, root: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """Size-then-hash: only files sharing a size get hashed, which keeps a
        big folder scan cheap."""
        import hashlib
        from collections import defaultdict

        base = self.resolve(root) if root else self.roots[0]

        def _scan() -> List[Dict[str, Any]]:
            by_size: Dict[int, List[Path]] = defaultdict(list)
            scanned = 0
            for current, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for filename in filenames:
                    if scanned >= limit * 20:
                        break
                    full = Path(current) / filename
                    try:
                        size = full.stat().st_size
                    except OSError:
                        continue
                    if size > 0:
                        by_size[size].append(full)
                        scanned += 1

            groups = []
            for size, paths in by_size.items():
                if len(paths) < 2:
                    continue
                by_hash: Dict[str, List[str]] = defaultdict(list)
                for path in paths[:20]:
                    try:
                        digest = hashlib.sha256()
                        with open(path, "rb") as handle:
                            while chunk := handle.read(262144):
                                digest.update(chunk)
                        by_hash[digest.hexdigest()].append(str(path))
                    except OSError:
                        continue
                for digest, duplicates in by_hash.items():
                    if len(duplicates) > 1:
                        groups.append(
                            {"size": size, "size_human": _human_size(size), "count": len(duplicates),
                             "paths": duplicates}
                        )
            groups.sort(key=lambda g: g["size"] * g["count"], reverse=True)
            return groups[:50]

        return await asyncio.get_event_loop().run_in_executor(None, _scan)

    def roots_info(self) -> List[Dict[str, Any]]:
        return [{"path": str(r), "exists": r.exists()} for r in self.roots]


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
