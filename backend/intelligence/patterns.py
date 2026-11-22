"""
Behavioural learning.

Learns which commands you actually use, when you use them, and what tends to
follow what — so JARVIS can predict a likely next action and offer it instead
of waiting to be asked.

Everything here is transparent and inspectable: patterns are rows you can read
and delete, and every prediction carries the evidence that produced it. No
embeddings, no opaque model — frequency, recency, time-of-day and transition
counts, which for a single person's command history is both sufficient and
explainable.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core.util import now_iso, parse_iso, since, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.patterns")

# Words that carry no intent, stripped before a command is fingerprinted.
_FILLER = {
    "please", "could", "would", "you", "can", "jarvis", "hey", "ok", "okay",
    "just", "now", "the", "a", "an", "for", "me", "my", "i", "to", "of", "and",
    "is", "it", "that", "this", "do", "get", "give",
}

_SEQUENCE_WINDOW_SECONDS = 600     # two commands within 10 minutes are "linked"
_MIN_COUNT_FOR_PREDICTION = 3


def normalise(text: str) -> str:
    """Fingerprint a command so 'open spotify' and 'Open Spotify please' collapse
    to the same pattern, while keeping enough to stay meaningful."""
    lowered = (text or "").lower().strip()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    words = [w for w in lowered.split() if w not in _FILLER and len(w) > 1]
    # Numbers vary between otherwise identical commands ("remind me in 20 minutes").
    words = ["#" if w.isdigit() else w for w in words]
    return " ".join(words[:8])


class PatternLearner:
    def __init__(self, db, settings_store, activity=None):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self._last_pattern_id: Optional[int] = None
        self._last_at: Optional[datetime] = None

    # ==================================================================
    async def enabled(self) -> bool:
        privacy = await self.settings_store.get("privacy", {})
        awareness = await self.settings_store.get("awareness", {})
        return bool(privacy.get("activity_tracking", True)) and bool(awareness.get("learning", True))

    # ==================================================================
    async def record(self, text: str, intent: str = "") -> Optional[int]:
        """Called for every command. Updates frequency, time histograms and the
        transition from the previous command."""
        if not await self.enabled():
            return None
        fingerprint = normalise(text)
        if not fingerprint:
            return None

        moment = utcnow().astimezone()
        row = await self.db.fetch_one(
            "SELECT * FROM command_patterns WHERE normalised = ?", (fingerprint,)
        )

        if row:
            hours = loads(row["hour_hist"], {}) or {}
            weekdays = loads(row["weekday_hist"], {}) or {}
            hours[str(moment.hour)] = hours.get(str(moment.hour), 0) + 1
            weekdays[str(moment.weekday())] = weekdays.get(str(moment.weekday()), 0) + 1

            previous = parse_iso(row["last_used"])
            gap = (utcnow() - previous).total_seconds() if previous else 0
            count = row["count"] + 1
            # Running mean of the gap between uses -- powers "you usually do this
            # about every N hours".
            avg_gap = ((row["avg_gap_s"] * (count - 2)) + gap) / max(1, count - 1) if count > 1 else 0

            await self.db.execute(
                "UPDATE command_patterns SET count = ?, hour_hist = ?, weekday_hist = ?,"
                " last_used = ?, avg_gap_s = ?, example = ? WHERE id = ?",
                (count, dumps(hours), dumps(weekdays), now_iso(), avg_gap,
                 truncate(text, 200), row["id"]),
            )
            pattern_id = row["id"]
        else:
            await self.db.execute(
                "INSERT INTO command_patterns (normalised, example, intent, count, hour_hist,"
                " weekday_hist, last_used, first_used) VALUES (?,?,?,?,?,?,?,?)",
                (fingerprint, truncate(text, 200), intent, 1,
                 dumps({str(moment.hour): 1}), dumps({str(moment.weekday()): 1}),
                 now_iso(), now_iso()),
            )
            fresh = await self.db.fetch_one(
                "SELECT id FROM command_patterns WHERE normalised = ?", (fingerprint,)
            )
            pattern_id = fresh["id"] if fresh else None

        # -- transition ----------------------------------------------------
        if (
            pattern_id
            and self._last_pattern_id
            and self._last_pattern_id != pattern_id
            and self._last_at
            and (utcnow() - self._last_at).total_seconds() < _SEQUENCE_WINDOW_SECONDS
        ):
            await self.db.execute(
                "INSERT INTO command_sequences (first_id, second_id, count) VALUES (?,?,1)"
                " ON CONFLICT(first_id, second_id) DO UPDATE SET count = count + 1",
                (self._last_pattern_id, pattern_id),
            )

        self._last_pattern_id = pattern_id
        self._last_at = utcnow()
        return pattern_id

    # ==================================================================
    async def frequent(self, limit: int = 12) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, normalised, example, count, last_used, avg_gap_s FROM command_patterns"
            " ORDER BY count DESC, last_used DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def due_now(self, tolerance_hours: float = 1.5) -> List[Dict[str, Any]]:
        """Commands you usually run around this time of day, on this weekday,
        that you haven't run recently."""
        if not await self.enabled():
            return []
        moment = utcnow().astimezone()
        rows = await self.db.fetch_all(
            "SELECT * FROM command_patterns WHERE count >= ?", (_MIN_COUNT_FOR_PREDICTION,)
        )

        candidates = []
        for row in rows:
            hours = loads(row["hour_hist"], {}) or {}
            weekdays = loads(row["weekday_hist"], {}) or {}
            total = max(1, int(row["count"]))

            # How concentrated is this command around the current hour?
            nearby = sum(hours.get(str((moment.hour + offset) % 24), 0) for offset in (-1, 0, 1))
            hour_score = nearby / total
            weekday_score = weekdays.get(str(moment.weekday()), 0) / total

            last = parse_iso(row["last_used"])
            hours_since = (utcnow() - last).total_seconds() / 3600 if last else 999

            # Only suggest if it is genuinely time-anchored and overdue.
            if hour_score < 0.35 or hours_since < tolerance_hours:
                continue
            expected_gap_hours = (row["avg_gap_s"] or 0) / 3600
            if expected_gap_hours and hours_since < expected_gap_hours * 0.6:
                continue

            confidence = min(0.95, hour_score * 0.6 + weekday_score * 0.25 + min(total / 20, 1) * 0.15)
            candidates.append({
                "pattern_id": row["id"],
                "command": row["example"],
                "confidence": round(confidence, 2),
                "reason": (
                    f"You've run this {total} time(s), {int(hour_score * 100)}% of them "
                    f"around {moment.hour:02d}:00, and last {int(hours_since)}h ago."
                ),
            })

        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        return candidates[:3]

    async def likely_next(self, text: str, limit: int = 3) -> List[Dict[str, Any]]:
        """What usually follows the command just given."""
        if not await self.enabled():
            return []
        fingerprint = normalise(text)
        row = await self.db.fetch_one(
            "SELECT id FROM command_patterns WHERE normalised = ?", (fingerprint,)
        )
        if not row:
            return []

        rows = await self.db.fetch_all(
            "SELECT s.count, p.example, p.id FROM command_sequences s"
            " JOIN command_patterns p ON p.id = s.second_id"
            " WHERE s.first_id = ? ORDER BY s.count DESC LIMIT ?",
            (row["id"], limit),
        )
        total = sum(r["count"] for r in rows) or 1
        return [
            {
                "command": r["example"],
                "pattern_id": r["id"],
                "confidence": round(r["count"] / total, 2),
                "reason": f"followed '{truncate(text, 40)}' {r['count']} time(s) before",
            }
            for r in rows if r["count"] >= 2
        ]

    # ==================================================================
    # personal vocabulary
    # ==================================================================
    async def learn_phrase(self, phrase: str, means: str, source: str = "user") -> Dict[str, Any]:
        await self.db.execute(
            "INSERT INTO vocabulary (phrase, means, source, uses, created_at) VALUES (?,?,?,0,?)"
            " ON CONFLICT(phrase) DO UPDATE SET means = excluded.means, source = excluded.source",
            (phrase.lower().strip(), means, source, now_iso()),
        )
        if self.activity:
            await self.activity.record(
                f"Learned that '{phrase}' means: {truncate(means, 80)}", source="patterns"
            )
        return {"phrase": phrase, "means": means}

    async def vocabulary(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT * FROM vocabulary ORDER BY uses DESC, phrase")]

    async def expand(self, text: str) -> Tuple[str, List[str]]:
        """Annotate a command with any personal phrases it contains, so the model
        gets the user's private shorthand without us rewriting their words."""
        entries = await self.vocabulary()
        if not entries:
            return text, []
        lowered = text.lower()
        matched = []
        for entry in entries:
            if entry["phrase"] and entry["phrase"] in lowered:
                matched.append(f"'{entry['phrase']}' means {entry['means']}")
                await self.db.execute(
                    "UPDATE vocabulary SET uses = uses + 1 WHERE phrase = ?", (entry["phrase"],)
                )
        return text, matched

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        return {
            "enabled": await self.enabled(),
            "patterns": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM command_patterns", default=0)),
            "sequences": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM command_sequences", default=0)),
            "vocabulary": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM vocabulary", default=0)),
            "top": await self.frequent(limit=5),
        }

    async def forget(self, pattern_id: int) -> bool:
        await self.db.execute(
            "DELETE FROM command_sequences WHERE first_id = ? OR second_id = ?",
            (pattern_id, pattern_id),
        )
        return await self.db.execute(
            "DELETE FROM command_patterns WHERE id = ?", (pattern_id,)) > 0

    async def clear(self) -> int:
        await self.db.execute("DELETE FROM command_sequences")
        return await self.db.execute("DELETE FROM command_patterns")
