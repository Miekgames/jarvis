"""Countdowns: up to three events, counting down on the main screen.

Each has a name, a day and optionally a time, and a news topic. The date comes
from the owner or from sites that agree with each other (`event_dates`); where
it came from is kept and shown, because a countdown is only worth what its
date is.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.util import new_id, now_iso
from . import event_dates
from .event_dates import Found

logger = logging.getLogger("jarvis.countdowns")

LIMIT = 3
NEWS_TTL = 600.0
MAX_NEWS = 10
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class CountdownError(ValueError):
    """Something the owner can act on, in words they can act on."""


def _same(a: str, b: str) -> bool:
    return event_dates.significant(a) == event_dates.significant(b)


def clean_day(value: str) -> str:
    value = (value or "").strip()
    try:
        if not _DAY.match(value):
            raise ValueError
        day = date.fromisoformat(value)
    except ValueError:
        raise CountdownError(f"'{value}' isn't a date I can use. Dates look like 2027-06-05.")
    if not 2000 <= day.year <= 2200:
        raise CountdownError(f"{value} is outside the years a countdown covers.")
    return value


def clean_clock(value: str) -> str:
    value = (value or "").strip()
    if value and not _CLOCK.match(value):
        raise CountdownError(f"'{value}' isn't a time I can use. Times look like 19:30.")
    return value


class Countdowns:
    def __init__(self, db, web=None, activity=None, settings=None) -> None:
        self.db = db
        self.web = web
        self.activity = activity
        self.settings = settings
        self._lock = asyncio.Lock()
        self._news: Dict[str, Dict[str, Any]] = {}

    # -- reading -------------------------------------------------------------
    def _shape(self, row: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now()
        target = event_dates.target_moment(row["day"], row["clock"] or "")
        today = now.date().isoformat()
        arrived = target <= now
        return {
            "id": row["id"], "title": row["title"], "topic": row["topic"] or "",
            "day": row["day"], "clock": row["clock"] or "", "origin": row["origin"],
            "source_name": row["source_name"] or "", "source_url": row["source_url"] or "",
            "sources": int(row["sources"] or 0), "checked_at": row["checked_at"],
            "arrived": arrived, "over": arrived and row["day"] < today,
            "left": event_dates.in_words(now, target),
        }

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM countdowns ORDER BY day, clock, created_at")
        items = [self._shape(dict(row)) for row in rows]
        # What is still coming first, soonest first; what has been, after.
        return sorted(items, key=lambda i: (i["over"], i["day"], i["clock"]))

    async def get(self, countdown_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM countdowns WHERE id = ?", (countdown_id,))
        if row is None:
            raise CountdownError("That countdown is no longer there.")
        return self._shape(dict(row))

    async def find(self, words: str) -> Optional[Dict[str, Any]]:
        """The countdown someone means: its id, its number, or words from it."""
        items = await self.list()
        words = (words or "").strip()
        for item in items:
            if item["id"] == words or _same(item["title"], words):
                return item
        if words.isdigit() and 1 <= int(words) <= len(items):
            return items[int(words) - 1]
        wanted = set(event_dates.significant(words))
        scored = [(len(wanted & set(event_dates.significant(i["title"]))), i) for i in items]
        scored = [pair for pair in scored if pair[0]]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            return None
        return scored[0][1]

    # -- finding the date ------------------------------------------------------
    async def look_up(self, event: str) -> Found:
        """When an event is, as the web has it. Raises if the web can't be reached."""
        event = event_dates.tidy(event)
        today = date.today()
        known = event_dates.fixed_day(event, today)
        if known:
            return Found("sure" if known >= today else "past", known, 0, "the calendar")
        if self.web is None:
            raise CountdownError("Web search isn't available, so I can't look the date up.")
        results: List[Dict[str, Any]] = []
        failed = 0
        # Films answer to "release date", matches and festivals to "date".
        for query in (f"{event} release date", f"{event} date"):
            try:
                results += await self.web.search(query, 8)
            except Exception as exc:
                failed += 1
                logger.info("date search failed for %r: %s", query, exc)
        if failed == 2:
            raise CountdownError("I couldn't reach the web to look the date up.")
        found = event_dates.weigh(event, results, today)
        if found.status in ("none", "unsure"):
            try:
                results += await self.web.search(f'"{event}" when', 8)
                found = event_dates.weigh(event, results, today)
            except Exception:
                logger.debug("second date search failed", exc_info=True)
        return found

    # -- changing --------------------------------------------------------------
    async def _changed(self) -> None:
        bus.publish("countdowns.changed", {"count": len(await self.list())}, source="countdowns")

    @staticmethod
    def _room(items: List[Dict[str, Any]], existing: Optional[Dict[str, Any]]) -> None:
        if existing is not None or len(items) < LIMIT:
            return
        over = [i["title"] for i in items if i["over"]]
        raise CountdownError(
            f"There are already {LIMIT} countdowns: "
            + ", ".join(f"“{i['title']}”" for i in items)
            + f". {LIMIT} is the most that can run at once, so one has to be removed first"
            + (f" (“{over[0]}” has already happened)." if over else "."))

    async def check_room(self, title: str) -> None:
        """Whether one more fits, before going to the trouble of looking it up."""
        items = await self.list()
        self._room(items, next((i for i in items if _same(i["title"], title)), None))

    async def add(self, title: str, day: str, clock: str = "", topic: Optional[str] = None,
                  origin: str = "owner", found: Optional[Found] = None) -> Dict[str, Any]:
        title = event_dates.tidy(title)
        if not title:
            raise CountdownError("The countdown needs the name of an event.")
        day, clock = clean_day(day), clean_clock(clock)
        if event_dates.target_moment(day, clock) <= datetime.now():
            raise CountdownError(f"{day}{' at ' + clock if clock else ''} has already passed.")
        if topic is None:
            # There is no news about someone's own birthday.
            topic = "" if event_dates.is_personal(title) else title
        topic = " ".join(topic.split())[:80]
        async with self._lock:
            items = await self.list()
            existing = next((i for i in items if _same(i["title"], title)), None)
            self._room(items, existing)
            countdown_id = existing["id"] if existing else new_id("cdn")
            values = (title, topic, day, clock, origin,
                      found.source_name if found else "", found.source_url if found else "",
                      found.sources if found else 0, now_iso() if found else None)
            if existing:
                await self.db.execute(
                    "UPDATE countdowns SET title=?, topic=?, day=?, clock=?, origin=?, source_name=?,"
                    " source_url=?, sources=?, checked_at=? WHERE id=?", values + (countdown_id,))
                self._news.pop(countdown_id, None)
            else:
                await self.db.execute(
                    "INSERT INTO countdowns (title, topic, day, clock, origin, source_name, source_url,"
                    " sources, checked_at, id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    values + (countdown_id, now_iso()))
        if self.activity is not None:
            await self.activity.record(f"Countdown to {title}: {day}{' ' + clock if clock else ''}.",
                                       source="countdowns")
        await self._changed()
        item = await self.get(countdown_id)
        item["updated"] = bool(existing)
        return item

    async def update(self, countdown_id: str, **changes: Any) -> Dict[str, Any]:
        """Change a countdown by hand: its name, day, time or news topic."""
        current = await self.get(countdown_id)
        title = event_dates.tidy(changes["title"]) if changes.get("title") is not None else current["title"]
        if not title:
            raise CountdownError("The countdown needs the name of an event.")
        day = clean_day(changes["day"]) if changes.get("day") is not None else current["day"]
        clock = clean_clock(changes["clock"]) if changes.get("clock") is not None else current["clock"]
        topic = (" ".join(str(changes["topic"]).split())[:80]
                 if changes.get("topic") is not None else current["topic"])
        moved = day != current["day"] or clock != current["clock"]
        if moved and event_dates.target_moment(day, clock) <= datetime.now():
            raise CountdownError(f"{day}{' at ' + clock if clock else ''} has already passed.")
        async with self._lock:
            others = [i for i in await self.list() if i["id"] != countdown_id]
            if any(_same(i["title"], title) for i in others):
                raise CountdownError(f"There is already a countdown to “{title}”.")
            if day != current["day"]:
                # The owner's date now; the old source no longer vouches for it.
                await self.db.execute(
                    "UPDATE countdowns SET title=?, topic=?, day=?, clock=?, origin='owner',"
                    " source_name='', source_url='', sources=0, checked_at=NULL WHERE id=?",
                    (title, topic, day, clock, countdown_id))
            else:
                await self.db.execute(
                    "UPDATE countdowns SET title=?, topic=?, clock=? WHERE id=?",
                    (title, topic, clock, countdown_id))
        self._news.pop(countdown_id, None)
        await self._changed()
        return await self.get(countdown_id)

    async def remove(self, countdown_id: str) -> Dict[str, Any]:
        item = await self.get(countdown_id)
        await self.db.execute("DELETE FROM countdowns WHERE id = ?", (countdown_id,))
        self._news.pop(countdown_id, None)
        await self._changed()
        return item

    async def recheck(self, countdown_id: str) -> Dict[str, Any]:
        """Look the date up again. Moves the countdown only if sites agree."""
        item = await self.get(countdown_id)
        found = await self.look_up(item["title"])
        if found.status != "sure" or found.day is None:
            return {"changed": False, "item": item, "certain": False,
                    "message": "I couldn't find a date that enough sites agree on, so I left it as it is."}
        day = found.day.isoformat()
        changed = day != item["day"]
        await self.db.execute(
            "UPDATE countdowns SET day=?, origin='web', source_name=?, source_url=?, sources=?,"
            " checked_at=? WHERE id=?",
            (day, found.source_name, found.source_url, found.sources, now_iso(), countdown_id))
        if changed:
            await self._changed()
        return {"changed": changed, "certain": True, "item": await self.get(countdown_id),
                "message": (f"The date has moved, from {item['day']} to {day}." if changed
                            else "Still the same date.")}

    # -- news --------------------------------------------------------------------
    async def news(self, countdown_id: str, refresh: bool = False) -> Dict[str, Any]:
        """The latest news on a countdown's topic, newest first."""
        item = await self.get(countdown_id)
        topic = item["topic"]
        if not topic:
            return {"topic": "", "articles": [], "fetched_at": None, "error": ""}
        kept = self._news.get(countdown_id)
        if kept and kept["topic"] == topic and not refresh and time.time() - kept["at"] < NEWS_TTL:
            return kept["reply"]
        try:
            if self.web is None:
                raise RuntimeError("web search is not available")
            raw = await self.web.news(topic, 25)
        except Exception as exc:
            logger.info("news for %r failed: %s", topic, exc)
            if "no results" in str(exc).lower():
                raw = []
            else:
                old = kept["reply"] if kept and kept["topic"] == topic else None
                return {**(old or {"topic": topic, "articles": [], "fetched_at": None}),
                        "error": "The news couldn't be reached just now."}
        reply = {"topic": topic, "articles": select_news(topic, raw), "fetched_at": now_iso(), "error": ""}
        self._news[countdown_id] = {"at": time.time(), "topic": topic, "reply": reply}
        return reply


def select_news(topic: str, raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The articles about the topic, newest first, each story once."""
    words = [w for w in event_dates.significant(topic) if not re.fullmatch(r"20\d{2}", w)] \
        or event_dates.significant(topic)
    kept: List[Dict[str, Any]] = []
    seen: List[set] = []
    for entry in sorted(raw, key=lambda r: str(r.get("date") or ""), reverse=True):
        title = " ".join(str(entry.get("title") or "").split())
        url = str(entry.get("url") or "")
        if not title or not url.startswith(("http://", "https://")):
            continue
        if not event_dates._about(words, f"{title} {entry.get('snippet') or ''}"):
            continue
        told = set(re.findall(r"[a-z0-9]+", title.lower())) - set(words)
        if any(told and len(told & other) / max(1, min(len(told), len(other))) >= 0.7 for other in seen):
            continue
        seen.append(told)
        kept.append({"title": title[:220], "url": url, "source": str(entry.get("source") or "")[:60],
                     "date": str(entry.get("date") or ""),
                     "snippet": " ".join(str(entry.get("snippet") or "").split())[:260]})
        if len(kept) >= MAX_NEWS:
            break
    return kept
