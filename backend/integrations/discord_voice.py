"""Calling the owner on Discord: a DM to join voice, then JARVIS talks.

A bot can't ring anyone the way a person's DM call does, so a call is: JARVIS
DMs the owner a link to a voice channel it can speak in, waits for them to
join (any voice channel the bot shares with them), joins them there and
speaks -- in JARVIS's own voice, the same edge-tts voice as the screen.

While the call is on, JARVIS listens (see discord_listen): what the owner
says is heard, answered out loud, and kept in the DMs; anything typed to the
bot is answered out loud too. Only the owner's voice can be heard -- Discord's
end-to-end encryption is opened as the owner. It leaves when the owner
leaves, when told to "hang up", or after an hour; a call nobody joins ends
after five minutes with a "missed call" DM.

No FFmpeg: edge-tts gives MP3, and PyAV (already here for speech
recognition) turns it into the 48 kHz stereo PCM Discord's voice wants.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("integrations.discord.voice")

CALL_WAIT_S = 300            # a call nobody joins: "missed call" after five minutes
CALL_MAX_S = 3600            # nobody needs JARVIS on the line for more than an hour
SETTLE_S = 1.0               # a moment after joining before talking
# View channel, send and read messages, connect, speak: what the bot needs.
INVITE_PERMISSIONS = 1024 + 2048 + 65536 + 1048576 + 2097152


VOICE_EXTRA = ("Discord calls are an optional extra. To turn them on, run "
               "`pip install -r requirements-voice.txt` in JARVIS's folder (for a download: "
               "`.venv\\Scripts\\python -m pip install -r requirements-voice.txt`), then restart JARVIS.")


def voice_ready() -> bool:
    """Whether discord.py can do voice here: PyNaCl, and davey for Discord's
    end-to-end voice encryption (without it, the call is joined but silent)."""
    try:
        import davey  # noqa: F401
        import nacl  # noqa: F401
    except ImportError:
        return False
    return True


def speakable(text: str) -> str:
    """Markdown and links out, so it reads aloud like speech."""
    text = re.sub(r"^-# .*$", "", text or "", flags=re.M)              # "from CBS Sports · …"
    text = re.sub(r"\[([^\]]+)\]\(<?https?://[^)>]+>?\)", r"\1", text)   # [name](<url>) -> name
    text = re.sub(r"<?https?://\S+>?", "", text)
    text = re.sub(r"[*_`#>|~]+", "", text)
    text = re.sub(r"^\s*(?:[-•]\s+)?(?:[☐☑✓]\s*)?", "", text, flags=re.M)   # "- ☐ eggs" -> "eggs"
    text = re.sub(r"[☐☑✓🔔📞]", "", text)
    return " ".join(text.split())[:2000]


def mp3_to_pcm(mp3: bytes) -> bytes:
    """edge-tts's MP3 as the 48 kHz, 16-bit stereo PCM Discord plays."""
    import av

    out = io.BytesIO()
    with av.open(io.BytesIO(mp3)) as container:
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
        for frame in container.decode(audio=0):
            for chunk in resampler.resample(frame):
                out.write(chunk.to_ndarray().tobytes())
        for chunk in resampler.resample(None):                          # what's left in the resampler
            out.write(chunk.to_ndarray().tobytes())
    return out.getvalue()


def encryption_state(voice) -> Dict[str, Any]:
    """Where Discord's end-to-end voice encryption (DAVE) has got to on this call.

    With it on, audio sent before the session is ready goes out unencrypted,
    and the listener's app throws it away: the bot's ring lights up green and
    nothing is heard. So nothing is said until this says it's safe.
    """
    connection = getattr(voice, "_connection", None)
    session = getattr(connection, "dave_session", None)
    return {
        "protocol": int(getattr(connection, "dave_protocol_version", 0) or 0),
        "ready": bool(session is not None and getattr(session, "ready", False)),
        "status": str(getattr(session, "status", "none")),
        "can_encrypt": bool(getattr(connection, "can_encrypt", False)),
    }


SECURE_WAIT_S = 15.0


class VoiceCalls:
    def __init__(self, bridge, tts=None) -> None:
        self.bridge = bridge
        self.tts = tts
        self.pending: Optional[Dict[str, Any]] = None
        self.voice = None                          # discord.VoiceClient while on a call
        self._timer: Optional[asyncio.Task] = None
        self._speaking = asyncio.Lock()            # one thing said at a time
        self.listener = None                       # hearing the owner, while on a call

    @property
    def active(self) -> bool:
        return self.voice is not None and self.voice.is_connected()

    def status(self) -> Dict[str, Any]:
        return {"in_call": self.active, "call_pending": self.pending is not None,
                "listening": self.active and self.listener is not None,
                "call_channel": getattr(getattr(self.voice, "channel", None), "name", "") if self.active else ""}

    # -- starting -------------------------------------------------------------------
    async def call(self, say: str = "") -> str:
        """Call the owner: join them if they're already in voice, else DM them to."""
        client = self.bridge.client
        if client is None or client.is_closed():
            raise RuntimeError("The Discord bot isn't connected.")
        if not self.bridge.allowed():
            raise RuntimeError("Discord is switched off in JARVIS's permissions.")
        owners = (await self.bridge.config())["owners"]
        if not owners:
            raise RuntimeError("Pair your Discord account first: say “pair Discord”.")
        if self.tts is None:
            raise RuntimeError("JARVIS's voice isn't available, so there's nothing to call with.")
        if not voice_ready():
            raise RuntimeError(VOICE_EXTRA)
        owner_id = int(owners[0]["id"])

        if self.active:
            if say:
                asyncio.create_task(self.speak(say))
            return f"We're already on a call in {self.voice.channel.name}."

        self.pending = {"owner": owner_id, "say": say, "since": time.time()}
        here = self._voice_channel_of(client, owner_id)
        if here is not None:
            asyncio.create_task(self._join(here))
            await self.bridge._record("discord.call", f"Joined the owner in {here.name} on Discord")
            return f"You're already in {here.name} on Discord, so I'm joining you there."

        channel = await self._channel_to_invite_to(client, owner_id)
        if channel is None:
            self.pending = None
            raise RuntimeError("I couldn't find a voice channel I can speak in. In your server's "
                               "settings, let the JARVIS bot Connect and Speak in a voice channel.")
        user = client.get_user(owner_id) or await client.fetch_user(owner_id)
        dm = user.dm_channel or await user.create_dm()
        link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
        await dm.send(f"📞 **JARVIS is calling.** Join me in voice: {channel.mention} in "
                      f"{channel.guild.name}\n{link}\nI'll start talking as soon as you're in. "
                      f"(If you can't make it, the call ends in {CALL_WAIT_S // 60} minutes.)")
        self._restart_timer(CALL_WAIT_S, self._missed)
        await self.bridge._record("discord.call", f"Called the owner on Discord ({channel.guild.name} / {channel.name})")
        return (f"Calling you on Discord: I've sent you a DM to join {channel.name} in "
                f"{channel.guild.name}. I'll speak as soon as you're in.")

    @staticmethod
    def _voice_channel_of(client, owner_id: int):
        for guild in client.guilds:
            for channel in guild.voice_channels:
                if owner_id in getattr(channel, "voice_states", {}):
                    return channel
        return None

    async def _channel_to_invite_to(self, client, owner_id: int):
        """A voice channel the owner can reach and the bot can speak in."""
        for guild in client.guilds:
            try:
                await guild.fetch_member(owner_id)          # not in this server: no use asking them
            except Exception:
                continue
            for channel in guild.voice_channels:
                rights = channel.permissions_for(guild.me)
                if rights.view_channel and rights.connect and rights.speak:
                    return channel
        return None

    # -- the owner joins, leaves -----------------------------------------------------
    async def on_voice_state(self, member, before, after) -> None:
        owners = {o["id"] for o in (await self.bridge.config())["owners"]}
        if str(member.id) not in owners:
            return
        if self.pending and after.channel is not None and before.channel != after.channel:
            await self._join(after.channel)
        elif self.active and before.channel == self.voice.channel and after.channel != self.voice.channel:
            await self.hang_up("You left the call.")

    async def _join(self, channel) -> None:
        pending, self.pending = self.pending, None
        listen = self.tts is not None and hasattr(self.tts, "transcribe_samples")
        try:
            # Not deafened when it listens: Discord only sends audio to members
            # who can hear, and shows exactly which it is.
            self.voice = await channel.connect(self_deaf=not listen)
        except Exception as exc:
            logger.warning("couldn't join %s: %s", channel, exc)
            await self._dm(f"I couldn't join {channel.name}: {type(exc).__name__}.")
            return
        self._restart_timer(CALL_MAX_S, lambda: self.hang_up("The call reached an hour, so I hung up."))
        await asyncio.sleep(SETTLE_S)               # let the connection settle before talking
        if not await self._secure():
            await self._dm("I'm in the voice channel, but Discord's voice encryption hasn't finished "
                           "setting up, so you may not hear me yet. I'll keep trying when I speak.")
        if listen:
            self._listen((pending or {}).get("owner"))
        hint = ("Just talk, and pause when you're done. Say hang up to end the call." if self.listener
                else "Type to me in our messages and I'll answer out loud here. Leave the channel "
                     "when you're done.")
        said = (pending or {}).get("say")
        await self.speak(f"Hi, it's JARVIS. {said + ' ' if said else ''}{hint}")

    def _listen(self, owner_id: Optional[int]) -> None:
        """Hear the owner in the call: their sentences go to JARVIS like typed ones."""
        from .discord_listen import CallListener

        if owner_id is None or self.voice is None:
            return
        try:
            self.listener = CallListener(self.voice, owner_id, self.tts.transcribe_samples, self._heard,
                                         loop=asyncio.get_running_loop())
            self.listener.start()
        except Exception as exc:
            logger.warning("couldn't listen in the call: %s", exc)
            self.listener = None

    async def _heard(self, words: str) -> None:
        """Something the owner said in the call: answered out loud, and kept in the DMs."""
        bridge = self.bridge
        if not bridge.allowed() or bridge.orchestrator is None:
            return
        await bridge._record("discord.call_heard", "Heard the owner in the Discord call",
                             {"chars": len(words)})
        owners = (await bridge.config())["owners"]
        client = bridge.client
        dm = None
        try:
            user = client.get_user(int(owners[0]["id"])) or await client.fetch_user(int(owners[0]["id"]))
            dm = user.dm_channel or await user.create_dm()
        except Exception:
            logger.debug("no DM channel for the call's record", exc_info=True)
        conversation = await bridge._conversation(dm.id, await bridge.config()) if dm is not None else None
        reply = ""
        async for event in bridge.orchestrator.chat(words, conversation_id=conversation,
                                                    source="discord-call"):
            if event.get("type") == "final":
                reply = event.get("text") or reply
            elif event.get("type") == "error":
                reply = f"Sorry, I couldn't do that: {event.get('error')}"
        if self.active and reply:
            await self.speak(reply)
        if dm is not None:
            from .discord_bridge import chunks

            for part in chunks(f"🎙️ *“{words}”*\n\n{reply or '(no answer)'}"):
                await dm.send(part)

    async def _secure(self, timeout: float = SECURE_WAIT_S) -> bool:
        """Wait until audio can be sent so it's heard: no E2EE, or E2EE ready."""
        deadline = time.monotonic() + timeout
        state = encryption_state(self.voice)
        while self.active and time.monotonic() < deadline:
            state = encryption_state(self.voice)
            if state["protocol"] == 0 or state["can_encrypt"]:
                logger.info("call audio can be heard: %s", state)
                return True
            await asyncio.sleep(0.25)
        logger.warning("voice encryption not ready after %.0fs: %s", timeout, state)
        return False

    # -- talking -------------------------------------------------------------------------
    async def speak(self, text: str) -> bool:
        """Say it in the call. False when there's no call or nothing to say."""
        clean = speakable(text)
        if not self.active or not clean or self.tts is None:
            return False
        import discord

        async with self._speaking:
            voice_prefs = await self.bridge.store.get("voice", {}) or {}
            try:
                mp3 = await self.tts.synthesise(clean, None, float(voice_prefs.get("rate") or 1.0))
            except Exception as exc:
                logger.warning("speech for the call failed: %s", exc)
                return False
            loop = asyncio.get_running_loop()
            pcm = await loop.run_in_executor(None, mp3_to_pcm, mp3)
            if not self.active or not await self._secure(5.0):
                return False
            finished = asyncio.Event()
            # This PC's own wake word may hear the call through the speakers:
            # tell it JARVIS is the one talking (it once answered itself).
            if hasattr(self.tts, "speaking_elsewhere"):
                self.tts.speaking_elsewhere(clean)
            if self.listener is not None:
                self.listener.paused = True            # not while JARVIS itself is talking
            self.voice.play(discord.PCMAudio(io.BytesIO(pcm)),
                            after=lambda error: loop.call_soon_threadsafe(finished.set))
            seconds = len(pcm) / (48000 * 2 * 2)
            try:
                await asyncio.wait_for(finished.wait(), timeout=seconds + 15)
            except asyncio.TimeoutError:
                logger.warning("the call's audio didn't finish playing")
            finally:
                if hasattr(self.tts, "done_speaking_elsewhere"):
                    self.tts.done_speaking_elsewhere()
                if self.listener is not None:
                    self.listener.paused = False
            logger.info("said %.1f s in the call; encryption %s, stats %s",
                        seconds, encryption_state(self.voice), self._encryption_stats())
            return True

    def _encryption_stats(self) -> str:
        session = getattr(getattr(self.voice, "_connection", None), "dave_session", None)
        if session is None:
            return "none"
        try:
            import davey

            stats = session.get_encryption_stats(davey.MediaType.audio)
            return f"{stats.successes} sent encrypted, {stats.failures} failed"
        except Exception as exc:
            return f"unavailable ({type(exc).__name__})"

    # -- ending -------------------------------------------------------------------------
    async def hang_up(self, why: str = "") -> bool:
        was_on = self.active or self.pending is not None
        self.pending = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.stop()
        voice, self.voice = self.voice, None
        if voice is not None:
            try:
                await voice.disconnect(force=True)
            except Exception:
                logger.debug("disconnect failed", exc_info=True)
        if was_on:
            await self.bridge._record("discord.call_ended", f"Discord call ended{(': ' + why) if why else ''}")
        return was_on

    async def _missed(self) -> None:
        if self.pending is None:
            return
        self.pending = None
        await self._dm("Missed call. Say “call me on Discord” again when you're free.")
        await self.bridge._record("discord.call_missed", "Discord call missed")

    def _restart_timer(self, seconds: float, then) -> None:
        if self._timer is not None:
            self._timer.cancel()

        async def wait():
            await asyncio.sleep(seconds)
            self._timer = None
            await then()

        self._timer = asyncio.create_task(wait())

    async def _dm(self, text: str) -> None:
        client = self.bridge.client
        owners = (await self.bridge.config())["owners"]
        if client is None or not owners:
            return
        try:
            user = client.get_user(int(owners[0]["id"])) or await client.fetch_user(int(owners[0]["id"]))
            await (user.dm_channel or await user.create_dm()).send(text)
        except Exception as exc:
            logger.warning("couldn't DM the owner: %s", exc)
