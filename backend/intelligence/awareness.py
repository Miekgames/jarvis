"""
Ambient awareness — the spine of the Intelligence Layer.

Tracks what you are actually doing: which application has focus, what its
window is called, how long you have been in it, whether you are idle, and which
applications are eating the machine. That context feeds the AI's prompt, the
proactive engine, gaming detection and automation triggers.

Privacy: window titles are the only text captured, they never leave the machine
unless you send them to a cloud model in a question, and the whole service is
switched off by a single setting.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ..core.proc import run as _run, popen as _popen
from ..core.events import bus
from ..core.util import iso, new_id, now_iso, parse_iso, since, truncate, utcnow

logger = logging.getLogger("jarvis.awareness")

IS_WINDOWS = platform.system() == "Windows"

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

# Rough categorisation drives mode suggestions and the productivity report.
CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "development": ("code", "devenv", "pycharm", "idea", "webstorm", "sublime", "windowsterminal",
                    "cmd", "powershell", "wt", "git", "docker", "rider", "clion", "neovim", "vim"),
    "browser": ("chrome", "firefox", "msedge", "brave", "opera", "arc", "safari", "zen"),
    "communication": ("discord", "slack", "teams", "zoom", "skype", "telegram", "whatsapp",
                      "signal", "thunderbird", "outlook"),
    "gaming": ("steam", "epicgameslauncher", "riotclient", "leagueoflegends", "valorant",
               "battle.net", "gog", "eadesktop", "ubisoftconnect", "minecraft", "csgo", "cs2",
               "dota2", "fortniteclient", "rocketleague", "gtav", "eldenring", "cyberpunk2077"),
    "media": ("spotify", "vlc", "mpc-hc", "musicbee", "foobar2000", "itunes", "plex", "obs"),
    "productivity": ("winword", "excel", "powerpnt", "onenote", "notion", "obsidian", "notepad",
                     "acrord32", "sumatrapdf", "libreoffice"),
    "creative": ("photoshop", "illustrator", "blender", "premiere", "afterfx", "figma",
                 "krita", "gimp", "davinciresolve", "unity", "unrealeditor"),
    "system": ("explorer", "taskmgr", "systemsettings", "control", "regedit", "mmc"),
}

# Anything in the gaming category that is a launcher rather than a game.
_LAUNCHERS = {"steam", "epicgameslauncher", "battle.net", "gog", "eadesktop", "ubisoftconnect",
              "riotclient"}

IDLE_THRESHOLD_SECONDS = 180
MIN_SESSION_SECONDS = 4          # ignore alt-tab flicker
POLL_SECONDS = 3


def categorise(app: str) -> str:
    stem = app.lower().replace(".exe", "")
    for category, markers in CATEGORIES.items():
        if any(marker in stem for marker in markers):
            return category
    return "other"


# ---------------------------------------------------------------------------
# platform probes
# ---------------------------------------------------------------------------
def foreground_window() -> Optional[Dict[str, Any]]:
    """Return {'app', 'title', 'pid'} for the focused window, or None."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            handle = user32.GetForegroundWindow()
            if not handle:
                return None

            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))

            app = ""
            if psutil is not None and pid.value:
                try:
                    app = psutil.Process(pid.value).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    app = ""
            return {"app": app, "title": buffer.value, "pid": pid.value}
        except Exception as exc:  # pragma: no cover - platform specific
            logger.debug("foreground probe failed: %s", exc)
            return None

    if platform.system() == "Darwin":
        import subprocess


        try:
            result = _run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first application process '
                 'whose frontmost is true'],
                capture_output=True, text=True, timeout=3,
            )
            name = result.stdout.strip()
            return {"app": name, "title": name, "pid": 0} if name else None
        except Exception:
            return None
    return None


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    if not IS_WINDOWS:
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:  # pragma: no cover
        return 0.0


def window_responding(pid: int) -> bool:
    """False when a window has stopped pumping messages -- i.e. it has hung."""
    if not IS_WINDOWS or not pid:
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        # SMTO_ABORTIFHUNG (0x0002) with a short timeout is the standard probe.
        result = ctypes.c_ulong()
        ok = user32.SendMessageTimeoutW(handle, 0x0000, 0, 0, 0x0002, 250,
                                        ctypes.byref(result))
        return bool(ok)
    except Exception:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
class AwarenessService:
    def __init__(self, db, settings, settings_store, activity, notifications=None, metrics=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.activity = activity
        self.notifications = notifications
        self.metrics = metrics
        # Attached by the container once FocusService exists, so observed
        # foreground seconds also land in the per-day time report.
        self.focus = None

        self._task: Optional[asyncio.Task] = None
        self._running = False

        self.current: Optional[Dict[str, Any]] = None
        self._session_id: Optional[str] = None
        self._session_started = 0.0
        self._was_idle = False
        self._known_pids: Dict[str, int] = {}
        self._hang_strikes: Dict[str, int] = defaultdict(int)
        self._reported_hangs: set = set()

    # ==================================================================
    async def start(self) -> None:
        if self._running or psutil is None:
            if psutil is None:
                logger.warning("psutil unavailable -- awareness disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="awareness")
        logger.info("awareness service started")

    async def stop(self) -> None:
        self._running = False
        await self._close_session()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def enabled(self) -> bool:
        awareness = await self.settings_store.get("awareness", {})
        return bool(awareness.get("enabled", True))

    # ==================================================================
    async def _loop(self) -> None:
        while self._running:
            try:
                if await self.enabled():
                    await self._tick()
                elif self._session_id:
                    await self._close_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("awareness tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self) -> None:
        loop = asyncio.get_event_loop()
        window = await loop.run_in_executor(None, foreground_window)
        idle = await loop.run_in_executor(None, idle_seconds)

        is_idle = idle >= IDLE_THRESHOLD_SECONDS
        if is_idle != self._was_idle:
            self._was_idle = is_idle
            bus.publish("awareness.idle" if is_idle else "awareness.active",
                        {"idle_seconds": idle}, source="awareness")
            if is_idle:
                await self._close_session()

        if is_idle or not window or not window.get("app"):
            return

        app = window["app"]
        title = truncate(window.get("title", ""), 300)

        # -- focus change --------------------------------------------------
        changed = self.current is None or self.current.get("app") != app
        title_changed = self.current is not None and self.current.get("title") != title

        if changed:
            await self._close_session()
            self.current = {"app": app, "title": title, "pid": window.get("pid", 0),
                            "category": categorise(app), "since": now_iso()}
            self._session_id = new_id("foc")
            self._session_started = time.monotonic()
            await self.db.execute(
                "INSERT INTO focus_sessions (id, app, title, category, started_at)"
                " VALUES (?,?,?,?,?)",
                (self._session_id, app, title, self.current["category"], now_iso()),
            )
            await self._touch_app(app, launched=app not in self._known_pids)
            self._known_pids[app] = window.get("pid", 0)
            bus.publish("awareness.focus", self.current, source="awareness")
        elif title_changed:
            self.current["title"] = title
            if self._session_id:
                await self.db.execute(
                    "UPDATE focus_sessions SET title = ? WHERE id = ?", (title, self._session_id)
                )
            bus.publish("awareness.title", self.current, source="awareness")

        await self._check_hang(window)

    # ==================================================================
    async def _close_session(self) -> None:
        if not self._session_id:
            return
        seconds = int(time.monotonic() - self._session_started)
        if seconds >= MIN_SESSION_SECONDS:
            await self.db.execute(
                "UPDATE focus_sessions SET ended_at = ?, seconds = ? WHERE id = ?",
                (now_iso(), seconds, self._session_id),
            )
            if self.current:
                await self.db.execute(
                    "UPDATE app_stats SET total_seconds = total_seconds + ?, last_seen = ?"
                    " WHERE app = ?",
                    (seconds, now_iso(), self.current["app"]),
                )
                # Feed the per-day time report. Attached by the container; the
                # daily breakdown is these same observed seconds, bucketed by
                # day, so the two can never disagree.
                if self.focus is not None:
                    try:
                        await self.focus.record_time(self.current["app"], seconds)
                    except Exception:
                        logger.debug("time-block write failed", exc_info=True)
        else:
            # Too short to be meaningful -- don't pollute the statistics.
            await self.db.execute("DELETE FROM focus_sessions WHERE id = ?", (self._session_id,))
        self._session_id = None

    async def _touch_app(self, app: str, launched: bool) -> None:
        existing = await self.db.fetch_one("SELECT app FROM app_stats WHERE app = ?", (app,))
        if existing:
            if launched:
                await self.db.execute(
                    "UPDATE app_stats SET launches = launches + 1, last_seen = ? WHERE app = ?",
                    (now_iso(), app),
                )
        else:
            await self.db.execute(
                "INSERT INTO app_stats (app, category, launches, last_seen, first_seen)"
                " VALUES (?,?,?,?,?)",
                (app, categorise(app), 1, now_iso(), now_iso()),
            )

    # ==================================================================
    async def _check_hang(self, window: Dict[str, Any]) -> None:
        """Three consecutive non-responses before reporting -- a single slow
        frame is normal, a hung window is not."""
        app = window.get("app", "")
        if not app:
            return
        responding = await asyncio.get_event_loop().run_in_executor(
            None, window_responding, window.get("pid", 0)
        )
        if responding:
            self._hang_strikes[app] = 0
            self._reported_hangs.discard(app)
            return

        self._hang_strikes[app] += 1
        if self._hang_strikes[app] >= 3 and app not in self._reported_hangs:
            self._reported_hangs.add(app)
            message = f"{app} has stopped responding."
            logger.info(message)
            bus.publish("awareness.app_hung", {"app": app, "pid": window.get("pid")},
                        source="awareness")
            await self.activity.record(message, level="warn", source="awareness")
            if self.notifications:
                await self.notifications.create(
                    title=f"{app} is not responding",
                    body="Ask me to close it, or wait for it to recover.",
                    priority="normal", source="awareness",
                )

    # ==================================================================
    # reads
    # ==================================================================
    def snapshot(self) -> Dict[str, Any]:
        """Synchronous, for the prompt builder."""
        if not self.current:
            return {"active_app": None, "idle": self._was_idle}
        elapsed = int(time.monotonic() - self._session_started)
        return {
            "active_app": self.current["app"],
            "window_title": self.current["title"],
            "category": self.current["category"],
            "focused_for_seconds": elapsed,
            "idle": self._was_idle,
        }

    async def usage(self, hours: int = 24, limit: int = 15) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT app, category, SUM(seconds) AS seconds, COUNT(*) AS sessions"
            " FROM focus_sessions WHERE started_at > ? AND seconds > 0"
            " GROUP BY app ORDER BY seconds DESC LIMIT ?",
            (since(hours * 3600), limit),
        )
        total = sum(int(r["seconds"] or 0) for r in rows) or 1
        return [
            {**dict(r), "minutes": round((r["seconds"] or 0) / 60, 1),
             "percent": round(100 * (r["seconds"] or 0) / total, 1)}
            for r in rows
        ]

    async def by_category(self, hours: int = 24) -> Dict[str, float]:
        rows = await self.db.fetch_all(
            "SELECT category, SUM(seconds) AS seconds FROM focus_sessions"
            " WHERE started_at > ? GROUP BY category",
            (since(hours * 3600),),
        )
        return {r["category"]: round((r["seconds"] or 0) / 60, 1) for r in rows}

    async def timeline(self, hours: int = 8, limit: int = 100) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT app, title, category, started_at, seconds FROM focus_sessions"
            " WHERE started_at > ? AND seconds > 0 ORDER BY started_at DESC LIMIT ?",
            (since(hours * 3600), limit),
        )

    # ==================================================================
    async def resource_hogs(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Which processes are actually costing you right now."""
        if psutil is None:
            return []

        def _scan() -> List[Dict[str, Any]]:
            processes = []
            for process in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    process.cpu_percent(None)  # prime
                    processes.append(process)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            time.sleep(0.4)

            scored = []
            for process in processes:
                try:
                    cpu = process.cpu_percent(None)
                    memory = process.info.get("memory_info")
                    scored.append({
                        "pid": process.info["pid"],
                        "name": process.info.get("name") or "?",
                        "cpu": round(cpu, 1),
                        "memory_mb": round((memory.rss / 1_048_576) if memory else 0, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            # Weight CPU over memory: a 4 GB idle process is less of a problem
            # than one pinning a core.
            scored.sort(key=lambda p: p["cpu"] * 12 + p["memory_mb"] / 100, reverse=True)
            return scored[:limit]

        return await asyncio.get_event_loop().run_in_executor(None, _scan)

    async def recommendations(self) -> List[Dict[str, Any]]:
        """Concrete, evidence-backed performance advice -- never vague."""
        advice: List[Dict[str, Any]] = []
        latest = self.metrics.latest() if self.metrics else {}
        hogs = await self.resource_hogs(limit=6)

        memory = latest.get("mem")
        if memory and memory > 85:
            worst = max(hogs, key=lambda p: p["memory_mb"], default=None)
            if worst and worst["memory_mb"] > 800:
                advice.append({
                    "issue": f"Memory is at {memory}%",
                    "cause": f"{worst['name']} is holding {worst['memory_mb']:.0f} MB",
                    "action": f"Close {worst['name']} if you're not using it",
                    "tool": {"name": "close_app", "arguments": {"name": worst["name"]}},
                })

        cpu = latest.get("cpu")
        if cpu and cpu > 85:
            worst = max(hogs, key=lambda p: p["cpu"], default=None)
            if worst and worst["cpu"] > 25:
                advice.append({
                    "issue": f"CPU is at {cpu}%",
                    "cause": f"{worst['name']} is using {worst['cpu']}%",
                    "action": f"Check what {worst['name']} is doing",
                })

        disk = latest.get("disk")
        if disk and disk > 90:
            advice.append({
                "issue": f"Disk is {disk}% full ({latest.get('disk_free_gb', '?')} GB free)",
                "cause": "Low headroom slows writes and can break updates",
                "action": "Run a duplicate-file scan or clear the downloads folder",
                "tool": {"name": "find_duplicate_files", "arguments": {}},
            })

        temperature = latest.get("gpu_temp")
        if temperature and temperature > 82:
            advice.append({
                "issue": f"GPU is at {temperature}°C",
                "cause": "Sustained high temperature",
                "action": "Check case airflow, or cap the frame rate",
            })

        return advice

    async def stats(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "enabled": await self.enabled(),
            "current": self.snapshot(),
            "tracked_apps": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM app_stats", default=0)),
            "sessions_today": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM focus_sessions WHERE started_at > ?",
                (since(86400),), default=0)),
        }
