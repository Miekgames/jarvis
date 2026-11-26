"""Which skill JARVIS is using right now, so the screen can wear its colour.

Every skill has a colour of its own: the ``color:`` line in its SKILL.md, or
one made from its name. The screen fades to it while the skill is in use and
back again after.

A reply uses a skill when the owner runs it ("/remind ..."), when the model
reads it (``use_skill``), or when it calls a tool that only that skill is
built on (``schedule_notification`` is the remind skill's). Tools several
skills share, like ``web_search``, don't count: an ordinary reply that
searches the web is still an ordinary reply. Long work keeps its skill's
colour for as long as it runs -- research stays orange until the report.
"""
from __future__ import annotations

import colorsys
import hashlib
import re
from typing import Any, Dict, List, Optional

from ..core.events import bus

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TOOL_NAME = re.compile(r"`([a-z]+(?:_[a-z]+)+)`")


def color_for(skill: Any) -> str:
    """The skill's own colour, or a steady one made from its name."""
    own = str(getattr(skill, "color", "") or "").strip()
    if HEX.match(own):
        return own.lower()
    # Any hue but the cyan-to-violet the screen already wears when idle, so a
    # skill's colour always shows as a change.
    digest = int(hashlib.sha256(str(skill.name).encode("utf-8")).hexdigest()[:8], 16)
    hue = ((digest % 240) + 290) % 360 / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.62, 0.85)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


# Everyday tools any reply may use. A skill that mentions one doesn't own it:
# searching the web, reading a page or a file is not "using the research skill".
EVERYDAY_TOOLS = {"web_search", "read_webpage", "read_file", "find_files", "list_folder",
                  "find_in_code", "read_source", "calendar_view", "quick_research", "web_news"}
# Tools whose skill isn't the one that happens to mention them.
TOOL_SKILLS = {"deep_research": "deep-research", "research_topic": "deep-research",
               "set_reminder": "remind", "schedule_notification": "remind"}


def skill_for_tool(skills: Any, tool: str) -> Optional[Any]:
    """The one enabled skill built on this tool; None if none, or if shared."""
    if skills is None or not tool or tool in EVERYDAY_TOOLS:
        return None
    if tool in TOOL_SKILLS:
        skill = skills.get(TOOL_SKILLS[tool])
        return skill if skill is not None and skill.enabled else None
    owners = [s for s in skills.all() if s.enabled and tool in TOOL_NAME.findall(s.body)]
    return owners[0] if len(owners) == 1 else None


class SkillActivity:
    """The skills in use: at most one for the reply, any number of long tasks."""

    def __init__(self) -> None:
        self._reply: Optional[Dict[str, Any]] = None
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def current(self) -> List[Dict[str, Any]]:
        return ([self._reply] if self._reply else []) + list(self._tasks.values())

    def _publish(self) -> None:
        bus.publish("skills.active", {"active": self.current()}, source="skills")

    @staticmethod
    def _entry(skill: Any, kind: str, task_id: str = "") -> Dict[str, Any]:
        return {"skill": skill.name, "color": color_for(skill), "kind": kind, "id": task_id}

    def replying_with(self, skill: Any) -> None:
        if skill is None or (self._reply and self._reply["skill"] == skill.name):
            return
        self._reply = self._entry(skill, "reply")
        self._publish()

    def tool_used(self, tool: str, arguments: Dict[str, Any], skills: Any) -> None:
        if skills is None:
            return
        if tool == "use_skill":                   # the model chose to follow this skill
            skill = skills.get(str((arguments or {}).get("name") or ""))
        elif self._reply is not None:
            return                                # the reply's skill stays for the reply
        else:
            skill = skill_for_tool(skills, tool)
        if skill is not None and skill.enabled:
            self.replying_with(skill)

    def reply_done(self) -> None:
        if self._reply is not None:
            self._reply = None
            self._publish()

    def task_started(self, task_id: str, skill: Any) -> None:
        if skill is None or not task_id:
            return
        self._tasks[task_id] = self._entry(skill, "task", task_id)
        self._publish()

    def task_done(self, task_id: str) -> None:
        if self._tasks.pop(task_id, None) is not None:
            self._publish()


skill_activity = SkillActivity()
