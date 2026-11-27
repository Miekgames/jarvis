"""JARVIS on Discord, through the owner's own bot.

The owner makes a bot in Discord's Developer Portal and pastes its token into
the Discord panel; it is sealed with DPAPI like every other secret JARVIS
keeps. Then they say "pair Discord" and DM the bot the code JARVIS shows:
from then on, what they send the bot -- a DM, or an @mention in a server if
they turn that on -- goes to the same JARVIS as the screen, and the answer
comes back as the bot's reply.

What keeps this safe:
- It is a bot account, marked BOT by Discord, never the owner's own account
  (automating a user account is against Discord's rules, and would let JARVIS
  pass as the owner).
- Only paired accounts get JARVIS. Anyone else who messages the bot gets one
  polite line, at most once an hour, and nothing runs.
- The `discord.chat` permission gates every message; revoking it, turning
  the bot off, or unpairing works immediately.
- Servers are off by default: a reply in a server is visible to everyone in
  that channel, and JARVIS can read the owner's files and mail.
- Anything that needs the owner's OK still needs it: the request is posted in
  the chat, and only a paired owner's "yes" approves it. Every message is in
  the audit log.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets as _random
import time
from typing import Any, Dict, List, Optional

from ..core import secrets
from ..core.events import Events, bus
from .discord_voice import INVITE_PERMISSIONS

logger = logging.getLogger("integrations.discord")

SECTION = "discord"
DEFAULTS: Dict[str, Any] = {"enabled": False, "token": "", "owners": [], "servers": False,
                            "notify": False, "call_reminders": False, "conversations": {}}
PAIR_MINUTES = 10
LIMIT = 1900                         # Discord's cap is 2000 characters a message
STRANGER_GAP_S = 3600                # one polite line an hour to someone not paired
VOICE_MESSAGE_LIMIT = 10_000_000    # bytes: several minutes of Discord voice message
_YES = re.compile(r"(?:y|yes|yep|yeah|ok|okay|approve|approved|do it|go ahead)[.! ]*", re.I)
_NO = re.compile(r"(?:n|no|nope|deny|denied|don'?t|cancel|stop)[.! ]*", re.I)
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{20,}$")


def chunks(text: str, limit: int = LIMIT) -> List[str]:
    """Split a reply into messages Discord accepts, at paragraph or line ends."""
    text = (text or "").strip() or "(no reply)"
    out: List[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(". ", 0, limit))
        cut = cut + 1 if cut > limit // 3 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


def with_sources(text: str, sources: List[Dict[str, str]]) -> str:
    """The answer, and the pages it came from as links that don't unfurl."""
    seen, links = set(), []
    for source in sources:
        url = str(source.get("url") or "")
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        seen.add(url)
        name = (source.get("source") or url.split("/")[2]).replace("]", "").replace("[", "")
        links.append(f"[{name}](<{url}>)")
    return text + ("\n-# from " + " · ".join(links[:5]) if links else "")


def strip_mention(text: str, bot_id: Optional[int]) -> str:
    if bot_id:
        text = re.sub(rf"<@!?{bot_id}>", " ", text or "")
    return " ".join((text or "").split())


class DiscordBridge:
    def __init__(self, settings_store, orchestrator, permissions=None, confirmations=None,
                 activity=None, audit=None, voice=None):
        self.store = settings_store
        self.orchestrator = orchestrator
        self.permissions = permissions
        self.confirmations = confirmations
        self.activity = activity
        self.audit = audit
        self.client = None
        self._task: Optional[asyncio.Task] = None
        self._error = ""
        self._code: Optional[str] = None
        self._code_until = 0.0
        self._strangers: Dict[str, float] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._waiting: Dict[int, str] = {}        # channel -> confirmation awaiting a yes/no
        self._unsubscribe: List[Any] = []
        from .discord_voice import VoiceCalls

        self.calls = VoiceCalls(self, voice)       # "call me on Discord"

    # -- settings -------------------------------------------------------------
    async def config(self) -> Dict[str, Any]:
        saved = await self.store.get(SECTION, {}) or {}
        return {**DEFAULTS, **saved}

    async def _save(self, **changes: Any) -> None:
        await self.store.patch(SECTION, changes)

    def allowed(self) -> bool:
        return self.permissions is None or self.permissions.allowed("discord.chat")

    async def status(self) -> Dict[str, Any]:
        config = await self.config()
        user = getattr(self.client, "user", None) if self.client else None
        connected = bool(self.client and user and not self.client.is_closed())
        return {
            "token_saved": bool(config["token"]),
            "token_storage": secrets.describe(config["token"]) if config["token"] else "",
            "enabled": bool(config["enabled"]),
            "connected": connected,
            "bot": str(user) if connected else "",
            "invite_url": (f"https://discord.com/oauth2/authorize?client_id={user.id}"
                           f"&scope=bot&permissions={INVITE_PERMISSIONS}") if connected else "",
            "servers": bool(config["servers"]),
            "notify": bool(config["notify"]),
            "call_reminders": bool(config["call_reminders"]),
            **self.calls.status(),
            "server_count": len(self.client.guilds) if connected else 0,
            "owners": [{"id": o["id"], "name": o.get("name", "")} for o in config["owners"]],
            "pairing": bool(self._code and time.time() < self._code_until),
            "allowed": self.allowed(),
            "error": self._error,
        }

    async def set_token(self, token: str) -> Dict[str, Any]:
        token = (token or "").strip()
        if not _TOKEN.match(token):
            raise ValueError("That doesn't look like a bot token. In the Developer Portal it's "
                             "under Bot → Reset Token: three parts separated by dots.")
        await self._save(token=secrets.seal(token), enabled=True)
        await self._record("discord.token_saved", "Discord bot token saved (sealed)")
        await self.restart()
        return await self.status()

    async def forget_token(self) -> Dict[str, Any]:
        await self.stop()
        await self._save(token="", enabled=False)
        await self._record("discord.token_removed", "Discord bot token removed")
        return await self.status()

    async def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        await self._save(enabled=bool(enabled))
        await self._record("discord.enabled" if enabled else "discord.disabled",
                           f"Discord bot turned {'on' if enabled else 'off'}")
        await (self.restart() if enabled else self.stop())
        return await self.status()

    async def set_servers(self, on: bool) -> Dict[str, Any]:
        await self._save(servers=bool(on))
        return await self.status()

    async def set_notify(self, on: bool) -> Dict[str, Any]:
        """Send JARVIS's notifications (reminders, finished research) to the
        paired accounts' DMs too. Off until asked: it sends their text to Discord."""
        await self._save(notify=bool(on))
        await self._record("discord.notify_on" if on else "discord.notify_off",
                           f"Notifications to Discord turned {'on' if on else 'off'}")
        return await self.status()

    async def set_call_reminders(self, on: bool) -> Dict[str, Any]:
        """Reminders ring as a Discord call: a DM to join voice, then said aloud."""
        await self._save(call_reminders=bool(on))
        await self._record("discord.call_reminders_on" if on else "discord.call_reminders_off",
                           f"Reminders as Discord calls turned {'on' if on else 'off'}")
        return await self.status()

    def new_pairing_code(self) -> str:
        self._code = f"{_random.randbelow(10**6):06d}"
        self._code_until = time.time() + PAIR_MINUTES * 60
        return self._code

    async def unpair(self, owner_id: str = "") -> Dict[str, Any]:
        config = await self.config()
        owners = [o for o in config["owners"] if owner_id and o["id"] != owner_id]
        await self._save(owners=owners)
        await self._record("discord.unpaired", f"Discord account(s) unpaired: {owner_id or 'all'}")
        return await self.status()

    # -- running ----------------------------------------------------------------
    async def start(self) -> None:
        config = await self.config()
        if not (config["enabled"] and config["token"]):
            return
        if self._task and not self._task.done():
            return
        self._error = ""
        self._task = asyncio.create_task(self._run(secrets.unseal(config["token"])), name="discord-bot")
        if not self._unsubscribe:
            self._unsubscribe = [bus.subscribe(Events.CONFIRMATION_REQUESTED, self._on_confirmation),
                                 bus.subscribe(Events.CONFIRMATION_RESOLVED, self._on_resolved),
                                 bus.subscribe(Events.NOTIFICATION, self._on_notification)]

    async def stop(self) -> None:
        try:
            await self.calls.hang_up("JARVIS's Discord bot was turned off.")
        except Exception:
            logger.debug("hang-up on stop failed", exc_info=True)
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("discord close failed", exc_info=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _run(self, token: str) -> None:
        try:
            import discord
        except ImportError:
            self._error = "The discord.py package isn't installed (pip install discord.py)."
            return
        from discord import app_commands

        intents = discord.Intents.default()      # DMs and @mentions need no privileged intent
        client = discord.Client(intents=intents)
        self.client = client
        tree = app_commands.CommandTree(client)

        # "/help" in Discord's own command picker. Typing /help as a message
        # works too: it goes to JARVIS like anything else.
        @tree.command(name="help", description="Everything JARVIS can do")
        @app_commands.describe(topic="Just one area, e.g. email, reminders, discord, skills")
        async def help_command(interaction, topic: Optional[str] = None):
            await self.slash_help(interaction, topic or "")

        if hasattr(app_commands, "allowed_contexts"):        # DMs with the bot, and servers
            app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)(help_command)

        @client.event
        async def on_ready():
            self._error = ""
            logger.info("discord bot online as %s", client.user)
            await self._log(f"Discord bot online as {client.user}")
            try:
                await tree.sync()
            except Exception as exc:
                logger.warning("couldn't register /help with Discord: %s", exc)

        @client.event
        async def on_voice_state_update(member, before, after):
            try:
                await self.calls.on_voice_state(member, before, after)
            except Exception:
                logger.exception("voice state handling failed")

        @client.event
        async def on_message(message):
            try:
                await self.handle(message)
            except Exception as exc:
                logger.exception("discord message failed")
                try:
                    await message.channel.send(f"Something went wrong: {type(exc).__name__}.")
                except Exception:
                    pass

        try:
            await client.start(token)
        except discord.LoginFailure:
            self._error = "Discord refused the token. Reset it in the Developer Portal and paste the new one."
        except discord.PrivilegedIntentsRequired:
            self._error = "Discord asked for a privileged intent this bot doesn't use."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"Couldn't connect to Discord: {type(exc).__name__}: {exc}"
            logger.warning("discord bot stopped: %s", exc)
        finally:
            # A failed login leaves discord.py's connection open: close it.
            if not client.is_closed():
                try:
                    await client.close()
                except Exception:
                    logger.debug("discord close after failure failed", exc_info=True)
            if self.client is client:
                self.client = None

    # -- messages -----------------------------------------------------------------
    async def handle(self, message) -> None:
        """One message to the bot: from the owner, run it; from anyone else, decline."""
        client = self.client
        if client is None or message.author.bot or message.author.id == getattr(client.user, "id", None):
            return
        is_dm = getattr(message, "guild", None) is None
        mentioned = client.user is not None and client.user in getattr(message, "mentions", [])
        config = await self.config()
        if not is_dm and not (mentioned and config["servers"]):
            return
        text = strip_mention(message.content or "", getattr(client.user, "id", None))
        author = str(message.author.id)
        owners = {o["id"] for o in config["owners"]}

        if author not in owners:
            await self._stranger(message, text, is_dm, config)
            return
        if not self.allowed():
            await message.reply("Discord chat is switched off in JARVIS's permissions.",
                                mention_author=False)
            return
        # A voice message from the phone: heard here, on this PC, then
        # answered like anything typed.
        heard = ""
        if not text:
            heard = await self._voice_message(message)
            if not heard:
                return
            text = heard

        channel_id = message.channel.id
        waiting = self._waiting.get(channel_id)
        if waiting and (_YES.fullmatch(text) or _NO.fullmatch(text)):
            approved = bool(_YES.fullmatch(text))
            self._waiting.pop(channel_id, None)
            await self.confirmations.resolve(waiting, approved, actor=f"discord:{author}")
            await message.add_reaction("✅" if approved else "✋")
            return

        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            await self._record("discord.message", f"Discord message from {message.author}",
                               {"channel": str(channel_id), "chars": len(text)})
            conversation = await self._conversation(channel_id, config)
            reply, sources = "", []
            async with message.channel.typing():
                async for event in self.orchestrator.chat(text, conversation_id=conversation,
                                                          source="discord"):
                    kind = event.get("type")
                    if kind == "tool_end":
                        sources += event.get("sources") or []
                    elif kind == "final":
                        reply = event.get("text") or reply
                    elif kind == "error":
                        reply = f"I couldn't finish that: {event.get('error')}"
            shown = with_sources(reply, sources)
            if heard:
                shown = f"🎙️ *“{heard}”*\n\n{shown}"   # what was understood, so a mishearing is visible
            for i, part in enumerate(chunks(shown)):
                if i == 0:
                    await message.reply(part, mention_author=False)
                else:
                    await message.channel.send(part)
            # On a call: the answer is read out loud there too.
            if self.calls.active:
                asyncio.create_task(self.calls.speak(reply))

    async def _voice_message(self, message) -> str:
        """The words of a voice message (or any audio file) the owner sent, else ""."""
        audio = next((a for a in getattr(message, "attachments", []) or []
                      if str(getattr(a, "content_type", "") or "").startswith("audio/")), None)
        if audio is None:
            return ""
        speech = self.calls.tts
        if speech is None or not hasattr(speech, "transcribe"):
            await message.reply("I can't listen to voice messages here: speech recognition isn't "
                                "available.", mention_author=False)
            return ""
        if (getattr(audio, "size", 0) or 0) > VOICE_MESSAGE_LIMIT:
            await message.reply("That recording is too long for me. Keep it under a few minutes.",
                                mention_author=False)
            return ""
        try:
            data = await audio.read()
            async with message.channel.typing():
                result = await speech.transcribe(data, getattr(audio, "filename", "") or "voice-message.ogg")
        except Exception as exc:
            logger.warning("voice message transcription failed: %s", exc)
            await message.reply(f"I couldn't make out that recording ({type(exc).__name__}).",
                                mention_author=False)
            return ""
        words = (result or {}).get("text", "").strip()
        if not words:
            await message.reply("I couldn't hear any words in that. Try again a little closer to "
                                "the mic?", mention_author=False)
            return ""
        await self._record("discord.voice_message", "Discord voice message transcribed",
                           {"seconds": round(float((result or {}).get("duration") or 0), 1)})
        return words

    async def slash_help(self, interaction, topic: str = "") -> None:
        """The /help slash command: only the owner sees it, even in a server."""
        config = await self.config()
        if str(interaction.user.id) not in {o["id"] for o in config["owners"]}:
            await interaction.response.send_message(
                "I'm a private AI assistant and only answer the person who runs me.", ephemeral=True)
            return
        if not self.allowed():
            await interaction.response.send_message(
                "Discord chat is switched off in JARVIS's permissions.", ephemeral=True)
            return
        await self._record("discord.help", f"/help on Discord{(' ' + topic) if topic else ''}")
        parts = chunks(self.orchestrator.help_text(topic))
        await interaction.response.send_message(parts[0], ephemeral=True)
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True)

    async def _stranger(self, message, text: str, is_dm: bool, config: Dict[str, Any]) -> None:
        author = str(message.author.id)
        code = re.fullmatch(r"pair\s+(\d{6})", text or "", re.I)
        if is_dm and code:
            if self._code and time.time() < self._code_until and code.group(1) == self._code:
                self._code = None
                owners = config["owners"] + [{"id": author, "name": str(message.author)}]
                await self._save(owners=owners)
                await self._record("discord.paired", f"Discord account paired: {message.author}",
                                   {"id": author})
                await message.reply("Paired. I'm JARVIS, the AI assistant running on your "
                                    "computer: message me here and I'll answer. Say “send my "
                                    "notifications to Discord” if you want reminders here too.",
                                    mention_author=False)
            else:
                await message.reply("That code isn't right or has expired. Say “pair Discord” "
                                    "to JARVIS for a new one.", mention_author=False)
            return
        last = self._strangers.get(author, 0.0)
        if time.time() - last < STRANGER_GAP_S:
            return
        self._strangers[author] = time.time()
        await message.reply("I'm a private AI assistant and only answer the person who runs me. "
                            "If that's you, say “pair Discord” to JARVIS and DM me the code.",
                            mention_author=False)

    async def _conversation(self, channel_id: int, config: Dict[str, Any]) -> str:
        """Each Discord chat is its own JARVIS conversation, kept across restarts."""
        known = (config.get("conversations") or {}).get(str(channel_id))
        conversation = await self.orchestrator.ensure_conversation(known)
        if conversation != known:
            await self._save(conversations={**(config.get("conversations") or {}),
                                            str(channel_id): conversation})
        return conversation

    # -- confirmations ------------------------------------------------------------
    async def _on_confirmation(self, event) -> None:
        """A tool wants the owner's OK during a Discord chat: ask in that chat."""
        data = event.data or {}
        config = await self.config()
        channel_id = next((int(c) for c, conv in (config.get("conversations") or {}).items()
                           if conv and conv == data.get("conversation_id")), None)
        if channel_id is None or self.client is None:
            return
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except Exception:
                return
        self._waiting[channel_id] = data.get("id")
        minutes = max(1, int((data.get("timeout") or 120) // 60))
        await channel.send(f"Before I do this I need your OK: **{data.get('summary', data.get('tool'))}** "
                           f"(risk: {data.get('risk', 'medium')}). Reply **yes** or **no** "
                           f"within {minutes} minute{'s' if minutes != 1 else ''}.")

    async def _on_resolved(self, event) -> None:
        resolved = (event.data or {}).get("id")
        for channel_id, waiting in list(self._waiting.items()):
            if waiting == resolved:
                self._waiting.pop(channel_id, None)

    # -- notifications ------------------------------------------------------------
    async def _on_notification(self, event) -> None:
        """A reminder going off, research finishing: a DM to each paired account.

        Only notifications that got past the mode's rules arrive here (focus
        mode holds back the minor ones), and only with "notify" turned on.
        """
        data = event.data or {}
        client = self.client
        if client is None or client.is_closed() or not self.allowed():
            return
        config = await self.config()
        if config["call_reminders"] and config["owners"] and data.get("title") == "Reminder":
            try:
                await self.calls.call(say=f"A reminder: {data.get('body') or ''}")
            except Exception as exc:
                logger.warning("couldn't call about a reminder: %s", exc)
        if not config["notify"] or not config["owners"]:
            return
        text = f"🔔 **{data.get('title') or 'JARVIS'}**"
        if data.get("body"):
            text += "\n" + str(data["body"])
        for owner in config["owners"]:
            try:
                user = client.get_user(int(owner["id"])) or await client.fetch_user(int(owner["id"]))
                channel = user.dm_channel or await user.create_dm()
                for part in chunks(text):
                    await channel.send(part)
            except Exception as exc:
                logger.warning("couldn't send a notification to Discord: %s", exc)

    # -- records ----------------------------------------------------------------
    async def _record(self, action: str, summary: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if self.audit is not None:
            try:
                await self.audit.record(action, subject="discord", detail={"summary": summary, **(detail or {})})
            except Exception:
                logger.debug("audit failed", exc_info=True)
        await self._log(summary)

    async def _log(self, text: str) -> None:
        if self.activity is not None:
            try:
                await self.activity.record(text, source="discord")
            except Exception:
                logger.debug("activity failed", exc_info=True)
