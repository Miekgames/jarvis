"""Discord by saying it: "pair Discord", "turn off Discord", "unpair Discord".

The token itself is the one thing that can't be said: it goes in the Discord
panel, which "set up Discord" opens.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from ...core.events import bus
from .registry import ToolRegistry, fail, ok

_SAID = [
    ("pair", re.compile(r"(?:pair|link|connect|set\s+up|setup)\s+(?:my\s+|the\s+)?discord(?:\s+bot)?"
                        r"(?:\s+(?:account|to\s+me))?")),
    ("on", re.compile(r"(?:turn|switch)\s+on\s+(?:the\s+)?discord(?:\s+bot)?|(?:turn|switch)\s+(?:the\s+)?"
                      r"discord(?:\s+bot)?\s+on|start\s+(?:the\s+)?discord(?:\s+bot)?")),
    ("off", re.compile(r"(?:turn|switch)\s+off\s+(?:the\s+)?discord(?:\s+bot)?|(?:turn|switch)\s+(?:the\s+)?"
                       r"discord(?:\s+bot)?\s+off|(?:stop|disconnect)\s+(?:the\s+)?discord(?:\s+bot)?")),
    ("unpair", re.compile(r"(?:unpair|unlink|forget)\s+(?:my\s+|the\s+)?discord(?:\s+accounts?)?")),
    ("notify_on", re.compile(r"(?:send|forward)\s+(?:me\s+)?(?:my\s+)?(?:notifications|reminders|alerts)"
                             r"\s+(?:to|on|through)\s+(?:my\s+)?discord(?:\s+too)?")),
    ("notify_off", re.compile(r"(?:stop|don'?t|do\s+not)\s+(?:sending|send|forwarding)\s+(?:me\s+)?(?:my\s+)?"
                              r"(?:notifications|reminders|alerts)\s+(?:to|on|through)\s+(?:my\s+)?discord")),
    ("send_files", re.compile(r"(?:send|share)\s+(?:me\s+)?(?P<what>.+?)\s+(?:to|on|through|over|in)\s+"
                              r"(?:my\s+)?discord(?:\s+dms?)?")),
    ("servers_on", re.compile(r"(?:answer|reply\s+to)\s+me\s+in\s+(?:discord\s+)?servers(?:\s+too)?")),
    ("servers_off", re.compile(r"(?:only|just)\s+(?:answer|reply)(?:\s+to)?\s+(?:me\s+)?in\s+(?:discord\s+)?"
                               r"(?:dms|direct\s+messages)|stop\s+answering\s+in\s+(?:discord\s+)?servers")),
]


# Calls: the message to say keeps its own capitals, so these match the
# original words rather than the lower-cased ones.
_CALL_REMINDERS = re.compile(r"call\s+me\s+(?:for|with|about)\s+(?:my\s+)?reminders(?:\s+on\s+discord)?"
                             r"|call\s+me\s+when\s+(?:a\s+)?reminders?\s+(?:goes|go)\s+off", re.I)
_NO_CALL_REMINDERS = re.compile(r"(?:stop|don'?t|do\s+not)\s+call(?:ing)?\s+me\s+(?:for|with|about)\s+"
                                r"(?:my\s+)?reminders", re.I)
_CALL = re.compile(r"(?:(?:call|ring|phone)\s+me|give\s+me\s+a\s+(?:call|ring))"
                   r"(?:\s+(?:on|through|over|via)\s+discord)?(?:\s+now)?"
                   r"(?:\s+(?:and|to)\s+(?P<verb>tell|remind)\s+me\s+(?:that\s+|about\s+)?(?P<say>.+))?", re.I)
_HANG_UP = re.compile(r"hang\s+up(?:\s+the\s+call)?|end\s+(?:the|this|our)\s+call|"
                      r"leave\s+the\s+(?:voice\s+)?(?:call|channel)", re.I)


def _call_said(said: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    if _NO_CALL_REMINDERS.fullmatch(said):
        return "discord_call_reminders", {"on": False}
    if _CALL_REMINDERS.fullmatch(said):
        return "discord_call_reminders", {"on": True}
    if _HANG_UP.fullmatch(said):
        return "discord_hang_up", {}
    m = _CALL.fullmatch(said)
    if m:
        say = (m.group("say") or "").strip()
        if say and (m.group("verb") or "").lower() == "remind":
            say = f"This is your reminder {say if say.lower().startswith('to ') else 'about ' + say}."
        elif say:
            say = say[:1].upper() + say[1:] + ("" if say[-1:] in ".!?" else ".")
        return "discord_call", {"say": say}
    return None


def discord_said(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(tool, arguments) for a plainly worded Discord request, else None."""
    plain = " ".join((message or "").strip(" .!?").replace("’", "'").split())
    plain = re.sub(r"^(?:hey\s+)?jarvis,?\s+", "", plain, flags=re.I)
    plain = re.sub(r"^(?:can you\s+|could you\s+|please\s+)", "", plain, flags=re.I)
    plain = re.sub(r",?\s+(?:please|jarvis)$", "", plain, flags=re.I)
    called = _call_said(plain)
    if called is not None:
        return called
    said = " ".join((message or "").lower().strip(" .!?").replace("’", "'").split())
    said = re.sub(r"^(?:jarvis,?\s+)?(?:can you\s+|could you\s+|please\s+)?", "", said)
    said = re.sub(r",?\s+(?:please|for me|now|jarvis)$", "", said)
    for action, pattern in _SAID:
        if pattern.fullmatch(said):
            if action == "pair":
                return "discord_pair", {}
            if action in ("on", "off"):
                return "discord_switch", {"on": action == "on"}
            if action == "unpair":
                return "discord_unpair", {}
            if action in ("notify_on", "notify_off"):
                return "discord_notify", {"on": action == "notify_on"}
            if action == "send_files":
                what = pattern.fullmatch(said).group("what")
                if re.fullmatch(r"(?:my\s+)?(?:notifications?|reminders?|alerts?)(?:\s+too)?", what):
                    return "discord_notify", {"on": True}
                return "discord_send_project", {"what": what}
            return "discord_servers", {"on": action == "servers_on"}
    return None


def _setup():
    """Open the Discord panel, and say how to get the token for it."""
    bus.publish("ui.open_panel", {"panel": "discord"}, source="ui")
    return ok({"open_panel": "discord"}, SETUP)


SETUP = ("To use me on Discord, make a bot for me first: in the Discord Developer Portal "
         "(discord.com/developers/applications) choose New Application, then Bot, then Reset "
         "Token, and paste the token into the Discord panel I've opened. Then say “pair Discord”.")


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "discord_pair",
        "Pair the owner's Discord account with JARVIS's Discord bot, so they can chat with JARVIS "
        "on Discord: 'pair Discord', 'connect Discord'. Gives a code to DM the bot.",
        {"type": "object", "properties": {}},
        permission="discord.chat",
        category="discord bot pair connect link chat",
    )
    async def discord_pair(ctx):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        status = await bridge.status()
        if not status["token_saved"]:
            return _setup()
        if not status["enabled"]:
            await bridge.set_enabled(True)
            status = await bridge.status()
        if not status["connected"]:
            return fail(status["error"] or "The bot is still connecting to Discord. Try again in a "
                        "few seconds.")
        code = bridge.new_pairing_code()
        from ...integrations.discord_bridge import PAIR_MINUTES

        invite = (" If you can't message it yet, add it to a server of yours first: "
                  f"{status['invite_url']}") if not status["server_count"] else ""
        return ok({"code": code, "bot": status["bot"]},
                  f"Send your bot, {status['bot']}, a direct message saying “pair {code}”. "
                  f"The code works for {PAIR_MINUTES} minutes.{invite}")

    @registry.add(
        "discord_switch",
        "Turn JARVIS's Discord bot on or off: 'turn off Discord', 'start the Discord bot'.",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        category="discord bot on off start stop",
    )
    async def discord_switch(ctx, on: bool):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        status = await bridge.status()
        if on and not status["token_saved"]:
            return _setup()
        await bridge.set_enabled(bool(on))
        return ok({"on": bool(on)}, "The Discord bot is on." if on else
                  "The Discord bot is off: it won't answer anyone until you turn it back on.")

    @registry.add(
        "discord_unpair",
        "Unpair every Discord account from JARVIS's bot: 'unpair Discord'.",
        {"type": "object", "properties": {}},
        category="discord bot unpair unlink forget",
    )
    async def discord_unpair(ctx):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        await bridge.unpair("")
        return ok({}, "Unpaired. The bot won't answer anyone on Discord until you pair again.")

    @registry.add(
        "discord_notify",
        "Whether JARVIS's notifications (reminders going off, research finishing) are also sent "
        "to the owner's Discord DMs: 'send my notifications to Discord'.",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        permission="discord.chat",
        category="discord bot notifications reminders alerts dm",
    )
    async def discord_notify(ctx, on: bool):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        status = await bridge.status()
        if on and not status["owners"]:
            return fail("Pair your Discord account first: say “pair Discord”.")
        await bridge.set_notify(bool(on))
        return ok({"notify": bool(on)},
                  "Your notifications will come to you on Discord too: reminders, finished research "
                  "and the rest, as a DM from your bot." if on else
                  "Notifications stay on the JARVIS screen and in Windows, not Discord.")

    @registry.add(
        "discord_call",
        "Call the owner on Discord: DMs them a link to join a voice channel, then joins them and "
        "speaks: 'call me on Discord', 'call me and tell me the oven's done'.",
        {"type": "object", "properties": {
            "say": {"type": "string", "description": "What to say once they join, if anything"},
        }},
        permission="discord.chat",
        category="discord call ring phone voice talk",
    )
    async def discord_call(ctx, say: str = ""):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        try:
            said = await bridge.calls.call(say=(say or "").strip())
        except RuntimeError as exc:
            return fail(str(exc))
        return ok(bridge.calls.status(), said)

    @registry.add(
        "discord_hang_up",
        "End the Discord call, or cancel one that hasn't been answered: 'hang up'.",
        {"type": "object", "properties": {}},
        category="discord call hang up end voice",
    )
    async def discord_hang_up(ctx):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        if await bridge.calls.hang_up("You hung up."):
            return ok({}, "Hung up.")
        return ok({}, "There's no call to hang up.")

    @registry.add(
        "discord_call_reminders",
        "Whether reminders ring as a Discord call (a DM to join voice, then the reminder said "
        "aloud): 'call me for reminders', 'stop calling me for reminders'.",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        permission="discord.chat",
        category="discord call reminders ring voice",
    )
    async def discord_call_reminders(ctx, on: bool):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        status = await bridge.status()
        if on and not status["owners"]:
            return fail("Pair your Discord account first: say “pair Discord”.")
        await bridge.set_call_reminders(bool(on))
        return ok({"call_reminders": bool(on)},
                  "When a reminder goes off I'll call you on Discord: a DM to join voice, then I'll "
                  "say it." if on else "Reminders won't call you on Discord any more.")

    @registry.add(
        "discord_send_project",
        "Send a project JARVIS built (a zip of its files, and screenshots) to the owner's Discord "
        "DMs: 'send me the tip calculator on Discord', 'send it to my Discord'.",
        {"type": "object", "properties": {
            "what": {"type": "string", "description": "Which project, in the owner's words ('it' for the latest)"},
        }},
        permission="discord.chat",
        category="discord send file files project zip share",
    )
    async def discord_send_project(ctx, what: str = ""):
        from pathlib import Path

        from ...autonomy.packing import find_project, pack, screenshots

        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        settings = ctx.get("settings")
        workspace = Path(getattr(settings, "DATA_DIR", ".")) / "projects"
        folder = find_project(workspace, what)
        if folder is None:
            return fail(f"I couldn't find a project matching “{what}”." if what else
                        "There aren't any projects to send yet.")
        try:
            files = [pack(folder, Path(getattr(settings, "DATA_DIR", ".")) / "outbox")] + screenshots(folder)
            said = await bridge.send_files(files, f"📦 **{folder.name}**")
        except RuntimeError as exc:
            return fail(str(exc))
        return ok({"folder": str(folder)}, said)

    @registry.add(
        "discord_servers",
        "Whether JARVIS's Discord bot also answers @mentions in servers (replies there are "
        "visible to everyone in the channel), or only direct messages.",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        permission="discord.chat",
        category="discord bot servers dms mentions",
    )
    async def discord_servers(ctx, on: bool):
        bridge = ctx.get("discord")
        if bridge is None:
            return fail("The Discord service isn't available.")
        await bridge.set_servers(bool(on))
        return ok({"servers": bool(on)},
                  "I'll answer your @mentions in servers too. Replies there are visible to everyone "
                  "in the channel." if on else "I'll only answer you in direct messages.")
