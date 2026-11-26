"""Countdowns by asking: "countdown to the Super Bowl", "how long until Christmas".

The model supplies the name of the event and nothing else that matters. The
date is read from the owner's own words, or worked out from evidence
(`services.event_dates`) -- a date the model put in the call is used only when
it is one that has been said in this conversation, because a date a small
model volunteers is usually one it remembers from training.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from ...services import event_dates
from ...services.countdowns import LIMIT, CountdownError
from .registry import ToolRegistry, fail, ok


def _nice(day: date) -> str:
    return day.strftime("%A %d %B %Y").replace(" 0", " ")


def _day_first(ctx) -> bool:
    region = str(getattr(ctx.get("settings"), "SEARCH_REGION", "us-en") or "us-en")
    return not region.lower().startswith("us")


async def _said(ctx) -> Tuple[Optional[str], str]:
    """The owner's latest message, and everything said recently."""
    db, conversation = ctx.get("db"), ctx.get("conversation_id")
    if db is None or not conversation:
        return None, ""
    try:
        rows = await db.fetch_all(
            "SELECT role, text FROM messages WHERE conversation_id = ?"
            " ORDER BY created_at DESC LIMIT 8", (conversation,))
    except Exception:
        return None, ""
    latest = next((row["text"] for row in rows if row["role"] == "user"), None)
    return latest, "\n".join(str(row["text"] or "") for row in rows)


def _given(value: str, today: date) -> Optional[date]:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        mentions = event_dates.find_dates(value or "", today)
        return mentions[0].day if mentions else None


async def _when(ctx, event: str, model_date: str) -> Tuple[Optional[date], str, Any]:
    """(day, where it came from, evidence). Raises CountdownError to ask the owner."""
    today = datetime.now().date()
    message, recent = await _said(ctx)

    # 1. The owner said when.
    if message:
        theirs = event_dates.date_in_words(message, today, _day_first(ctx))
        if theirs:
            return theirs, "owner", None
    # "Yes, the 16th" -- a date from earlier in this conversation.
    offered = _given(model_date, today)
    if offered and message and len(message.split()) <= 8 and any(
            m.day == offered for m in event_dates.find_dates(recent, today)):
        return offered, "owner", None

    # 2. Only the owner could know.
    if event_dates.is_personal(event):
        raise CountdownError(f"Only the owner knows when “{event}” is. Ask them for the date "
                             "(and the time, if it matters). No countdown was added.")

    # 3. Look it up.
    found = await ctx.get("countdowns").look_up(event)
    if found.status == "sure":
        return found.day, "web", found
    if found.status == "past":
        raise CountdownError(
            f"“{event}” has already happened: {_nice(found.day)}"
            + (f", according to {found.source_name}" if found.source_name else "")
            + ". No countdown was added. Tell the owner that plainly.")
    if found.status == "unsure":
        maybe = [found.day] + [d for d, _ in found.others]
        raise CountdownError(
            f"I couldn't confirm when “{event}” is. The best lead is {_nice(maybe[0])} "
            f"({found.sources} site{'s' if found.sources != 1 else ''}, {found.source_name})"
            + ("; others say " + ", ".join(_nice(d) for d in maybe[1:3]) if len(maybe) > 1 else "")
            + ". No countdown was added. Tell the owner exactly this and ask which date to use.")
    raise CountdownError(f"I couldn't find a date for “{event}”; it may not have one yet. No countdown "
                         "was added. Say so, and ask the owner for the date if they know it. "
                         "Do not suggest a date yourself.")


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "add_countdown",
        "Put a live countdown to an event on the main screen: a film or game release, a match, "
        "a holiday, a birthday, anything with a date. Use for 'countdown to X', 'add a countdown "
        "for X', 'count down the days to X'. Just give the event's name: the date is looked up "
        f"from several websites, or taken from the owner's words. At most {LIMIT} at a time.",
        {"type": "object", "properties": {
            "event": {"type": "string", "description": "The event's name, e.g. 'Super Bowl 2027'"},
            "date": {"type": "string",
                     "description": "Only if the owner said the date: YYYY-MM-DD. Otherwise leave empty."},
        }, "required": ["event"]},
        category="countdown event timer days until",
    )
    async def add_countdown(ctx, event: str, date: str = ""):
        service = ctx.get("countdowns")
        if service is None:
            return fail("Countdowns are not available.")
        event = event_dates.tidy(event)
        if not event:
            return fail("Ask the owner which event to count down to.")
        message, _ = await _said(ctx)
        try:
            await service.check_room(event)
            day, origin, found = await _when(ctx, event, date)
            item = await service.add(event, day.isoformat(),
                                     clock=event_dates.time_in_words(message or ""),
                                     origin=origin, found=found)
        except CountdownError as exc:
            return fail(str(exc))
        said = (f"{'Updated the' if item['updated'] else 'Added a'} countdown to {item['title']}: "
                f"{_nice(day)}{' at ' + item['clock'] if item['clock'] else ''}, "
                f"{item['left']} from now.")
        if origin == "web":
            said += (f" {found.sources} websites agree on the date ({found.source_name})."
                     if found.sources else " That date is fixed in the calendar.")
        said += " It is on the main screen now" + (
            "; clicking it shows the latest news." if item["topic"] else ".")
        return ok({"title": item["title"], "date": item["day"], "time": item["clock"]}, said)

    @registry.add(
        "list_countdowns",
        "The countdowns on the main screen and exactly how long each has left.",
        {"type": "object", "properties": {}},
        category="countdown event timer days until",
    )
    async def list_countdowns(ctx):
        service = ctx.get("countdowns")
        if service is None:
            return fail("Countdowns are not available.")
        items = await service.list()
        if not items:
            return ok([], "There are no countdowns yet.")
        now = datetime.now()
        rows: List[Dict[str, Any]] = []
        for n, item in enumerate(items, 1):
            target = event_dates.target_moment(item["day"], item["clock"])
            rows.append({"n": n, "event": item["title"], "on": _nice(target.date()),
                         "time": item["clock"], "left": event_dates.in_words(now, target)})
        return ok(rows, f"{len(rows)} of {LIMIT} countdowns. Use the 'left' values exactly as given.")

    @registry.add(
        "how_long_until",
        "Exactly how long until an event: 'how long until Christmas', 'how many days until the "
        "World Cup', 'when does X come out'. Checks the countdowns, the calendar and the web. "
        "Does not add a countdown.",
        {"type": "object", "properties": {
            "event": {"type": "string", "description": "The event"},
        }, "required": ["event"]},
        category="countdown event days weeks months until left long when",
    )
    async def how_long_until(ctx, event: str):
        service = ctx.get("countdowns")
        if service is None:
            return fail("Countdowns are not available.")
        event = event_dates.tidy(event)
        kept = next((i for i in await service.list()
                     if event_dates.significant(i["title"]) == event_dates.significant(event)), None)
        now = datetime.now()
        if kept:
            target = event_dates.target_moment(kept["day"], kept["clock"])
            return ok(None, f"{kept['title']} is on {_nice(target.date())}: "
                            f"{event_dates.in_words(now, target)} from now.")
        try:
            day, _origin, found = await _when(ctx, event, "")
        except CountdownError as exc:
            return fail(str(exc).replace(" No countdown was added.", ""))
        target = event_dates.target_moment(day.isoformat())
        total = (day - now.date()).days
        return ok(None, f"{event} is on {_nice(day)}: {event_dates.in_words(now, target)} from now "
                        f"({total} days)."
                  + (f" {found.sources} websites agree." if found is not None and found.sources else ""))

    @registry.add(
        "remove_countdown",
        "Take a countdown off the main screen.",
        {"type": "object", "properties": {
            "event": {"type": "string", "description": "Its name, words from it, or its number"},
        }, "required": ["event"]},
        category="countdown event timer remove delete stop",
    )
    async def remove_countdown(ctx, event: str):
        service = ctx.get("countdowns")
        if service is None:
            return fail("Countdowns are not available.")
        item = await service.find(event_dates.tidy(event))
        if item is None:
            names = ", ".join(f"“{i['title']}”" for i in await service.list())
            return fail(f"No single countdown matches “{event}”. "
                        + (f"There are: {names}." if names else "There are none."))
        await service.remove(item["id"])
        return ok({"removed": item["title"]}, f"Removed the countdown to {item['title']}.")
