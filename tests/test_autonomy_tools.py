"""Asking JARVIS what it built, and having it handed over.

The While away tab shows the result. These are the same facts reached by
simply asking -- and the same rule applies to them: what is said to be in
the app is what was kept, what is still wrong is said first, and nothing is
recalled from having meant to do it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.ai.tools import build_registry
from backend.autonomy.guard import NEVER_UNATTENDED, Guard, Verdict
from backend.autonomy.service import AutonomyService
from backend.autonomy.session import HIDDEN, RECORD, AutonomySession, Phase

PAGE = "<!DOCTYPE html><html><body><h1>Habits</h1></body></html>"


def _service(tmp_path) -> AutonomyService:
    class _Settings:
        DATA_DIR = tmp_path

    return AutonomyService(_Settings())


def _finished_build(service: AutonomyService, trial=None) -> Path:
    """A build on disk, as it is after a restart: files and a record."""
    workspace = service.root / "20261114-120000"
    (workspace / HIDDEN).mkdir(parents=True)
    (workspace / "index.html").write_text(PAGE, encoding="utf-8")
    (workspace / HIDDEN / RECORD).write_text(json.dumps({
        "prompt": "build me a habit tracker app", "mode": "build",
        "phase": "done", "running": False, "artifact": "index.html",
        "app": {"file": "index.html", "lines": 240, "version": 5,
                "checked": "browser", "trial": trial or {}},
        "steps": [
            {"title": "A first working version", "kept": True, "detail": "190 lines"},
            {"title": "Add streak counter", "kept": True, "detail": "+22 −0 lines"},
            {"title": "Add reminders", "kept": False, "detail": "it returned the file unchanged"},
        ],
        "report": "Built index.html",
    }), encoding="utf-8")
    return workspace


class _Taildrop:
    def __init__(self):
        self.sent = []

    async def send(self, path, device):
        self.sent.append((Path(path), device))
        return {"ok": True, "detail": f"Sent to {device}."}


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_asked_what_it_built_it_says_what_is_in_the_app_and_what_is_not(tmp_path):
    service = _service(tmp_path)
    _finished_build(service)
    registry = build_registry({"autonomy": service})

    result = await registry.get("away_work_report").handler(registry.context)

    assert result.ok
    said = result.message
    assert "I built index.html (240 lines" in said
    assert "In the app: Add streak counter." in said
    assert "they are NOT in the app: Add reminders." in said
    # The feature that was thrown away is nowhere described as present.
    assert "Add reminders" not in said.split("I tried these")[0]
    assert result.data["in_the_app"] == ["A first working version", "Add streak counter"]
    assert result.data["tried_and_not_kept"][0]["why"] == "it returned the file unchanged"
    assert result.data["app_file"].endswith("index.html")


@pytest.mark.asyncio
async def test_what_is_still_wrong_is_said_before_what_was_added(tmp_path):
    service = _service(tmp_path)
    _finished_build(service, trial={
        "ran": True, "saved": False,
        "problems": ["what was added was gone after reloading the page, so "
                     "it is not being saved"]})
    registry = build_registry({"autonomy": service})

    result = await registry.get("away_work_report").handler(registry.context)

    said = result.message
    assert "STILL WRONG" in said and "not being saved" in said
    assert said.index("STILL WRONG") < said.index("In the app")
    assert result.data["still_wrong"]


@pytest.mark.asyncio
async def test_asked_with_nothing_built_it_says_so(tmp_path):
    registry = build_registry({"autonomy": _service(tmp_path)})
    result = await registry.get("away_work_report").handler(registry.context)
    assert not result.ok and "Nothing has been built" in result.message


@pytest.mark.asyncio
async def test_saying_you_are_back_in_conversation_hands_over_and_frees_the_slot(tmp_path):
    service = _service(tmp_path)
    workspace = _finished_build(service)
    session = AutonomySession("build me a habit tracker app", 60, 60, workspace)
    session.started_at = datetime.now(timezone.utc)
    session.mode, session.artifact = "build", "index.html"
    session.app = {"file": "index.html", "lines": 240, "version": 5, "checked": "browser"}
    session.steps = [{"title": "A first working version", "kept": True, "detail": ""},
                     {"title": "Add streak counter", "kept": True, "detail": ""}]
    session.phase = Phase.DONE
    service.current = session
    registry = build_registry({"autonomy": service})

    result = await registry.get("welcome_back").handler(registry.context)

    assert result.ok and result.message.startswith("Welcome back.")
    assert "Add streak counter" in result.message
    assert result.data["acknowledged"] is True
    assert service.current is None, "the slot must be free for the next run"


@pytest.mark.asyncio
async def test_the_app_is_sent_from_the_run_and_from_nowhere_else(tmp_path):
    """The model supplies no path. There is nothing for it to supply one
    in -- so "send the app to my phone" cannot be talked into sending some
    other file on the machine."""
    service = _service(tmp_path)
    workspace = _finished_build(service)
    taildrop = _Taildrop()
    registry = build_registry({"autonomy": service, "taildrop": taildrop})
    tool = registry.get("send_built_app_to_device")

    assert set(tool.input_schema.get("properties", {})) == {"device"}

    result = await tool.handler(registry.context)
    assert result.ok and "Sent index.html" in result.message
    assert taildrop.sent == [((workspace / "index.html").resolve(), "phone")]


@pytest.mark.asyncio
async def test_sending_asks_first(tmp_path):
    from backend.core.permissions import Risk

    registry = build_registry({"autonomy": _service(tmp_path), "taildrop": _Taildrop()})
    tool = registry.get("send_built_app_to_device")
    assert tool.risk == Risk.MEDIUM
    assert "phone" in tool.confirm_summary({})


@pytest.mark.asyncio
async def test_with_no_app_there_is_nothing_to_send(tmp_path):
    taildrop = _Taildrop()
    registry = build_registry({"autonomy": _service(tmp_path), "taildrop": taildrop})
    result = await registry.get("send_built_app_to_device").handler(registry.context)
    assert not result.ok and taildrop.sent == []


@pytest.mark.parametrize("tool", ["welcome_back", "send_built_app_to_device"])
def test_a_run_cannot_work_its_own_controls(tmp_path, tool):
    """A run that can announce the owner is back has a way to end itself
    and clear its own report; one that can send files is deciding what
    lands on their phone."""
    assert tool in NEVER_UNATTENDED
    assert Guard(tmp_path).check(tool, {}).verdict is Verdict.REFUSE


def test_the_tools_can_be_found_from_how_people_ask():
    """Local models are shown a handful of tools chosen by the words in the
    request, so these have to be reachable from ordinary phrasing."""
    registry = build_registry({})
    for request, wanted in (
        ("what did you build while I was away", "away_work_report"),
        ("is the app finished", "away_work_report"),
        ("send the app to my phone", "send_built_app_to_device"),
    ):
        chosen = [schema["name"] for schema in registry.select_schemas(request, limit=12)]
        assert wanted in chosen, (request, chosen)


@pytest.mark.parametrize("said", ["I'm back", "im back", "I am back", "Im home",
                                  "hey jarvis, I'm back", "I've returned",
                                  "we're home"])
def test_saying_you_are_back_reaches_the_tool_for_it(tmp_path, said):
    """Every word of "I'm back" is scaffolding to the ranking, so it scored
    nothing and the one tool it was meant for was never offered."""
    service = _service(tmp_path)
    service.current = AutonomySession("build a thing", 60, 60, tmp_path / "w")
    registry = build_registry({"autonomy": service})

    chosen = [schema["name"] for schema in registry.select_schemas(said, limit=12)]
    assert "welcome_back" in chosen[:4], chosen


@pytest.mark.parametrize("said", ["I'm back", "go back to the previous song",
                                  "roll back the last change", "back up my notes"])
def test_it_is_not_offered_when_there_is_nothing_to_hand_over(tmp_path, said):
    """It ends a run that is still going. So it is not put in front of a
    small model on the strength of the word "back" -- and not at all
    unless there is unattended work waiting."""
    idle = build_registry({"autonomy": _service(tmp_path)})
    chosen = [schema["name"] for schema in idle.select_schemas(said, limit=12)]
    assert "welcome_back" not in chosen


@pytest.mark.parametrize("said", ["go back to the previous song",
                                  "roll back the last change", "back up my notes",
                                  "call me back at five"])
def test_other_uses_of_the_word_back_do_not_offer_it_mid_run(tmp_path, said):
    service = _service(tmp_path)
    service.current = AutonomySession("build a thing", 60, 60, tmp_path / "w")
    registry = build_registry({"autonomy": service})

    chosen = [schema["name"] for schema in registry.select_schemas(said, limit=12)]
    assert "welcome_back" not in chosen
