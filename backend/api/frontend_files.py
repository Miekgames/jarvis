"""Serving the built interface (`frontend/dist`) from the backend.

- The page itself is never cached. A phone that had cached it kept showing
  the old interface after an update, until Safari felt like asking again.
- Everything under `assets/` has a hash of its contents in its name, so one
  name never changes meaning and can be cached for good.
- Only files inside the built interface are served: "../../.env" is not one.
- The home-screen manifest goes out with its proper type.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

mimetypes.add_type("application/manifest+json", ".webmanifest")
NO_CACHE = {"Cache-Control": "no-cache"}


class ForeverFiles(StaticFiles):
    """The built interface's hashed assets: cacheable for a year."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def inside(root: Path, wanted: str) -> Path | None:
    """The file `wanted` names under `root`, or None if it isn't one there."""
    root = root.resolve()
    candidate = (root / wanted).resolve()
    if wanted and candidate.is_file() and root in candidate.parents:
        return candidate
    return None


def mount_interface(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", ForeverFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        found = inside(dist, full_path)
        return FileResponse(found or dist / "index.html", headers=NO_CACHE)
