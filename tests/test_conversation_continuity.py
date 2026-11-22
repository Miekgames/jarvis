"""Which conversation a command joins.

Spoken commands used to pass no conversation id at all, so every one started
a fresh thread with no history. JARVIS could not remember what it had just
said because, as far as it was concerned, it never had: forty-four of
eighty-two conversations on this machine were single-turn orphans created
exactly that way.

Voice and typing are one conversation. Saying "what about the second one"
after typing a question is the obvious thing to want.
"""
from __future__ import annotations

import pytest


class _DB:
    """Just enough database for conversation bookkeeping."""

    def __init__(self, recent=None):
        self.conversations = {}
        self.inserted = []
        self.recent = recent            # what the "recently touched" query finds

    @property
    def recent(self):
        return self._recent

    @recent.setter
    def recent(self, cid):
        # A conversation the "most recently touched" query returns is, by
        # definition, one that exists. The stub has to honour that or it
        # tests a database that cannot occur.
        self._recent = cid
        if cid:
            self.conversations[cid] = True

    async def fetch_one(self, sql, params=()):
        if "WHERE id = ?" in sql:
            cid = params[0]
            return {"id": cid} if cid in self.conversations else None
        if "ORDER BY updated_at DESC" in sql:
            return {"id": self.recent} if self.recent else None
        return None

    async def execute(self, sql, params=()):
        if sql.strip().upper().startswith("INSERT INTO CONVERSATIONS"):
            self.conversations[params[0]] = True
            self.inserted.append(params[0])
        return None


def _orchestrator(db):
    from backend.ai.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.db = db
    orchestrator.active_conversation_id = None
    return orchestrator


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unnamed_command_continues_the_thread_in_play():
    db = _DB()
    db.conversations["conv_live"] = True
    orchestrator = _orchestrator(db)
    orchestrator.active_conversation_id = "conv_live"

    assert await orchestrator.current_conversation() == "conv_live"
    assert not db.inserted, "it must not start a new conversation"


@pytest.mark.asyncio
async def test_the_first_command_after_a_restart_picks_up_the_recent_thread():
    """Restarting the backend should not lose the thread of a conversation
    that was happening a minute ago."""
    db = _DB(recent="conv_yesterday_evening")
    orchestrator = _orchestrator(db)

    assert await orchestrator.current_conversation() == "conv_yesterday_evening"
    assert not db.inserted


@pytest.mark.asyncio
async def test_a_long_silence_starts_a_new_thread():
    """Continuing a conversation from hours ago is its own kind of wrong.
    With nothing recent, the query returns nothing and a thread is created."""
    db = _DB(recent=None)
    orchestrator = _orchestrator(db)

    created = await orchestrator.current_conversation()
    assert created in db.inserted, "a new conversation should have been created"


@pytest.mark.asyncio
async def test_a_vanished_conversation_is_not_reused():
    """The remembered thread may have been deleted from under us."""
    db = _DB(recent=None)
    orchestrator = _orchestrator(db)
    orchestrator.active_conversation_id = "conv_deleted"   # not in db.conversations

    created = await orchestrator.current_conversation()
    assert created != "conv_deleted"
    assert created in db.inserted
    assert orchestrator.active_conversation_id != "conv_deleted"


@pytest.mark.asyncio
async def test_the_thread_is_remembered_once_resolved():
    db = _DB(recent="conv_recent")
    orchestrator = _orchestrator(db)

    first = await orchestrator.current_conversation()
    db.recent = "conv_something_else"          # a different thread gets touched
    second = await orchestrator.current_conversation()
    assert second == first, "it must stay on the thread it settled on"


def test_the_spoken_command_runner_passes_a_conversation():
    """The regression itself: `chat(text)` with no conversation_id."""
    import io
    from pathlib import Path

    source = io.open(Path(__file__).resolve().parent.parent / "backend" / "container.py",
                     encoding="utf-8").read()
    runner = source.split("async def _run_spoken_command")[1].split("async def ")[0]
    assert "current_conversation()" in runner
    assert "conversation_id=conversation_id" in runner


# ---------------------------------------------------------------------------
# "recently" has to mean recently
#
# Timestamps are stored as ISO 8601 -- "2026-11-01T20:16:47+00:00" -- while
# SQLite's `datetime('now')` yields "2026-11-01 20:07:33". Compared as
# strings those match through the date and then hit 'T' against a space, and
# 'T' sorts higher: every conversation from *today* counted as "within the
# last ten minutes". The bug is invisible until the clock passes midnight or
# someone counts the rows.
# ---------------------------------------------------------------------------
from datetime import timedelta

from backend.core.util import now_iso, utcnow


def test_the_stored_format_and_the_cutoff_format_agree():
    cutoff = (utcnow() - timedelta(minutes=180)).isoformat()
    stored = now_iso()
    assert stored > cutoff, "a conversation touched now must count as recent"

    old = (utcnow() - timedelta(minutes=300)).isoformat()
    assert not old > cutoff, "a five-hour-old conversation must not count as recent"


def test_sqlites_datetime_would_have_been_wrong():
    """Guards the shape of the bug rather than the fix, so a well-meaning
    tidy-up back to `datetime('now')` fails loudly.

    The timestamps are written out rather than derived from the clock. An
    earlier version built them with `utcnow() - timedelta(hours=6)`, which
    only lands on the same calendar date when the clock is at least six hours
    past midnight UTC -- so the test passed all afternoon and failed at two
    in the morning. The property has nothing to do with the time of day: it
    is that 'T' sorts above a space.
    """
    sqlite_style = "2026-11-01 20:07:33"            # datetime('now', '-10 minutes')
    earlier_that_day = "2026-11-01T14:00:00+00:00"  # six hours before the cutoff

    assert earlier_that_day > sqlite_style, (
        "this comparison is the broken one -- a six-hour-old conversation "
        "counts as being within the last ten minutes -- which is why the "
        "query uses an ISO cutoff instead")

    # And the fixed form gets it right.
    iso_cutoff = "2026-11-01T20:07:33+00:00"
    assert not earlier_that_day > iso_cutoff


def test_the_query_does_not_use_sqlite_datetime_arithmetic():
    import io
    from pathlib import Path

    source = io.open(Path(__file__).resolve().parent.parent / "backend" / "ai"
                     / "orchestrator.py", encoding="utf-8").read()
    body = source.split("async def current_conversation")[1].split("async def ")[0]
    # Comments legitimately quote the broken form while explaining it, so
    # look at the code: strip comment lines before checking.
    code = " ".join(line for line in body.splitlines()
                    if not line.lstrip().startswith("#"))
    assert "datetime('now'" not in code, (
        "comparing ISO timestamps against SQLite's datetime() format matches "
        "everything from the same day")
