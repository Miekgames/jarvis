"""What the model is told it knows.

Memory was mechanically fine and practically useless: recall was purely
lexical, so facts about the user surfaced only when their words happened to
match, and they competed for the same handful of slots as search results.
"""
from __future__ import annotations

import pytest

from backend.ai.memory import MIN_THREAD_TURNS, MemoryStore, _clean


class _DB:
    """An in-memory stand-in: rows in, rows out."""

    def __init__(self, memories=(), conversations=()):
        self.memories = list(memories)
        self.conversations = list(conversations)

    async def fetch_all(self, sql, params=()):
        if "FROM conversations" in sql:
            minimum = params[1] if len(params) > 1 else 0
            return [c for c in self.conversations if c["turns"] >= minimum]
        return list(self.memories)

    async def fetch_one(self, sql, params=()):
        return None

    async def execute(self, sql, params=()):
        return 1


def mem(key, value, importance=3, pinned=0, category="general", mid=None):
    return {"id": mid or key, "category": category, "key": key, "value": value,
            "importance": importance, "pinned": pinned, "tags": "[]",
            "source": "test", "created_at": "", "updated_at": ""}


def conv(title, turns, cid="c1"):
    return {"id": cid, "title": title, "updated_at": "2026-11-12T00:00:00+00:00",
            "turns": turns}


# ------------------------------------------------------------------- core
@pytest.mark.asyncio
async def test_pinned_and_important_facts_are_always_included():
    """They used to compete with search hits for the same slots, so asking
    about anything specific pushed out the things that should never leave."""
    store = MemoryStore(_DB([
        mem("brand", "Deepfield is the wallpaper brand", 5, pinned=1),
        mem("trivia", "something unrelated", 2),
    ]))
    core = await store.core()
    assert [c["key"] for c in core] == ["brand"]

    block = await store.context_block("how do I fix the printer")
    assert "Deepfield" in block, (
        "a question about something else must not evict what is always true")


@pytest.mark.asyncio
async def test_importance_four_counts_as_core_without_being_pinned():
    store = MemoryStore(_DB([mem("x", "important thing", 4)]))
    assert len(await store.core()) == 1


@pytest.mark.asyncio
async def test_low_importance_facts_are_not_core():
    store = MemoryStore(_DB([mem("x", "minor thing", 2)]))
    assert await store.core() == []


@pytest.mark.asyncio
async def test_a_core_fact_is_not_repeated_in_the_relevant_section():
    store = MemoryStore(_DB([
        mem("brand", "Deepfield wallpapers", 5, pinned=1),
    ]))
    block = await store.context_block("tell me about Deepfield wallpapers")
    assert block.count("Deepfield wallpapers") == 1


# --------------------------------------------------------------- threads
@pytest.mark.asyncio
async def test_trivial_conversations_are_not_offered_as_current_work():
    """The first version listed "name two colours" and "hello jarvis this is
    a test" under "recently under discussion". A single question plus a tool
    call already makes several rows, so a low bar catches everything."""
    store = MemoryStore(_DB(conversations=[
        conv("hello jarvis this is a test", 4),
        conv("name two colours", 5),
    ]))
    assert await store.recent_threads() == []


@pytest.mark.asyncio
async def test_a_substantial_thread_is_offered():
    store = MemoryStore(_DB(conversations=[
        conv("designing the wallpaper generator", MIN_THREAD_TURNS + 10),
    ]))
    threads = await store.recent_threads()
    assert len(threads) == 1
    assert "wallpaper generator" in threads[0]["title"]


@pytest.mark.asyncio
async def test_near_duplicate_threads_are_collapsed():
    store = MemoryStore(_DB(conversations=[
        conv("working on the earnings counter", 30, "a"),
        conv("working on the earnings counter again", 30, "b"),
    ]))
    assert len(await store.recent_threads()) == 1


@pytest.mark.asyncio
async def test_a_database_without_the_tables_does_not_break_the_prompt():
    class _Broken:
        async def fetch_all(self, sql, params=()):
            if "conversations" in sql:
                raise RuntimeError("no such table")
            return []
        async def fetch_one(self, sql, params=()):
            return None

    store = MemoryStore(_Broken())
    assert await store.recent_threads() == []
    assert await store.context_block("anything") == ""


# ----------------------------------------------------------------- tidying
def test_titles_are_cleaned_for_the_prompt():
    assert _clean("  lots   of   space  ") == "lots of space"
    assert "�" not in _clean("broken � title")
    assert _clean("trailing punctuation...") == "trailing punctuation"
    assert _clean(None) == ""


@pytest.mark.asyncio
async def test_memory_being_switched_off_yields_nothing():
    class _Settings:
        async def get(self, key, default=None):
            return {"memory": False} if key == "privacy" else {}

    store = MemoryStore(_DB([mem("x", "secret", 5, pinned=1)]), _Settings())
    assert await store.context_block("anything") == ""
