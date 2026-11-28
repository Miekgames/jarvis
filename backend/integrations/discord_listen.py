"""Hearing the owner in a Discord call, so they can just talk.

discord.py can speak in voice but not listen, so this is the listening half:

1. Every UDP packet the voice connection receives is handed here (discord.py's
   socket listener, on its reader thread).
2. Opus audio packets (RTP payload type 120) are unwrapped from Discord's
   transport encryption: AEAD XChaCha20-Poly1305, the "rtpsize" mode, with the
   key discord.py negotiated.
3. Then from DAVE, Discord's end-to-end encryption -- decrypted *as the owner*.
   Only the owner's audio comes out of that; anyone else in the channel stays
   unheard, by construction rather than by filtering.
4. Opus is decoded to PCM and gathered until the owner pauses (~0.8 s).
5. The sentence is transcribed by JARVIS's own speech recognition, on this PC,
   from memory: nothing is written to disk, nothing is kept.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("integrations.discord.listen")

OPUS_PAYLOAD = 120
MODE = "aead_xchacha20_poly1305_rtpsize"
PAUSE_S = 0.8                 # a pause this long ends a sentence
MIN_SPEECH_S = 0.4            # shorter is a cough or a click
MAX_SPEECH_S = 30.0           # a monologue is cut into pieces this long
SILENCE_FRAME = b"\xf8\xff\xfe"
BYTES_PER_SECOND = 48000 * 2 * 2
# What speech recognition "hears" in near-silence, not worth answering.
NOT_WORDS = {"", "you", "you.", "thank you.", "thanks for watching!", "bye.", "uh", "um", "hmm"}


def split_rtp(packet: bytes) -> Optional[Tuple[bytes, bytes, int, int]]:
    """(authenticated header, encrypted body, ssrc, extension words) of an audio packet.

    None for anything that isn't Opus audio: RTCP reports, keep-alives.
    In the rtpsize modes the header that's authenticated (not encrypted)
    includes the 4-byte extension preamble; the extension's body is
    encrypted along with the audio.
    """
    if len(packet) < 12 + 4 or packet[0] >> 6 != 2 or (packet[1] & 0x7F) != OPUS_PAYLOAD:
        return None
    header_len = 12 + 4 * (packet[0] & 0x0F)
    words = 0
    if packet[0] & 0x10:                                   # an RTP header extension follows
        if len(packet) < header_len + 4:
            return None
        words = struct.unpack_from(">H", packet, header_len + 2)[0]
        header_len += 4
    if len(packet) < header_len + 4 + 16:                 # nonce and the AEAD tag at least
        return None
    ssrc = struct.unpack_from(">I", packet, 8)[0]
    return packet[:header_len], packet[header_len:], ssrc, words


def open_transport(packet: bytes, key: bytes) -> Optional[Tuple[int, bytes]]:
    """(ssrc, the Opus frame) of an audio packet, or None if it isn't one or won't open."""
    parts = split_rtp(packet)
    if parts is None:
        return None
    header, body, ssrc, words = parts
    import nacl.exceptions
    import nacl.secret

    nonce = bytes(body[-4:]) + bytes(20)
    try:
        plain = nacl.secret.Aead(key).decrypt(bytes(body[:-4]), header, nonce)
    except nacl.exceptions.CryptoError:
        return None
    return ssrc, plain[words * 4:]                         # past the extension's body


class CallListener:
    """Turns the owner's voice in a call into sentences, one at a time."""

    def __init__(self, voice_client, owner_id: int, transcribe: Callable[[Any], Awaitable[str]],
                 heard: Callable[[str], Awaitable[None]], loop: Optional[asyncio.AbstractEventLoop] = None):
        self.voice = voice_client
        self.owner_id = int(owner_id)
        self.transcribe = transcribe
        self.heard = heard
        self.loop = loop or asyncio.get_event_loop()
        self.paused = False                                 # while JARVIS itself is talking
        self._lock = threading.Lock()
        self._speech: Dict[int, Dict[str, Any]] = {}         # ssrc -> pcm, first, last
        self._decoders: Dict[int, Any] = {}
        self._watcher: Optional[asyncio.Task] = None
        self._handling = asyncio.Lock()
        self.stats = {"packets": 0, "opened": 0, "owner_frames": 0, "not_owner": 0, "sentences": 0}

    # -- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        connection = self.voice._connection
        if getattr(self.voice, "mode", MODE) != MODE:
            logger.warning("voice mode %s isn't one I can open: listening off", self.voice.mode)
            return
        connection.add_socket_listener(self._on_packet)
        self._watcher = self.loop.create_task(self._watch())
        logger.info("listening in the call")

    def stop(self) -> None:
        try:
            self.voice._connection.remove_socket_listener(self._on_packet)
        except Exception:
            logger.debug("couldn't remove the socket listener", exc_info=True)
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        logger.info("stopped listening: %s", self.stats)

    # -- discord.py's reader thread ----------------------------------------------------
    def _on_packet(self, packet: bytes) -> None:
        self.stats["packets"] += 1
        if self.paused:
            return
        try:
            opened = open_transport(packet, bytes(self.voice.secret_key))
            if opened is None:
                return
            self.stats["opened"] += 1
            ssrc, frame = opened
            frame = self._as_owner(frame)
            if frame is None or frame == SILENCE_FRAME:
                return
            self.stats["owner_frames"] += 1
            decoder = self._decoders.get(ssrc)
            if decoder is None:
                import discord

                decoder = self._decoders[ssrc] = discord.opus.Decoder()
            pcm = decoder.decode(frame)
        except Exception:
            logger.debug("a packet couldn't be read", exc_info=True)
            return
        now = time.monotonic()
        with self._lock:
            speech = self._speech.setdefault(ssrc, {"pcm": bytearray(), "first": now, "last": now})
            speech["pcm"] += pcm
            speech["last"] = now

    def _as_owner(self, frame: bytes) -> Optional[bytes]:
        """The frame decrypted as the owner's, under DAVE; as-is without it."""
        connection = self.voice._connection
        session = getattr(connection, "dave_session", None)
        if not getattr(connection, "dave_protocol_version", 0) or session is None:
            return frame
        try:
            import davey

            return session.decrypt(self.owner_id, davey.MediaType.audio, frame)
        except Exception:
            self.stats["not_owner"] += 1          # someone else's voice, or keys still settling
            return None

    # -- the event loop: when a sentence ends -----------------------------------------
    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(0.15)
            now = time.monotonic()
            done = []
            with self._lock:
                for ssrc, speech in list(self._speech.items()):
                    long_enough = len(speech["pcm"]) / BYTES_PER_SECOND
                    if now - speech["last"] >= PAUSE_S or long_enough >= MAX_SPEECH_S:
                        done.append(bytes(self._speech.pop(ssrc)["pcm"]))
            for pcm in done:
                if len(pcm) / BYTES_PER_SECOND >= MIN_SPEECH_S and not self.paused:
                    self.loop.create_task(self._sentence(pcm))

    async def _sentence(self, pcm: bytes) -> None:
        async with self._handling:                 # one thing at a time, in order
            try:
                words = (await self.transcribe(to_whisper(pcm)) or "").strip()
            except Exception as exc:
                logger.warning("couldn't transcribe the call: %s", exc)
                return
            if words.lower() in NOT_WORDS or len(words) < 2:
                return
            self.stats["sentences"] += 1
            await self.heard(words)


def to_whisper(pcm: bytes):
    """48 kHz stereo 16-bit PCM as the 16 kHz mono float32 speech recognition wants."""
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    mono = samples.reshape(-1, 2).mean(axis=1) / 32768.0
    mono = mono[: len(mono) - len(mono) % 3]
    return mono.reshape(-1, 3).mean(axis=1).astype(np.float32)   # 48 kHz -> 16 kHz
