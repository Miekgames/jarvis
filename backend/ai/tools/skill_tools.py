"""Skills as tools: read one before doing the work, or save a new one."""
from __future__ import annotations

from ...core.permissions import Risk
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "use_skill",
        "Read the step-by-step instructions of one of your skills before doing a "
        "task it covers -- for example email-draft, meeting-notes, topic-research, "
        "daily-digest, debug-error. Then follow them.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "The skill's name"},
            "task": {"type": "string", "description": "What it is being used for"},
        }, "required": ["name"]},
        permission="files.read",
        category="skills",
    )
    async def use_skill(ctx, name: str, task: str = ""):
        library = ctx.get("skills")
        if library is None:
            return fail("Skills are not available.")
        skill = library.get(name)
        if skill is None:
            known = ", ".join(s.name for s in library.enabled()) or "none"
            return fail(f"There is no skill called '{name}'. Skills available: {known}.")
        if not skill.enabled:
            return fail(f"The skill '{name}' is switched off"
                        + (" because it was flagged when imported" if skill.flagged else "")
                        + ". Do the task without it.")
        lead = f"Instructions from the skill '{skill.name}'"
        lead += f" for: {task}" if task else ""
        return ok(f"{lead}. Follow them now.\n\n{skill.body}",
                  skill=skill.name, tier=skill.tier)

    @registry.add(
        "save_skill",
        "Save step-by-step instructions as a reusable skill, when the user asks you to "
        "remember how to do something a particular way.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "short-hyphenated-name"},
            "description": {"type": "string", "description": "When to use it, in one sentence"},
            "instructions": {"type": "string", "description": "The steps, in markdown"},
        }, "required": ["name", "description", "instructions"]},
        permission="files.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Save a new skill '{a.get('name')}': {a.get('description')}",
        category="skills",
    )
    async def save_skill(ctx, name: str, description: str, instructions: str):
        library = ctx.get("skills")
        if library is None:
            return fail("Skills are not available.")
        from ...skills.library import SkillError, slug

        try:
            skill = library.create(slug(name), description, instructions, tier="mine")
        except SkillError as exc:
            return fail(str(exc))
        note = "" if skill.enabled else (
            " It was saved switched off because its instructions looked like an "
            "attempt to override JARVIS's rules.")
        return ok({"name": skill.name}, f"Saved the skill '{skill.name}'.{note} "
                  f"Invoke it any time with /{skill.name}.")
