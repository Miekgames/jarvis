"""Checklists: a grocery list, a workout, a packing list -- things to tick off.

Made and changed by saying it ("make a grocery list: eggs, milk and bread",
"add butter to my grocery list", "check off eggs") or in the Checklists
panel, and the panel and the conversation always agree: every change is
announced as "checklists.changed".

Ticking an item off keeps it on the list -- a workout is done again next
week -- so "uncheck everything" resets a list, and "clear the checked ones"
is a separate, deliberate tidy.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional

from ..core.events import bus
from ..core.util import new_id, now_iso

MAX_ITEMS = 200
_FILLER = re.compile(r"\b(?:my|the|a|an|list|lists|checklist|checklists|check\s+list)\b", re.I)


def tidy_title(words: str) -> str:
    """ "grocery list" -> "Grocery list"; "leg day" -> "Leg day". """
    words = " ".join((words or "").strip(" .:!?\"'").split())
    return (words[:1].upper() + words[1:])[:80] if words else "Checklist"


def split_items(text: str) -> List[str]:
    """ "eggs, milk and bread" -> three items; "eggs milk and bread" said aloud
    has no commas, so "and" separates too. With commas, only the first "and"
    of the last part does: "butter, jam and mac and cheese" keeps the mac and
    cheese together."""
    text = (text or "").strip(" .!?")
    if not text:
        return []
    if "," in text or ";" in text or "\n" in text:
        parts = re.split(r"\s*[,;\n]\s*", text)
        parts = parts[:-1] + re.split(r"\s+and\s+", parts[-1], maxsplit=1)
    else:
        parts = re.split(r"\s+and\s+", text)
    items = []
    for part in parts:
        part = re.sub(r"^(?:and|also|some|a\s+few)\s+", "", part.strip(" .-*•"), flags=re.I)
        if part:
            items.append(part[:200])
    return items


def _key(words: str) -> str:
    """What a name or an item is, for matching: "my Groceries list" ~ "grocery"."""
    words = _FILLER.sub(" ", (words or "").lower())
    words = re.sub(r"[^a-z0-9 ]", " ", words)
    out = []
    for word in words.split():
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return " ".join(out)


def _squash(key: str) -> str:
    """ "work out" and "workout" are the same list: spacing doesn't count."""
    return key.replace(" ", "")


def _best(things: List[Dict[str, Any]], wanted: str, name: Callable[[Dict[str, Any]], str],
          partial_both_ways: bool = False) -> Optional[Dict[str, Any]]:
    """The one thing the words mean, most certain match first, else None.

    "work out" didn't find "Workout" when matching went word for word; then
    "chest press" vs "chest-press", "grocerys" vs "groceries". In order:
    the same words; the same letters ignoring spaces and hyphens; one
    contained in the other; a near spelling -- each only when one thing fits.
    """
    keyed = [(thing, _key(name(thing))) for thing in things]
    tests = [
        lambda key: key == wanted,
        lambda key: _squash(key) == _squash(wanted),
        lambda key: _squash(wanted) in _squash(key) or (partial_both_ways and _squash(key) in _squash(wanted)),
    ]
    for test in tests:
        hits = [thing for thing, key in keyed if key and test(key)]
        if len(hits) == 1 or (hits and test is tests[0]):
            return hits[0]
        if len(hits) > 1:
            return None                                   # more than one fits: ask, don't guess
    scored = sorted(((SequenceMatcher(None, _squash(wanted), _squash(key)).ratio(), i)
                     for i, (thing, key) in enumerate(keyed) if key), reverse=True)
    if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
        return keyed[scored[0][1]][0]
    return None


class Checklists:
    def __init__(self, db) -> None:
        self.db = db

    # -- reading ------------------------------------------------------------------
    async def all(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT c.id, c.title, c.updated_at,"
            " (SELECT COUNT(*) FROM checklist_items i WHERE i.checklist_id = c.id) AS total,"
            " (SELECT COUNT(*) FROM checklist_items i WHERE i.checklist_id = c.id AND i.done = 1) AS done"
            " FROM checklists c ORDER BY c.updated_at DESC")
        return [dict(r) for r in rows]

    async def get(self, checklist_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT id, title, updated_at FROM checklists WHERE id = ?",
                                      (checklist_id,))
        if row is None:
            raise KeyError("No such checklist.")
        items = await self.db.fetch_all(
            "SELECT id, text, done FROM checklist_items WHERE checklist_id = ? ORDER BY position, created_at",
            (checklist_id,))
        return {**dict(row), "items": [{"id": i["id"], "text": i["text"], "done": bool(i["done"])}
                                       for i in items]}

    async def find(self, words: str) -> Optional[Dict[str, Any]]:
        """The checklist the words name: "groceries" finds "Grocery list"."""
        wanted = _key(words)
        lists = await self.all()
        if not lists:
            return None
        if not wanted:
            return lists[0] if len(lists) == 1 else None
        return _best(lists, wanted, lambda c: c["title"], partial_both_ways=True)

    @staticmethod
    def find_item(checklist: Dict[str, Any], words: str) -> Optional[Dict[str, Any]]:
        wanted = _key(words)
        if not wanted:
            return None
        return _best(checklist["items"], wanted, lambda i: i["text"])

    async def find_item_anywhere(self, words: str) -> List[Dict[str, Any]]:
        """Every (checklist, item) whose item the words name, across all lists."""
        found = []
        for summary in await self.all():
            checklist = await self.get(summary["id"])
            item = self.find_item(checklist, words)
            if item is not None:
                found.append({"checklist": checklist, "item": item})
        return found

    # -- changing -------------------------------------------------------------------
    async def _touch(self, checklist_id: str) -> None:
        await self.db.execute("UPDATE checklists SET updated_at = ? WHERE id = ?", (now_iso(), checklist_id))
        bus.publish("checklists.changed", {"id": checklist_id}, source="checklists")

    async def create(self, title: str, items: Optional[List[str]] = None) -> Dict[str, Any]:
        title = tidy_title(title)
        existing = await self.find(title)
        if existing is not None and _key(existing["title"]) == _key(title):
            if items:
                await self.add(existing["id"], items)
            return await self.get(existing["id"])
        checklist_id = new_id("chk")
        now = now_iso()
        await self.db.execute("INSERT INTO checklists (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                              (checklist_id, title, now, now))
        if items:
            await self.add(checklist_id, items)
        else:
            await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def add(self, checklist_id: str, items: List[str]) -> Dict[str, Any]:
        checklist = await self.get(checklist_id)
        have = {_key(i["text"]) for i in checklist["items"]}
        position = len(checklist["items"])
        for text in items:
            text = " ".join(str(text).split())[:200]
            if not text or _key(text) in have:
                continue                           # "add milk" twice is still one milk
            if position >= MAX_ITEMS:
                raise ValueError(f"A checklist holds up to {MAX_ITEMS} items.")
            await self.db.execute(
                "INSERT INTO checklist_items (id, checklist_id, text, done, position, created_at)"
                " VALUES (?,?,?,?,?,?)", (new_id("itm"), checklist_id, text, 0, position, now_iso()))
            have.add(_key(text))
            position += 1
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def set_done(self, checklist_id: str, item_id: str, done: bool) -> Dict[str, Any]:
        await self.db.execute("UPDATE checklist_items SET done = ? WHERE id = ? AND checklist_id = ?",
                              (1 if done else 0, item_id, checklist_id))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def edit_item(self, checklist_id: str, item_id: str, text: str) -> Dict[str, Any]:
        text = " ".join((text or "").split())[:200]
        if not text:
            raise ValueError("An item needs some words.")
        await self.db.execute("UPDATE checklist_items SET text = ? WHERE id = ? AND checklist_id = ?",
                              (text, item_id, checklist_id))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def remove_item(self, checklist_id: str, item_id: str) -> Dict[str, Any]:
        await self.db.execute("DELETE FROM checklist_items WHERE id = ? AND checklist_id = ?",
                              (item_id, checklist_id))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def uncheck_all(self, checklist_id: str) -> Dict[str, Any]:
        await self.db.execute("UPDATE checklist_items SET done = 0 WHERE checklist_id = ?", (checklist_id,))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def clear_checked(self, checklist_id: str) -> Dict[str, Any]:
        await self.db.execute("DELETE FROM checklist_items WHERE checklist_id = ? AND done = 1", (checklist_id,))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def rename(self, checklist_id: str, title: str) -> Dict[str, Any]:
        await self.db.execute("UPDATE checklists SET title = ? WHERE id = ?", (tidy_title(title), checklist_id))
        await self._touch(checklist_id)
        return await self.get(checklist_id)

    async def delete(self, checklist_id: str) -> Dict[str, Any]:
        checklist = await self.get(checklist_id)
        await self.db.execute("DELETE FROM checklist_items WHERE checklist_id = ?", (checklist_id,))
        await self.db.execute("DELETE FROM checklists WHERE id = ?", (checklist_id,))
        bus.publish("checklists.changed", {"id": checklist_id, "deleted": True}, source="checklists")
        return checklist


def describe(checklist: Dict[str, Any]) -> str:
    """A checklist as text, for a reply or a Discord message."""
    items = checklist.get("items") or []
    if not items:
        return f"**{checklist['title']}** is empty."
    done = sum(1 for i in items if i["done"])
    lines = [f"**{checklist['title']}** ({done} of {len(items)} done)"]
    lines += [f"- {'☑' if i['done'] else '☐'} {i['text']}" for i in items]
    return "\n".join(lines)
