"""/help: everything JARVIS can do, with something to say for each.

One list for the screen, the public edition and the Discord bot. Every line
is tied to the tool that does it, so the list only ever shows what this copy
of JARVIS really has: a line whose tool isn't installed is left out, and one
whose permission is switched off says which switch turns it on.

"/help" is the overview: each area, what it covers, and two things to try.
"/help <area>" ("/help discord", "/help email") lists everything in it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class Line(NamedTuple):
    say: str          # something to say that really works
    what: str         # what it does, briefly
    tool: str         # the tool that does it: no tool, no line


class Section(NamedTuple):
    key: str
    title: str
    aliases: Tuple[str, ...]
    summary: str                      # what the area covers, for the overview
    lines: Tuple[Line, ...]
    needs: str = ""                   # a tool without which the whole area is left out


SECTIONS: Tuple[Section, ...] = (
    Section('lookup', 'Ask and look things up', ('ask', 'look up', 'lookups', 'facts', 'web', 'weather'),
            "today's facts with sources, web search, weather, units and currency, world clock, Wikipedia, definitions, maths, dice", (
        Line("who's projected to win tonight's game?", "today's facts, read from the day's articles, with sources", 'live_lookup'),
        Line('search the web for cheap flights to Denver', 'a web search', 'web_search'),
        Line('weather in Chicago', 'conditions and the forecast', 'weather'),
        Line('70 F in C', 'unit conversions', 'convert_units'),
        Line('50 dollars in euros', 'currency, at the published rate', 'convert_currency'),
        Line('time in Tokyo', 'the time anywhere', 'world_clock'),
        Line('tell me about the Eiffel Tower', 'a Wikipedia summary', 'wikipedia_lookup'),
        Line('define serendipity', "a word's meaning", 'define_word'),
        Line("what's 15% of 240?", 'exact maths', 'calculate'),
        Line('roll two dice', 'coin flips, dice and random picks', 'flip_coin_roll_dice'),
    )),
    Section('research', 'Research', ('research', 'report', 'study'),
            "long research reports with sources and what's next, or a quick answer", (
        Line('research Switch 2 emulators for Android', "15 to 20 minutes of reading, then a report with sources and what's next", 'research_topic'),
        Line('quick research on tide times', 'a few minutes, a short answer', 'quick_research'),
        Line('stop the research', 'stops it', 'stop_task'),
    )),
    Section('reminders', 'Reminders and timers', ('reminder', 'reminders', 'remind', 'timer', 'timers', 'alarm'),
            'one-off and repeating reminders, timers', (
        Line('remind me at 7 to call Dad', 'a reminder, also as a Windows notification', 'schedule_notification'),
        Line('every weekday at 7:30 tell me to stretch', 'a repeating reminder', 'schedule_notification'),
        Line('set a timer for 10 minutes', 'a timer', 'timer'),
        Line('what reminders do I have?', 'the list', 'list_reminders'),
        Line("cancel the 7 o'clock reminder", 'cancels one', 'cancel_reminder'),
    )),
    Section('checklists', 'Checklists', ('checklist', 'checklists', 'lists', 'list', 'grocery', 'shopping', 'workout'),
            'grocery lists, workouts, anything to tick off', (
        Line('show me my checklists', 'opens them: pick one, tap to check and uncheck', 'show_checklists'),
        Line('make a grocery list: eggs, milk and bread', 'a new checklist', 'make_checklist'),
        Line('add butter to my grocery list', 'adds items', 'add_to_checklist'),
        Line('check off eggs', 'ticks one off (“uncheck eggs” to undo)', 'check_off'),
        Line("what's on my grocery list?", 'reads it out', 'show_checklists'),
        Line('reset my workout list', 'unticks everything to use it again', 'reset_checklist'),
        Line('clear the checked items from my grocery list', 'removes what you got', 'reset_checklist'),
        Line('delete my packing list', 'deletes a whole list (asks first)', 'delete_checklist'),
    )),
    Section('countdowns', 'Countdowns', ('countdown', 'countdowns', 'how long'),
            'countdowns on the main screen, how long until anything', (
        Line('countdown to the World Cup', 'a countdown on the main screen', 'add_countdown'),
        Line('how long until Christmas?', 'days, hours and minutes to go', 'how_long_until'),
        Line('remove the World Cup countdown', 'takes one off', 'remove_countdown'),
    )),
    Section('calendar', 'Calendar and your day', ('calendar', 'day', 'schedule', 'meetings', 'plan'),
            'your calendar, free time, new events, a plan for the day', (
        Line("what's on today?", 'your calendar', 'calendar_view'),
        Line('plan my day', 'a plan from your calendar, tasks and goals', 'plan_my_day'),
        Line('when am I free tomorrow afternoon?', 'free time', 'find_free_time'),
        Line('add lunch with Sam on Friday at 1', 'a new event (asks first)', 'calendar_create'),
        Line('what should I do next?', "a suggestion from what's due", 'what_should_i_do_next'),
    )),
    Section('email', 'Email', ('email', 'mail', 'inbox'),
            "what's new, search, read, and reply (only after you approve)", (
        Line('any new emails?', "what's new, without marking anything read", 'check_email'),
        Line('find the email from my landlord', 'searches your mailbox', 'search_email'),
        Line('read me the latest one', 'reads a message', 'read_email'),
        Line("reply and say I'll be there", 'sends only after you approve it', 'send_email'),
    )),
    Section('files', 'Files and documents', ('files', 'file', 'documents', 'folders', 'pdf'),
            'find, list, move and tidy files; questions about your documents; summaries', (
        Line('find my resume', 'finds files', 'find_files'),
        Line("what's in my Downloads folder?", 'lists a folder', 'list_folder'),
        Line('what does my lease say about pets?', 'answers from your documents, citing them', 'search_documents'),
        Line('summarise this PDF', 'a summary of a document', 'summarise_document'),
        Line('move the invoices into a folder called Taxes', 'moves, copies and creates', 'move_file'),
        Line('delete the old installers', 'to a recoverable trash, after asking', 'delete_file'),
    )),
    Section('computer', 'Your computer', ('computer', 'pc', 'apps', 'system', 'volume', 'screen'),
            "open and close apps, volume and media, system health, screenshots, what's on screen", (
        Line('open Spotify', 'opens apps', 'launch_app'),
        Line('close Chrome', 'closes them', 'close_app'),
        Line('volume to 30%', 'volume', 'volume_control'),
        Line('pause the music', 'media keys', 'media_control'),
        Line("what's using my CPU?", 'CPU, memory, disk, GPU and network', 'system_status'),
        Line('take a screenshot', 'captures the screen', 'take_screenshot'),
        Line('what am I looking at?', "reads what's on screen", 'look_at_screen'),
    )),
    Section('away', "Work while you're away", ('away', 'unattended', 'build', 'tasks', 'background'),
            "unattended building while you're out, background tasks", (
        Line("while I'm away for an hour, build me a habit tracker", 'builds, tests and reports honestly', 'start_away_work'),
        Line("I'm back", 'hands over what was done', 'welcome_back'),
        Line('stop the away work', 'stops it', 'stop_away_work'),
        Line("what's running?", 'background tasks', 'list_tasks'),
    )),
    Section('automations', 'Automations and schedules', ('automation', 'automations', 'schedules', 'routines'),
            'scheduled jobs, rules, focus and other modes', (
        Line('every morning at 8, tell me the weather', 'a scheduled job', 'create_schedule'),
        Line('what automations do I have?', 'the list', 'list_automations'),
        Line('switch to focus mode', 'modes that quiet notifications', 'set_mode'),
    )),
    Section('memory', 'Memory', ('memory', 'remember', 'forget'),
            'lasting facts about you, seen and deletable', (
        Line("remember I'm vegetarian", 'keeps a lasting fact', 'remember'),
        Line('what do you remember about me?', 'what it knows', 'recall'),
        Line("forget that I'm vegetarian", 'deletes it', 'forget'),
    )),
    Section('goals', 'Goals and habits', ('goals', 'goal', 'habits'),
            "goals and habits, and how you're doing", (
        Line('set a goal to run a 5k by March', 'a goal it helps you keep', 'set_goal'),
        Line('how am I doing on my goals?', 'progress', 'goal_progress'),
        Line('what are my habits?', 'your routines', 'my_habits'),
    )),
    Section('settings', 'Settings, just by saying it', ('settings', 'setting', 'theme', 'voice', 'colours', 'colors'),
            'wake word, answer length, voice, themes, skill colours, undo', (
        Line('turn on the wake word', 'say “Jarvis, …” to start', 'change_setting'),
        Line('make your answers shorter', 'answer length', 'change_setting'),
        Line('speak a bit slower', 'voice speed and volume', 'change_setting'),
        Line('switch to the Ember theme', 'themes', 'change_setting'),
        Line('stop changing colours', 'skill colours on or off', 'change_setting'),
        Line('undo that', 'puts the last change back', 'undo_setting'),
    )),
    Section('screens', 'Screens', ('screens', 'panels', 'open', 'menu'),
            'open or close any part of JARVIS by name', (
        Line('open reminders', 'opens any part of JARVIS by name', 'open_panel'),
        Line('close that', 'closes it', 'open_panel'),
    )),
    Section('discord', 'Discord', ('discord', 'bot'),
            'chat with JARVIS through your own Discord bot, notifications as DMs, calls in voice', (
        Line('pair Discord', 'chat with JARVIS from your own Discord bot', 'discord_pair'),
        Line('send my notifications to Discord', 'reminders and finished research as DMs', 'discord_notify'),
        Line('answer me in Discord servers too', '@mentions in servers, not only DMs', 'discord_servers'),
        Line('turn off Discord', 'stops the bot', 'discord_switch'),
        Line('unpair Discord', 'nobody can use it until you pair again', 'discord_unpair'),
        Line('call me on Discord', 'DMs you to join voice, then talks to you there', 'discord_call'),
        Line('hang up', 'ends the call', 'discord_hang_up'),
        Line('call me for reminders', 'reminders ring as a Discord call', 'discord_call_reminders'),
    )),
    Section('devices', 'Phone and devices', ('phone', 'devices', 'smart home', 'home'),
            'send files to your phone, smart home', (
        Line('send this file to my phone', 'to your devices', 'send_file_to_device'),
        Line('turn off the living room lights', 'smart home, through Home Assistant', 'smart_home_control'),
    )),
    Section('selfedit', 'Editing JARVIS itself', ('self-edit', 'selfedit', 'self edit', 'code', 'edit yourself'),
            "a separate local model edits JARVIS's own code, checked and undoable", (
        Line('edit yourself so the clock shows seconds', 'a separate local model writes the change; nothing is written yet', 'self_edit_model'),
        Line('apply it', 'writes it, checks it, puts it back if it breaks', 'apply_edit'),
        Line('undo that edit', 'restores the file', 'undo_edit'),
        Line('which model edits your code?', 'the code model', 'self_edit_model'),
    ), needs="self_edit_model"),
    Section('developer', 'Developer tools', ('developer', 'dev', 'git', 'tests', 'programming'),
            'error explanations, tests, git, code review', (
        Line('explain this error: …', 'against the real source', 'explain_error'),
        Line('run the tests', 'runs them', 'run_tests'),
        Line('what changed in git?', 'status, diffs and history', 'git_status'),
        Line('review my changes', 'a code review', 'review_code'),
    )),
)

_ASKED = re.compile(r"(?:/help|/commands|help|commands|what\s+can\s+you\s+do|what\s+do\s+you\s+do|"
                    r"list\s+(?:(?:your|all(?:\s+your)?)\s+)?commands|show\s+me\s+(?:the\s+|your\s+)?commands)"
                    r"(?:\s+(?:with|for|on|about))?(?:\s+(?P<topic>.+))?", re.I)


def help_said(message: str) -> Optional[str]:
    """The topic asked about ('' for everything) if this asks for help, else None."""
    said = " ".join((message or "").strip().strip("?!.").split())
    said = re.sub(r"^(?:hey\s+)?jarvis[,:]?\s+", "", said, flags=re.I)
    m = _ASKED.fullmatch(said)
    if not m:
        return None
    topic = (m.group("topic") or "").strip().lower()
    # "help me write an email" is a request, not a question about JARVIS:
    # without the slash, only an area's own name counts as a topic.
    if topic and not said.startswith("/") and not _exact(topic):
        return None
    return topic


def _exact(topic: str) -> bool:
    return topic in ("skills", "skill") or any(
        topic in (s.key, s.title.lower()) or topic in s.aliases for s in SECTIONS)


def _section(topic: str) -> Optional[Section]:
    topic = topic.lower().strip()
    for section in SECTIONS:
        if topic in (section.key, section.title.lower()) or topic in section.aliases:
            return section
    for section in SECTIONS:                  # "help with my email" -> email
        if any(re.search(rf"\b{re.escape(alias)}\b", topic) for alias in (section.key,) + section.aliases):
            return section
    return None


def _available(registry, permissions, section: Section) -> List[Tuple[Line, str]]:
    """The section's lines whose tools exist, with a note when one is switched off."""
    from ..core.permissions import REGISTRY

    labels = {p.key: p.label for p in REGISTRY}
    if section.needs and (registry is None or registry.get(section.needs) is None):
        return []
    out = []
    for line in section.lines:
        tool = registry.get(line.tool) if registry is not None else None
        if tool is None:
            continue
        note = ""
        key = getattr(tool, "permission", "") or ""
        if key and permissions is not None and not permissions.allowed(key):
            note = f"off: turn on “{labels.get(key, key)}” in Permissions"
        out.append((line, note))
    return out


def _skills(skills) -> List[Any]:
    if skills is None:
        return []
    try:
        return sorted((s for s in skills.all() if s.enabled), key=lambda s: s.name)
    except Exception:
        return []


def build_help(registry, skills=None, permissions=None, topic: str = "") -> str:
    """The help text: the overview, or every line of one area."""
    topic = (topic or "").strip().lower()
    if topic in ("skills", "skill", "slash", "slash commands"):
        enabled = _skills(skills)
        if not enabled:
            return "No skills are installed. Open Skills to write or import one."
        rows = [f"- `/{s.name}` — {s.description}" for s in enabled]
        return "**Skills** — type the name with a slash, then what you want:\n" + "\n".join(rows)

    if topic:
        section = _section(topic)
        if section is None:
            names = ", ".join(s.key for s in SECTIONS) + ", skills"
            return f"There's no help for “{topic}”. Try /help with one of: {names}."
        lines = _available(registry, permissions, section)
        if not lines:
            return f"This copy of JARVIS doesn't have {section.title.lower()}."
        rows = [f"- “{line.say}” — {line.what}" + (f" ({note})" if note else "") for line, note in lines]
        return f"**{section.title}**\n" + "\n".join(rows)

    out = ["## What JARVIS can do",
           "Say it or type it. Plain requests happen at once; anything risky asks you first. "
           "`/help <area>` lists everything in one area, e.g. `/help email`.", ""]
    for section in SECTIONS:
        lines = _available(registry, permissions, section)
        if not lines:
            continue
        tries = " · ".join(f"“{line.say}”" for line, _ in lines[:2])
        off = " Some of it is switched off in Permissions." if any(note for _, note in lines) else ""
        # One list item per area: a renderer joins plain line breaks into one paragraph.
        out.append(f"- **{section.title}** (`/help {section.key}`): {section.summary}.{off} "
                   f"Try {tries}")
    enabled = _skills(skills)
    if enabled:
        names = " ".join(f"`/{s.name}`" for s in enabled)
        out.append(f"- **Skills** (`/help skills`): {names}")
    return "\n".join(out)
