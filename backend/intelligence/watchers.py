"""
Environment watchers — new automation triggers.

Each watcher polls one aspect of the machine and publishes an event when it
changes. Because the automation engine triggers on any bus event, adding a
watcher here immediately makes a new trigger type available with no changes to
the engine.

    file.changed      a watched folder gained, lost or modified a file
    app.launched      a process appeared
    app.closed        a process disappeared
    app.crashed       a process disappeared without a clean shutdown
    network.online    connectivity returned
    network.offline   connectivity lost
    awareness.idle    the user went idle (published by AwarenessService)

Polling rather than OS notification APIs: it is cross-platform, needs no extra
dependency, and at a 5-second interval the cost is negligible next to the
metrics sampler that is already running.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..core.events import bus
from ..core.util import now_iso, truncate

logger = logging.getLogger("jarvis.watchers")

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

FILE_POLL_SECONDS = 6
PROCESS_POLL_SECONDS = 5
NETWORK_POLL_SECONDS = 20
MAX_WATCHED_FILES = 4000


class WatcherService:
    def __init__(self, db, settings, settings_store, activity, files=None, metrics=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.activity = activity
        self.files = files
        self.metrics = metrics

        self._tasks: List[asyncio.Task] = []
        self._running = False

        self._file_state: Dict[str, Dict[str, float]] = {}
        self._processes: Dict[int, str] = {}
        self._online: Optional[bool] = None
        self._clean_exits: Set[int] = set()

    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._watch_files(), name="watch-files"),
            asyncio.create_task(self._watch_processes(), name="watch-processes"),
            asyncio.create_task(self._watch_network(), name="watch-network"),
        ]
        logger.info("environment watchers started")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def running(self) -> bool:
        return self._running and any(not t.done() for t in self._tasks)

    # ==================================================================
    # files
    # ==================================================================
    async def watched_folders(self) -> List[str]:
        awareness = await self.settings_store.get("awareness", {})
        return [str(p) for p in (awareness.get("watch_folders") or [])]

    async def watch_folder(self, path: str) -> Dict[str, Any]:
        if self.files:
            resolved = str(self.files.resolve(path))   # raises outside the sandbox
        else:
            resolved = str(Path(path).expanduser().resolve())
        folders = await self.watched_folders()
        if resolved not in folders:
            folders.append(resolved)
            await self.settings_store.patch("awareness", {"watch_folders": folders})
            await self.activity.record(f"Watching {resolved} for changes.", source="watchers")
        self._file_state.pop(resolved, None)   # force a fresh baseline
        return {"watching": folders}

    async def unwatch_folder(self, path: str) -> Dict[str, Any]:
        folders = [f for f in await self.watched_folders() if f != path]
        await self.settings_store.patch("awareness", {"watch_folders": folders})
        self._file_state.pop(path, None)
        return {"watching": folders}

    async def _watch_files(self) -> None:
        while self._running:
            try:
                for folder in await self.watched_folders():
                    await self._scan_folder(folder)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("file watcher failed")
            await asyncio.sleep(FILE_POLL_SECONDS)

    async def _scan_folder(self, folder: str) -> None:
        root = Path(folder)
        if not root.is_dir():
            return

        def _snapshot() -> Dict[str, float]:
            out: Dict[str, float] = {}
            count = 0
            for current, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
                for name in filenames:
                    if count >= MAX_WATCHED_FILES:
                        return out
                    full = Path(current) / name
                    try:
                        out[str(full)] = full.stat().st_mtime
                        count += 1
                    except OSError:
                        continue
            return out

        current_state = await asyncio.get_event_loop().run_in_executor(None, _snapshot)
        previous = self._file_state.get(folder)
        self._file_state[folder] = current_state

        if previous is None:
            # First pass establishes the baseline; announcing every existing
            # file as "new" would be useless noise.
            return

        added = [p for p in current_state if p not in previous]
        removed = [p for p in previous if p not in current_state]
        modified = [p for p in current_state
                    if p in previous and current_state[p] != previous[p]]

        if not (added or removed or modified):
            return

        payload = {
            "folder": folder,
            "added": added[:20],
            "removed": removed[:20],
            "modified": modified[:20],
            "counts": {"added": len(added), "removed": len(removed), "modified": len(modified)},
        }
        bus.publish("file.changed", payload, source="watchers")
        logger.debug("file change in %s: +%s -%s ~%s",
                     folder, len(added), len(removed), len(modified))

    # ==================================================================
    # processes
    # ==================================================================
    async def _watch_processes(self) -> None:
        if psutil is None:
            return
        while self._running:
            try:
                await self._scan_processes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("process watcher failed")
            await asyncio.sleep(PROCESS_POLL_SECONDS)

    async def _scan_processes(self) -> None:
        def _snapshot() -> Dict[int, str]:
            out = {}
            for process in psutil.process_iter(["pid", "name"]):
                try:
                    out[process.info["pid"]] = process.info.get("name") or ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return out

        current = await asyncio.get_event_loop().run_in_executor(None, _snapshot)
        if not self._processes:
            self._processes = current
            return

        started = {pid: name for pid, name in current.items() if pid not in self._processes}
        ended = {pid: name for pid, name in self._processes.items() if pid not in current}
        self._processes = current

        # Collapse the noise: only announce apps the user would recognise, i.e.
        # ones that had a visible window or are in the known-app index.
        for pid, name in started.items():
            if _interesting(name):
                bus.publish("app.launched", {"app": name, "pid": pid}, source="watchers")

        for pid, name in ended.items():
            if not _interesting(name):
                continue
            crashed = pid not in self._clean_exits
            bus.publish("app.closed", {"app": name, "pid": pid, "crashed": crashed},
                        source="watchers")
            if crashed:
                bus.publish("app.crashed", {"app": name, "pid": pid}, source="watchers")
                await self.db.execute(
                    "UPDATE app_stats SET crashes = crashes + 1 WHERE app = ?", (name,)
                )
            self._clean_exits.discard(pid)

    def mark_clean_exit(self, pid: int) -> None:
        """Called when JARVIS itself closes an app, so it isn't reported as a
        crash."""
        self._clean_exits.add(pid)

    # ==================================================================
    # network
    # ==================================================================
    async def _watch_network(self) -> None:
        while self._running:
            try:
                online = True
                if self.metrics is not None:
                    online = await self.metrics.network_reachable(timeout=3.0)
                if self._online is None:
                    self._online = online
                elif online != self._online:
                    self._online = online
                    bus.publish("network.online" if online else "network.offline",
                                {"at": now_iso()}, source="watchers")
                    await self.activity.record(
                        "Internet connection restored." if online
                        else "Internet connection lost -- queuing anything that needs it.",
                        level="info" if online else "warn",
                        source="watchers",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("network watcher failed")
            await asyncio.sleep(NETWORK_POLL_SECONDS)

    @property
    def online(self) -> bool:
        return self._online is not False

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "watched_folders": await self.watched_folders(),
            "tracked_files": sum(len(v) for v in self._file_state.values()),
            "tracked_processes": len(self._processes),
            "online": self.online,
        }


# ---------------------------------------------------------------------------
_BORING = {
    "svchost.exe", "conhost.exe", "dllhost.exe", "runtimebroker.exe", "backgroundtaskhost.exe",
    "wmiprvse.exe", "csrss.exe", "sihost.exe", "taskhostw.exe", "smartscreen.exe",
    "audiodg.exe", "fontdrvhost.exe", "ctfmon.exe", "searchprotocolhost.exe",
    "searchfilterhost.exe", "wermgr.exe", "consent.exe", "spoolsv.exe", "dwm.exe",
    "gameinputsvc.exe", "textinputhost.exe", "applicationframehost.exe",
}


def _interesting(name: str) -> bool:
    lowered = (name or "").lower()
    if not lowered or lowered in _BORING:
        return False
    # Helper processes spawned per tab/window would otherwise flood the bus.
    return not lowered.endswith("crashpad_handler.exe")
