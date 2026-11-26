"""Intelligence Layer: pattern learning, proactive gating, planning arithmetic,
goal decomposition, reasoning heuristics and the device mesh."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from backend.core.util import ahead, iso, now_iso, since, utcnow
from backend.intelligence.awareness import categorise
from backend.intelligence.goals import _parse_steps
from backend.intelligence.patterns import PatternLearner, normalise
from backend.intelligence.planning import PlanningService
from backend.intelligence.proactive import ProactiveEngine
from backend.intelligence.reasoning import ReasoningService
from backend.services.devices import DeviceService


# ===========================================================================
# fixtures
# ===========================================================================
@pytest.fixture
async def patterns(db, settings_store, activity):
    return PatternLearner(db, settings_store, activity)


@pytest.fixture
async def reasoning(db, settings_store, activity, patterns):
    return ReasoningService(db, settings_store, activity, awareness=None, patterns=patterns)


@pytest.fixture
async def devices(db, settings, activity, audit):
    service = DeviceService(db, settings, activity, audit)
    await service.bootstrap()
    return service


class _Orchestrator:
    """Stand-in that returns whatever it is told to, so planning tests assert
    on the arithmetic rather than on model output."""

    def __init__(self, reply: str = "narrated"):
        self.reply = reply
        self.prompts = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, use_tools=False):
        self.prompts.append(prompt)
        return self.reply


@pytest.fixture
async def planning(db, settings_store, calendar_service, engine):
    return PlanningService(db, settings_store, calendar_service, engine, _Orchestrator())


@pytest.fixture
async def calendar_service(db, activity, notifications):
    from backend.services.calendar import CalendarService

    return CalendarService(db, activity, notifications)


# ===========================================================================
# normalisation & categorisation
# ===========================================================================
def test_normalise_collapses_phrasing():
    assert normalise("Open Spotify please") == normalise("open spotify")
    assert normalise("JARVIS, could you open Spotify") == normalise("open spotify")


def test_normalise_generalises_numbers():
    assert normalise("remind me in 20 minutes") == normalise("remind me in 45 minutes")


def test_normalise_keeps_distinct_commands_distinct():
    assert normalise("open spotify") != normalise("close spotify")


def test_categorise_known_apps():
    assert categorise("Code.exe") == "development"
    assert categorise("chrome.exe") == "browser"
    assert categorise("discord.exe") == "communication"
    assert categorise("steam.exe") == "gaming"
    assert categorise("something_unheard_of.exe") == "other"


# ===========================================================================
# pattern learning
# ===========================================================================
@pytest.mark.asyncio
async def test_repeated_commands_increment_one_pattern(patterns, db):
    for _ in range(3):
        await patterns.record("open spotify")
    rows = await db.fetch_all("SELECT * FROM command_patterns")
    assert len(rows) == 1
    assert rows[0]["count"] == 3


@pytest.mark.asyncio
async def test_sequences_are_learned(patterns, db):
    for _ in range(2):
        await patterns.record("open spotify")
        await patterns.record("set volume to 40")
    rows = await db.fetch_all("SELECT * FROM command_sequences")
    assert rows and rows[0]["count"] == 2

    predictions = await patterns.likely_next("open spotify")
    assert predictions
    assert "volume" in predictions[0]["command"]
    assert predictions[0]["reason"]


@pytest.mark.asyncio
async def test_no_prediction_from_a_single_observation(patterns):
    await patterns.record("open spotify")
    await patterns.record("do something else")
    assert await patterns.likely_next("open spotify") == []


@pytest.mark.asyncio
async def test_learning_respects_the_privacy_switch(patterns, settings_store, db):
    await settings_store.patch("privacy", {"activity_tracking": False})
    assert await patterns.record("open spotify") is None
    assert await db.fetch_value("SELECT COUNT(*) FROM command_patterns", default=0) == 0


@pytest.mark.asyncio
async def test_vocabulary_round_trip(patterns):
    await patterns.learn_phrase("the site", "his portfolio at example.dev")
    _, matched = await patterns.expand("deploy the site now")
    assert matched and "portfolio" in matched[0]


@pytest.mark.asyncio
async def test_forgetting_a_pattern_removes_its_sequences(patterns, db):
    await patterns.record("first thing")
    await patterns.record("second thing")
    row = await db.fetch_one("SELECT id FROM command_patterns LIMIT 1")

    await patterns.forget(row["id"])
    remaining = await db.fetch_all(
        "SELECT * FROM command_sequences WHERE first_id = ? OR second_id = ?",
        (row["id"], row["id"]),
    )
    assert remaining == []


# ===========================================================================
# reasoning
# ===========================================================================
@pytest.mark.asyncio
async def test_confusion_is_detected(reasoning):
    result = await reasoning.read_the_room(None, "no, that's not what I meant")
    assert result["confused"]
    assert result["guidance"]
    assert "same explanation" in result["guidance"]


@pytest.mark.asyncio
async def test_ordinary_message_is_not_flagged_as_confusion(reasoning):
    result = await reasoning.read_the_room(None, "open spotify")
    assert not result["confused"]


@pytest.mark.asyncio
async def test_vague_command_is_flagged_as_ambiguous(reasoning):
    result = await reasoning.ambiguity("delete that")
    assert result["ambiguous"]
    assert result["destructive"]
    assert "one clarifying question" in result["guidance"]


@pytest.mark.asyncio
async def test_specific_command_is_not_flagged(reasoning):
    result = await reasoning.ambiguity("open the file report.pdf in my documents folder")
    assert not result["ambiguous"]


@pytest.mark.asyncio
async def test_decisions_are_logged_and_retrievable(reasoning):
    await reasoning.log_decision(
        subject="model routing",
        summary="Used the reasoning model for this request.",
        rationale="The request asked for a comparison across several sources.",
        inputs={"purpose": "reasoning"},
        alternatives=["fast model", "primary model"],
    )
    decisions = await reasoning.decisions()
    assert len(decisions) == 1
    assert decisions[0]["alternatives"] == ["fast model", "primary model"]
    assert decisions[0]["inputs"]["purpose"] == "reasoning"


@pytest.mark.asyncio
async def test_explain_recent_is_honest_when_there_is_nothing(reasoning):
    result = await reasoning.explain_recent()
    assert result["explained"] is False


# ===========================================================================
# planning arithmetic -- the part that must never be hallucinated
# ===========================================================================
@pytest.mark.asyncio
async def test_conflicts_detects_overlap(planning, calendar_service):
    base = utcnow() + timedelta(days=1)
    start = base.replace(hour=10, minute=0, second=0, microsecond=0)
    await calendar_service.create("Standup", iso(start), iso(start + timedelta(minutes=60)))
    await calendar_service.create("Review", iso(start + timedelta(minutes=30)),
                                  iso(start + timedelta(minutes=90)))

    conflicts = await planning.conflicts(days=3)
    assert len(conflicts) == 1
    assert conflicts[0]["overlap_minutes"] == 30
    assert conflicts[0]["severity"] == "high"


@pytest.mark.asyncio
async def test_no_conflict_for_back_to_back_meetings(planning, calendar_service):
    base = utcnow() + timedelta(days=1)
    start = base.replace(hour=10, minute=0, second=0, microsecond=0)
    await calendar_service.create("A", iso(start), iso(start + timedelta(minutes=30)))
    await calendar_service.create("B", iso(start + timedelta(minutes=30)),
                                  iso(start + timedelta(minutes=60)))
    assert await planning.conflicts(days=3) == []


@pytest.mark.asyncio
async def test_free_blocks_exclude_meetings(planning, calendar_service):
    tomorrow = (utcnow().astimezone() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    meeting_start = tomorrow.replace(hour=11)
    await calendar_service.create(
        "Long meeting", iso(meeting_start), iso(meeting_start + timedelta(hours=2)))

    blocks = await planning.free_blocks(day_offset=1)
    assert blocks
    # No block may overlap 11:00-13:00.
    from backend.core.util import parse_iso

    for block in blocks:
        start = parse_iso(block["start"]).astimezone()
        end = parse_iso(block["end"]).astimezone()
        assert not (start < meeting_start + timedelta(hours=2)
                    and end > meeting_start), f"block {block} overlaps the meeting"


@pytest.mark.asyncio
async def test_free_blocks_ignore_slivers(planning, calendar_service):
    blocks = await planning.free_blocks(day_offset=1, minimum_minutes=600)
    assert all(b["minutes"] >= 600 for b in blocks)


@pytest.mark.asyncio
async def test_next_action_prioritises_an_imminent_meeting(planning, calendar_service):
    soon = utcnow() + timedelta(minutes=10)
    await calendar_service.create("Client call", iso(soon))

    result = await planning.next_action()
    assert result["kind"] == "meeting"
    assert result["urgency"] == "now"
    assert "Client call" in result["recommendation"]


@pytest.mark.asyncio
async def test_next_action_is_honest_when_idle(planning):
    result = await planning.next_action()
    assert result["kind"] in ("idle", "focus")
    assert result["why"]


@pytest.mark.asyncio
async def test_daily_plan_facts_are_computed_not_narrated(planning, calendar_service):
    # The planner works in *local* days, because that is what "tomorrow" means
    # to a user. Building the event in UTC makes this test fail whenever the
    # local date and the UTC date disagree — i.e. late evening or early morning.
    tomorrow = (utcnow().astimezone() + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    await calendar_service.create("Design review", iso(tomorrow))

    result = await planning.daily_plan(day_offset=1)
    assert result["facts"]["meeting_count"] == 1
    assert result["facts"]["events"][0]["title"] == "Design review"
    assert result["plan"] == "narrated"


@pytest.mark.asyncio
async def test_narration_failure_falls_back_to_raw_facts(db, settings_store, calendar_service,
                                                         engine):
    class Broken:
        async def ask(self, *args, **kwargs):
            raise RuntimeError("provider down")

    service = PlanningService(db, settings_store, calendar_service, engine, Broken())
    result = await service.daily_plan()
    assert "AI narration unavailable" in result["plan"]
    assert result["facts"]["date"]


# ===========================================================================
# proactive gating
# ===========================================================================
@pytest.mark.asyncio
async def test_proactive_is_off_by_default(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    assert await engine.enabled() is False
    assert await engine._may_speak() is False


@pytest.mark.asyncio
async def test_proactive_stays_silent_while_asleep(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    await settings_store.patch("awareness", {"proactive": True})
    await settings_store.set("sleeping", True)
    assert await engine._may_speak() is False


@pytest.mark.asyncio
async def test_proactive_stays_silent_in_quiet_modes(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    await settings_store.patch("awareness", {"proactive": True})
    await settings_store.set("active_mode", "focus")
    assert await engine._may_speak() is False


@pytest.mark.asyncio
async def test_proactive_respects_the_hourly_limit(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    await settings_store.patch("awareness", {"proactive": True, "max_suggestions_per_hour": 2})

    assert await engine._may_speak() is True
    await engine.offer("test", "One")
    await engine.offer("test", "Two")
    assert await engine._may_speak() is False


@pytest.mark.asyncio
async def test_dismissed_suggestions_are_not_repeated(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    offered = await engine.offer("routine", "Run the backup?")
    await engine.resolve(offered["id"], accepted=False)

    assert await engine._recently_dismissed("routine", "Run the backup?") is True


@pytest.mark.asyncio
async def test_pending_suggestion_is_not_duplicated(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    await engine.offer("routine", "Run the backup?")
    assert await engine._recently_dismissed("routine", "Run the backup?") is True


@pytest.mark.asyncio
async def test_stale_suggestions_expire(db, settings_store, activity, notifications):
    engine = ProactiveEngine(db, settings_store, activity, notifications, {})
    offered = await engine.offer("routine", "Old one")
    await db.execute("UPDATE suggestions SET ts = ? WHERE id = ?", (since(9 * 3600), offered["id"]))

    assert await engine.expire_stale(hours=6) == 1
    assert await engine.pending() == []


# ===========================================================================
# goal decomposition
# ===========================================================================
def test_parse_steps_handles_a_fenced_block():
    raw = """Here is the plan:
```json
[
  {"title": "Search", "detail": "Find sources", "agent": "researcher",
   "task_type": "research", "depends_on": [], "params": {"query": "x"}},
  {"title": "Write", "detail": "Draft it", "agent": "general",
   "task_type": "agent", "depends_on": [0], "params": {}}
]
```
That should do it."""
    steps = _parse_steps(raw)
    assert len(steps) == 2
    assert steps[1]["depends_on"] == [0]
    assert steps[0]["params"]["query"] == "x"


def test_parse_steps_handles_bare_json():
    steps = _parse_steps('[{"title": "Only step", "task_type": "agent"}]')
    assert len(steps) == 1
    assert steps[0]["agent"] == "general"


def test_parse_steps_rejects_garbage():
    assert _parse_steps("I couldn't work out a plan, sorry.") == []
    assert _parse_steps("") == []


def test_parse_steps_drops_entries_without_a_title():
    steps = _parse_steps('[{"detail": "no title"}, {"title": "good"}]')
    assert len(steps) == 1
    assert steps[0]["title"] == "good"


def test_parse_steps_ignores_thinking_scratchpad():
    """Local reasoning models emit <think> blocks whose prose contains brackets."""
    raw = (
        "<think>Okay, so I need to plan this. Maybe [step 1, step 2]? "
        "Let me consider {options} first.</think>\n"
        '[{"title": "Actual step", "task_type": "agent", "depends_on": []}]'
    )
    steps = _parse_steps(raw)
    assert len(steps) == 1
    assert steps[0]["title"] == "Actual step"


def test_parse_steps_handles_an_unterminated_think_block():
    raw = '<think>reasoning with [brackets]\n[{"title": "Recovered", "task_type": "agent"}]'
    steps = _parse_steps(raw)
    assert steps == [] or steps[0]["title"] == "Recovered"


def test_parse_steps_accepts_a_bare_object():
    steps = _parse_steps('{"title": "Only one thing", "task_type": "agent"}')
    assert len(steps) == 1
    assert steps[0]["title"] == "Only one thing"


# ===========================================================================
# devices
# ===========================================================================
@pytest.mark.asyncio
async def test_host_registers_itself_once(devices, db):
    await devices.bootstrap()
    rows = await db.fetch_all("SELECT * FROM devices WHERE is_self = 1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_pairing_round_trip(devices):
    pairing = await devices.begin_pairing("Sam's phone", "mobile")
    assert len(pairing["code"]) == 6

    claimed = await devices.complete_pairing(pairing["code"], "Pixel")
    assert claimed["token"]
    assert claimed["device_id"]

    authenticated = await devices.authenticate(claimed["token"])
    assert authenticated is not None
    assert authenticated["name"] == "Pixel"


@pytest.mark.asyncio
async def test_pairing_code_is_single_use(devices):
    pairing = await devices.begin_pairing()
    await devices.complete_pairing(pairing["code"], "First")
    with pytest.raises(PermissionError):
        await devices.complete_pairing(pairing["code"], "Second")


@pytest.mark.asyncio
async def test_bad_pairing_code_is_rejected(devices):
    with pytest.raises(PermissionError):
        await devices.complete_pairing("000000", "Impostor")


@pytest.mark.asyncio
async def test_device_token_is_hashed_not_stored(devices, db):
    pairing = await devices.begin_pairing()
    claimed = await devices.complete_pairing(pairing["code"], "Phone")
    rows = await db.fetch_all("SELECT token_hash FROM devices WHERE token_hash != ''")
    assert rows and all(r["token_hash"] != claimed["token"] for r in rows)


@pytest.mark.asyncio
async def test_listed_devices_never_expose_the_token_hash(devices):
    pairing = await devices.begin_pairing()
    await devices.complete_pairing(pairing["code"], "Phone")
    for device in await devices.list():
        assert "token_hash" not in device


@pytest.mark.asyncio
async def test_devices_get_least_privilege_by_default(devices):
    pairing = await devices.begin_pairing()
    claimed = await devices.complete_pairing(pairing["code"], "Phone")
    assert "control" not in claimed["permissions"]
    assert "ask" in claimed["permissions"]


@pytest.mark.asyncio
async def test_host_cannot_be_unpaired(devices, db):
    host = await db.fetch_one("SELECT id FROM devices WHERE is_self = 1")
    with pytest.raises(PermissionError):
        await devices.revoke(host["id"])


@pytest.mark.asyncio
async def test_handoff_is_queued_then_claimed_once(devices):
    pairing = await devices.begin_pairing()
    claimed = await devices.complete_pairing(pairing["code"], "Phone")
    device_id = claimed["device_id"]

    await devices.handoff(device_id, {"kind": "command", "text": "start the backup"})

    inbox = await devices.claim(device_id)
    assert len(inbox) == 1
    assert inbox[0]["payload"]["text"] == "start the backup"

    assert await devices.claim(device_id) == []


@pytest.mark.asyncio
async def test_handoff_to_unknown_device_fails(devices):
    with pytest.raises(KeyError):
        await devices.handoff("dev_nonexistent", {"kind": "note", "text": "hi"})


# ---------------------------------------------------------------------------
# spotting an unlisted game
#
# The old rule was "uses more than 700 MB and is not a known non-game", which
# in 2026 means every Electron application. This assistant's own desktop app
# was being detected as a game, repeatedly, while the user worked.
# ---------------------------------------------------------------------------
from backend.intelligence.gaming import _in_a_game_library


def test_a_steam_game_is_recognised_by_where_it_lives():
    assert _in_a_game_library(r"D:\SteamLibrary\steamapps\common\Elden Ring\eldenring.exe")
    assert _in_a_game_library(r"C:\Program Files (x86)\Steam\steamapps\common\Dota 2\dota2.exe")


def test_other_launchers_are_recognised_too():
    for path in (r"C:\Program Files\Epic Games\Fortnite\Fortnite.exe",
                 r"C:\Riot Games\VALORANT\live\VALORANT.exe",
                 r"C:\Program Files (x86)\GOG Galaxy\Games\Witcher 3\witcher3.exe",
                 r"C:\XboxGames\Forza\Content\forza.exe",
                 r"C:\Program Files (x86)\Rockstar Games\GTAV\GTA5.exe"):
        assert _in_a_game_library(path), path


def test_ordinary_desktop_applications_are_not_games():
    """The actual false positive: a large Electron app in AppData."""
    for path in (r"C:\Users\Sam\AppData\Local\Programs\claude\claude.exe",
                 r"C:\Users\Sam\AppData\Local\Discord\app-1.0\Discord.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Users\Sam\AppData\Local\Programs\Microsoft VS Code\Code.exe",
                 r"C:\Program Files\Mozilla Firefox\firefox.exe"):
        assert not _in_a_game_library(path), path


def test_an_unreadable_path_is_not_treated_as_evidence():
    """AccessDenied gives an empty path. That is an absence of evidence, not
    evidence of a game -- guessing here flips the user into gaming mode."""
    assert not _in_a_game_library("")
    assert not _in_a_game_library(None)


def test_forward_slashes_are_handled():
    assert _in_a_game_library("D:/SteamLibrary/steamapps/common/Hades/hades.exe")
