"""The screen wears the colour of the skill in use: research orange, and back.

What matters: every skill has a colour that shows as a change (never the
screen's own blue-violet); the colour comes on when a skill is really used,
not for any web search; it goes when the reply ends or the work stops; and
the owner can turn it off by saying so.
"""
from __future__ import annotations

import colorsys
from types import SimpleNamespace

import pytest

from backend.ai.tools.control_tools import plain_setting
from backend.skills.activity import SkillActivity, color_for, skill_for_tool
from backend.skills.library import BUNDLED_DIR, SkillLibrary, parse, render


def _hue(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "skills", BUNDLED_DIR)


def test_every_bundled_skill_has_its_own_colour(library):
    colours = {s.name: color_for(s) for s in library.all()}
    assert len(colours) >= 17
    assert len(set(colours.values())) == len(colours), "no two skills share a colour"
    for name, colour in colours.items():
        assert not 170 <= _hue(colour) <= 290, f"{name} would look like the idle screen"
    assert colours["deep-research"] == "#ffa63d", "deep research is Ember orange"


def test_a_skill_without_a_colour_gets_a_steady_one_of_its_own():
    one = SimpleNamespace(name="my-recipes", color="")
    assert color_for(one) == color_for(SimpleNamespace(name="my-recipes", color=""))
    assert not 170 <= _hue(color_for(one)) <= 290
    assert color_for(SimpleNamespace(name="x", color="not a colour")) != "not a colour"


def test_the_colour_survives_saving_a_skill(library):
    skill = library.get("remind")
    front, _ = parse(render(skill))
    assert front["color"] == skill.color == "#4fe0a8"


@pytest.mark.parametrize("tool, skill", [
    ("research_topic", "deep-research"), ("deep_research", "deep-research"),
    ("schedule_notification", "remind"), ("set_reminder", "remind"), ("add_countdown", "countdown"),
    ("web_search", None),                       # an ordinary reply may search the web
    ("read_file", None), ("read_webpage", None), ("open_panel", None),
])
def test_which_tools_mean_a_skill_is_in_use(library, tool, skill):
    found = skill_for_tool(library, tool)
    assert (found.name if found else None) == skill


def test_a_reply_wears_its_skill_until_it_ends(library):
    activity = SkillActivity()
    heard = []
    activity._publish = lambda: heard.append(activity.current())
    activity.tool_used("schedule_notification", {}, library)
    assert [a["skill"] for a in activity.current()] == ["remind"]
    activity.tool_used("schedule_notification", {}, library)     # once is enough
    assert len(heard) == 1
    activity.tool_used("use_skill", {"name": "email-draft"}, library)
    assert activity.current()[0]["skill"] == "email-draft"
    activity.reply_done()
    assert activity.current() == [] and heard[-1] == []


def test_research_keeps_its_colour_while_it_runs(library):
    activity = SkillActivity()
    activity.task_started("task_1", library.get("deep-research"))
    activity.replying_with(library.get("remind"))                # a reminder meanwhile
    assert [a["kind"] for a in activity.current()] == ["reply", "task"]
    activity.reply_done()
    assert activity.current() == [{"skill": "deep-research", "color": "#ffa63d",
                                   "kind": "task", "id": "task_1"}]
    activity.task_done("task_1")
    assert activity.current() == []


def test_the_reply_colour_goes_when_jarvis_goes_idle(library):
    from backend.ai.orchestrator import JarvisState, Orchestrator
    from backend.skills.activity import skill_activity

    skill_activity.replying_with(library.get("countdown"))
    fake = SimpleNamespace(_state=JarvisState.WORKING, _state_detail="")
    try:
        Orchestrator.set_state(fake, JarvisState.IDLE)
        assert skill_activity.current() == []
    finally:
        skill_activity.reply_done()


@pytest.mark.parametrize("said, found", [
    ("stop changing colours", ("skill colours", "off")),
    ("please don't change the colors", ("skill colours", "off")),
    ("start changing colours again", ("skill colours", "on")),
    ("turn off skill colours", ("skill colours", "off")),
])
def test_skill_colours_can_be_turned_off_by_saying_so(said, found):
    assert plain_setting(said) == found


def test_a_skill_run_by_name_keeps_its_colour_for_the_reply(library):
    activity = SkillActivity()
    activity._publish = lambda: None
    activity.replying_with(library.get("meeting-notes"))         # "/meeting-notes ..."
    activity.tool_used("set_reminder", {}, library)              # it sets a reminder
    assert activity.current()[0]["skill"] == "meeting-notes"
