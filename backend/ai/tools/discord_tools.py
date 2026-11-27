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
    ("servers_on", re.compile(r"(?:answer|reply\s+to)\s+me\s+in\s+(?:discord\s+)?servers(?:\s+too)?")),
    ("servers_off", re.compile(r"(?:only|just)\s+(?:answer|reply)(?:\s+to)?\s+(?:me\s+)?in\s+(?:discord\s+)?"
                               r"(?:dms|direct\s+messages)|stop\s+answering\s+in\s+(?:discord\s+)?servers")),
]


def discord_said(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(tool, arguments) for a plainly worded Discord request, else None."""
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
