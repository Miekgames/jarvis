"""Doing by saying: settings and While-away work, without opening a menu.

"Turn on the wake word", "make your answers shorter", "switch to the Ember
theme", "speak a bit slower"; "while I'm away for two hours, build me a habit
tracker", "stop the away work".

Settings are a fixed list with fixed meanings -- the model names the setting
and the value in words, and the change itself is worked out here, so "louder"
can't become a volume of 11.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from .registry import ToolRegistry, fail, ok

# name -> (settings key, field, kind, details, other names)
SETTINGS: Dict[str, Tuple[str, str, str, Any, Tuple[str, ...]]] = {
    "wake word": ("voice", "wake_word_enabled", "bool", None,
                  ("hey jarvis", "listening for my voice", "always listening", "hotword")),
    "voice replies": ("voice", "enabled", "bool", None,
                      ("speaking", "talking", "voice", "read replies aloud", "speech")),
    "speaking speed": ("voice", "rate", "scale", (0.5, 2.0, 0.2, 1.0),
                       ("talking speed", "speech speed", "voice speed", "rate", "speed")),
    "volume": ("voice", "volume", "scale", (0.0, 1.0, 0.2, 1.0), ("loudness",)),
    "answer length": ("personality", "response_length", "choice",
                      {"short": "concise", "shorter": "concise", "brief": "concise",
                       "concise": "concise", "normal": "balanced", "medium": "balanced",
                       "balanced": "balanced", "long": "detailed", "longer": "detailed",
                       "detailed": "detailed", "briefer": "concise", "more detailed": "detailed"},
                      ("answers", "replies", "response length", "reply length")),
    "tone": ("personality", "tone", "choice",
             {"composed": "composed", "calm": "composed", "warm": "warm", "friendly": "warm",
              "terse": "terse", "blunt": "terse", "playful": "playful", "funny": "playful"},
             ("personality", "manner", "style")),
    "humour": ("personality", "humour", "scale", (0.0, 1.0, 0.2, 0.2), ("humor", "jokes")),
    "privacy mode": ("privacy", "privacy_mode", "bool", None, ("privacy",)),
    "learning about me": ("ai", "learn_automatically", "bool", None,
                          ("learning", "learn about me", "remembering things about me")),
    "suggestions": ("awareness", "proactive", "bool", None, ("proactive suggestions", "tips")),
    "reduced motion": ("theme", "reduced_motion", "bool", None, ("animations", "motion")),
    "high contrast": ("theme", "high_contrast", "bool", None, ("contrast",)),
    "skill colours": ("theme", "skill_colors", "bool", None,
                      ("skill colors", "skill colour", "skill color", "colour changes",
                       "color changes", "changing colours", "changing colors")),
    "text size": ("theme", "font_scale", "scale", (0.8, 1.4, 0.1, 1.0), ("font size", "text")),
    "particles": ("particles", "count", "int", (800, 9000, 1.5, 4200),
                  ("particle count", "particle field", "dots")),
    "notification sound": ("notifications", "sound", "bool", None, ("sounds", "alert sound")),
    "daily briefing": ("briefing", "enabled", "bool", None, ("morning briefing", "briefing")),
}

_ON = re.compile(r"\b(on|yes|enable|enabled|start|true|activate|turn it on)\b", re.I)
_OFF = re.compile(r"\b(off|no|disable|disabled|stop|false|deactivate|turn it off|mute)\b", re.I)
_UP = re.compile(r"\b(up|more|faster|louder|bigger|larger|higher|increase|raise|longer)\b", re.I)
_DOWN = re.compile(r"\b(down|less|fewer|slower|quieter|smaller|lower|decrease|reduce)\b", re.I)


def which_setting(words: str, exact: bool = False) -> Optional[str]:
    said = " ".join((words or "").lower().replace("’", "'").replace("_", " ").replace("-", " ").split())
    said = re.sub(r"^(?:the|my|your)\s+", "", said)
    for name, (_k, _f, _kind, _d, others) in SETTINGS.items():
        if said == name or said in others:
            return name
    if exact:
        return None
    for name, (_k, _f, _kind, _d, others) in SETTINGS.items():
        if any(re.search(rf"\b{re.escape(n)}\b", said) for n in (name, *others)):
            return name
    return None


def new_value(name: str, value: str, current: Any) -> Any:
    """The setting's new value from words like "on", "slower", "1.2", "warm"."""
    _key, _field, kind, details, _others = SETTINGS[name]
    words = (value or "").strip().lower()
    if kind == "bool":
        if _OFF.search(words):
            return False
        if _ON.search(words):
            return True
        raise ValueError(f"Say whether to turn {name} on or off.")
    if kind == "choice":
        for word, chosen in details.items():
            if re.search(rf"\b{word}\b", words):
                return chosen
        if _UP.search(words) and name == "answer length":
            return "detailed"
        if _DOWN.search(words) and name == "answer length":
            return "concise"
        raise ValueError(f"{name.capitalize()} can be: "
                         + ", ".join(sorted(set(details.values()))) + ".")
    low, high, step, default = details
    number = re.search(r"-?\d+(?:\.\d+)?", words)
    if re.search(r"\b(default|normal|reset)\b", words):
        result = default
    elif number:
        result = float(number.group(0))
        if kind == "scale" and high <= 2 and result > high:     # "volume 80" means 80%
            result = result / 100
    elif _UP.search(words):
        result = current * step if kind == "int" else (current or default) + step
    elif _DOWN.search(words):
        result = current / step if kind == "int" else (current or default) - step
    else:
        raise ValueError(f"Say a value for {name}, or 'more' / 'less'.")
    result = max(low, min(high, result))
    return int(round(result)) if kind == "int" else round(result, 2)


async def apply(store, setting: str, value: str):
    """Change a setting from words. Returns (done, what to say)."""
    if re.search(r"\b(theme|colou?rs?|look)\b", f"{setting} {value}", re.I):
        from ...services import themes

        wanted = f"{value} {setting}".lower()
        key = next((k for k, t in themes.THEMES.items()
                    if k.replace("_", " ") in wanted or t["name"].lower() in wanted), None)
        if key is None:
            return False, ("The themes are: " + ", ".join(t["name"] for t in themes.THEMES.values())
                           + ". Which one?")
        await store.patch("theme", {"preset": key, **themes.palette(key)})
        return True, f"Switched to the {themes.THEMES[key]['name']} theme."
    name = which_setting(setting) or which_setting(f"{setting} {value}")
    if name is None:
        return False, (f"There's no setting called “{setting}”. Settings that can be changed by "
                       "asking: " + ", ".join(SETTINGS) + ", and the theme.")
    key, field, kind, _details, _others = SETTINGS[name]
    current = (await store.get(key, {}) or {}).get(field)
    try:
        chosen = new_value(name, value or setting, current)
    except ValueError as exc:
        return False, str(exc)
    await store.patch(key, {field: chosen})
    shown = {True: "on", False: "off"}.get(chosen, chosen) if kind == "bool" else chosen
    if key == "voice" and field == "volume":
        shown = f"{int(chosen * 100)}%"
    same = current is not None and current == chosen
    return True, f"{name[:1].upper() + name[1:]} is now {shown}." + (" (It already was.)" if same else "")


# Settings said plainly enough to do without asking the model what was meant.
_PLAIN = [
    (re.compile(r"^(?:please\s+)?(?:turn|switch)\s+(on|off)\s+(?:the\s+|your\s+|my\s+)?(.+)$"), 2, 1),
    (re.compile(r"^(?:please\s+)?(?:turn|switch)\s+(?:the\s+|your\s+|my\s+)?(.+?)\s+(on|off)$"), 1, 2),
    (re.compile(r"^(?:please\s+)?(enable|disable|mute|unmute)\s+(?:the\s+|your\s+|my\s+)?(.+)$"), 2, 1),
    (re.compile(r"^(?:please\s+)?(?:make\s+)?(?:your\s+)?(answers|replies)\s+(shorter|longer|more detailed|briefer)$"), 1, 2),
    (re.compile(r"^(?:please\s+)?(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?(faster|slower)$"), None, 1),
    (re.compile(r"^(?:please\s+)?(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?(louder|quieter)$"), None, 1),
    (re.compile(r"^(?:please\s+)?(?:switch|change|go|set)\s+(?:it\s+|the\s+theme\s+)?to\s+(?:the\s+)?(.+?)\s+theme$"), 1, None),
    (re.compile(r"^(?:please\s+)?use\s+the\s+(.+?)\s+theme$"), 1, None),
    (re.compile(r"^(?:please\s+)?(?:set|change)\s+(?:the\s+|your\s+)?(.+?)\s+to\s+(.+)$"), 1, 2),
]


def plain_setting(message: str):
    """(setting, value) for a plainly worded settings request, else None."""
    said = " ".join((message or "").lower().strip(" .!?").replace("’", "'").split())
    said = re.sub(r"^(?:jarvis,?\s+)?(?:can you\s+|could you\s+)?", "", said)
    said = re.sub(r",?\s+(?:please|for me|now|jarvis)$", "", said)
    if re.fullmatch(r"(?:please\s+)?(?:stop|don't|do not)\s+chang(?:e|ing)\s+(?:the\s+|your\s+)?colou?rs?", said):
        return "skill colours", "off"
    if re.fullmatch(r"(?:please\s+)?(?:start\s+)?chang(?:e|ing)\s+(?:the\s+|your\s+)?colou?rs?\s+again", said):
        return "skill colours", "on"
    for pattern, setting_at, value_at in _PLAIN:
        m = pattern.match(said)
        if not m:
            continue
        if value_at is None:                                   # a theme
            return f"{m.group(setting_at)} theme", m.group(setting_at)
        value = m.group(value_at)
        if setting_at is None:                                  # speak faster / louder
            return ("volume" if value in ("louder", "quieter") else "speaking speed"), value
        setting = m.group(setting_at)
        if setting in ("answers", "replies"):
            setting = "answer length"
        if value in ("mute",):
            value = "off"
        if value in ("unmute",):
            value = "on"
        if which_setting(setting, exact=True) is None and "theme" not in setting:
            return None                                        # "turn off the lights": not ours
        return setting, value
    return None


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "change_setting",
        "Change one of JARVIS's settings by saying so: " + ", ".join(SETTINGS) + ", or the theme "
        "(e.g. 'switch to the Ember theme'). Use for 'turn on the wake word', 'make your answers "
        "shorter', 'speak slower', 'turn off suggestions', 'more particles'.",
        {"type": "object", "properties": {
            "setting": {"type": "string", "description": "Which setting, in the owner's words"},
            "value": {"type": "string", "description": "The new value in words: on, off, slower, "
                                                       "warm, 1.2, the theme's name..."},
        }, "required": ["setting", "value"]},
        category="setting settings turn on off enable disable theme voice wake word change make",
    )
    async def change_setting(ctx, setting: str, value: str = ""):
        store = ctx.get("settings_store")
        if store is None:
            return fail("Settings are not available.")
        done, said = await apply(store, setting, value)
        return ok({"setting": setting}, said) if done else fail(said)

    # ------------------------------------------------------------------------
    @registry.add(
        "start_away_work",
        "Start working unattended while the owner is out: 'while I'm away, build me a habit "
        "tracker', 'I'm going out for 2 hours, make me a budgeting app'. Risky steps are held for "
        "the owner, never done alone. Not for things to do right now.",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "What to work on, in the owner's words"},
            "minutes": {"type": "integer", "description": "How long the owner is away, if they said"},
        }, "required": ["task"]},
        category="away while unattended build out leave going work",
    )
    async def start_away_work(ctx, task: str, minutes: int = 0):
        from ...autonomy.session import MAX_WORK_MINUTES
        from .countdown_tools import _said

        service = ctx.get("autonomy")
        if service is None:
            return fail("While-away work is not available.")
        said, _ = await _said(ctx)
        away = _minutes_said(said or "") or (int(minutes) if minutes else 0) or 60
        away = max(15, min(away, 24 * 60))
        work = max(10, min(int(away * 0.75), MAX_WORK_MINUTES))
        try:
            run = await service.start(task, away, work, False)
        except (ValueError, RuntimeError) as exc:
            return fail(str(exc))
        return ok({"started": True, "away_minutes": away, "work_minutes": work},
                  f"Started working on “{task}” while you're away: about {work} minutes of work "
                  f"within the {away} minutes you're out. Anything risky is held for you. Say "
                  "“I'm back” for the report, or “stop the away work” to stop it.")

    @registry.add(
        "stop_away_work",
        "Stop the work JARVIS is doing while the owner is away: wrap up now ('stop the away work'), "
        "or abandon it immediately ('cancel the away work').",
        {"type": "object", "properties": {
            "how": {"type": "string", "enum": ["wrap up", "cancel"]},
        }},
        category="away stop cancel unattended work",
    )
    async def stop_away_work(ctx, how: str = "wrap up"):
        from .countdown_tools import _said

        service = ctx.get("autonomy")
        if service is None or not getattr(service, "busy", False):
            return fail("Nothing is being worked on while you're away.")
        said, _ = await _said(ctx)
        # The owner's own word decides it: "cancel" means now, not "wrap up".
        if re.search(r"\b(cancel|abort|abandon|kill|scrap|immediately|right now)\b", said or "", re.I):
            how = "cancel"
        elif re.search(r"\b(wrap|finish|i'?m back)\b", said or "", re.I):
            how = "wrap up"
        if how == "cancel":
            await service.cancel()
            return ok(None, "Stopped the away work immediately, without wrapping up.")
        await service.stop()
        return ok(None, "Asked the away work to finish its current step and wrap up.")


    @registry.add(
        "stop_task",
        "Stop something JARVIS is running in the background -- research, a build, any task: "
        "'stop the research', 'cancel that task'. Not for the while-away work (stop_away_work).",
        {"type": "object", "properties": {
            "which": {"type": "string", "description": "Which one, in the owner's words "
                                                       "('the research', 'the switch 2 one')"},
        }},
        category="stop cancel task research background running job",
    )
    async def stop_task(ctx, which: str = ""):
        engine = ctx.get("tasks")
        if engine is None:
            return fail("Background tasks are not available.")
        running = await engine.list(active_only=True, limit=20)
        if not running:
            return fail("Nothing is running in the background right now.")
        chosen = pick_task(running, which)
        if chosen is None:
            return fail("More than one thing is running: "
                        + "; ".join(f"“{task_label(t)}”" for t in running)
                        + ". Say which to stop.")
        try:
            await engine.cancel(chosen["id"])
        except (KeyError, PermissionError) as exc:
            return fail(f"Couldn't stop “{task_label(chosen)}”: {exc}")
        return ok({"task_id": chosen["id"]}, f"Stopped “{task_label(chosen)}”.")


def task_label(task: Dict[str, Any]) -> str:
    params = task.get("params") or {}
    if isinstance(params, str):
        params = {}
    topic = params.get("question") or params.get("topic") or ""
    kind = str(task.get("type") or task.get("name") or "task").replace("_", " ")
    return f"{kind}: {topic}" if topic else str(task.get("name") or kind)


def pick_task(running, which: str) -> Optional[Dict[str, Any]]:
    """The running task the owner means; with only one running, that one."""
    words = {w for w in re.findall(r"[a-z0-9]+", (which or "").lower())
             if w not in {"the", "that", "this", "task", "stop", "cancel", "one", "it", "a", "my"}}
    if words:
        scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", task_label(t).lower()))), t)
                         for t in running), key=lambda pair: -pair[0])
        if scored[0][0] and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
    return running[0] if len(running) == 1 else None


def _minutes_said(text: str) -> Optional[int]:
    """"for 2 hours", "for half an hour", "for 45 minutes" -> minutes."""
    said = (text or "").lower()
    m = re.search(r"\b(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|eight|ten|twelve|half an)"
                  r"\s*(hour|hr|minute|min)s?\b", said)
    if not m:
        return None
    words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "eight": 8, "ten": 10, "twelve": 12, "half an": 0.5}
    n = float(m.group(1)) if m.group(1)[0].isdigit() else words[m.group(1)]
    return int(n * (60 if m.group(2) in ("hour", "hr") else 1))
