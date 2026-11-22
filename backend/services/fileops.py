"""
Bulk file operations: duplicates, disk usage, bulk rename, tags, content search.

Everything here is confined to the same authorised roots as the rest of the file
system (`JARVIS_FILE_ROOTS`), checked by resolving the path and comparing against
each root -- so a symlink or `..` cannot walk out.

Anything destructive is a two-step: you get a plan you can read, and nothing is
written until you apply it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("services.fileops")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", ".cache", "AppData", "$RECYCLE.BIN", "System Volume Information"}
HASH_CHUNK = 1 << 20
MAX_SCAN_FILES = 200_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return "{:.1f} {}".format(size, unit) if unit != "B" else "{:.0f} B".format(size)
        size /= 1024.0
    return "{:.1f} TB".format(size)


def quick_signature(path: Path, sample: int = 65536) -> str:
    """Hash the head and tail plus the size.

    Full hashing of a large tree is slow, and two files that share size, first
    64 KB and last 64 KB are duplicates for every practical purpose here. Files
    that match are hashed in full before anything is reported.
    """
    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(sample))
        if size > sample * 2:
            handle.seek(-sample, os.SEEK_END)
            digest.update(handle.read(sample))
    return digest.hexdigest()


def full_hash(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=20)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class FileOpsService:
    """Scanning, organising and tagging, all inside the authorised roots."""

    def __init__(self, db, settings, activity=None, confirmations=None):
        self.db = db
        self.settings = settings
        self.activity = activity
        self.confirmations = confirmations
        self._plans: Dict[str, Dict[str, Any]] = {}

    # ==================================================================
    # authorisation
    # ==================================================================
    def _roots(self) -> List[Path]:
        return [Path(root).expanduser().resolve()
                for root in (self.settings.FILE_ROOTS or [])]

    def authorise(self, raw: str) -> Path:
        """Resolve a path and prove it is inside an authorised root."""
        if not raw or not str(raw).strip():
            raise ValueError("Which folder?")
        candidate = Path(str(raw)).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ValueError("That path cannot be read: {}".format(exc))

        roots = self._roots()
        if not roots:
            raise PermissionError("No file roots are configured (JARVIS_FILE_ROOTS).")
        for root in roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise PermissionError(
            "{} is outside the folders JARVIS may read. Allowed: {}".format(
                resolved, ", ".join(str(r) for r in roots)))

    def _walk(self, root: Path, max_files: int = MAX_SCAN_FILES):
        """Yield files, skipping build noise and anything unreadable."""
        count = 0
        for base, dirs, names in os.walk(root, onerror=lambda _: None):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                path = Path(base) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    yield path, path.stat()
                except OSError:
                    continue
                count += 1
                if count >= max_files:
                    return

    # ==================================================================
    # scanning
    # ==================================================================
    async def duplicates(self, folder: str, min_bytes: int = 1024,
                         limit: int = 100) -> Dict[str, Any]:
        """Find identical files. Confirmed by a full hash, never by name."""
        root = self.authorise(folder)

        by_size: Dict[int, List[Path]] = {}
        scanned = 0
        for path, stat in self._walk(root):
            if stat.st_size >= min_bytes:
                by_size.setdefault(stat.st_size, []).append(path)
                scanned += 1

        candidates: Dict[str, List[Path]] = {}
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            for path in paths:
                try:
                    candidates.setdefault(quick_signature(path), []).append(path)
                except OSError:
                    continue

        groups = []
        wasted = 0
        for signature, paths in candidates.items():
            if len(paths) < 2:
                continue
            confirmed: Dict[str, List[Path]] = {}
            for path in paths:
                try:
                    confirmed.setdefault(full_hash(path), []).append(path)
                except OSError:
                    continue
            for digest, matched in confirmed.items():
                if len(matched) < 2:
                    continue
                size = matched[0].stat().st_size
                wasted += size * (len(matched) - 1)
                groups.append({
                    "hash": digest[:16],
                    "size": size,
                    "size_human": human_bytes(size),
                    "count": len(matched),
                    "wasted": human_bytes(size * (len(matched) - 1)),
                    "files": [str(p) for p in sorted(matched)],
                    "keep": str(sorted(matched)[0]),
                })

        groups.sort(key=lambda g: -g["size"] * (g["count"] - 1))
        return {
            "folder": str(root),
            "scanned": scanned,
            "groups": groups[:limit],
            "group_count": len(groups),
            "reclaimable": human_bytes(wasted),
            "reclaimable_bytes": wasted,
            "note": "Nothing has been deleted. Sizes are measured, not estimated.",
        }

    async def large_files(self, folder: str, limit: int = 40,
                          min_mb: float = 50) -> Dict[str, Any]:
        root = self.authorise(folder)
        threshold = int(min_mb * 1024 * 1024)
        found: List[Tuple[int, Path, float]] = []
        for path, stat in self._walk(root):
            if stat.st_size >= threshold:
                found.append((stat.st_size, path, stat.st_mtime))
        found.sort(key=lambda item: -item[0])

        return {
            "folder": str(root),
            "threshold": human_bytes(threshold),
            "files": [{"path": str(p), "size": human_bytes(s), "bytes": s,
                       "modified": datetime.fromtimestamp(m).isoformat(timespec="seconds")}
                      for s, p, m in found[:limit]],
            "total_found": len(found),
            "total_size": human_bytes(sum(s for s, _, _ in found)),
        }

    async def disk_usage(self, folder: str, depth: int = 1) -> Dict[str, Any]:
        """Size of each immediate child, so you can see where space actually went."""
        root = self.authorise(folder)
        if not root.is_dir():
            raise ValueError("{} is not a folder.".format(root))

        children = []
        total = 0
        for entry in sorted(root.iterdir()):
            if entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    size, count = entry.stat().st_size, 1
                else:
                    size, count = 0, 0
                    for _, stat in self._walk(entry):
                        size += stat.st_size
                        count += 1
            except OSError:
                continue
            total += size
            children.append({"name": entry.name, "path": str(entry),
                             "is_dir": entry.is_dir(), "bytes": size,
                             "size": human_bytes(size), "files": count})

        children.sort(key=lambda c: -c["bytes"])
        for child in children:
            child["share"] = round(child["bytes"] / total, 4) if total else 0.0
        return {"folder": str(root), "total": human_bytes(total), "total_bytes": total,
                "children": children[:60], "depth": depth}

    async def find_content(self, folder: str, pattern: str, glob: str = "",
                           limit: int = 100, regex: bool = False) -> Dict[str, Any]:
        """Search inside text files. Binary files are skipped, not mangled."""
        root = self.authorise(folder)
        if not pattern:
            raise ValueError("What should I search for?")

        try:
            matcher = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
        except re.error as exc:
            raise ValueError("That is not a valid regular expression: {}".format(exc))

        matches: List[Dict[str, Any]] = []
        searched = 0
        skipped_binary = 0
        for path, stat in self._walk(root):
            if glob and not path.match(glob):
                continue
            if stat.st_size > 4 * 1024 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                skipped_binary += 1
                continue
            searched += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    matches.append({"path": str(path), "line": number,
                                    "text": line.strip()[:200]})
                    if len(matches) >= limit:
                        return {"folder": str(root), "pattern": pattern, "matches": matches,
                                "files_searched": searched, "truncated": True,
                                "skipped_binary": skipped_binary}

        return {"folder": str(root), "pattern": pattern, "matches": matches,
                "files_searched": searched, "truncated": False,
                "skipped_binary": skipped_binary}

    async def recent_files(self, hours: int = 24, limit: int = 40) -> Dict[str, Any]:
        """Recently modified files across every authorised root."""
        cutoff = datetime.now().timestamp() - hours * 3600
        found: List[Tuple[float, Path, int]] = []
        for root in self._roots():
            if not root.exists():
                continue
            for path, stat in self._walk(root, max_files=60_000):
                if stat.st_mtime >= cutoff:
                    found.append((stat.st_mtime, path, stat.st_size))
        found.sort(key=lambda item: -item[0])
        return {
            "hours": hours,
            "files": [{"path": str(p), "size": human_bytes(s),
                       "modified": datetime.fromtimestamp(m).isoformat(timespec="seconds")}
                      for m, p, s in found[:limit]],
            "total": len(found),
        }

    # ==================================================================
    # bulk rename -- planned, reviewed, then applied
    # ==================================================================
    async def plan_rename(self, folder: str, find: str, replace: str, glob: str = "*",
                          regex: bool = False) -> Dict[str, Any]:
        """Build a rename plan. Writes nothing."""
        root = self.authorise(folder)
        if not root.is_dir():
            raise ValueError("{} is not a folder.".format(root))
        if not find:
            raise ValueError("What should I replace?")

        try:
            matcher = re.compile(find if regex else re.escape(find))
        except re.error as exc:
            raise ValueError("That is not a valid regular expression: {}".format(exc))

        changes = []
        conflicts = []
        planned_names = set()
        for entry in sorted(root.iterdir()):
            if not entry.is_file() or not entry.match(glob):
                continue
            new_name = matcher.sub(replace, entry.name)
            if new_name == entry.name:
                continue
            if not new_name.strip() or any(ch in new_name for ch in '\\/:*?"<>|'):
                conflicts.append({"from": entry.name, "to": new_name,
                                  "reason": "not a valid filename"})
                continue
            target = entry.with_name(new_name)
            if target.exists() or new_name in planned_names:
                conflicts.append({"from": entry.name, "to": new_name,
                                  "reason": "target already exists"})
                continue
            planned_names.add(new_name)
            changes.append({"from": str(entry), "to": str(target),
                            "from_name": entry.name, "to_name": new_name})

        plan_id = "plan_" + uuid.uuid4().hex[:10]
        self._plans[plan_id] = {"changes": changes, "folder": str(root), "created": _now()}
        return {
            "plan_id": plan_id if changes else "",
            "folder": str(root),
            "changes": changes[:200],
            "count": len(changes),
            "conflicts": conflicts,
            "applied": False,
            "note": "Nothing has been renamed. Apply the plan to carry it out."
                    if changes else "No filenames matched.",
        }

    async def apply_rename(self, plan_id: str) -> Dict[str, Any]:
        plan = self._plans.get(plan_id)
        if not plan:
            raise LookupError(
                "No rename plan with id {}. Plans are held in memory and are lost on "
                "restart -- build a new one.".format(plan_id))

        renamed, failed = [], []
        for change in plan["changes"]:
            source, target = Path(change["from"]), Path(change["to"])
            try:
                # Re-check at apply time; the folder may have changed since planning.
                self.authorise(str(source))
                self.authorise(str(target.parent))
                if target.exists():
                    failed.append({"from": change["from_name"], "error": "target now exists"})
                    continue
                source.rename(target)
                renamed.append({"from": change["from_name"], "to": change["to_name"]})
            except Exception as exc:
                failed.append({"from": change["from_name"], "error": str(exc)[:200]})

        self._plans.pop(plan_id, None)
        if self.activity:
            await self.activity.record(
                "bulk rename: {} renamed, {} failed".format(len(renamed), len(failed)),
                level="warn" if failed else "info", source="files")
        return {"applied": True, "renamed": renamed, "failed": failed,
                "count": len(renamed)}

    async def organise_plan(self, folder: str) -> Dict[str, Any]:
        """Plan sorting loose files into folders by type. Writes nothing."""
        root = self.authorise(folder)
        buckets = {
            "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic"},
            "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".epub"},
            "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
            "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".xz"},
            "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
            "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
            "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".appimage"},
            "Code": {".py", ".js", ".ts", ".tsx", ".java", ".c", ".cpp", ".rs", ".go", ".rb"},
        }

        moves = []
        for entry in sorted(root.iterdir()):
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            for bucket, extensions in buckets.items():
                if suffix in extensions:
                    moves.append({"from": str(entry), "to": str(root / bucket / entry.name),
                                  "bucket": bucket, "name": entry.name})
                    break

        by_bucket: Dict[str, int] = {}
        for move in moves:
            by_bucket[move["bucket"]] = by_bucket.get(move["bucket"], 0) + 1

        plan_id = "plan_" + uuid.uuid4().hex[:10]
        self._plans[plan_id] = {"changes": moves, "folder": str(root), "created": _now(),
                                "kind": "organise"}
        return {"plan_id": plan_id if moves else "", "folder": str(root),
                "moves": moves[:200], "count": len(moves), "by_bucket": by_bucket,
                "applied": False,
                "note": "Nothing has been moved." if moves else "Nothing to sort."}

    async def apply_organise(self, plan_id: str) -> Dict[str, Any]:
        plan = self._plans.get(plan_id)
        if not plan or plan.get("kind") != "organise":
            raise LookupError("No organise plan with id {}.".format(plan_id))

        moved, failed = [], []
        for change in plan["changes"]:
            source, target = Path(change["from"]), Path(change["to"])
            try:
                self.authorise(str(source))
                self.authorise(str(target.parent.parent))
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    failed.append({"name": change["name"], "error": "already there"})
                    continue
                source.rename(target)
                moved.append({"name": change["name"], "bucket": change["bucket"]})
            except Exception as exc:
                failed.append({"name": change["name"], "error": str(exc)[:200]})

        self._plans.pop(plan_id, None)
        if self.activity:
            await self.activity.record(
                "organised {} file(s)".format(len(moved)), level="info", source="files")
        return {"applied": True, "moved": moved, "failed": failed, "count": len(moved)}

    # ==================================================================
    # tags
    # ==================================================================
    async def tag(self, path: str, tags: Any) -> Dict[str, Any]:
        resolved = self.authorise(path)
        if not resolved.exists():
            raise FileNotFoundError("{} does not exist.".format(resolved))
        names = [t.strip().lower() for t in (
            tags if isinstance(tags, (list, tuple)) else str(tags).split(",")) if t.strip()]
        if not names:
            raise ValueError("Which tag?")
        for name in names:
            await self.db.execute(
                "INSERT OR IGNORE INTO file_tags (path, tag, added_at) VALUES (?, ?, ?)",
                (str(resolved), name, _now()))
        return {"path": str(resolved), "tags": names}

    async def untag(self, path: str, tag: str) -> Dict[str, Any]:
        resolved = self.authorise(path)
        removed = await self.db.execute(
            "DELETE FROM file_tags WHERE path = ? AND tag = ?",
            (str(resolved), tag.strip().lower()))
        return {"path": str(resolved), "removed": removed}

    async def tagged(self, tag: str = "") -> Dict[str, Any]:
        """Files by tag. Missing files are reported rather than silently listed."""
        if tag:
            rows = await self.db.fetch_all(
                "SELECT path, tag, added_at FROM file_tags WHERE tag = ? ORDER BY added_at DESC",
                (tag.strip().lower(),))
        else:
            rows = await self.db.fetch_all(
                "SELECT path, tag, added_at FROM file_tags ORDER BY added_at DESC LIMIT 500")

        files = []
        for row in rows:
            exists = Path(row["path"]).exists()
            files.append({"path": row["path"], "tag": row["tag"],
                          "added_at": row["added_at"], "exists": exists})

        counts = await self.db.fetch_all(
            "SELECT tag, COUNT(*) AS n FROM file_tags GROUP BY tag ORDER BY n DESC")
        missing = len([f for f in files if not f["exists"]])
        return {"files": files, "tags": counts, "missing": missing}
