"""Checklists to tick off, and scrolling back through earlier messages."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ai.tools import checklist_tools as ct
from backend.ai.tools.checklist_tools import checklist_said
from backend.services.checklists import Checklists, describe, split_items


@pytest.fixture
async def lists(db):
    return Checklists(db)


@pytest.fixture
async def fake(registry, lists):
    """Just enough orchestrator for the instant path, with real tools."""
    from backend.ai.orchestrator import Orchestrator

    ct.register(registry)
    registry.context._services["checklists"] = lists
    made = SimpleNamespace(registry=registry)
    made.say = lambda text: Orchestrator._plain_checklist(made, text)
    return made


@pytest.mark.parametrize("text, items", [
    ("eggs, milk and bread", ["eggs", "milk", "bread"]),
    ("eggs milk and bread", ["eggs milk", "bread"]),
    ("butter, jam and mac and cheese", ["butter", "jam", "mac and cheese"]),
    ("squats; lunges; plank", ["squats", "lunges", "plank"]),
    ("", []),
])
def test_items_from_words(text, items):
    assert split_items(text) == items


@pytest.mark.parametrize("said, expected", [
    ("show me my checklists", ("show_checklists", {})),
    ("what's on my grocery list?", ("show_checklists", {"checklist": "grocery"})),
    ("make a grocery list with eggs, milk and bread",
     ("make_checklist", {"title": "Grocery list", "items": ["eggs", "milk", "bread"]})),
    ("make a workout checklist: squats, push-ups, plank",
     ("make_checklist", {"title": "Workout", "items": ["squats", "push-ups", "plank"]})),
    ("add butter to my grocery list", ("add_to_checklist", {"items": ["butter"], "checklist": "grocery"})),
    ("check off eggs", ("check_off", {"item": "eggs", "checklist": "", "done": True})),
    ("uncheck eggs", ("check_off", {"item": "eggs", "checklist": "", "done": False})),
    ("reset my workout list", ("reset_checklist", {"checklist": "workout", "mode": "uncheck"})),
    ("clear the checked items from my grocery list",
     ("reset_checklist", {"checklist": "grocery", "mode": "clear_checked"})),
    ("delete my packing list", ("delete_checklist", {"checklist": "packing"})),
])
def test_saying_it(said, expected):
    assert checklist_said(said) == expected


@pytest.mark.parametrize("said", ["check the weather", "check my email", "make me a sandwich",
                                  "delete the file", "what's the weather"])
def test_not_checklists(said):
    assert checklist_said(said) is None


async def test_a_grocery_list_by_saying_it(fake, lists):
    said = await fake.say("make a grocery list with eggs, milk and bread")
    assert said.startswith("Made it.") and "☐ eggs" in said

    assert await fake.say("add butter to my groceries list") == "Added 1 to **Grocery list**."
    assert await fake.say("add milk to my grocery list") == "Those are already on **Grocery list**."
    assert (await fake.say("check off eggs")).startswith("Checked off eggs. 3 left")
    assert (await fake.say("uncheck eggs")).startswith("Unchecked eggs.")

    grocery = await lists.get((await lists.find("grocery"))["id"])
    assert [i["text"] for i in grocery["items"]] == ["eggs", "milk", "bread", "butter"]
    assert not any(i["done"] for i in grocery["items"])


async def test_ticking_everything_and_clearing(fake, lists):
    await fake.say("make a workout checklist: squats, push-ups, plank")
    for item in ("squats", "push-ups"):
        await fake.say(f"check off {item}")
    assert (await fake.say("check off plank")).endswith("That's everything!")
    assert await fake.say("reset my workout list") == "Everything on **Workout** is unchecked again."
    await fake.say("check off squats")
    assert await fake.say("clear the checked items from my workout list") == \
        "Cleared 1 checked item from **Workout**."
    workout = await lists.get((await lists.find("workout"))["id"])
    assert [i["text"] for i in workout["items"]] == ["push-ups", "plank"]


async def test_an_item_on_two_lists_asks_which(fake):
    await fake.say("make a grocery list: eggs")
    await fake.say("make a camping list: eggs, tent")
    said = await fake.say("check off eggs")
    assert "more than one checklist" in said
    assert (await fake.say("check off eggs on my camping list")).startswith("Checked off eggs")


async def test_show_answers_in_words_too(fake):
    """On Discord there's no panel to open, so the reply itself has the list."""
    assert "don't have any checklists" in await fake.say("show me my checklists")
    await fake.say("make a grocery list: eggs, milk")
    assert "**Grocery list** (0 of 2 done)" in await fake.say("show me my checklists")
    assert "☐ milk" in await fake.say("what's on my grocery list?")
    assert "There's no checklist called “holiday”" in await fake.say("what's on my holiday list?")


async def test_deleting_a_list_is_risky_and_asks(registry, fake):
    assert registry.get("delete_checklist").risk == "medium"


def test_describe():
    assert describe({"title": "Empty", "items": []}) == "**Empty** is empty."


# -- the panel's API ----------------------------------------------------------
async def test_the_panel_api(lists, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.api import checklist_routes
    from backend.api.deps import current_user

    monkeypatch.setattr(checklist_routes.container, "get", lambda name: lists if name == "checklists" else None)
    app = FastAPI()
    app.include_router(checklist_routes.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: {"id": "owner"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        made = (await client.post("/api/checklists", json={"title": "Groceries", "items": ["eggs"]})).json()
        added = (await client.post(f"/api/checklists/{made['id']}/items", json={"items": ["milk"]})).json()
        eggs = added["items"][0]
        ticked = (await client.patch(f"/api/checklists/{made['id']}/items/{eggs['id']}", json={"done": True})).json()
        assert ticked["items"][0]["done"] is True
        summary = (await client.get("/api/checklists")).json()["checklists"][0]
        assert (summary["title"], summary["total"], summary["done"]) == ("Groceries", 2, 1)
        cleared = (await client.post(f"/api/checklists/{made['id']}/reset", json={"mode": "clear_checked"})).json()
        assert [i["text"] for i in cleared["items"]] == ["milk"]
        assert (await client.get("/api/checklists/nope")).status_code == 404
        assert (await client.delete(f"/api/checklists/{made['id']}")).status_code == 200
        assert (await client.get("/api/checklists")).json()["checklists"] == []


# -- scrolling back --------------------------------------------------------------
async def _conversation(db, cid, when):
    await db.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                     (cid, "t", when, when))


async def _message(db, mid, cid, role, text, when, blocks="[]"):
    await db.execute("INSERT INTO messages (id, conversation_id, role, text, blocks, created_at)"
                     " VALUES (?,?,?,?,?,?)", (mid, cid, role, text, blocks, when))


async def test_scrolling_back_pages_through_what_was_said(db):
    from backend.ai.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.db = db
    await _conversation(db, "yesterday", "2026-11-25T20:00:00+00:00")
    await _conversation(db, "today", "2026-11-26T09:00:00+00:00")
    await _conversation(db, "discord", "2026-11-26T09:30:00+00:00")
    for n in range(5):
        await _message(db, f"y{n}", "yesterday", "user" if n % 2 == 0 else "assistant",
                       f"yesterday {n}", f"2026-11-25T20:0{n}:00+00:00")
    await _message(db, "t0", "today", "user", "what's the weather?", "2026-11-26T09:00:00+00:00")
    await _message(db, "t1", "today", "assistant", "Let me check.", "2026-11-26T09:00:01+00:00",
                   '[{"type": "tool_use", "id": "x", "name": "weather", "input": {}}]')
    await _message(db, "t2", "today", "user", "", "2026-11-26T09:00:02+00:00",
                   '[{"type": "tool_result", "tool_use_id": "x", "content": "12C"}]')
    await _message(db, "t3", "today", "assistant", "12 degrees.", "2026-11-26T09:00:03+00:00")
    await _message(db, "d0", "discord", "user", "from discord", "2026-11-26T09:30:00+00:00")

    first = await orchestrator.timeline(limit=3, exclude=["discord"])
    assert [m["text"] for m in first["messages"]] == ["yesterday 4", "what's the weather?", "12 degrees."]
    assert first["more"] is True, "the tool round and Discord are left out"

    older = await orchestrator.timeline(before=first["messages"][0]["created_at"], limit=10, exclude=["discord"])
    assert [m["text"] for m in older["messages"]] == [f"yesterday {n}" for n in range(4)]
    assert older["more"] is False


async def test_the_thread_still_going_carries_on(db):
    from datetime import timedelta

    from backend.ai.orchestrator import Orchestrator
    from backend.core.util import utcnow

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.db = db
    await _conversation(db, "old", (utcnow() - timedelta(hours=9)).isoformat())
    assert await orchestrator.recent_conversation() is None, "hours ago: a new thread"
    await _conversation(db, "now", (utcnow() - timedelta(minutes=5)).isoformat())
    await _conversation(db, "discord", utcnow().isoformat())
    assert await orchestrator.recent_conversation(exclude=["discord"]) == "now"
