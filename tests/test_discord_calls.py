"""Calling the owner on Discord: DM to join voice, join when they're in, speak.

Stand-ins for discord.py's client, channels and voice connection: nothing
here talks to Discord.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.ai.tools.discord_tools import discord_said
from backend.integrations import discord_voice
from backend.integrations.discord_bridge import DiscordBridge
from backend.integrations.discord_voice import speakable

OWNER = 42


class DM:
    def __init__(self):
        self.id = 900
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class Voice:
    """A connected voice client: plays at once, remembers what it played."""

    def __init__(self, channel):
        self.channel = channel
        self.connected = True
        self.played = 0
        self.listeners = []
        self.mode = "aead_xchacha20_poly1305_rtpsize"
        self.secret_key = list(range(32))
        self._connection = SimpleNamespace(add_socket_listener=self.listeners.append,
                                           remove_socket_listener=self.listeners.remove,
                                           dave_protocol_version=0, dave_session=None)

    def is_connected(self):
        return self.connected

    def play(self, source, after=None):
        self.played += 1
        if after:
            after(None)

    async def disconnect(self, force=False):
        self.connected = False


class VoiceChannel:
    def __init__(self, guild, name="General", cid=500, people=()):
        self.guild, self.name, self.id = guild, name, cid
        self.voice_states = {p: object() for p in people}
        self.mention = f"<#{cid}>"
        self.joined = None

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, connect=True, speak=True)

    async def connect(self, self_deaf=False):
        self.joined = Voice(self)
        self.joined.deafened = self_deaf
        return self.joined


class TTS:
    def __init__(self):
        self.said = []
        self.speaking = False
        self.heard_while_speaking = []

    def speaking_elsewhere(self, text):
        self.speaking = True
        self.heard_while_speaking.append(text)

    def done_speaking_elsewhere(self):
        self.speaking = False

    async def synthesise(self, text, voice=None, rate=1.0):
        self.said.append(text)
        return b"mp3"


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(discord_voice, "mp3_to_pcm", lambda mp3: b"\0" * 3840 * 3)
    monkeypatch.setattr(discord_voice, "SETTLE_S", 0)
    # The voice extra is optional: these tests are about the call, not the packages.
    monkeypatch.setattr(discord_voice, "voice_ready", lambda: True)


@pytest.fixture
async def bridge(settings_store):
    tts = TTS()
    made = DiscordBridge(settings_store, orchestrator=None, voice=tts)
    guild = SimpleNamespace(id=7, name="Game Night", me=object(), voice_channels=[])

    async def fetch_member(uid):
        if uid != OWNER:
            raise LookupError
        return object()
    guild.fetch_member = fetch_member
    channel = VoiceChannel(guild)
    guild.voice_channels.append(channel)
    dm = DM()
    owner = SimpleNamespace(id=OWNER, dm_channel=dm)
    made.client = SimpleNamespace(user=SimpleNamespace(id=999), guilds=[guild], is_closed=lambda: False,
                                  get_user=lambda uid: owner if uid == OWNER else None)
    await settings_store.patch("discord", {"owners": [{"id": str(OWNER), "name": "owner"}]})
    made.test = SimpleNamespace(tts=tts, guild=guild, channel=channel, dm=dm)
    return made


def _state(channel=None):
    return SimpleNamespace(channel=channel)


async def test_a_call_dms_a_link_then_speaks_when_you_join(bridge):
    said = await bridge.calls.call(say="The oven's done.")
    assert "sent you a DM" in said and "General in Game Night" in said
    dm = bridge.test.dm.sent[0]
    assert "JARVIS is calling" in dm and "https://discord.com/channels/7/500" in dm
    assert bridge.calls.status()["call_pending"]

    member = SimpleNamespace(id=OWNER)
    await bridge.calls.on_voice_state(member, _state(None), _state(bridge.test.channel))
    assert bridge.calls.active and bridge.test.channel.joined is not None
    assert bridge.test.tts.said[0].startswith("Hi, it's JARVIS. The oven's done. ")
    assert bridge.test.channel.joined.played == 1
    await bridge.calls.hang_up()


async def test_someone_else_joining_voice_does_nothing(bridge):
    await bridge.calls.call()
    await bridge.calls.on_voice_state(SimpleNamespace(id=7), _state(None), _state(bridge.test.channel))
    assert not bridge.calls.active and bridge.calls.status()["call_pending"]
    await bridge.calls.hang_up()


async def test_already_in_voice_means_joining_at_once(bridge):
    bridge.test.channel.voice_states = {OWNER: object()}
    said = await bridge.calls.call()
    assert "already in General" in said
    for _ in range(5):
        await asyncio.sleep(0)
    assert bridge.calls.active and bridge.test.dm.sent == []
    assert bridge.test.tts.said[0].startswith("Hi, it's JARVIS. Type to me")
    await bridge.calls.hang_up()


async def test_leaving_the_channel_hangs_up(bridge):
    await bridge.calls.call()
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    voice = bridge.test.channel.joined
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(bridge.test.channel), _state(None))
    assert not bridge.calls.active and voice.connected is False


async def test_an_unanswered_call_is_a_missed_call(bridge):
    await bridge.calls.call()
    await bridge.calls._missed()
    assert not bridge.calls.status()["call_pending"]
    assert "Missed call" in bridge.test.dm.sent[-1]
    await bridge.calls.hang_up()


async def test_hang_up_before_joining_cancels(bridge):
    await bridge.calls.call()
    assert await bridge.calls.hang_up() is True
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    assert not bridge.calls.active, "a cancelled call doesn't follow you into voice later"
    assert await bridge.calls.hang_up() is False


async def test_no_call_without_pairing_or_permission(bridge, settings_store):
    bridge.permissions = SimpleNamespace(allowed=lambda key: False)
    with pytest.raises(RuntimeError, match="permissions"):
        await bridge.calls.call()
    bridge.permissions = None
    await settings_store.patch("discord", {"owners": []})
    with pytest.raises(RuntimeError, match="Pair"):
        await bridge.calls.call()


async def test_a_reminder_can_ring_as_a_call(bridge):
    await bridge.set_call_reminders(True)
    event = SimpleNamespace(data={"title": "Reminder", "body": "Call Dad"})
    await bridge._on_notification(event)
    assert bridge.calls.status()["call_pending"]
    assert bridge.calls.pending["say"] == "A reminder: Call Dad"
    await bridge.calls.hang_up()


async def test_reminders_do_not_ring_unless_asked(bridge):
    await bridge._on_notification(SimpleNamespace(data={"title": "Reminder", "body": "Call Dad"}))
    assert not bridge.calls.status()["call_pending"]


async def test_during_a_call_the_answer_is_spoken_too(bridge):
    await bridge.calls.call()
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    assert await bridge.calls.speak("**Grocery list**\n- ☐ eggs\nfrom [CBS](<https://x.y>)") is True
    assert bridge.test.tts.said[-1] == "Grocery list eggs from CBS"
    await bridge.calls.hang_up()
    assert await bridge.calls.speak("anything") is False, "no call, nothing said"


def test_speakable_drops_markdown_links_and_marks():
    assert speakable("Hi **there**, see <https://x.com>\n-# from CBS · USA TODAY") == "Hi there, see"
    assert speakable("- ☑ milk\n- ☐ eggs") == "milk eggs"


@pytest.mark.parametrize("said, expected", [
    ("call me on Discord", ("discord_call", {"say": ""})),
    ("call me on Discord and tell me the oven's done", ("discord_call", {"say": "The oven's done."})),
    ("call me and remind me to take the bins out",
     ("discord_call", {"say": "This is your reminder to take the bins out."})),
    ("hang up", ("discord_hang_up", {})),
    ("call me for reminders", ("discord_call_reminders", {"on": True})),
    ("stop calling me for reminders", ("discord_call_reminders", {"on": False})),
    ("call me Sam", None),
    ("call mom", None),
])
def test_saying_it(said, expected):
    assert discord_said(said) == expected


async def test_without_the_voice_extra_it_says_how_to_add_it(bridge, monkeypatch):
    monkeypatch.setattr(discord_voice, "voice_ready", lambda: False)
    with pytest.raises(RuntimeError, match="requirements-voice.txt"):
        await bridge.calls.call()
    assert bridge.test.dm.sent == [], "no DM for a call that can't happen"


async def test_the_wake_word_is_told_jarvis_is_talking_in_the_call(bridge):
    """Through the PC's speakers, "Hi, it's JARVIS" set off the wake word and
    JARVIS answered its own greeting."""
    await bridge.calls.call(say="Hello.")
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    assert bridge.test.tts.heard_while_speaking[0].startswith("Hi, it's JARVIS. Hello.")
    assert bridge.test.tts.speaking is False, "and it knows when the call went quiet"
    await bridge.calls.hang_up()



class Hearing(TTS):
    async def transcribe_samples(self, samples):
        return "check off eggs"


class Chat:
    def __init__(self):
        self.asked = []

    async def ensure_conversation(self, conversation_id=None):
        return conversation_id or "conv_dm"

    async def chat(self, text, conversation_id=None, source="text"):
        self.asked.append((text, source))
        yield {"type": "final", "text": "Checked off eggs. 2 left on **Grocery list**."}


async def test_talking_in_the_call_is_heard_answered_and_kept(bridge):
    bridge.calls.tts = hearing = Hearing()
    bridge.orchestrator = Chat()
    await bridge.calls.call()
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    voice = bridge.test.channel.joined
    assert voice.deafened is False, "listening, so not deafened"
    assert bridge.calls.listener is not None and voice.listeners, "the call's audio comes to the listener"
    assert "Just talk" in hearing.said[0]

    await bridge.calls._heard("check off eggs")
    assert bridge.orchestrator.asked == [("check off eggs", "discord-call")]
    assert hearing.said[-1] == "Checked off eggs. 2 left on Grocery list."
    assert any("check off eggs" in m and "Checked off eggs" in m for m in bridge.test.dm.sent)

    await bridge.calls.hang_up()
    assert bridge.calls.listener is None and voice.listeners == [], "stops listening when the call ends"


async def test_without_speech_recognition_it_joins_deafened(bridge):
    await bridge.calls.call()
    await bridge.calls.on_voice_state(SimpleNamespace(id=OWNER), _state(None), _state(bridge.test.channel))
    assert bridge.test.channel.joined.deafened is True and bridge.calls.listener is None
    await bridge.calls.hang_up()
