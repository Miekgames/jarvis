"""Hearing the owner in a Discord call: real packets in, sentences out.

Packets are built the way Discord sends them: Opus audio, sealed with the
negotiated key (AEAD XChaCha20-Poly1305, "rtpsize": the RTP header and the
extension preamble authenticated, the rest encrypted, a 4-byte nonce at the
end). Nothing here talks to Discord.
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
from types import SimpleNamespace

import pytest

nacl_secret = pytest.importorskip("nacl.secret")
discord = pytest.importorskip("discord")

from backend.integrations import discord_listen  # noqa: E402
from backend.integrations.discord_listen import CallListener, open_transport, split_rtp, to_whisper  # noqa: E402

KEY = bytes(range(32))
OWNER = 42


def _frames(seconds: float):
    """Opus frames of a 300 Hz tone: something with sound in it."""
    if not discord.opus.is_loaded():
        discord.opus._load_default()
    encoder = discord.opus.Encoder()
    frames, n = [], int(seconds / 0.02)
    for i in range(n):
        samples = bytearray()
        for k in range(960):
            v = int(8000 * math.sin(2 * math.pi * 300 * (i * 960 + k) / 48000))
            samples += struct.pack("<hh", v, v)
        frames.append(encoder.encode(bytes(samples), 960))
    return frames


def _packet(opus: bytes, seq: int, ssrc: int = 1234, extension: bool = True, counter: int = 1) -> bytes:
    header = bytearray(12)
    header[0] = 0x90 if extension else 0x80              # version 2, X bit for the extension
    header[1] = 0x78                                     # payload type 120: Opus
    struct.pack_into(">HII", header, 2, seq, seq * 960, ssrc)
    body = opus
    if extension:
        header += bytes([0xBE, 0xDE, 0x00, 0x01])        # one-word extension follows (encrypted)
        body = bytes([0x10, 0x00, 0x00, 0x00]) + opus
    nonce = struct.pack(">I", counter) + bytes(20)
    sealed = nacl_secret.Aead(KEY).encrypt(body, bytes(header), nonce).ciphertext
    return bytes(header) + sealed + nonce[:4]


@pytest.mark.parametrize("extension", [True, False])
def test_an_audio_packet_opens_to_its_opus_frame(extension):
    frame = _frames(0.02)[0]
    opened = open_transport(_packet(frame, 7, extension=extension), KEY)
    assert opened == (1234, frame)


def test_a_wrong_key_or_a_report_opens_to_nothing():
    frame = _frames(0.02)[0]
    assert open_transport(_packet(frame, 7), bytes(32)) is None
    rtcp = bytes([0x80, 200]) + bytes(30)               # a receiver report, not audio
    assert split_rtp(rtcp) is None
    assert open_transport(b"\x80\x78", KEY) is None


def test_audio_becomes_what_speech_recognition_wants():
    pcm = struct.pack("<hh", 16384, 16384) * 4800        # 0.1 s at 48 kHz stereo
    samples = to_whisper(pcm)
    assert len(samples) == 1600 and abs(float(samples[0]) - 0.5) < 1e-3   # 16 kHz mono float


class Voice:
    def __init__(self):
        self.listeners = []
        self.mode = discord_listen.MODE
        self.secret_key = list(KEY)
        self._connection = SimpleNamespace(add_socket_listener=self.listeners.append,
                                           remove_socket_listener=self.listeners.remove,
                                           dave_protocol_version=0, dave_session=None)


async def test_a_spoken_sentence_reaches_jarvis_after_a_pause(monkeypatch):
    monkeypatch.setattr(discord_listen, "PAUSE_S", 0.2)
    voice = Voice()
    lengths, heard = [], []

    async def transcribe(samples):
        lengths.append(len(samples))
        return "what's on my grocery list"

    async def got(words):
        heard.append(words)

    listener = CallListener(voice, OWNER, transcribe, got, loop=asyncio.get_running_loop())
    listener.start()
    feed = voice.listeners[0]
    for seq, frame in enumerate(_frames(0.6)):
        feed(_packet(frame, seq, counter=seq + 1))
    assert heard == [], "nothing until the pause"
    for _ in range(20):
        await asyncio.sleep(0.05)
        if heard:
            break
    assert heard == ["what's on my grocery list"]
    assert abs(lengths[0] - 0.6 * 16000) < 400, "the whole sentence, at 16 kHz"
    listener.stop()
    assert voice.listeners == []


async def test_nothing_is_heard_while_jarvis_talks_or_from_a_cough(monkeypatch):
    monkeypatch.setattr(discord_listen, "PAUSE_S", 0.2)
    voice = Voice()
    heard = []

    async def transcribe(samples):
        return "something"

    async def got(words):
        heard.append(words)

    listener = CallListener(voice, OWNER, transcribe, got, loop=asyncio.get_running_loop())
    listener.start()
    feed = voice.listeners[0]
    listener.paused = True
    for seq, frame in enumerate(_frames(0.6)):
        feed(_packet(frame, seq, counter=seq + 1))
    listener.paused = False
    for seq, frame in enumerate(_frames(0.1)):           # too short to be a sentence
        feed(_packet(frame, 100 + seq, counter=100 + seq))
    await asyncio.sleep(0.6)
    assert heard == []
    listener.stop()


async def test_only_the_owners_voice_opens_under_e2ee(monkeypatch):
    """Under DAVE the frame is decrypted as the owner's; anyone else's fails."""
    voice = Voice()
    calls = []

    class Session:
        def decrypt(self, user_id, media_type, frame):
            calls.append(user_id)
            if frame.startswith(b"owner:"):
                return frame[len(b"owner:"):]
            raise ValueError("not this sender's key")

    voice._connection.dave_protocol_version = 1
    voice._connection.dave_session = Session()
    listener = CallListener(voice, OWNER, None, None, loop=asyncio.get_running_loop())
    assert listener._as_owner(b"owner:opus") == b"opus"
    assert listener._as_owner(b"someone else") is None
    assert calls == [OWNER, OWNER] and listener.stats["not_owner"] == 1


@pytest.mark.parametrize("words", ["", "you", "Thank you.", "um"])
async def test_noise_is_not_an_answerable_sentence(words, monkeypatch):
    heard = []

    async def transcribe(samples):
        return words

    async def got(text):
        heard.append(text)

    listener = CallListener(Voice(), OWNER, transcribe, got, loop=asyncio.get_running_loop())
    await listener._sentence(struct.pack("<hh", 0, 0) * 48000)
    assert heard == []
