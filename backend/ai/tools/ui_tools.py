"""Opening any part of JARVIS by asking: "open my reminders", "show settings".

Everything in the sidebar can be reached by saying so. The screen opens the
panel when it receives the event; if no JARVIS screen is connected, that is
said plainly rather than claiming something opened.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ...core.events import bus
from .registry import ToolRegistry, fail, ok

# panel key -> (name as shown, other words people use for it)
PANELS: Dict[str, Tuple[str, List[str]]] = {
    "tasks": ("Tasks", ["task manager", "jobs", "background tasks", "running"]),
    "email": ("Email", ["mail", "inbox", "gmail"]),
    "away": ("While away", ["away", "away mode", "while i'm away", "unattended"]),
    "agents": ("Agents", ["agent"]),
    "notifications": ("Notifications", ["alerts"]),
    "reminders": ("Reminders", ["reminder", "scheduled notifications"]),
    "skills": ("Skills", ["skill"]),
    "workshop": ("Workshop", ["wallpapers", "wallpaper packs", "packs"]),
    "selfedit": ("Self-edit", ["self edit", "self-editing", "your own code"]),
    "developer": ("Developer", ["code", "git", "workspace", "dev"]),
    "research": ("Research", ["research projects", "reports"]),
    "history": ("History", ["conversation history", "past conversations", "old chats"]),
    "memory": ("Memory", ["memories", "what you remember"]),
    "documents": ("Documents", ["docs", "my documents", "files index"]),
    "voice": ("Heard", ["what you heard", "voice log", "wake word"]),
    "activity": ("Activity", ["activity log", "log"]),
    "goals": ("Goals", ["goal"]),
    "calendar": ("Calendar", ["diary", "schedule", "events"]),
    "countdowns": ("Countdowns", ["countdown", "timers"]),
    "everyday": ("Everyday", ["everyday lookups", "lookups", "converter"]),
    "discord": ("Discord", ["discord bot", "the discord bot", "discord settings"]),
    "automations": ("Automations", ["automation", "rules"]),
    "system": ("System", ["system monitor", "pc stats", "performance"]),
    "integrations": ("Integrations", ["connections"]),
    "plugins": ("Plug-ins", ["plugins", "plug ins", "mcp"]),
    "settings": ("Settings", ["preferences", "options"]),
}


def _screens(hub) -> int:
    """How many JARVIS screens are connected (a property on the real hub)."""
    count = getattr(hub, "client_count", None)
    return int(count() if callable(count) else count or 0)


def which_panel(words: str, exact: bool = False) -> Optional[str]:
    said = " ".join(re.sub(r"[^a-z0-9' -]", " ", (words or "").lower()).split())
    said = re.sub(r"^(?:the|my|your)\s+", "", said)
    said = re.sub(r"\s+(?:panel|page|tab|screen|menu|section)$", "", said)
    for key, (label, others) in PANELS.items():
        if said in (key, label.lower()) or said in others:
            return key
    if exact:
        return None
    for key, (label, others) in PANELS.items():
        names = [key, label.lower(), *others]
        if any(re.search(rf"\b{re.escape(name)}\b", said) for name in names):
            return key
    return None


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "open_panel",
        "Open a part of JARVIS on screen: " + ", ".join(label for label, _ in PANELS.values())
        + ". Use for 'open settings', 'show my reminders', 'go to email'. Use 'close' to close the "
        "open panel.",
        {"type": "object", "properties": {
            "panel": {"type": "string", "description": "Which part, e.g. 'reminders', or 'close'"},
        }, "required": ["panel"]},
        category="open show panel screen menu sidebar settings close go",
    )
    async def open_panel(ctx, panel: str):
        hub = ctx.get("hub")
        if hub is not None and _screens(hub) == 0:
            return fail("No JARVIS screen is open right now, so there's nothing to open it on.")
        if (panel or "").strip().lower() in ("close", "close it", "none", "hide"):
            bus.publish("ui.open_panel", {"panel": None}, source="ui")
            return ok({"panel": None}, "Closed the panel.")
        key = which_panel(panel)
        if key is None:
            return fail(f"There's no part of JARVIS called “{panel}”. The parts are: "
                        + ", ".join(label for label, _ in PANELS.values()) + ".")
        bus.publish("ui.open_panel", {"panel": key}, source="ui")
        return ok({"panel": key}, f"Opened {PANELS[key][0]} on screen.")
