"""What scrolls out of a long conversation is summarised, not lost."""
from __future__ import annotations

import pytest

from backend.ai.compression import ConversationSummaries
from backend.core.util import new_id


class _Model:
    def __init__(self, reply="The user and JARVIS agreed a budget of £400 for the trip."):
        self.reply = reply
        self.prompts = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, temperature=None):
        self.prompts.append(prompt)
        return self.reply


async def _conversation(db, messages):
    conversation_id = new_id("conv")
    await db.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (conversation_id, "test", "2026-11-16T10:00:00", "2026-11-16T10:00:00"))
    for index, (role, text) in enumerate(messages):
        await db.execute(
            "INSERT INTO messages (id, conversation_id, role, text, blocks, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (new_id("msg"), conversation_id, role, text, "[]",
             f"2026-11-16T10:{index:02d}:00"))
    return conversation_id


@pytest.mark.asyncio
async def test_what_fell_off_is_summarised_and_kept(db):
    conversation_id = await _conversation(db, [
        ("user", "Let's plan the trip. Budget is £400."),
        ("assistant", "Got it, £400."),
        ("user", "Book trains, not flights."),
        ("assistant", "Trains it is."),
        ("user", "What's the weather there?"),
    ])
    model = _Model()
    store = ConversationSummaries(db, model)

    # The last two messages still fit; the first three fell off.
    assert await store.refresh(conversation_id, "2026-11-16T10:03:00")

    assert "Budget is £400" in model.prompts[0]
    assert "Book trains" in model.prompts[0]
    assert "weather" not in model.prompts[0], "what is still in view is not summarised"
    assert await store.get(conversation_id) == model.reply


@pytest.mark.asyncio
async def test_each_message_is_summarised_once(db):
    conversation_id = await _conversation(db, [
        ("user", "First thing."), ("assistant", "Noted."),
        ("user", "Second thing."), ("assistant", "Noted again."),
    ])
    model = _Model("Summary v1")
    store = ConversationSummaries(db, model)
    await store.refresh(conversation_id, "2026-11-16T10:02:00")

    model.reply = "Summary v2"
    assert not await store.refresh(conversation_id, "2026-11-16T10:02:00"), \
        "nothing new fell off, so no model call"
    assert len(model.prompts) == 1

    assert await store.refresh(conversation_id, "2026-11-16T10:03:00")
    assert "Summary so far:\nSummary v1" in model.prompts[1]
    assert "Second thing" not in model.prompts[1].split("continuing from there:")[0]
    assert await store.get(conversation_id) == "Summary v2"


@pytest.mark.asyncio
async def test_a_failing_model_leaves_the_old_summary_alone(db):
    conversation_id = await _conversation(db, [("user", "a"), ("assistant", "b"), ("user", "c")])
    store = ConversationSummaries(db, _Model("kept"))
    await store.refresh(conversation_id, "2026-11-16T10:01:00")

    class _Broken:
        async def ask(self, *args, **kwargs):
            raise RuntimeError("down")

    broken = ConversationSummaries(db, _Broken())
    assert not await broken.refresh(conversation_id, "2026-11-16T10:02:00")
    assert await broken.get(conversation_id) == "kept"


@pytest.mark.asyncio
async def test_no_conversation_means_no_summary(db):
    assert await ConversationSummaries(db, _Model()).get(None) == ""
    assert await ConversationSummaries(db, _Model()).get("conv_missing") == ""
