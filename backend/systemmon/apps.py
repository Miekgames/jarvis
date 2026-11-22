"""
Application discovery, launching and termination.

Windows is the primary target (Start Menu shortcut index + shell aliases), with
working fallbacks for macOS and Linux so the module never hard-fails.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import time
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.apps")

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

_ALIASES: Dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "powershell": "powershell.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "settings": "ms-settings:",
    "snipping tool": "snippingtool.exe",
    "wordpad": "write.exe",
    "registry editor": "regedit.exe",
    "character map": "charmap.exe",
    "magnifier": "magnify.exe",
    "on-screen keyboard": "osk.exe",
    "camera": "microsoft.windows.camera:",
    "photos": "ms-photos:",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "maps": "bingmaps:",
    "device manager": "devmgmt.msc",
    "disk management": "diskmgmt.msc",
    "services": "services.msc",
    "resource monitor": "resmon.exe",
    "system information": "msinfo32.exe",
}

# Terminating these would take the desktop or JARVIS itself down.
_PROTECTED = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "services.exe", "lsass.exe",
    "smss.exe", "wininit.exe", "svchost.exe", "system", "registry", "dwm.exe",
    "python.exe", "pythonw.exe", "node.exe",
}


class AppLauncher:
    def __init__(self) -> None:
        self._index: Optional[List[Tuple[str, str]]] = None

    # ------------------------------------------------------------------
    def launch(self, name: str) -> Dict[str, Any]:
        query = (name or "").strip()
        if not query:
            return {"ok": False, "message": "No application name given."}
        key = query.lower()

        if IS_WINDOWS:
            if key in _ALIASES:
                return self._launch(_ALIASES[key], query)
            match = self._best_shortcut(key)
            if match:
                display, path = match
                try:
                    os.startfile(path)  # type: ignore[attr-defined]
                    return {"ok": True, "message": f"Launched {display}.", "resolved": display}
                except OSError as exc:
                    logger.warning("startfile failed for %s: %s", path, exc)
            exe = key if key.endswith(".exe") else f"{key}.exe"
            if shutil.which(exe) or shutil.which(key):
                return self._launch(exe, query)
            try:
                os.startfile(query)  # type: ignore[attr-defined]
                return {"ok": True, "message": f"Asked Windows to open '{query}'.", "resolved": query}
            except OSError:
                pass
        elif IS_MAC:
            result = subprocess.run(["open", "-a", query], capture_output=True, text=True)
            if result.returncode == 0:
                return {"ok": True, "message": f"Launched {query}.", "resolved": query}
        else:
            binary = shutil.which(key)
            if binary:
                subprocess.Popen([binary], start_new_session=True)
                return {"ok": True, "message": f"Launched {query}.", "resolved": query}

        suggestions = self.suggest(key)
        hint = f" Closest matches: {', '.join(suggestions)}." if suggestions else ""
        return {"ok": False, "message": f"Couldn't find an app matching '{query}'.{hint}",
                "suggestions": suggestions}

    # ------------------------------------------------------------------
    def close(self, name: str) -> Dict[str, Any]:
        """Ask a process to exit. Refuses on anything system-critical."""
        query = (name or "").strip().lower()
        if not query:
            return {"ok": False, "message": "No application name given."}

        target = query if query.endswith(".exe") or not IS_WINDOWS else f"{query}.exe"
        if target.lower() in _PROTECTED:
            return {"ok": False, "message": f"'{name}' is a protected system process -- I won't terminate it."}

        try:
            import psutil
        except ImportError:
            return {"ok": False, "message": "psutil is not installed, so I can't manage processes."}

        matched: List[Any] = []
        labels: List[str] = []
        for process in psutil.process_iter(["name", "pid"]):
            try:
                process_name = (process.info.get("name") or "").lower()
                if not process_name or process_name in _PROTECTED:
                    continue
                if process_name == target.lower() or query in process_name:
                    matched.append(process)
                    labels.append(f"{process.info['name']} (pid {process.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not matched:
            return {"ok": False, "message": f"No running process matched '{name}'."}

        for process in matched:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        _, still_alive = psutil.wait_procs(matched, timeout=4)
        for process in still_alive:
            try:
                process.kill()  # graceful terminate ignored -- force it
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return {
            "ok": True,
            "message": f"Closed {len(matched)} process(es): {', '.join(labels[:5])}",
            "closed": labels,
            "forced": len(still_alive),
        }

    # ------------------------------------------------------------------
    def list_apps(self, limit: int = 200) -> List[str]:
        names = sorted({display for display, _ in self._get_index()}, key=str.lower)
        return names[:limit]

    def suggest(self, key: str, n: int = 3) -> List[str]:
        scored = [(self._score(key, display.lower()), display) for display, _ in self._get_index()]
        scored.sort(reverse=True)
        return [name for score, name in scored[:n] if score > 0.4]

    def refresh(self) -> int:
        self._index = self._build_index()
        return len(self._index)

    # ------------------------------------------------------------------
    def _launch(self, command: str, display: str) -> Dict[str, Any]:
        """Start an executable or shell URI without going through a shell.

        This used to build `start "" "{command}"` and hand it to cmd.exe, with
        `command` partly derived from whatever the user asked to open. Even
        behind a `shutil.which` check, interpolating a name into a command
        line is how an argument becomes an instruction. `os.startfile` is the
        Windows API for "open this the way Explorer would", takes a path or a
        URI, and parses nothing.
        """
        try:
            if IS_WINDOWS:
                os.startfile(command)  # type: ignore[attr-defined]
            else:
                resolved = shutil.which(command)
                if not resolved:
                    return {"ok": False, "message": f"'{command}' is not on PATH."}
                subprocess.Popen([resolved], start_new_session=True)
            return {"ok": True, "message": f"Launched {display}.", "resolved": display}
        except OSError as exc:
            return {"ok": False, "message": f"Failed to launch {display}: {exc}"}

    def _best_shortcut(self, key: str) -> Optional[Tuple[str, str]]:
        best: Optional[Tuple[float, str, str]] = None
        for display, path in self._get_index():
            score = self._score(key, display.lower())
            if best is None or score > best[0]:
                best = (score, display, path)
        return (best[1], best[2]) if best and best[0] >= 0.55 else None

    @staticmethod
    def _score(query: str, candidate: str) -> float:
        if query == candidate:
            return 2.0
        if candidate.startswith(query):
            return 1.5
        if query in candidate:
            return 1.0 + len(query) / max(len(candidate), 1) * 0.3
        query_tokens, candidate_tokens = set(query.split()), set(candidate.split())
        if query_tokens and query_tokens <= candidate_tokens:
            return 0.9
        return SequenceMatcher(None, query, candidate).ratio()

    def _get_index(self) -> List[Tuple[str, str]]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> List[Tuple[str, str]]:
        if not IS_WINDOWS:
            if IS_MAC:
                apps = []
                for root in (Path("/Applications"), Path.home() / "Applications"):
                    if root.exists():
                        apps.extend((p.stem, str(p)) for p in root.glob("*.app"))
                return apps
            return []

        roots: List[Path] = []
        for var, tail in (("APPDATA", None), ("PROGRAMDATA", None)):
            base = os.environ.get(var)
            if base:
                roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

        skip = ("uninstall", "readme", "help", "website", "documentation", "release notes")
        seen: Dict[str, str] = {}
        for root in roots:
            if not root.exists():
                continue
            try:
                for link in root.rglob("*.lnk"):
                    lowered = link.stem.lower()
                    if any(word in lowered for word in skip):
                        continue
                    seen.setdefault(lowered, str(link))
            except OSError:
                continue

        index = [(Path(path).stem, path) for path in seen.values()]
        logger.info("indexed %s Start Menu shortcuts", len(index))
        return index


# Not a process anyone wants at the top of "what is using my CPU": it is the
# measure of the CPU nobody is using.
_IDLE_PROCESSES = {"system idle process", "idle"}


# How long to watch before reporting CPU shares. psutil measures the CPU a
# process has used *since the last time you asked it*, so a single pass over
# fresh handles reports 0.0 for everything -- which is exactly what the system
# panel showed, under a column headed "cpu", sorted by it.
_CPU_SAMPLE_S = 0.25

# How long a previous measurement leaves psutil's handles usable as a
# starting point. Longer than this and the window would be so wide the
# numbers stop describing "now".
_CACHE_WARM_S = 120.0
_last_sampled_at = 0.0


def list_processes(limit: int = 25, sort_by: str = "cpu") -> List[Dict[str, Any]]:
    """The processes actually costing something, newest measurement first.

    Measured rather than guessed, and deliberately frugal about which
    per-process fields it asks for. On Windows the cost is wildly uneven:
    walking the list is 0.01s, adding `memory_info` is 1.7s, and adding
    `status` is 6.8s. `status` was being fetched for several hundred
    processes and then never displayed -- five seconds of an empty panel for
    a field nobody reads.
    """
    try:
        import psutil
    except ImportError:
        return []

    cores = psutil.cpu_count() or 1

    handles = []
    for process in psutil.process_iter():
        handles.append(process)

    # psutil hands back the same Process objects each time, so a recent call
    # has already left a starting point on them and the measurement window is
    # simply the time since. Priming again would cost a second pass -- 1.7s
    # here, as much as the measurement itself -- to learn nothing new.
    global _last_sampled_at
    now = time.monotonic()
    if now - _last_sampled_at > _CACHE_WARM_S:
        for process in handles:
            try:
                process.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(_CPU_SAMPLE_S)    # runs in an executor, never on the loop
    _last_sampled_at = time.monotonic()

    rows: List[Dict[str, Any]] = []
    for process in handles:
        try:
            name = process.name()
            if name.lower() in _IDLE_PROCESSES:
                continue
            # psutil reports percent of one core, so a busy process on an
            # eight-thread machine reads 400% beside a system-wide figure of
            # 50%. Dividing by the core count puts them on one scale, which
            # is the only way the column means anything next to the others.
            rows.append({
                "pid": process.pid,
                "name": name,
                "cpu": round((process.cpu_percent(None) or 0.0) / cores, 1),
                "handle": process,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if sort_by == "memory":
        # Sorting by memory needs it for everything, so pay for it once.
        for row in rows:
            row["memory_mb"] = _rss_mb(row["handle"])
        rows.sort(key=lambda r: r["memory_mb"], reverse=True)
    else:
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        rows = rows[:limit]
        for row in rows:                  # only the ones being returned
            row["memory_mb"] = _rss_mb(row["handle"])

    for row in rows:
        row.pop("handle", None)
    return rows[:limit]


def _rss_mb(process) -> float:
    try:
        return round(process.memory_info().rss / 1_048_576, 1)
    except Exception:
        return 0.0
