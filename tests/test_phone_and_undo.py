"""Phone polish, sources under answers, and "undo that".

- "undo that" / "change it back" puts the last setting or theme change made
  by asking back the way it was -- and leaves "undo that edit" to self-edit.
- A looked-up answer carries the pages it read, for the screen to link.
- The built interface is served so a phone never keeps a stale page, and
  only files inside it can be fetched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ai.tools import control_tools as ct


@pytest.fixture(autouse=True)
def _no_old_changes():
    ct._CHANGES.clear()
    yield
    ct._CHANGES.clear()


async def _fake(registry, settings_store):
    ct.register(registry)
    registry.context._services["settings_store"] = settings_store
    return SimpleNamespace(registry=registry)


async def test_undo_puts_the_theme_back(registry, settings_store):
    from backend.ai.orchestrator import Orchestrator

    fake = await _fake(registry, settings_store)
    await settings_store.patch("theme", {"preset": "arc_reactor"})
    before = dict(await settings_store.get("theme"))

    assert await Orchestrator._plain_setting(fake, "switch to the ember theme") == "Switched to the Ember theme."
    assert await Orchestrator._plain_setting(fake, "undo that") == "Back to the Arc Reactor theme."
    assert await settings_store.get("theme") == before, "every colour back, not just the name"


async def test_undo_puts_a_setting_back_and_only_once(registry, settings_store):
    from backend.ai.orchestrator import Orchestrator

    fake = await _fake(registry, settings_store)
    await settings_store.patch("voice", {"wake_word_enabled": False, "rate": 1.0})

    await Orchestrator._plain_setting(fake, "speak a bit slower")
    await Orchestrator._plain_setting(fake, "turn on the wake word")
    assert await Orchestrator._plain_setting(fake, "Jarvis, change it back please") == "Wake word is back to off."
    assert (await settings_store.get("voice"))["wake_word_enabled"] is False
    assert (await settings_store.get("voice"))["rate"] == 0.8, "only the last change"

    assert await Orchestrator._plain_setting(fake, "undo that") == "Speaking speed is back to 1.0."
    # Nothing left to undo: not ours to answer, the model gets it.
    assert await Orchestrator._plain_setting(fake, "undo that") is None


async def test_a_change_to_the_same_value_is_not_an_undo_step(registry, settings_store):
    from backend.ai.orchestrator import Orchestrator

    fake = await _fake(registry, settings_store)
    await settings_store.patch("voice", {"wake_word_enabled": True})
    await Orchestrator._plain_setting(fake, "turn on the wake word")         # already on
    assert ct.last_change() is None


@pytest.mark.parametrize("said", ["undo that edit", "undo that change", "undo the last change",
                                  "undo my reminder", "put the kettle back"])
def test_other_undos_are_left_alone(said):
    ct._CHANGES.append({"at": __import__("time").monotonic(), "key": "theme", "before": {}, "what": "theme"})
    assert not ct.plain_undo(said)


def test_an_old_change_is_not_undone(monkeypatch):
    ct._CHANGES.append({"at": 0.0, "key": "theme", "before": {}, "what": "theme"})
    monkeypatch.setattr(ct.time, "monotonic", lambda: ct.UNDO_WINDOW_S + 10.0)
    assert ct.last_change() is None
    assert not ct.plain_undo("undo that")


# ---------------------------------------------------------------------------
async def test_a_look_up_hands_the_screen_the_pages_it_read(registry, monkeypatch):
    from backend.ai.tools import web_tools
    from backend.services import live_lookup

    async def found(web, question):
        return {"query": "bears vs lions", "results": [], "pages": [
            {"title": "Picks", "url": "https://www.cbssports.com/picks", "source": "CBS Sports",
             "date": "", "excerpts": ["The Bears are 1.5-point favourites."]},
            {"title": "Odd", "url": "javascript:alert(1)", "source": "Bad", "date": "",
             "excerpts": ["x"]},
        ]}

    monkeypatch.setattr(live_lookup, "look_up", found)
    web_tools.register(registry)
    result = await registry.dispatch("live_lookup", {"question": "who wins bears v lions"})
    assert result.ok
    assert result.meta["sources"] == [
        {"title": "Picks", "url": "https://www.cbssports.com/picks", "source": "CBS Sports"}]


# ---------------------------------------------------------------------------
def _interface(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.frontend_files import mount_interface

    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>JARVIS</html>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"name": "JARVIS"}', encoding="utf-8")
    (tmp_path / "frontend" / "secret.txt").write_text("SECRET", encoding="utf-8")
    app = FastAPI()
    mount_interface(app, dist)
    return TestClient(app)


def test_the_page_is_never_cached_so_a_phone_sees_updates(tmp_path):
    client = _interface(tmp_path)
    for path in ("/", "/some/screen"):
        page = client.get(path)
        assert page.status_code == 200 and "JARVIS" in page.text
        assert page.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_cached_for_good(tmp_path):
    response = _interface(tmp_path).get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_the_home_screen_manifest_has_its_type(tmp_path):
    response = _interface(tmp_path).get("/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")


@pytest.mark.parametrize("path", ["/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt"])
def test_nothing_outside_the_interface_is_served(tmp_path, path):
    response = _interface(tmp_path).get(path)
    assert "SECRET" not in response.text


def test_inside():
    from pathlib import Path

    from backend.api.frontend_files import inside

    root = Path(__file__).resolve().parent
    assert inside(root, "conftest.py") == root / "conftest.py"
    assert inside(root, "../pytest.ini") is None
    assert inside(root, "") is None


# ---------------------------------------------------------------------------
def test_emojis_are_for_reading_not_speaking():
    from backend.voice.service import without_emojis

    assert without_emojis("✅ Done! 🛒 Added milk. ⚠️ It's 5°C, wear a coat 🧥.") == \
        "Done! Added milk. It's 5°C, wear a coat."


@pytest.mark.parametrize("said, value", [("stop using emojis", "off"), ("use emojis", "on"),
                                         ("turn off emojis", "off"), ("start using emojis again", "on")])
def test_emojis_by_saying_it(said, value):
    assert ct.plain_setting(said) == ("emojis", value)


def test_the_prompt_follows_the_emoji_setting():
    from backend.ai.prompt import _emoji_rule

    rule = _emoji_rule({})
    assert "one emoji" in rule
    assert not any(ch in rule for ch in "✅🛒📅⚠"), "no examples for a small model to copy"
    assert _emoji_rule({"emojis": False}) == "- Don't use emojis."
