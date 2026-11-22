"""
Focus sessions, time tracking, habits and wellbeing.

The numbers here are counted, never estimated. Time per app comes from the
presence service's real observations of the foreground window; a focus score is
arithmetic over those seconds; a streak is a walk backwards through actual
entries. Nothing in this module asks a model what your day looked like.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.focus")

# Categories used to split "work" from "distraction" in the focus score. Apps
# JARVIS has never seen are 'other' and count as neither, rather than being
# guessed into one bucket.
DEFAULT_CATEGORIES: Dict[str, List[str]] = {
    "development": ["code", "studio", "pycharm", "webstorm", "vim", "terminal", "powershell",
                    "cmd", "windowsterminal", "sublime", "atom", "idea"],
    "browsing": ["chrome", "firefox", "edge", "safari", "brave", "opera", "arc"],
    "communication": ["slack", "teams", "discord", "zoom", "outlook", "thunderbird", "mail"],
    "writing": ["word", "notion", "obsidian", "notepad", "typora", "docs"],
    "entertainment": ["spotify", "vlc", "netflix", "steam", "epicgames", "youtube", "twitch"],
    "design": ["figma", "photoshop", "illustrator", "blender", "gimp", "inkscape"],
}

PRODUCTIVE = {"development", "writing", "design"}
DISTRACTING = {"entertainment"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def categorise(app: str) -> str:
    """Bucket an application name. Unknown apps stay 'other' on purpose."""
    name = (app or "").lower().replace(".exe", "").strip()
    if not name:
        return "other"
    for category, needles in DEFAULT_CATEGORIES.items():
        for needle in needles:
            if needle in name:
                return category
    return "other"


def humanise(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return "{}h {}m".format(hours, minutes)
    if hours:
        return "{}h".format(hours)
    if minutes:
        return "{}m".format(minutes)
    return "{}s".format(seconds)


class FocusService:
    """Sessions, time blocks, habits and wellbeing logs."""

    def __init__(self, db, activity=None, notifications=None):
        self.db = db
        self.activity = activity
        self.notifications = notifications

    # ==================================================================
    # focus sessions
    # ==================================================================
    async def start_session(self, goal: str = "", minutes: int = 25,
                            kind: str = "focus") -> Dict[str, Any]:
        running = await self.current_session()
        if running:
            raise ValueError(
                "A {} session is already running ({} left). Finish or cancel it first.".format(
                    running["kind"], humanise(running["remaining_seconds"])))

        minutes = max(1, min(int(minutes or 25), 480))
        session_id = "focus_" + uuid.uuid4().hex[:10]
        await self.db.execute(
            "INSERT INTO pomodoro_sessions (id, kind, goal, planned_min, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (session_id, kind, (goal or "").strip(), minutes, _now()))
        if self.activity:
            await self.activity.record(
                "{} session started ({} min){}".format(
                    kind, minutes, ": " + goal if goal else ""),
                level="info", source="focus")
        return await self.get_session(session_id)

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one(
            "SELECT * FROM pomodoro_sessions WHERE id = ?", (session_id,))
        if not record:
            raise LookupError("No focus session with id {}.".format(session_id))
        return self._decorate(record)

    def _decorate(self, record: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(record)
        started = _parse(record["started_at"])
        ended = _parse(record["ended_at"]) if record["ended_at"] else None
        reference = ended or datetime.now(timezone.utc)
        elapsed = max(0, (reference - started).total_seconds()) if started else 0
        planned = record["planned_min"] * 60
        out["elapsed_seconds"] = int(elapsed)
        out["elapsed"] = humanise(elapsed)
        out["remaining_seconds"] = int(max(0, planned - elapsed))
        out["remaining"] = humanise(max(0, planned - elapsed))
        out["overrun"] = elapsed > planned
        out["progress"] = min(1.0, elapsed / planned) if planned else 0.0
        return out

    async def current_session(self) -> Optional[Dict[str, Any]]:
        record = await self.db.fetch_one(
            "SELECT * FROM pomodoro_sessions WHERE status = 'running' "
            "ORDER BY started_at DESC LIMIT 1")
        return self._decorate(record) if record else None

    async def end_session(self, status: str = "completed",
                          notes: str = "") -> Dict[str, Any]:
        running = await self.current_session()
        if not running:
            return {"ok": False, "message": "No focus session is running."}

        await self.db.execute(
            "UPDATE pomodoro_sessions SET ended_at = ?, status = ?, notes = ? WHERE id = ?",
            (_now(), status, notes or "", running["id"]))
        finished = await self.get_session(running["id"])
        if self.activity:
            await self.activity.record(
                "focus session {} after {}".format(status, finished["elapsed"]),
                level="info", source="focus")
        if self.notifications and status == "completed":
            await self.notifications.push(
                "Focus session finished",
                "{} of focus{}".format(finished["elapsed"],
                                       " on " + finished["goal"] if finished["goal"] else ""),
                priority="normal", category="focus")
        return {"ok": True, "session": finished}

    async def interrupt(self, app: str = "") -> Dict[str, Any]:
        """Record a distraction against the running session."""
        running = await self.current_session()
        if not running:
            return {"ok": False, "message": "No focus session is running."}
        await self.db.execute(
            "UPDATE pomodoro_sessions SET interruptions = interruptions + 1 WHERE id = ?",
            (running["id"],))
        await self.db.execute(
            "INSERT INTO distractions (session_id, app, at) VALUES (?, ?, ?)",
            (running["id"], app or "unknown", _now()))
        return {"ok": True, "interruptions": running["interruptions"] + 1}

    async def sessions(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pomodoro_sessions ORDER BY started_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),))
        return [self._decorate(r) for r in rows]

    async def session_streak(self) -> Dict[str, Any]:
        """Consecutive days ending today with at least one completed session."""
        rows = await self.db.fetch_all(
            "SELECT DISTINCT date(started_at) AS day FROM pomodoro_sessions "
            "WHERE status = 'completed' ORDER BY day DESC")
        days = {r["day"] for r in rows if r["day"]}
        streak = 0
        cursor = datetime.now().date()
        # Today not yet counted should not break a run built yesterday.
        if cursor.isoformat() not in days:
            cursor -= timedelta(days=1)
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)
        return {"streak_days": streak, "total_days": len(days)}

    # ==================================================================
    # time tracking
    # ==================================================================
    async def record_time(self, app: str, seconds: int, day: str = "") -> None:
        """Add observed foreground seconds for an app. Called by presence."""
        if not app or seconds <= 0:
            return
        day = day or _today()
        await self.db.execute(
            "INSERT INTO time_blocks (day, app, category, seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day, app) DO UPDATE SET seconds = seconds + excluded.seconds",
            (day, app, categorise(app), int(seconds)))

    async def time_report(self, day: str = "") -> Dict[str, Any]:
        day = day or _today()
        rows = await self.db.fetch_all(
            "SELECT app, category, seconds FROM time_blocks WHERE day = ? "
            "ORDER BY seconds DESC", (day,))
        total = sum(r["seconds"] for r in rows)

        by_category: Dict[str, int] = {}
        for row in rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + row["seconds"]

        return {
            "day": day,
            "tracked_seconds": total,
            "tracked": humanise(total),
            "apps": [{"app": r["app"], "category": r["category"], "seconds": r["seconds"],
                      "time": humanise(r["seconds"]),
                      "share": round(r["seconds"] / total, 3) if total else 0.0}
                     for r in rows[:25]],
            "by_category": [{"category": c, "seconds": s, "time": humanise(s),
                             "share": round(s / total, 3) if total else 0.0}
                            for c, s in sorted(by_category.items(), key=lambda kv: -kv[1])],
            "measured": bool(rows),
            "note": "" if rows else "No foreground activity has been recorded for this day.",
        }

    async def focus_score(self, day: str = "") -> Dict[str, Any]:
        """Productive share of categorised time.

        Uncategorised time is excluded from both halves rather than counted as
        neutral, so the score describes the time it can actually speak to.
        """
        report = await self.time_report(day)
        productive = sum(c["seconds"] for c in report["by_category"]
                         if c["category"] in PRODUCTIVE)
        distracting = sum(c["seconds"] for c in report["by_category"]
                          if c["category"] in DISTRACTING)
        judged = productive + distracting

        if not judged:
            return {"day": report["day"], "score": None,
                    "message": "Not enough categorised activity to score this day.",
                    "productive": humanise(productive), "distracting": humanise(distracting)}

        return {
            "day": report["day"],
            "score": round(productive / judged, 3),
            "productive_seconds": productive,
            "distracting_seconds": distracting,
            "productive": humanise(productive),
            "distracting": humanise(distracting),
            "uncategorised": humanise(report["tracked_seconds"] - judged),
            "basis": "productive / (productive + distracting), over categorised time only",
        }

    async def week_review(self) -> Dict[str, Any]:
        """Seven days of tracked time and completed sessions."""
        start = (datetime.now().date() - timedelta(days=6)).isoformat()
        rows = await self.db.fetch_all(
            "SELECT day, SUM(seconds) AS seconds FROM time_blocks WHERE day >= ? "
            "GROUP BY day ORDER BY day", (start,))
        sessions = await self.db.fetch_all(
            "SELECT date(started_at) AS day, COUNT(*) AS n FROM pomodoro_sessions "
            "WHERE status = 'completed' AND date(started_at) >= ? GROUP BY day", (start,))
        session_by_day = {s["day"]: s["n"] for s in sessions}

        days = []
        for offset in range(7):
            day = (datetime.now().date() - timedelta(days=6 - offset)).isoformat()
            seconds = next((r["seconds"] for r in rows if r["day"] == day), 0)
            days.append({"day": day, "seconds": seconds, "time": humanise(seconds),
                         "sessions": session_by_day.get(day, 0)})

        total = sum(d["seconds"] for d in days)
        active_days = len([d for d in days if d["seconds"] > 0])
        return {
            "from": start,
            "days": days,
            "total": humanise(total),
            "total_seconds": total,
            "daily_average": humanise(total / active_days) if active_days else "0s",
            "sessions": sum(d["sessions"] for d in days),
            "active_days": active_days,
        }

    # ==================================================================
    # habits
    # ==================================================================
    async def add_habit(self, name: str, cadence: str = "daily", target: float = 1,
                        unit: str = "", colour: str = "#5ad2ff") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A habit needs a name.")
        if cadence not in ("daily", "weekly"):
            raise ValueError("Cadence must be 'daily' or 'weekly'.")

        existing = await self.db.fetch_one(
            "SELECT id FROM habits WHERE lower(name) = lower(?) AND archived = 0", (name,))
        if existing:
            raise ValueError("You already track a habit called '{}'.".format(name))

        habit_id = "habit_" + uuid.uuid4().hex[:10]
        await self.db.execute(
            "INSERT INTO habits (id, name, cadence, target, unit, colour, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (habit_id, name, cadence, float(target or 1), unit or "", colour, _now()))
        return await self.habit(habit_id)

    async def habit(self, habit_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT * FROM habits WHERE id = ?", (habit_id,))
        if not record:
            raise LookupError("No habit with id {}.".format(habit_id))
        record["archived"] = bool(record["archived"])
        return record

    async def resolve_habit(self, reference: str) -> Dict[str, Any]:
        record = await self.db.fetch_one(
            "SELECT * FROM habits WHERE id = ? OR lower(name) = lower(?) LIMIT 1",
            (reference, reference))
        if not record:
            record = await self.db.fetch_one(
                "SELECT * FROM habits WHERE name LIKE ? AND archived = 0 LIMIT 1",
                ("%" + (reference or "") + "%",))
        if not record:
            raise LookupError("No habit matching '{}'.".format(reference))
        record["archived"] = bool(record["archived"])
        return record

    async def check_in(self, reference: str, value: float = 1, day: str = "",
                       note: str = "") -> Dict[str, Any]:
        habit = await self.resolve_habit(reference)
        day = day or _today()
        await self.db.execute(
            "INSERT INTO habit_entries (habit_id, day, value, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(habit_id, day) DO UPDATE SET value = excluded.value, "
            "note = excluded.note",
            (habit["id"], day, float(value), note or ""))
        streak = await self.habit_streak(habit["id"])
        return {"habit": habit["name"], "day": day, "value": value,
                "streak_days": streak["streak_days"],
                "met_target": float(value) >= float(habit["target"])}

    async def habit_streak(self, habit_id: str) -> Dict[str, Any]:
        habit = await self.habit(habit_id)
        rows = await self.db.fetch_all(
            "SELECT day, value FROM habit_entries WHERE habit_id = ? ORDER BY day DESC",
            (habit_id,))
        met = {r["day"] for r in rows if float(r["value"]) >= float(habit["target"])}

        streak = 0
        cursor = datetime.now().date()
        if cursor.isoformat() not in met:
            cursor -= timedelta(days=1)
        while cursor.isoformat() in met:
            streak += 1
            cursor -= timedelta(days=1)

        best = 0
        run = 0
        previous = None
        for day in sorted(met):
            current = datetime.fromisoformat(day).date()
            run = run + 1 if previous and (current - previous).days == 1 else 1
            best = max(best, run)
            previous = current

        return {"habit": habit["name"], "streak_days": streak, "best_streak": best,
                "days_met": len(met), "days_logged": len(rows)}

    async def habits(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM habits {} ORDER BY created_at".format(
                "" if include_archived else "WHERE archived = 0"))
        today = _today()
        out = []
        for record in rows:
            record["archived"] = bool(record["archived"])
            entry = await self.db.fetch_one(
                "SELECT value FROM habit_entries WHERE habit_id = ? AND day = ?",
                (record["id"], today))
            streak = await self.habit_streak(record["id"])
            record["today"] = float(entry["value"]) if entry else 0.0
            record["done_today"] = bool(entry) and float(entry["value"]) >= float(record["target"])
            record["streak_days"] = streak["streak_days"]
            record["best_streak"] = streak["best_streak"]
            out.append(record)
        return out

    async def archive_habit(self, reference: str) -> Dict[str, Any]:
        habit = await self.resolve_habit(reference)
        await self.db.execute("UPDATE habits SET archived = 1 WHERE id = ?", (habit["id"],))
        return {"archived": True, "habit": habit["name"]}

    async def habit_history(self, reference: str, days: int = 30) -> Dict[str, Any]:
        habit = await self.resolve_habit(reference)
        start = (datetime.now().date() - timedelta(days=max(1, days) - 1)).isoformat()
        rows = await self.db.fetch_all(
            "SELECT day, value, note FROM habit_entries WHERE habit_id = ? AND day >= ? "
            "ORDER BY day", (habit["id"], start))
        by_day = {r["day"]: r for r in rows}

        series = []
        for offset in range(max(1, days)):
            day = (datetime.now().date() - timedelta(days=days - 1 - offset)).isoformat()
            entry = by_day.get(day)
            series.append({"day": day, "value": float(entry["value"]) if entry else 0.0,
                           "met": bool(entry) and float(entry["value"]) >= float(habit["target"])})

        met = len([s for s in series if s["met"]])
        return {"habit": habit["name"], "target": habit["target"], "unit": habit["unit"],
                "series": series, "days": len(series), "days_met": met,
                "rate": round(met / len(series), 3) if series else 0.0}

    # ==================================================================
    # wellbeing
    # ==================================================================
    async def log_wellbeing(self, kind: str, value: float, note: str = "",
                            day: str = "") -> Dict[str, Any]:
        kind = (kind or "").strip().lower()
        if kind not in ("mood", "energy", "sleep", "water", "steps"):
            raise ValueError(
                "Kind must be one of: mood, energy, sleep, water, steps.")
        day = day or _today()
        await self.db.execute(
            "INSERT INTO wellbeing (day, kind, value, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day, kind) DO UPDATE SET value = excluded.value, note = excluded.note",
            (day, kind, float(value), note or ""))
        return {"logged": True, "kind": kind, "value": value, "day": day}

    async def wellbeing(self, kind: str = "", days: int = 14) -> Dict[str, Any]:
        start = (datetime.now().date() - timedelta(days=max(1, days) - 1)).isoformat()
        if kind:
            rows = await self.db.fetch_all(
                "SELECT day, kind, value, note FROM wellbeing WHERE kind = ? AND day >= ? "
                "ORDER BY day", (kind, start))
        else:
            rows = await self.db.fetch_all(
                "SELECT day, kind, value, note FROM wellbeing WHERE day >= ? ORDER BY day",
                (start,))

        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(
                {"day": row["day"], "value": row["value"], "note": row["note"]})

        summary = {}
        for name, entries in by_kind.items():
            values = [e["value"] for e in entries]
            summary[name] = {
                "entries": len(values),
                "average": round(sum(values) / len(values), 2) if values else None,
                "latest": values[-1] if values else None,
            }
        return {"from": start, "series": by_kind, "summary": summary}

    async def correlate(self, reference: str, days: int = 21) -> Dict[str, Any]:
        """Compare a habit against tracked focus time on the same days.

        Reports a correlation only when there is enough data, and always says
        plainly that this is association, not cause.
        """
        habit = await self.resolve_habit(reference)
        start = (datetime.now().date() - timedelta(days=max(2, days) - 1)).isoformat()

        entries = await self.db.fetch_all(
            "SELECT day, value FROM habit_entries WHERE habit_id = ? AND day >= ?",
            (habit["id"], start))
        blocks = await self.db.fetch_all(
            "SELECT day, SUM(seconds) AS seconds FROM time_blocks WHERE day >= ? "
            "AND category IN ('development', 'writing', 'design') GROUP BY day", (start,))

        habit_by_day = {e["day"]: float(e["value"]) for e in entries}
        focus_by_day = {b["day"]: float(b["seconds"]) for b in blocks}
        shared = sorted(set(habit_by_day) & set(focus_by_day))

        if len(shared) < 5:
            return {"ok": False, "habit": habit["name"], "overlapping_days": len(shared),
                    "message": "Only {} day(s) have both a habit entry and tracked focus time. "
                               "At least 5 are needed before a comparison means anything.".format(
                                   len(shared))}

        xs = [habit_by_day[d] for d in shared]
        ys = [focus_by_day[d] for d in shared]
        r = _pearson(xs, ys)
        return {
            "ok": True,
            "habit": habit["name"],
            "overlapping_days": len(shared),
            "correlation": None if r is None else round(r, 3),
            "caveat": "This is association over {} days, not cause. A small sample can "
                      "produce a large number by chance.".format(len(shared)),
        }


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson's r, or None when a series has no variance (r is undefined)."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    numerator = sum(a * b for a, b in zip(dx, dy))
    denominator = (sum(a * a for a in dx) * sum(b * b for b in dy)) ** 0.5
    if denominator == 0:
        return None
    return numerator / denominator


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
