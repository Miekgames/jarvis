"""
Gaming layer.

Detects when a game is actually running (as opposed to a launcher sitting in
the tray), tracks playtime, samples performance during the session, switches to
gaming mode automatically if you allow it, and writes a session summary when
you stop.

Frame rate is deliberately *not* claimed: measuring FPS needs an overlay or
PresentMon-class instrumentation that this build does not ship. The session
report gives GPU load, GPU temperature and CPU instead, and says plainly that
FPS is unavailable rather than inventing a number.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.events import bus
from ..core.util import humanize_duration, new_id, now_iso, parse_iso, since, truncate, utcnow
from .awareness import _LAUNCHERS, categorise

logger = logging.getLogger("jarvis.gaming")

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

POLL_SECONDS = 10
# A game has to be foreground for this long before it counts as a session --
# stops a menu glance from creating a record.
MIN_SESSION_SECONDS = 90

# Heuristics for spotting a game that isn't in the known list: a big-memory,
# GPU-heavy, fullscreen-ish process that isn't a browser or a known app.
_MIN_GAME_MEMORY_MB = 700


class GamingService:
    def __init__(self, db, settings_store, activity, awareness, metrics, modes=None,
                 notifications=None, orchestrator=None):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self.awareness = awareness
        self.metrics = metrics
        self.modes = modes
        self.notifications = notifications
        self.orchestrator = orchestrator

        self._task: Optional[asyncio.Task] = None
        self._running = False

        self._session: Optional[Dict[str, Any]] = None
        self._samples: List[Dict[str, float]] = []
        self._previous_mode: Optional[str] = None

    # ==================================================================
    async def start(self) -> None:
        if self._running or psutil is None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="gaming")
        logger.info("gaming service started")

    async def stop(self) -> None:
        self._running = False
        await self._end_session(crashed=False)
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

    async def settings(self) -> Dict[str, Any]:
        return await self.settings_store.get("gaming", {})

    # ==================================================================
    async def _loop(self) -> None:
        while self._running:
            try:
                config = await self.settings()
                if config.get("enabled", True):
                    await self._tick(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gaming tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self, config: Dict[str, Any]) -> None:
        detected = await self.detect(config)

        if detected and not self._session:
            await self._begin_session(detected, config)
        elif detected and self._session:
            if detected["process"] != self._session["process"]:
                await self._end_session(crashed=False)
                await self._begin_session(detected, config)
            else:
                self._sample()
        elif self._session and not detected:
            # Distinguish a quit from a crash: if the process is gone entirely
            # while the session was very short, treat it as a crash.
            still_alive = _process_alive(self._session["process"])
            await self._end_session(crashed=not still_alive and self._elapsed() < 120)

    def _elapsed(self) -> int:
        return int(time.monotonic() - self._session["started_monotonic"]) if self._session else 0

    # ==================================================================
    async def detect(self, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Is a game in the foreground right now?"""
        config = config or await self.settings()
        snapshot = self.awareness.snapshot() if self.awareness else {}
        app = (snapshot or {}).get("active_app")
        if not app:
            return None

        stem = app.lower().replace(".exe", "")
        extra = {str(g).lower() for g in (config.get("known_games") or [])}
        ignored = {str(g).lower() for g in (config.get("ignore") or [])}
        if stem in ignored:
            return None

        # A launcher in the foreground is not a game.
        if stem in _LAUNCHERS:
            return None

        is_known = categorise(app) == "gaming" or stem in extra
        if is_known:
            return {"game": _pretty(app), "process": app, "confidence": "known"}

        if config.get("heuristic_detection", True) and _looks_like_a_game(app):
            return {"game": _pretty(app), "process": app, "confidence": "heuristic"}
        return None

    # ==================================================================
    async def _begin_session(self, detected: Dict[str, Any], config: Dict[str, Any]) -> None:
        self._session = {
            "id": new_id("game"),
            "game": detected["game"],
            "process": detected["process"],
            "started_at": now_iso(),
            "started_monotonic": time.monotonic(),
            "confidence": detected["confidence"],
        }
        self._samples = []
        logger.info("game detected: %s (%s)", detected["game"], detected["confidence"])
        bus.publish("gaming.started", {"game": detected["game"], "process": detected["process"]},
                    source="gaming")

        if config.get("auto_mode", False) and self.modes:
            self._previous_mode = await self.settings_store.get("active_mode", "normal")
            if self._previous_mode != "gaming":
                try:
                    await self.modes.activate("gaming")
                    await self.activity.record(
                        f"{detected['game']} detected -- switched to gaming mode.", source="gaming")
                except Exception:
                    logger.exception("could not switch to gaming mode")

    def _sample(self) -> None:
        if self.metrics is None:
            return
        latest = self.metrics.latest()
        if latest:
            self._samples.append({
                "cpu": float(latest.get("cpu") or 0),
                "gpu": float(latest.get("gpu") or 0),
                "gpu_temp": float(latest.get("gpu_temp") or 0),
                "mem": float(latest.get("mem") or 0),
            })

    async def _end_session(self, crashed: bool) -> None:
        if not self._session:
            return
        session = self._session
        self._session = None
        seconds = int(time.monotonic() - session["started_monotonic"])

        if seconds < MIN_SESSION_SECONDS and not crashed:
            self._samples = []
            return

        def mean(key: str) -> Optional[float]:
            values = [s[key] for s in self._samples if s.get(key)]
            return round(sum(values) / len(values), 1) if values else None

        def peak(key: str) -> Optional[float]:
            values = [s[key] for s in self._samples if s.get(key)]
            return round(max(values), 1) if values else None

        await self.db.execute(
            "INSERT INTO game_sessions (id, game, process, started_at, ended_at, seconds,"
            " avg_cpu, avg_gpu, max_gpu_temp, crashed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session["id"], session["game"], session["process"], session["started_at"],
             now_iso(), seconds, mean("cpu"), mean("gpu"), peak("gpu_temp"),
             1 if crashed else 0),
        )

        bus.publish(
            "gaming.ended",
            {"game": session["game"], "seconds": seconds, "crashed": crashed},
            source="gaming",
        )
        await self.activity.record(
            f"{session['game']} session ended after {humanize_duration(seconds)}"
            + (" (it appears to have crashed)" if crashed else "."),
            level="warn" if crashed else "info",
            source="gaming",
        )

        if crashed and self.notifications:
            await self.notifications.create(
                title=f"{session['game']} may have crashed",
                body=f"It closed after only {humanize_duration(seconds)}.",
                priority="normal", source="gaming",
            )

        config = await self.settings()
        if config.get("auto_mode", False) and self.modes and self._previous_mode:
            try:
                await self.modes.activate(self._previous_mode)
            except Exception:
                pass
            self._previous_mode = None

        self._samples = []
        asyncio.create_task(self._summarise(session["id"]))

    # ==================================================================
    async def _summarise(self, session_id: str) -> None:
        """Written after the fact so it never delays mode restoration."""
        if self.orchestrator is None:
            return
        row = await self.db.fetch_one("SELECT * FROM game_sessions WHERE id = ?", (session_id,))
        if not row:
            return

        total = await self.playtime(row["game"])
        facts = {
            "game": row["game"],
            "this_session": humanize_duration(row["seconds"]),
            "avg_cpu_percent": row["avg_cpu"],
            "avg_gpu_percent": row["avg_gpu"],
            "peak_gpu_temp_c": row["max_gpu_temp"],
            "crashed": bool(row["crashed"]),
            "total_playtime": humanize_duration(total["total_seconds"]),
            "sessions_all_time": total["sessions"],
            "note": "Frame rate is not measured by this build.",
        }
        try:
            summary = await self.orchestrator.ask(
                "Write a two-sentence gaming session summary from these facts. Mention thermals "
                "only if the peak GPU temperature is above 80. Do not mention or estimate FPS.\n\n"
                f"{facts}",
                purpose="fast", max_tokens=250,
            )
            await self.db.execute(
                "UPDATE game_sessions SET summary = ? WHERE id = ?", (summary, session_id))
        except Exception:
            logger.debug("gaming summary unavailable")

    # ==================================================================
    # reads
    # ==================================================================
    async def playtime(self, game: Optional[str] = None) -> Dict[str, Any]:
        if game:
            row = await self.db.fetch_one(
                "SELECT COUNT(*) AS sessions, SUM(seconds) AS total FROM game_sessions"
                " WHERE game = ?", (game,),
            )
            return {"game": game, "sessions": row["sessions"] or 0,
                    "total_seconds": row["total"] or 0}

        rows = await self.db.fetch_all(
            "SELECT game, COUNT(*) AS sessions, SUM(seconds) AS total,"
            " MAX(started_at) AS last_played FROM game_sessions"
            " GROUP BY game ORDER BY total DESC LIMIT 25"
        )
        return {
            "library": [
                {"game": r["game"], "sessions": r["sessions"],
                 "total_seconds": r["total"] or 0,
                 "hours": round((r["total"] or 0) / 3600, 1),
                 "last_played": r["last_played"]}
                for r in rows
            ],
            "total_seconds": sum(r["total"] or 0 for r in rows),
        }

    async def sessions(self, limit: int = 20, game: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM game_sessions"
        params: List[Any] = []
        if game:
            sql += " WHERE game = ?"
            params.append(game)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.db.fetch_all(sql, params)]

    async def current(self) -> Optional[Dict[str, Any]]:
        if not self._session:
            return None
        return {
            "game": self._session["game"],
            "process": self._session["process"],
            "elapsed": humanize_duration(self._elapsed()),
            "elapsed_seconds": self._elapsed(),
            "confidence": self._session["confidence"],
            "samples": len(self._samples),
        }

    async def stats(self) -> Dict[str, Any]:
        config = await self.settings()
        return {
            "running": self.running,
            "enabled": config.get("enabled", True),
            "auto_mode": config.get("auto_mode", False),
            "current": await self.current(),
            "sessions_recorded": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM game_sessions", default=0)),
        }


# ---------------------------------------------------------------------------
def _pretty(process_name: str) -> str:
    stem = process_name.replace(".exe", "").replace("_", " ").replace("-", " ")
    return stem.title() if stem.islower() or stem.isupper() else stem


def _process_alive(name: str) -> bool:
    if psutil is None:
        return False
    lowered = name.lower()
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == lowered:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# Where games actually live. An executable's install location is the one
# cheap signal that genuinely separates a game from everything else large.
_GAME_LIBRARIES: Tuple[str, ...] = (
    r"\steamapps\common",
    r"\steam\steamapps",
    r"\epic games",
    r"\gog galaxy\games",
    r"\gog games",
    r"\riot games",
    r"\battle.net",
    r"\ea games",
    r"\ea desktop",
    r"\origin games",
    r"\ubisoft\ubisoft game launcher",
    r"\xboxgames",
    r"\windowsapps\microsoft.xbox",
    r"\rockstar games",
    r"\minecraft",
)


def _in_a_game_library(path: str) -> bool:
    lowered = (path or "").lower().replace("/", "\\")
    return any(marker in lowered for marker in _GAME_LIBRARIES)


def _looks_like_a_game(name: str) -> bool:
    """Heuristic for unlisted games.

    Memory alone is not evidence. The previous rule was "uses more than
    700 MB and is not in a known non-game category", which in practice means
    every Electron application on the machine: this assistant's own desktop
    app was being detected as a game, repeatedly, while the user worked --
    exactly the false positive the rule was meant to avoid.

    So large memory is only the cheap pre-filter now, and the decision rests
    on where the executable actually lives. Games are installed by launchers
    into recognisable places; Slack and VS Code are not. If the path cannot
    be read, that is not evidence either, and the answer is no.
    """
    if psutil is None:
        return False
    if categorise(name) not in ("other", "gaming"):
        return False

    lowered = name.lower()
    for process in psutil.process_iter(["name", "memory_info", "exe"]):
        try:
            if (process.info.get("name") or "").lower() != lowered:
                continue
            memory = process.info.get("memory_info")
            if not memory or memory.rss / 1_048_576 < _MIN_GAME_MEMORY_MB:
                continue
            if _in_a_game_library(process.info.get("exe") or ""):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False
