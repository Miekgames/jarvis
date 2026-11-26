"""Reminders from plain words, research that stays on the question, opening any panel.

The reminder failures that matter are the wrong time and the wrong day of the
week. The research failures that matter are drifting off the question, trusting
lookalike sites, and stopping early. Each has tests here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.ai.tools.ui_tools import PANELS, which_panel
from backend.intelligence import long_research as lr
from backend.scheduler import phrases
from backend.scheduler.cron import CronExpression

THU = datetime(2026, 11, 26, 18, 30)          # a Thursday evening
MORNING = datetime(2026, 11, 26, 6, 30)


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """Research waits between searches; in tests the waits are recorded, not slept."""
    waits = []

    async def sleep(self, seconds):
        waits.append(seconds)

    monkeypatch.setattr(lr.LongResearch, "_sleep", sleep)
    return waits


# ===========================================================================
# when a reminder goes off
# ===========================================================================
@pytest.mark.parametrize("said, at", [
    ("remind me at 7pm to call dad", datetime(2026, 11, 26, 19, 0)),
    ("at 7", datetime(2026, 11, 26, 19, 0)),                 # evening: 7pm, not tomorrow 7am
    ("at 11", datetime(2026, 11, 26, 23, 0)),
    ("in 20 minutes", datetime(2026, 11, 26, 18, 50)),
    ("in half an hour", datetime(2026, 11, 26, 19, 0)),
    ("in 2 hours to stretch", datetime(2026, 11, 26, 20, 30)),
    ("tomorrow morning", datetime(2026, 11, 27, 9, 0)),
    ("tomorrow at 9:15am", datetime(2026, 11, 27, 9, 15)),
    ("on friday at 6pm", datetime(2026, 11, 27, 18, 0)),
    ("on June 5 at 3pm", datetime(2027, 6, 5, 15, 0)),
    ("tonight", datetime(2026, 11, 26, 20, 0)),
])
def test_once(said, at):
    found = phrases.parse(said, THU)
    assert found.kind == "once" and found.run_at == at


def test_a_bare_hour_in_the_morning_is_this_morning():
    assert phrases.parse("at 7", MORNING).run_at == datetime(2026, 11, 26, 7, 0)


@pytest.mark.parametrize("said, cron", [
    ("every monday at 9am", "0 9 * * 1"),
    ("every weekday at 7:30am", "30 7 * * 1-5"),
    ("every weekend at 10", "0 10 * * 0,6"),
    ("every day at 8pm", "0 20 * * *"),
    ("every morning", "0 9 * * *"),
    ("every sunday at noon", "0 12 * * 0"),
    ("every monday and thursday at 6pm", "0 18 * * 1,4"),
    ("every month on the 1st at 9", "0 9 1 * *"),
    ("every year on june 5 at 8am", "0 8 5 6 *"),
])
def test_repeating(said, cron):
    found = phrases.parse(said, THU)
    assert found.kind == "cron" and found.cron == cron
    CronExpression(found.cron)                 # and the scheduler accepts it


def test_the_cron_days_are_the_days_meant():
    """The classic mistake: Python counts Monday as 0, cron counts Sunday as 0."""
    expression = CronExpression(phrases.parse("every weekday at 7:30am", THU).cron)
    after = datetime(2026, 11, 28, 12, 0)       # Saturday noon
    assert expression.next_after(after) == datetime(2026, 11, 30, 7, 30)   # Monday, not Sunday


@pytest.mark.parametrize("said, seconds", [
    ("every 2 hours", 7200), ("every 30 minutes", 1800), ("hourly", 3600), ("every 2 days at 9am", 172800),
])
def test_intervals(said, seconds):
    found = phrases.parse(said, THU)
    assert found.kind == "interval" and found.interval_seconds == seconds


@pytest.mark.parametrize("said", ["remind me to buy milk", "today", "in 10 seconds", "every 10 seconds",
                                  "every year", "", "sometime"])
def test_no_time_means_asking(said):
    assert phrases.parse(said, THU) is None


def test_a_missing_time_is_filled_in_and_said():
    found = phrases.parse("next monday", THU)
    assert found.run_at == datetime(2026, 11, 30, 9, 0) and "09:00" in found.assumed


# ===========================================================================
# reminders, end to end against the real scheduler
# ===========================================================================
@pytest.mark.asyncio
async def test_a_reminder_is_scheduled_listed_and_cancelled(scheduler):
    from backend.services.reminders import ReminderError, Reminders

    reminders = Reminders(scheduler)
    item = await reminders.add("take my tablets", "every weekday at 7:30am")
    assert item["repeats"] and item["says"] == "every weekday at 07:30"
    once = await reminders.add("call Dad", "in 20 minutes")
    listed = await reminders.list()
    assert [i["message"] for i in listed] == ["call Dad", "take my tablets"], "soonest first"
    assert (await reminders.find("tablets"))["id"] == item["id"]
    assert await reminders.cancel(once["id"])
    assert [i["message"] for i in await reminders.list()] == ["take my tablets"]
    with pytest.raises(ReminderError):
        await reminders.add("buy milk", "sometime")
    with pytest.raises(ReminderError):
        await reminders.add("", "at 7pm")


@pytest.mark.asyncio
async def test_a_due_reminder_notifies_and_reaches_windows(monkeypatch, db, notifications):
    from backend.automations.actions import ActionRunner
    from backend.integrations import windows_toast

    shown = []

    async def fake_show(title, body):
        shown.append((title, body))
        return True

    monkeypatch.setattr(windows_toast, "show", fake_show)
    runner = ActionRunner({"notifications": notifications, "db": db})
    result = await runner.run({"kind": "notify", "title": "Reminder", "body": "call Dad",
                               "priority": "high", "desktop": True, "source": "reminder"}, {})
    assert shown == [("JARVIS · Reminder", "call Dad")]
    stored = await notifications.list(limit=5)
    assert stored[0]["body"] == "call Dad" and stored[0]["priority"] == "high"
    assert result.get("windows_notification", (result.get("result") or {}).get("windows_notification"))


def test_the_windows_notification_script_cannot_be_broken_out_of():
    from backend.integrations.windows_toast import script_for

    script = script_for("x' ; Remove-Item C:\\ -Recurse #", "<toast>\"evil\"</toast>")
    assert "Remove-Item" not in script and "evil" not in script, "text only ever travels as base64"


# ===========================================================================
# research stays on the question
# ===========================================================================
Q = lr.normalised("switch two emulator for android")


def test_the_question_is_read_one_way():
    assert Q == "switch 2 emulator for android"
    assert lr.wordings("switch two emulator for android")[0] == "switch 2 emulator android"


@pytest.mark.parametrize("text, strict, loose", [
    ("Switch 2 Android emulator", True, True),
    ("Is there a Switch 2 emulator for Android? Eden adds support", True, True),
    ("Nintendo Switch 2 release date", False, True),
    ("Nintendo Switch emulator Android Yuzu", False, False),      # the 2 is the point
    ("SmartGaGa vs NoxPlayer 7 user reviews", False, False),
    ("best android emulator for PC", False, False),
])
def test_relevance(text, strict, loose):
    assert lr.relevant(Q, text) is strict
    assert lr.relevant(Q, text, strict=False) is loose


@pytest.mark.parametrize("url, flagged", [
    ("https://eglantineofficial.com/", True), ("https://lichiiemulator.app/vs", True),
    ("https://switch2rom.com/x", True), ("https://switch2e.com/android/", True),
    ("https://www.androidauthority.com/banana", False), ("https://github.com/org/emu", False),
    ("https://www.nintendo.com/switch2", False), ("https://en.wikipedia.org/wiki/Switch_2", False),
])
def test_lookalike_sites_are_flagged(url, flagged):
    assert lr.lookalike(url, Q) is flagged


class _Web:
    def __init__(self):
        self.searches = []

    async def search(self, query, n=10):
        self.searches.append(query)
        n = len(self.searches)
        return [
            {"url": f"https://news{n}.example.com/a", "title": f"Switch 2 emulator for Android update {n}",
             "snippet": "A Switch 2 emulator runs on Android phones."},
            {"url": f"https://pc{n}.example.com/b", "title": "Best Android emulators for PC",
             "snippet": "NoxPlayer and BlueStacks reviewed."},
        ]

    async def fetch(self, url, max_chars=9000):
        if "pc" in url:
            return {"ok": True, "title": "PC emulators", "text": "NoxPlayer review. " * 60}
        return {"ok": True, "title": "Switch 2 on Android",
                "text": "The Switch 2 emulator Banana runs Mario Kart World on Android phones. " * 20}


class _Model:
    def __init__(self):
        self.prompts = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, temperature=None):
        self.prompts.append(prompt)
        if "Write EXACTLY 4 paragraphs" in prompt:
            return ("Yes, one exists (news1.example.com).\n\nDetails (news2.example.com).\n\n"
                    "Caveats.\n\nWhat's next: watch for updates.")
        if "searches" in prompt.lower() and "notes" in prompt.lower():
            return "SmartGaGa vs NoxPlayer reviews\nBanana Switch 2 emulator android compatibility"
        if "web searches" in prompt.lower() or "plan" in prompt.lower():
            return "switch 2 emulator android\nswitch 2 emulator android reddit"
        return "- Banana runs Mario Kart World on Android (switch 2 emulator)"


class _Clock:
    """Each look at the clock moves it on 20 seconds (or `step`)."""

    def __init__(self, step=20.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


@pytest.mark.asyncio
async def test_research_keeps_going_stays_on_topic_and_reports_in_four_parts():
    web, model, logged = _Web(), _Model(), []

    async def log(line):
        logged.append(line)

    result = await lr.LongResearch(model, web, log=log).run(
        "switch two emulator for android", minutes=15, clock=_Clock())
    assert result["minutes"] >= 15, "it does not stop at the first empty queue"
    assert result["stopped_early"] is None
    assert all("smartgaga" not in q.lower() for q in web.searches), "off-topic searches are dropped"
    assert not any("pc" in s["url"] for s in result["sources"]), "pages about other things are not used"
    assert len(lr.paragraphs(result["report"])) == 4
    assert lr.paragraphs(result["report"])[-1].startswith("What's next")
    assert any(line.startswith("Searched") for line in logged)
    writer = next(p for p in model.prompts if "Write EXACTLY 4 paragraphs" in p)
    assert "switch 2 emulator for android" in writer, "the writer sees the number as a number"


@pytest.mark.asyncio
async def test_with_nothing_found_the_report_says_so():
    class Empty(_Web):
        async def search(self, query, n=10):
            self.searches.append(query)
            return []

    result = await lr.LongResearch(_Model(), Empty()).run("switch two emulator for android",
                                                          minutes=15, clock=_Clock())
    assert "couldn't find anything reliable" in result["report"]
    assert result["sources"] == []


@pytest.mark.asyncio
async def test_when_nothing_new_turns_up_it_stops_and_says_so():
    class Same(_Web):
        async def search(self, query, n=10):           # the same one page, every time
            self.searches.append(query)
            return [{"url": "https://news1.example.com/a", "title": "Switch 2 emulator for Android",
                     "snippet": "A Switch 2 emulator runs on Android phones."}]

    research = lr.LongResearch(_Model(), Same())
    result = await research.run("switch two emulator for android", minutes=15, clock=_Clock(2.0))
    assert result["stopped_early"] is not None and result["minutes"] < 15
    assert "nothing new was turning up" in lr.as_message(result)


def test_a_report_message_lists_its_sources():
    message = lr.as_message({"report": "A\n\nB\n\nC", "minutes": 16.2, "searches": 30, "pages_read": 40,
                             "sources": [{"title": "T", "url": "https://x.example/y", "site": "x.example"}]})
    assert "16.2 minutes" in message and "[T](https://x.example/y)" in message


# ===========================================================================
# every panel by asking
# ===========================================================================
@pytest.mark.parametrize("said, key", [
    ("settings", "settings"), ("my reminders", "reminders"), ("the email panel", "email"),
    ("while i'm away", "away"), ("plug ins", "plugins"), ("conversation history", "history"),
    ("countdowns", "countdowns"), ("what you heard", "voice"), ("nonsense", None),
])
def test_panels_by_name(said, key):
    assert which_panel(said) == key


def test_every_sidebar_entry_can_be_opened_by_asking():
    import re
    from pathlib import Path

    sidebar = (Path(__file__).resolve().parent.parent / "frontend/src/components/Sidebar.tsx").read_text(
        encoding="utf-8")
    keys = set(re.findall(r"\{ key: '([a-z]+)', label:", sidebar))
    assert keys and keys <= set(PANELS), keys - set(PANELS)


@pytest.mark.asyncio
async def test_opening_says_so_only_when_a_screen_is_there():
    from types import SimpleNamespace

    from backend.ai.tools import ui_tools
    from backend.ai.tools.registry import ToolContext, ToolRegistry
    from backend.core.events import bus

    seen = []
    bus.subscribe("ui.open_panel", lambda event: seen.append(event.data)) if hasattr(bus, "subscribe") else None
    screens = SimpleNamespace(count=0, client_count=lambda: screens.count)
    registry = ToolRegistry(ToolContext(hub=screens))
    ui_tools.register(registry)
    nobody = await registry.dispatch("open_panel", {"panel": "settings"})
    assert not nobody.ok and "No JARVIS screen" in nobody.message
    screens.count = 1
    opened = await registry.dispatch("open_panel", {"panel": "my reminders"})
    assert opened.ok and opened.data == {"panel": "reminders"}
    assert not (await registry.dispatch("open_panel", {"panel": "the moon"})).ok


@pytest.mark.parametrize("query, tool", [
    ("open settings", "open_panel"), ("show me my reminders", "open_panel"),
    ("remind me at 7pm to call dad", "schedule_notification"),
    ("every weekday at 7:30 remind me to take my tablets", "schedule_notification"),
    ("research whether a switch 2 emulator exists for android", "deep_research"),
    ("can you look into budget headphones", "deep_research"),
])
def test_a_small_model_is_offered_the_right_tool(query, tool):
    from backend.ai.tools.registry import Tool, ToolContext, ToolRegistry, ok
    from backend.ai.tools import os_tools, reminder_tools, ui_tools

    registry = ToolRegistry(ToolContext())
    for module in (ui_tools, reminder_tools):
        module.register(registry)
    try:
        os_tools.register(registry)
    except Exception:
        pass
    for n in range(60):
        registry.register(Tool(f"other_{n}", "does another thing with settings reminders research",
                               {"type": "object", "properties": {}}, lambda ctx: ok()))
    assert tool in {s["name"] for s in registry.select_schemas(query, limit=8)}


@pytest.mark.asyncio
async def test_research_waits_while_the_owner_is_being_answered(monkeypatch):
    import asyncio

    class Busy:
        owner_busy = True

        def __init__(self):
            self.asked_while_busy = False

        async def ask(self, *args, **kwargs):
            self.asked_while_busy = self.asked_while_busy or self.owner_busy
            return "NONE"

    model = Busy()
    worker = lr.LongResearch(model, None)

    async def free_up():
        await asyncio.sleep(1.2)
        model.owner_busy = False

    await asyncio.gather(worker._ask("q", "s"), free_up())
    assert not model.asked_while_busy, "it waited for the owner's reply to finish"


def test_screen_counting_works_on_the_real_hub():
    """The fake hub in the test above has a method; the real one a property."""
    from backend.ai.tools.ui_tools import _screens
    from backend.core.ws import WebSocketHub

    assert _screens(WebSocketHub()) == 0


@pytest.mark.parametrize("said, panel, reply", [
    ("open settings", "settings", "Opening Settings."),
    ("Jarvis, please show me my reminders", "reminders", "Opening Reminders."),
    ("close that", None, "Closed."),
    ("open spotify", "-", None),                     # an app, not a panel: the model's job
    ("show me what's running", "-", None),
    ("open my email from sam", "-", None),
])
def test_open_and_close_happen_without_the_model(said, panel, reply):
    from types import SimpleNamespace

    from backend.ai.orchestrator import Orchestrator
    from backend.core.events import bus

    seen = []
    stop = bus.subscribe("ui.open_panel", lambda event: seen.append(event.data["panel"]))
    hub = SimpleNamespace(client_count=1)              # a property, like the real one
    fake = SimpleNamespace(registry=SimpleNamespace(context=SimpleNamespace(get=lambda name: hub)))
    try:
        assert Orchestrator._open_or_close(fake, said) == reply
    finally:
        stop()


def test_opening_with_no_screen_says_so_instead_of_pretending():
    from types import SimpleNamespace

    from backend.ai.orchestrator import Orchestrator

    hub = SimpleNamespace(client_count=0)
    fake = SimpleNamespace(registry=SimpleNamespace(context=SimpleNamespace(get=lambda name: hub)))
    said = Orchestrator._open_or_close(fake, "open settings")
    assert said.startswith("There's no JARVIS screen open") and "Settings" in said
    assert Orchestrator._open_or_close(fake, "close that") is None


# ===========================================================================
# settings, by saying so
# ===========================================================================
from backend.ai.tools import control_tools as ct  # noqa: E402


@pytest.mark.parametrize("said, found", [
    ("turn on the wake word", ("wake word", "on")),
    ("Jarvis, can you turn the wake word off please?", ("wake word", "off")),
    ("make your answers shorter", ("answer length", "shorter")),
    ("switch to the ember theme", ("ember theme", "ember")),
    ("speak a bit slower", ("speaking speed", "slower")),
    ("talk louder", ("volume", "louder")),
    ("disable suggestions", ("suggestions", "disable")),
    ("set the volume to 60", ("volume", "60")),
    ("change the tone to warm", ("tone", "warm")),
    # not settings: these go to the model as usual
    ("turn off the lights", None),
    ("turn on spotify", None),
    ("turn off the voice in the video", None),
    ("set the alarm to 7", None),
    ("switch to firefox", None),
])
def test_plain_settings_requests_are_recognised(said, found):
    assert ct.plain_setting(said) == found


@pytest.mark.parametrize("words, name", [
    ("wake_word", "wake word"), ("speaking-speed", "speaking speed"), ("the volume", "volume"),
    ("answers", "answer length"), ("kettle", None),
])
def test_setting_names_as_a_model_writes_them(words, name):
    assert ct.which_setting(words) == name


@pytest.mark.parametrize("name, value, current, result", [
    ("wake word", "on", False, True),
    ("suggestions", "disable", True, False),
    ("speaking speed", "slower", 1.0, 0.8),
    ("speaking speed", "faster", 2.0, 2.0),            # never past the top
    ("volume", "60", 1.0, 0.6),                        # "60" means 60%
    ("particles", "more", 4200, 6300),
    ("answer length", "longer", "balanced", "detailed"),
    ("tone", "friendly", "composed", "warm"),
])
def test_new_values_from_words(name, value, current, result):
    assert ct.new_value(name, value, current) == result


def test_a_setting_without_a_value_is_asked_about_not_guessed():
    with pytest.raises(ValueError):
        ct.new_value("wake word", "maybe", False)


@pytest.mark.parametrize("said, minutes", [
    ("while I'm away for half an hour build me a tip calculator", 30),
    ("I'm going out for 2 hours", 120), ("back in 45 minutes", 45), ("make me an app", None),
])
def test_how_long_away(said, minutes):
    assert ct._minutes_said(said) == minutes


async def test_saying_a_setting_changes_it(registry, settings_store):
    from types import SimpleNamespace

    from backend.ai.orchestrator import Orchestrator

    ct.register(registry)
    registry.context._services["settings_store"] = settings_store
    fake = SimpleNamespace(registry=registry)
    await settings_store.patch("voice", {"wake_word_enabled": False, "rate": 1.0})
    await settings_store.patch("personality", {"response_length": "balanced"})

    said = await Orchestrator._plain_setting(fake, "turn on the wake word")
    assert said == "Wake word is now on."
    assert (await settings_store.get("voice"))["wake_word_enabled"] is True

    assert await Orchestrator._plain_setting(fake, "speak a bit slower") == "Speaking speed is now 0.8."
    assert (await settings_store.get("voice"))["rate"] == 0.8

    assert await Orchestrator._plain_setting(fake, "make your answers shorter") == "Answer length is now concise."
    assert (await settings_store.get("personality"))["response_length"] == "concise"

    assert await Orchestrator._plain_setting(fake, "switch to the ember theme") == "Switched to the Ember theme."
    assert (await settings_store.get("theme"))["preset"] == "ember"

    assert (await Orchestrator._plain_setting(fake, "switch to the paisley theme")).startswith("The themes are")
    assert (await settings_store.get("theme"))["preset"] == "ember"     # unchanged
    assert await Orchestrator._plain_setting(fake, "what's the weather") is None


@pytest.mark.parametrize("said, topic", [
    ("research switch 2 emulators for android", "switch 2 emulators for android"),
    ("Jarvis, can you look into Switch 2 emulators please?", "Switch 2 emulators"),
    ("do some research on solar panels for my roof", "solar panels for my roof"),
    ("find out everything you can about the Avengers Doomsday cast", "the Avengers Doomsday cast"),
    ("quick research on tide times", None),            # quick: the model's quick lookup
    ("dig into it", None),                             # needs the conversation
    ("look into my email from sam", None),             # not web research
    ("what's the research on coffee", None),
])
def test_research_requests_start_the_long_research(said, topic):
    from backend.ai.orchestrator import research_topic_in

    assert research_topic_in(said) == topic


async def test_saying_research_starts_it_without_the_model(registry):
    from types import SimpleNamespace

    from backend.ai.orchestrator import Orchestrator
    from backend.ai.tools.registry import ok

    started = []

    @registry.add("research_topic", "test", {"type": "object", "properties": {}})
    async def research_topic(ctx, topic: str, minutes: int = 17):
        started.append(topic)
        return ok(None, f"Researching “{topic}” now.")

    fake = SimpleNamespace(registry=registry)
    said = await Orchestrator._plain_research(fake, "research switch 2 emulators")
    assert said.startswith("Researching “switch 2 emulators” now. It takes about 17 minutes")
    assert "Tell them" not in said, "words meant for the model never reach the owner"
    assert started == ["switch 2 emulators"]
    assert await Orchestrator._plain_research(fake, "how are you") is None


@pytest.mark.parametrize("said, how", [
    ("cancel the away work", "cancel"), ("stop the away work right now", "cancel"),
    ("wrap up the away work", "wrap up"), ("stop the away work", "wrap up"),
])
async def test_stopping_away_work_follows_the_owners_word(registry, said, how):
    from types import SimpleNamespace

    from backend.ai.tools import countdown_tools

    calls = []

    class Away:
        busy = True

        async def cancel(self):
            calls.append("cancel")

        async def stop(self):
            calls.append("wrap up")

    async def fake_said(ctx):
        return said, said

    ct.register(registry)
    registry.context._services["autonomy"] = Away()
    original, countdown_tools._said = countdown_tools._said, fake_said
    try:
        result = await registry.dispatch("stop_away_work", {"how": "wrap up"}, skip_confirmation=True)
    finally:
        countdown_tools._said = original
    assert result.ok and calls == [how]


@pytest.mark.parametrize("said, which", [
    ("stop the research", "research"), ("Jarvis, cancel the research please", "research"),
    ("stop researching", "research"), ("cancel that task", "task"),
    ("stop the switch 2 research", "switch 2 research"),
    ("stop the away work", None), ("stop the music", None), ("cancel my reminder", None),
])
def test_stopping_by_saying_so(said, which):
    from backend.ai.orchestrator import stop_said

    assert stop_said(said) == which


def test_the_task_the_owner_means():
    one = {"id": "a", "type": "long_research", "params": {"question": "switch 2 emulators"}}
    two = {"id": "b", "type": "build", "name": "Build the tip calculator", "params": {}}
    assert ct.pick_task([one], "research") is one
    assert ct.pick_task([one, two], "the switch 2 research") is one
    assert ct.pick_task([one, two], "the tip calculator") is two
    assert ct.pick_task([one, two], "task") is None          # ambiguous: ask which


@pytest.mark.asyncio
async def test_an_empty_results_page_is_tried_again(_no_real_waiting):
    class Flaky(_Web):
        attempts = 0

        async def search(self, query, n=10):
            self.attempts += 1
            if self.attempts == 1:                      # the first attempt: nothing
                return []
            return await super().search(query, n)

    web = Flaky()
    research = lr.LongResearch(_Model(), web)
    found = lr.Findings(lr.normalised("switch two emulator for android"))
    hits = await research._search(found, "switch 2 emulator android")
    assert hits and web.attempts == 2 and found.searches == 1
    assert _no_real_waiting == [lr.RETRY_WAITS_S[0]], "waited before trying again"


@pytest.mark.asyncio
async def test_planned_searches_lose_their_labels():
    class Labels(_Model):
        async def ask(self, prompt, system="", purpose="fast", max_tokens=None, temperature=None):
            return ("Search for: Hokkaido switch 2 emulator android performance\n"
                    "Search 2: Banana switch 2 emulator android games")

    planned = await lr.LongResearch(Labels(), _Web()).plan(
        lr.normalised("switch two emulator for android"), [])
    assert planned == ["Hokkaido switch 2 emulator android performance",
                       "Banana switch 2 emulator android games"]
