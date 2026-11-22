"""
Structured logging + the durable activity feed.

`setup_logging` configures stdlib logging (console + rotating JSON file).
`ActivityLog` writes the user-visible feed to the database and publishes it so
the dashboard updates live. Both run every message through `redact()` so
credentials never reach disk.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import Events, bus
from .util import now_iso, redact, truncate


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": now_iso(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for field in ("task_id", "tool", "action", "duration_ms", "session", "result"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))[:4000]
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[38;5;244m",
        "INFO": "\033[38;5;81m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[38;5;199m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%H:%M:%S")
        name = record.name.replace("jarvis.", "")
        message = redact(record.getMessage())
        line = f"{stamp}  {record.levelname:<7} {name:<18} {message}"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        if self.color:
            tint = self.COLORS.get(record.levelname, "")
            return f"{tint}{line}{self.RESET}"
        return line


def setup_logging(level: str = "INFO", log_dir: Optional[Path] = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter(color=sys.stdout.isatty()))
    root.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Third-party noise reduction
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class ActivityLog:
    """The feed the user actually reads: 'Calendar synchronised', 'Task #184
    started', 'AI provider unavailable -- switching to fallback'."""

    def __init__(self, db):
        self.db = db

    async def record(
        self,
        message: str,
        level: str = "info",
        source: str = "jarvis",
        meta: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> None:
        clean = redact(truncate(message, 1000))
        await self.db.execute(
            "INSERT INTO activity_log (ts, level, source, message, meta, task_id) VALUES (?,?,?,?,?,?)",
            (now_iso(), level, source, clean, json.dumps(meta or {}, default=str), task_id),
        )
        bus.publish(
            Events.ACTIVITY,
            {"message": clean, "level": level, "source": source, "meta": meta or {}, "task_id": task_id},
            source=source,
        )

    async def recent(
        self,
        limit: int = 100,
        level: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM activity_log WHERE 1=1"
        params: List[Any] = []
        if level:
            sql += " AND level = ?"
            params.append(level)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if search:
            sql += " AND message LIKE ?"
            params.append(f"%{search}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(limit, 1000))
        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            try:
                row["meta"] = json.loads(row.get("meta") or "{}")
            except Exception:
                row["meta"] = {}
        return rows

    async def purge(self, before_iso: Optional[str] = None) -> int:
        if before_iso:
            return await self.db.execute("DELETE FROM activity_log WHERE ts < ?", (before_iso,))
        return await self.db.execute("DELETE FROM activity_log")
