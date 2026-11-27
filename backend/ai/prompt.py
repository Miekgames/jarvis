"""
System prompt assembly.

Kept in its own module because it is the single highest-leverage piece of
behaviour in the app, and because it needs live context -- the time, the active
mode, what is currently running, what is coming up -- that only the database
can answer.
"""
from __future__ import annotations

import platform
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.util import now_iso, parse_iso, utcnow

_TONE = {
    "composed": "Composed, precise, quietly confident. Understatement over enthusiasm.",
    "warm": "Warm and personable while staying efficient.",
    "terse": "Extremely brief. Answer, then stop.",
    "playful": "Light and quick-witted, but never at the cost of clarity.",
}

_LENGTH = {
    "concise": "Default to one to three sentences. Expand only when the user asks for depth.",
    "balanced": "A short paragraph for most answers.",
    "detailed": "Give thorough answers with structure when the topic warrants it.",
}

_BASE_RULES = """
Operating rules:
- Say what you actually did. Never claim an action succeeded when the tool
  reported an error, and never describe a capability as done when it is queued.
- If a tool returns an error, tell the user plainly what failed and what you
  can do instead.
- If a permission is disabled or an integration is not connected, say so and
  name the setting -- do not pretend the capability does not exist.
- Prefer doing over asking, but for anything destructive, outbound, financial
  or physical, let the confirmation system handle it: call the tool and the
  user will be asked.
- Long or open-ended work (research, multi-step preparation, batch file work)
  belongs in a background task via `create_task`, not in a single reply.
  Tell the user the task started and that they can watch it run.
- When the user says "what's next", "stop", "that one" and similar, resolve it
  from context: recent messages, the active task list, the next calendar event.
- Voice replies are spoken aloud. Keep them speakable: no markdown, no code
  blocks, no bullet symbols, no URLs read character by character.
"""


def _emoji_rule(personality: Dict[str, Any]) -> str:
    """Emojis where they add meaning ("use emojis" / "stop using emojis")."""
    if personality.get("emojis", True) is False:
        return "- Don't use emojis."
    return ("- Use an emoji now and then where it adds meaning or warmth (✅ done, 🛒 groceries, "
            "📅 a date, ⚠️ a warning): one or two in a reply at most, never in code or file paths.")


async def build_compact_prompt(
    settings,
    personality: Dict[str, Any],
    db,
    settings_store,
    extra_context: str = "",
) -> str:
    """A deliberately small system prompt for local models.

    Measured on this machine: a 7B local model calls tools correctly with a
    short prompt and 12 tools, but narrates ("I will use tool_44") with the
    full prompt even when given a single tool. The long-form operating rules,
    the tool inventory and the style directives are what drown it -- so local
    models get identity, the honesty rule, and the live facts, nothing else.
    """
    name = personality.get("name") or settings.ASSISTANT_NAME
    owner = personality.get("address_user_as") or settings.OWNER_NAME or "the user"

    lines = [
        f"You are {name}, {owner}'s AI assistant on their {platform.system()} computer.",
        "",
        "Rules:",
        "- You have tools. When a question needs live data about THIS computer, the "
        "user's calendar, their files, their tasks, or the web, CALL A TOOL.",
        "- For general knowledge (facts, definitions, maths, language, writing), just "
        "ANSWER. Do not call a tool, and do not say you don't know something you know.",
        "- Only call a tool from the list you were given. If no tool fits, answer "
        "directly in plain language.",
        "- NEVER invent numbers, application names, window titles or file paths. For "
        "anything about this machine or the user's own data, if you did not get it "
        "from a tool or from the context below (what you remember about them, their "
        "documents, this conversation), say you don't know.",
        "- When a tool returns values, quote them EXACTLY as given, with the field's own "
        "units. Do not convert, add up, or combine numbers from different fields. Read "
        "each field name carefully: disk_free_gb is free space, mem_total_gb is memory.",
        "- Keep answers to one or two sentences.",
        _emoji_rule(personality),
    ]

    context = await _live_context(db, settings_store, settings)
    if context:
        lines += ["", "Current facts:", context]
    if extra_context:
        lines += ["", extra_context]

    return "\n".join(lines)


async def build_system_prompt(
    settings,
    personality: Dict[str, Any],
    memory_block: str,
    registry,
    db,
    settings_store,
    extra_context: str = "",
) -> str:
    name = personality.get("name") or settings.ASSISTANT_NAME
    owner = personality.get("address_user_as") or settings.OWNER_NAME or "the user"
    tone = _TONE.get(personality.get("tone", "composed"), _TONE["composed"])
    length = _LENGTH.get(personality.get("response_length", "concise"), _LENGTH["concise"])
    humour = float(personality.get("humour", 0.2) or 0)
    formality = personality.get("formality", "formal")

    sections: List[str] = []

    sections.append(
        f"You are {name}, {owner}'s personal AI command centre, running locally on their "
        f"{platform.system()} machine. You are not a chat window: you have real control over "
        f"this computer, a persistent task engine, a scheduler, an automation engine and a "
        f"set of integrations."
    )

    sections.append(
        f"Voice and manner:\n- {tone}\n- {length}\n{_emoji_rule(personality)}\n"
        f"- Formality: {formality}. Humour level: {humour:.1f} out of 1.0."
        + (f"\n- Address the user as {owner} when it reads naturally, not in every sentence."
           if owner != "the user" else "")
    )

    if personality.get("custom_instructions"):
        sections.append(f"Standing instructions from {owner}:\n{personality['custom_instructions']}")

    sections.append(_BASE_RULES.strip())

    # -- live context --------------------------------------------------------
    context = await _live_context(db, settings_store, settings)
    if context:
        sections.append("Current context:\n" + context)

    if memory_block:
        sections.append(memory_block)

    # -- capability inventory ------------------------------------------------
    available = registry.schemas()
    if available:
        by_category: Dict[str, List[str]] = {}
        for tool in registry.all():
            if any(schema["name"] == tool.name for schema in available):
                by_category.setdefault(tool.category, []).append(tool.name)
        listing = "\n".join(f"- {cat}: {', '.join(sorted(names))}" for cat, names in sorted(by_category.items()))
        sections.append(f"Tools currently enabled:\n{listing}")

    denied = [t for t in registry.all() if t.permission and not _allowed(registry, t)]
    if denied:
        names = ", ".join(sorted({t.permission for t in denied}))
        sections.append(
            f"Capabilities the user has switched off (mention the setting by name if asked): {names}"
        )

    if extra_context:
        sections.append(extra_context)

    return "\n\n".join(s for s in sections if s).strip()


def _allowed(registry, tool) -> bool:
    permissions = registry.context.get("permissions")
    if permissions is None or not tool.permission:
        return True
    return permissions.allowed(tool.permission)


async def _live_context(db, settings_store, settings) -> str:
    lines: List[str] = []
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    lines.append(f"- Local time: {now.strftime('%A %d %B %Y, %H:%M %Z')}"
                 + (f" ({offset})" if offset else ""))
    # From OpenJarvis's time context: tools hand back UTC more often than
    # not, and "08:54 UTC" is not an answer to "when is my meeting".
    lines.append("- Times from tools are often in UTC: convert them to the user's "
                 "local time before saying them, unless asked otherwise.")

    mode = settings_store.get_cached("active_mode", "normal")
    if mode and mode != "normal":
        lines.append(f"- Active mode: {mode}")

    # running work
    try:
        tasks = await db.fetch_all(
            "SELECT id, name, status, progress, current_step FROM tasks"
            " WHERE status IN ('running','queued','paused','waiting') ORDER BY created_at DESC LIMIT 6"
        )
        if tasks:
            rendered = "; ".join(
                f"{t['name']} ({t['status']}, {int((t['progress'] or 0) * 100)}%)" for t in tasks
            )
            lines.append(f"- Background tasks: {rendered}")
        else:
            lines.append("- Background tasks: none running")
    except Exception:
        pass

    # next events
    try:
        events = await db.fetch_all(
            "SELECT title, start_at, location, link FROM calendar_events"
            " WHERE start_at >= ? ORDER BY start_at ASC LIMIT 3",
            (now_iso(),),
        )
        if events:
            rendered = "; ".join(f"{e['title']} at {_friendly(e['start_at'])}" for e in events)
            lines.append(f"- Next events: {rendered}")
    except Exception:
        pass

    # unread notifications
    try:
        unread = await db.fetch_value(
            "SELECT COUNT(*) FROM notifications WHERE read = 0 AND dismissed = 0", default=0
        )
        if unread:
            lines.append(f"- Unread notifications: {unread}")
    except Exception:
        pass

    return "\n".join(lines)


def _friendly(value: Optional[str]) -> str:
    dt = parse_iso(value)
    if dt is None:
        return "an unknown time"
    local = dt.astimezone()
    today = utcnow().astimezone().date()
    if local.date() == today:
        return local.strftime("%H:%M today")
    delta = (local.date() - today).days
    if delta == 1:
        return local.strftime("%H:%M tomorrow")
    return local.strftime("%a %d %b %H:%M")
