"""Small shared helpers: ids, timestamps, text, safe coercion."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------

def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw


# ---------------------------------------------------------------------------
# time -- everything is stored as ISO-8601 UTC text
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def since(seconds: float) -> str:
    return iso(utcnow() - timedelta(seconds=seconds)) or now_iso()


def ahead(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds)) or now_iso()


def humanize_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def truncate(text: Any, limit: int = 400) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "item"


_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization|bearer)\b\s*[=:]\s*\S+"
)


def redact(text: Any) -> str:
    """Strip anything that looks like a credential before it reaches a log."""
    s = "" if text is None else str(text)
    s = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "<redacted>", s)
    s = re.sub(r"gh[pousr]_[A-Za-z0-9]{16,}", "<redacted>", s)
    return s


# ---------------------------------------------------------------------------
# coercion
# ---------------------------------------------------------------------------

def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
