"""/help: everything JARVIS can do -- only what this copy really has."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ai.tools import build_registry
from backend.services.help import SECTIONS, build_help, help_said


@pytest.fixture(scope="module")
def registry():
    return build_registry({})


@pytest.mark.parametrize("said, topic", [
    ("/help", ""), ("help", ""), ("/commands", ""), ("what can you do?", ""),
    ("Jarvis, what can you do", ""), ("list your commands", ""), ("show me your commands", ""),
    ("/help discord", "discord"), ("/help email", "email"), ("help with email", "email"),
    ("help skills", "skills"), ("/help me write a poem", "me write a poem"),
])
def test_asking_for_help(said, topic):
    assert help_said(said) == topic


@pytest.mark.parametrize("said", ["help me write an email", "help me", "helpful tips",
                                  "can you help with my homework", "what's the weather"])
def test_requests_that_are_not_about_jarvis(said):
    assert help_said(said) is None


def test_every_line_is_a_real_tool(registry):
    # An area that needs a tool this edition doesn't have is left out whole.
    missing = [(s.key, line.tool) for s in SECTIONS for line in s.lines
               if registry.get(line.tool) is None and not (s.needs and registry.get(s.needs) is None)]
    assert missing == [], "a help line for a tool that doesn't exist would be a fake feature"


def test_the_overview_covers_every_area(registry):
    text = build_help(registry)
    for section in SECTIONS:
        if section.needs and registry.get(section.needs) is None:
            assert f"/help {section.key}" not in text, section.key
        else:
            assert f"/help {section.key}" in text, section.key
    assert "who's projected to win tonight's game?" in text


def test_a_missing_tool_is_not_listed(registry, monkeypatch):
    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: None if name.startswith("discord_") else real_get(name))
    text = build_help(registry)
    assert "Discord" not in text
    assert "doesn't have discord" in build_help(registry, topic="discord")


def test_an_area_needing_a_tool_is_left_out_whole(registry, monkeypatch):
    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: None if name == "self_edit_model" else real_get(name))
    assert "Editing JARVIS itself" not in build_help(registry)
    assert "apply it" not in build_help(registry, topic="selfedit")


def test_a_switched_off_permission_says_which_switch(registry):
    permissions = SimpleNamespace(allowed=lambda key: key != "email.send")
    text = build_help(registry, permissions=permissions, topic="email")
    assert "reply and say I'll be there" in text
    assert "turn on “Send email” in Permissions" in text
    assert "Some of it is switched off" in build_help(registry, permissions=permissions)


def test_skills_are_listed_by_their_slash_command(registry):
    skills = SimpleNamespace(all=lambda: [
        SimpleNamespace(name="remind", description="Set reminders", enabled=True),
        SimpleNamespace(name="old", description="Off", enabled=False),
    ])
    assert "`/remind`" in build_help(registry, skills=skills)
    detail = build_help(registry, skills=skills, topic="skills")
    assert "`/remind` — Set reminders" in detail and "/old" not in detail


def test_an_unknown_area(registry):
    assert "There's no help for “banana”" in build_help(registry, topic="banana")


def test_help_is_answered_at_once_without_the_model(registry):
    from backend.ai.orchestrator import Orchestrator

    fake = SimpleNamespace(registry=registry)
    fake.help_text = lambda topic="": Orchestrator.help_text(fake, topic)
    assert Orchestrator._plain_help(fake, "/help").startswith("## What JARVIS can do")
    assert Orchestrator._plain_help(fake, "/help reminders").startswith("**Reminders and timers**")
    assert Orchestrator._plain_help(fake, "help me write an email") is None


# ---------------------------------------------------------------------------
class Interaction:
    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


async def test_discord_slash_help_is_for_the_owner_only(settings_store, registry):
    from backend.integrations.discord_bridge import DiscordBridge

    orchestrator = SimpleNamespace(help_text=lambda topic="": build_help(registry, topic=topic))
    bridge = DiscordBridge(settings_store, orchestrator)
    await settings_store.patch("discord", {"owners": [{"id": "42", "name": "owner"}]})

    stranger = Interaction(7)
    await bridge.slash_help(stranger, "")
    assert len(stranger.sent) == 1 and "private AI assistant" in stranger.sent[0][0]

    owner = Interaction(42)
    await bridge.slash_help(owner, "")
    assert owner.sent[0][0].startswith("## What JARVIS can do")
    assert all(ephemeral for _, ephemeral in owner.sent), "only the owner sees it, even in a server"
    assert all(len(text) <= 2000 for text, _ in owner.sent)
