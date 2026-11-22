"""The wallpaper workshop: one service holding what has been made.

Everything here is local. Building a pack writes images into its own
directory and nothing else; this module reaches no network at all, which a
test enforces, and that is what makes the whole of it safe to drive by voice
from a phone.

Products live under `data/products/`, on whichever drive the project is on.
That matters on this machine specifically: C: has about twelve gigabytes
free and a single pack is a quarter of a gigabyte, so defaulting to the
project directory rather than a home folder is the difference between
working and filling the system drive.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import packs, palettes
from .packs import BuiltPack, PackPlan
from .wallpapers import RESOLUTIONS, STYLE_DESCRIPTIONS, STYLES

logger = logging.getLogger("jarvis.business")

# A build is minutes of CPU, so only one runs at a time. Two concurrent
# builds would not be faster and would make the progress reporting a lie.
_BUILD_LOCK = asyncio.Lock()


class BusinessService:
    """Generating, packaging and drafting. Never publishing."""

    def __init__(self, settings, activity=None, audit=None):
        self.settings = settings
        self.activity = activity
        self.audit = audit
        self.root = Path(settings.DATA_DIR) / "products"
        self._progress: Dict[str, Any] = {}

    # -- discovery ---------------------------------------------------------
    def catalogue(self) -> Dict[str, Any]:
        """What can be made, for the model and the interface alike."""
        return {
            "styles": {name: STYLE_DESCRIPTIONS.get(name, name)
                       for name in sorted(STYLES)},
            "palettes": [
                {"slug": p.slug, "name": p.name, "mood": p.mood,
                 "dark": p.dark, "colors": list(p.colors)}
                for p in palettes.PALETTES
            ],
            "resolutions": {name: list(size) for name, size in RESOLUTIONS.items()},
            "root": str(self.root),
        }

    def products(self) -> List[Dict[str, Any]]:
        """Packs already built, newest first."""
        if not self.root.exists():
            return []
        out = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "manifest.json"
            if not manifest.exists():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            images = list((directory / "wallpapers").rglob("*.png"))
            archive = directory / f"{directory.name}.zip"
            out.append({
                "slug": directory.name,
                "name": payload.get("pack", {}).get("name", directory.name),
                "palette": payload.get("pack", {}).get("palette"),
                "designs": payload.get("pack", {}).get("count"),
                "files": len(images),
                "megabytes": round(
                    sum(p.stat().st_size for p in images) / 1e6, 1),
                "archive": str(archive) if archive.exists() else None,
                "preview": str(directory / "preview.png")
                if (directory / "preview.png").exists() else None,
                "built_at": datetime.fromtimestamp(
                    manifest.stat().st_mtime, tz=timezone.utc).isoformat(),
                "directory": str(directory),
            })
        out.sort(key=lambda p: p["built_at"], reverse=True)
        return out

    def find(self, slug: str) -> Optional[Dict[str, Any]]:
        wanted = slug.strip().lower()
        for product in self.products():
            if product["slug"] == wanted or product["name"].lower() == wanted:
                return product
        return None

    # -- planning ----------------------------------------------------------
    def plan(self, palette: str, count: int = 8, seed: Optional[int] = None,
             styles: Optional[List[str]] = None,
             resolutions: Optional[List[str]] = None,
             name: Optional[str] = None) -> PackPlan:
        chosen = packs.suggest(palette, count=count,
                               seed=seed if seed is not None else _seed_now(),
                               styles=styles, name=name)
        if resolutions:
            chosen.resolutions = tuple(resolutions)
        chosen.validate()
        return chosen

    def estimate(self, plan: PackPlan) -> Dict[str, Any]:
        """What a build would cost, including whether there is room for it."""
        import shutil

        numbers = packs.estimate_size(plan)
        try:
            free = shutil.disk_usage(self.root.parent if self.root.parent.exists()
                                     else Path.cwd()).free
        except OSError:
            free = None

        needed = numbers["megabytes_with_archive"] * 1e6
        numbers["free_megabytes"] = round(free / 1e6, 1) if free else None
        numbers["fits"] = (free is None) or (free > needed * 1.5)
        if not numbers["fits"]:
            numbers["warning"] = (
                f"This needs about {numbers['megabytes_with_archive']:.0f} MB "
                f"and only {numbers['free_megabytes']:.0f} MB is free.")
        return numbers

    # -- building ----------------------------------------------------------
    @property
    def progress(self) -> Dict[str, Any]:
        return dict(self._progress)

    async def build(self, plan: PackPlan, overwrite: bool = False,
                    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
                    ) -> Dict[str, Any]:
        """Render a pack. Minutes of CPU, so it runs off the event loop."""
        plan.validate()
        room = self.estimate(plan)
        if not room.get("fits", True):
            raise OSError(room.get("warning", "Not enough disk space."))

        async with _BUILD_LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            loop = asyncio.get_running_loop()

            def report(done: int, total: int, name: str) -> None:
                state = {"slug": plan.slug, "done": done, "total": total,
                         "file": name,
                         "percent": round(done / max(total, 1) * 100)}
                self._progress = state
                if on_progress is not None:
                    # Called from the worker thread, so hand it back to the
                    # loop rather than touching the event bus from here.
                    loop.call_soon_threadsafe(on_progress, state)

            self._progress = {"slug": plan.slug, "done": 0, "total": 0,
                              "percent": 0, "file": "starting"}
            try:
                built: BuiltPack = await asyncio.to_thread(
                    packs.build, plan, self.root, report, overwrite)
            finally:
                self._progress = {}

        await self._record(
            f"built the wallpaper pack '{plan.name}' "
            f"({len(built.images)} files, {built.bytes_written/1e6:.0f} MB)")
        return built.summary()



    # -- internals ---------------------------------------------------------
    def _plan_from_disk(self, slug: str) -> PackPlan:
        product = self.find(slug)
        if product is None:
            known = ", ".join(p["slug"] for p in self.products()) or "none yet"
            raise KeyError(f"No pack called {slug!r}. Built so far: {known}")
        payload = json.loads(
            (Path(product["directory"]) / "manifest.json").read_text(encoding="utf-8"))
        fields = dict(payload["pack"])
        fields["styles"] = tuple(fields.get("styles", ()))
        fields["resolutions"] = tuple(fields.get("resolutions", ()))
        return PackPlan(**fields)

    def _built_from_disk(self, slug: str, plan: PackPlan) -> BuiltPack:
        product = self.find(slug)
        directory = Path(product["directory"])
        images = sorted((directory / "wallpapers").rglob("*.png"))
        archive = directory / f"{directory.name}.zip"
        preview = directory / "preview.png"
        return BuiltPack(
            plan=plan, directory=directory, images=images,
            preview=preview if preview.exists() else None,
            archive=archive if archive.exists() else None,
            manifest=directory / "manifest.json",
            bytes_written=sum(p.stat().st_size for p in images),
        )

    async def _record(self, message: str) -> None:
        if self.activity is not None:
            try:
                await self.activity.record(message, source="business", level="info")
            except Exception:
                logger.debug("activity log failed", exc_info=True)
        if self.audit is not None:
            try:
                await self.audit.record("business.pack", subject=message)
            except Exception:
                logger.debug("audit failed", exc_info=True)


def _seed_now() -> int:
    """A seed that varies but stays small enough to read aloud."""
    return int(datetime.now(timezone.utc).strftime("%j%H%M")) % 100000
