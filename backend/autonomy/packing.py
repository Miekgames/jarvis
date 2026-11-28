"""Packing a finished project to send: a zip of what was made, and how it looks.

Sent to the owner's Discord DMs when a project finishes, or when they ask
("send me the tip calculator on Discord"). What gets left out is what can be
rebuilt or is bulk -- node_modules, virtual environments, git history -- and
the screenshots go separately, as pictures, not inside the zip.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import List, Optional

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env", ".next", ".cache",
             "dist-electron", ".pytest_cache", ".mypy_cache", "screenshots", ".idea", ".vscode"}
# Discord's upload cap for a bot is about 10 MB a message; keep some margin.
UPLOAD_LIMIT = 9_500_000
_FILLER = {"my", "the", "a", "an", "project", "app", "files", "file", "code", "that", "it", "this",
           "one", "last", "latest", "website", "page", "thing", "folder", "me", "please"}


def pack(folder: Path, out_dir: Path) -> Path:
    """`<folder name>.zip` in out_dir, without the bulk that can be rebuilt."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{folder.name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            parts = set(path.relative_to(folder).parts[:-1])
            if path.is_file() and not (parts & SKIP_DIRS):
                archive.write(path, path.relative_to(folder))
    return target


def screenshots(folder: Path, limit: int = 2) -> List[Path]:
    """The newest screenshots the build took of itself, if it took any."""
    shots = sorted((folder / "screenshots").glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if (folder / "screenshots").is_dir() else []
    return shots[:limit]


def find_project(workspace: Path, words: str) -> Optional[Path]:
    """The project folder the words mean: "the tip calculator", or "it" for the newest."""
    if not workspace.is_dir():
        return None
    folders = sorted((p for p in workspace.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not folders:
        return None
    wanted = [w for w in re.findall(r"[a-z0-9]+", (words or "").lower()) if w not in _FILLER]
    if not wanted:
        return folders[0]
    scored = [(sum(1 for w in wanted if w in folder.name.lower()), -i, folder)
              for i, folder in enumerate(folders)]
    best = max(scored)
    return best[2] if best[0] > 0 else None
