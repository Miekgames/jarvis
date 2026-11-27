"""JARVIS on Discord: only the paired owner gets JARVIS, and it stays safe.

Uses stand-ins for discord.py's client and messages, so nothing here talks
to Discord.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from backend.ai.tools.discord_tools import discord_said
from backend.integrations import discord_bridge as db_mod
from backend.integrations.discord_bridge import DiscordBridge, chunks, strip_mention, with_sources

BOT_ID = 999


class Channel:
    def __init__(self, cid=1):
        self.id = cid
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    def typing(self):
        @contextlib.asynccontextmanager
        async def typing():
            yield
        return typing()


class Message:
    def __init__(self, text, author_id=42, name="owner#0001", dm=True, mentions=(), channel=None, bot=False):
        self.content = text
        self.author = type("Author", (), {"id": author_id, "bot": bot, "__str__": lambda self: name})()
        self.guild = None if dm else SimpleNamespace(id=5)
        self.mentions = list(mentions)
        self.channel = channel or Channel()
        self.replies = []
        self.reactions = []

    async def reply(self, text, mention_author=False):
        self.replies.append(text)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class Orchestrator:
    def __init__(self, reply="Hello from JARVIS.", sources=()):
        self.reply, self.sources, self.asked = reply, list(sources), []

    async def ensure_conversation(self, conversation_id=None):
        return conversation_id or "conv_discord"

    async def chat(self, text, conversation_id=None, source="text"):
        self.asked.append((text, conversation_id, source))
        yield {"type": "start"}
        if self.sources:
            yield {"type": "tool_end", "tool": "live_lookup", "sources": self.sources}
        yield {"type": "final", "text": self.reply}


class Permissions:
    def __init__(self, on=True):
        self.on = on

    def allowed(self, key):
        return self.on if key == "discord.chat" else True


class Confirmations:
    def __init__(self):
        self.resolved = []

    async def resolve(self, cid, approved, actor="user"):
        self.resolved.append((cid, approved, actor))


@pytest.fixture
async def bridge(settings_store):
    bot_user = SimpleNamespace(id=BOT_ID)
    made = DiscordBridge(settings_store, Orchestrator(), Permissions(), Confirmations())
    made.client = SimpleNamespace(user=bot_user, is_closed=lambda: False, guilds=[])
    return made


async def _pair(bridge, author_id=42):
    code = bridge.new_pairing_code()
    message = Message(f"pair {code}", author_id=author_id)
    await bridge.handle(message)
    return message


async def test_a_stranger_gets_one_polite_line_and_nothing_runs(bridge):
    first, second = Message("delete my files", author_id=7), Message("hello?", author_id=7)
    await bridge.handle(first)
    await bridge.handle(second)
    assert len(first.replies) == 1 and "only answer the person who runs me" in first.replies[0]
    assert second.replies == [], "once an hour, not a reply per message"
    assert bridge.orchestrator.asked == []


async def test_pairing_with_the_code_then_chatting(bridge):
    paired = await _pair(bridge)
    assert paired.replies and paired.replies[0].startswith("Paired.")
    assert "AI assistant" in paired.replies[0], "says what it is"
    assert (await bridge.status())["owners"][0]["id"] == "42"

    message = Message("what's the weather?")
    await bridge.handle(message)
    assert bridge.orchestrator.asked == [("what's the weather?", "conv_discord", "discord")]
    assert message.replies == ["Hello from JARVIS."]


async def test_a_wrong_or_used_code_pairs_nobody(bridge):
    code = bridge.new_pairing_code()
    wrong = Message("pair 000000" if code != "000000" else "pair 111111", author_id=8)
    await bridge.handle(wrong)
    assert "isn't right" in wrong.replies[0]
    await _pair(bridge, author_id=42)
    again = Message(f"pair {code}", author_id=8)
    await bridge.handle(again)                 # the code was used up
    assert [o["id"] for o in (await bridge.status())["owners"]] == ["42"]


async def test_an_expired_code_pairs_nobody(bridge, monkeypatch):
    code = bridge.new_pairing_code()
    monkeypatch.setattr(db_mod.time, "time", lambda: bridge._code_until + 1)
    message = Message(f"pair {code}", author_id=8)
    await bridge.handle(message)
    assert (await bridge.status())["owners"] == []


async def test_revoking_the_permission_stops_it_at_once(bridge):
    await _pair(bridge)
    bridge.permissions.on = False
    message = Message("open notepad")
    await bridge.handle(message)
    assert "switched off" in message.replies[0]
    assert bridge.orchestrator.asked == []


async def test_servers_are_off_by_default(bridge):
    await _pair(bridge)
    bot = bridge.client.user
    in_server = Message(f"<@{BOT_ID}> hi", dm=False, mentions=[bot])
    await bridge.handle(in_server)
    assert bridge.orchestrator.asked == [] and in_server.replies == []

    await bridge.set_servers(True)
    await bridge.handle(in_server)
    assert bridge.orchestrator.asked[-1][0] == "hi", "the mention is taken out"
    unmentioned = Message("chatting with friends", dm=False)
    await bridge.handle(unmentioned)
    assert len(bridge.orchestrator.asked) == 1, "a server message not to the bot is ignored"


async def test_other_bots_are_ignored(bridge):
    await _pair(bridge)
    await bridge.handle(Message("hi", author_id=42, bot=True))
    assert bridge.orchestrator.asked == []


async def test_yes_in_the_chat_approves_what_is_waiting(bridge):
    await _pair(bridge)
    channel = Channel(cid=1)
    bridge._waiting[1] = "cfm_1"
    yes = Message("yes", channel=channel)
    await bridge.handle(yes)
    assert bridge.confirmations.resolved == [("cfm_1", True, "discord:42")]
    assert bridge.orchestrator.asked == [], "a yes is an answer, not a new question"
    bridge._waiting[1] = "cfm_2"
    await bridge.handle(Message("no", channel=channel))
    assert bridge.confirmations.resolved[-1][:2] == ("cfm_2", False)


async def test_only_the_owner_can_approve(bridge):
    await _pair(bridge)
    bridge._waiting[1] = "cfm_1"
    await bridge.handle(Message("yes", author_id=7, channel=Channel(cid=1)))
    assert bridge.confirmations.resolved == []


async def test_the_confirmation_is_asked_in_the_chat_it_came_from(bridge, settings_store):
    channel = Channel(cid=1)
    bridge.client.get_channel = lambda cid: channel if cid == 1 else None
    await settings_store.patch("discord", {"conversations": {"1": "conv_discord"}})
    event = SimpleNamespace(data={"id": "cfm_9", "summary": "Delete report.docx", "risk": "high",
                                  "conversation_id": "conv_discord", "timeout": 120})
    await bridge._on_confirmation(event)
    assert bridge._waiting[1] == "cfm_9"
    assert "Delete report.docx" in channel.sent[0] and "yes" in channel.sent[0]

    other = SimpleNamespace(data={"id": "cfm_10", "conversation_id": "conv_screen"})
    await bridge._on_confirmation(other)
    assert len(channel.sent) == 1, "a confirmation from the screen stays on the screen"


async def test_sources_come_as_links_that_do_not_unfurl(bridge):
    await _pair(bridge)
    bridge.orchestrator.sources = [{"url": "https://www.cbssports.com/x", "source": "CBS Sports"},
                                   {"url": "javascript:alert(1)", "source": "Bad"}]
    message = Message("who wins?")
    await bridge.handle(message)
    assert "[CBS Sports](<https://www.cbssports.com/x>)" in message.replies[0]
    assert "javascript" not in message.replies[0]


def test_long_replies_are_split_under_discords_limit():
    text = ("A sentence that goes on. " * 200).strip()
    parts = chunks(text)
    assert len(parts) > 1 and all(len(p) <= db_mod.LIMIT for p in parts)
    assert " ".join(parts).replace("  ", " ") == text


def test_mentions_are_taken_out():
    assert strip_mention(f"<@{BOT_ID}> what's up", BOT_ID) == "what's up"
    assert strip_mention(f"hey <@!{BOT_ID}>", BOT_ID) == "hey"


def test_with_sources_without_any():
    assert with_sources("Hi.", []) == "Hi."


async def test_a_token_that_isnt_one_is_refused(bridge):
    with pytest.raises(ValueError):
        await bridge.set_token("hunter2")


async def test_the_token_is_sealed_and_never_shown(bridge, settings_store, monkeypatch):
    async def no_restart():
        return None
    monkeypatch.setattr(bridge, "restart", no_restart)
    # Made up, and built from pieces so no token-shaped text sits in the code
    # (GitHub's secret scanning blocks a push that contains one).
    token = ".".join(["MTIz" + "NDU2" * 5, "Gab" + "cDE", "abcdefghij" * 3 + "AB"])
    status = await bridge.set_token(token)
    saved = (await settings_store.get("discord"))["token"]
    assert token not in saved
    assert token not in str(status)
    from backend.core import secrets
    assert secrets.unseal(saved) == token


@pytest.mark.parametrize("said, expected", [
    ("pair discord", ("discord_pair", {})),
    ("Jarvis, connect my Discord please", ("discord_pair", {})),
    ("turn off discord", ("discord_switch", {"on": False})),
    ("start the discord bot", ("discord_switch", {"on": True})),
    ("unpair discord", ("discord_unpair", {})),
    ("answer me in discord servers too", ("discord_servers", {"on": True})),
    ("only answer me in dms", ("discord_servers", {"on": False})),
    ("what is discord", None),
    ("turn on the wake word", None),
])
def test_saying_it(said, expected):
    assert discord_said(said) == expected


# ---------------------------------------------------------------------------
class DMUser:
    def __init__(self):
        self.dm_channel = Channel(cid=77)

    async def create_dm(self):
        return self.dm_channel


async def test_notifications_reach_discord_only_when_asked(bridge):
    await _pair(bridge)
    user = DMUser()
    bridge.client.get_user = lambda uid: user if uid == 42 else None
    event = SimpleNamespace(data={"title": "Reminder", "body": "Call Dad"})

    await bridge._on_notification(event)
    assert user.dm_channel.sent == [], "off until the owner asks"

    await bridge.set_notify(True)
    await bridge._on_notification(event)
    assert user.dm_channel.sent == ["🔔 **Reminder**\nCall Dad"]

    bridge.permissions.on = False
    await bridge._on_notification(event)
    assert len(user.dm_channel.sent) == 1, "the permission still gates it"


@pytest.mark.parametrize("said, expected", [
    ("send my notifications to discord", ("discord_notify", {"on": True})),
    ("send me reminders on discord too", ("discord_notify", {"on": True})),
    ("stop sending notifications to discord", ("discord_notify", {"on": False})),
])
def test_saying_notifications(said, expected):
    assert discord_said(said) == expected


# ---------------------------------------------------------------------------
class Attachment:
    def __init__(self, content_type="audio/ogg", size=20_000, filename="voice-message.ogg"):
        self.content_type, self.size, self.filename = content_type, size, filename

    async def read(self):
        return b"OggS..."


class Speech:
    def __init__(self, words="what's on my grocery list"):
        self.words = words

    async def transcribe(self, data, filename="audio.webm"):
        return {"text": self.words, "duration": 2.0}


async def test_a_voice_message_is_heard_and_answered(bridge):
    await _pair(bridge)
    bridge.calls.tts = Speech()
    message = Message("")
    message.attachments = [Attachment()]
    await bridge.handle(message)
    assert bridge.orchestrator.asked[-1][0] == "what's on my grocery list"
    assert message.replies[0].startswith("🎙️ *“what's on my grocery list”*")
    assert "Hello from JARVIS." in message.replies[0]


async def test_a_voice_message_with_no_words(bridge):
    await _pair(bridge)
    bridge.calls.tts = Speech(words="")
    message = Message("")
    message.attachments = [Attachment()]
    await bridge.handle(message)
    assert bridge.orchestrator.asked == [] and "couldn't hear any words" in message.replies[0]


async def test_a_stranger_voice_message_is_not_transcribed(bridge):
    bridge.calls.tts = Speech()
    message = Message("", author_id=7)
    message.attachments = [Attachment()]
    await bridge.handle(message)
    assert bridge.orchestrator.asked == [] and "only answer the person" in message.replies[0]


async def test_an_image_is_not_a_voice_message(bridge):
    await _pair(bridge)
    bridge.calls.tts = Speech()
    message = Message("")
    message.attachments = [Attachment(content_type="image/png")]
    await bridge.handle(message)
    assert bridge.orchestrator.asked == [] and message.replies == []
