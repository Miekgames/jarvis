"""Task handler registration."""
from __future__ import annotations

from typing import Any, Dict, List

from .agent import AGENT_PROFILES, agent_handler, meeting_prep_handler
from .maintenance import (
    backup_handler,
    briefing_handler,
    diagnostics_handler,
    housekeeping_handler,
    organise_handler,
)
from .project import project_handler
from .research import deep_research_handler, long_research_handler, research_handler

# type -> (handler, resumable, human label, one-line description for the model)
HANDLERS: Dict[str, Dict[str, Any]] = {
    "research": {
        "handler": research_handler,
        "resumable": True,
        "label": "Web research",
        "description": "Multi-source web research producing a cited report. Params: query, depth "
                       "(quick|standard|deep).",
    },
    "deep_research": {
        "handler": deep_research_handler,
        "resumable": True,
        "label": "Deep research",
        "description": "Multi-source research with citations, conflict detection and staleness "
                       "checks, saved as a resumable project. Params: question, depth, project_id.",
    },
    "project": {
        "handler": project_handler,
        "resumable": False,
        "label": "Coding project",
        "description": "A coding or app-building project, done by Claude Code in its own folder "
                       "(the local coder if Claude Code can't). Params: task, minutes, folder, "
                       "conversation_id.",
    },
    "long_research": {
        "handler": long_research_handler,
        "resumable": False,
        "label": "Long research",
        "description": "15-20 minutes of searching and reading, then a 3-4 paragraph report of "
                       "what was found and what's next. Params: question, minutes, conversation_id.",
    },
    "agent": {
        "handler": agent_handler,
        "resumable": True,
        "label": "Autonomous agent",
        "description": "General multi-step work using any available tool. Params: objective, agent "
                       f"({'|'.join(AGENT_PROFILES)}), max_iterations.",
    },
    "meeting_prep": {
        "handler": meeting_prep_handler,
        "resumable": False,
        "label": "Meeting preparation",
        "description": "Gather context for a meeting and produce a brief. Params: event_id (optional; "
                       "defaults to the next event).",
    },
    "briefing": {
        "handler": briefing_handler,
        "resumable": False,
        "label": "Daily briefing",
        "description": "Compose the spoken daily briefing. Params: include (list of sections).",
    },
    "diagnostics": {
        "handler": diagnostics_handler,
        "resumable": False,
        "label": "Self-diagnostics",
        "description": "Check every subsystem and report health. No params.",
    },
    "backup": {
        "handler": backup_handler,
        "resumable": False,
        "label": "Backup",
        "description": "Verified backup of the JARVIS database. Params: keep (how many to retain).",
    },
    "organise_files": {
        "handler": organise_handler,
        "resumable": True,
        "label": "Organise a folder",
        "description": "Sort a folder into category subfolders. Params: path, apply (false = dry run).",
    },
    "housekeeping": {
        "handler": housekeeping_handler,
        "resumable": False,
        "label": "Housekeeping",
        "description": "Prune old tasks, metrics, logs and expired sessions, then compact the database.",
    },
}


def register_all(engine) -> List[str]:
    for task_type, spec in HANDLERS.items():
        engine.register(task_type, spec["handler"], resumable=spec["resumable"])
    return sorted(HANDLERS)


def catalogue() -> str:
    """Rendered into the create_task tool description so the model knows what
    it can actually queue."""
    return "\n".join(f"- {name}: {spec['description']}" for name, spec in sorted(HANDLERS.items()))
