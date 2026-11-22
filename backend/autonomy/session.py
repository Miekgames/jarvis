"""A run that happens while you are out.

Two clocks, and the difference between them is the point.

**Away** is when you will be back. **Work** is how long JARVIS should
actually be busy, and may be shorter -- because the useful thing is not a
session that stops the instant you walk in, it is one that has tidied up
before you do. The last slice of the working budget is therefore reserved:
nothing new is started, whatever is half-done gets finished or backed out,
and a report is written. You come home to a result, not to surgery in
progress.

The phases, in order:

    planning     turn the prompt into a list of jobs
    working      do them
    researching  when the list empties: find out what comparable apps do,
                 and turn the good parts into more jobs
    wrapping_up  finish what is started, write the report, stop
    done

`researching` exists because "work for six hours on X" almost always has
less than six hours of obvious work in it. The alternative to looking
outward is an agent that either idles or invents filler, and of those two
only one of them is honest about having run out.

Nothing here decides on its own what is safe: every action goes through
`guard`, and anything it holds is recorded rather than done.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.events import bus
from .guard import Decision, Guard, Verdict

logger = logging.getLogger("jarvis.autonomy")

# The tail of the working budget kept for finishing up. Proportional, with a
# floor, because ten per cent of twenty minutes is not enough time to write
# anything down.
WRAP_FRACTION = 0.12
# The floor used to be eight minutes, which collided with the half-budget
# cap: a twelve-minute run reserved six minutes -- half of it -- and stopped
# starting work after six. Writing the report is fast; the reserve exists to
# finish whatever job is in flight, and two minutes does that.
WRAP_MINIMUM_MINUTES = 2.0
WRAP_MAXIMUM_MINUTES = 45.0

# Hard ceiling. "Up to a day" taken literally.
MAX_WORK_MINUTES = 24 * 60

# How often the loop checks the clock and the stop flag.
TICK_SECONDS = 5.0

# Where a run keeps what it needs and the owner does not: earlier versions,
# the candidate under test, the saved record. A dot-directory, so the list
# of what was made shows the app rather than the scaffolding around it.
HIDDEN = ".jarvis"
RECORD = "run.json"


class Phase(str, Enum):
    PLANNING = "planning"
    WORKING = "working"
    RESEARCHING = "researching"
    WRAPPING_UP = "wrapping_up"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class Job:
    """One unit of work on the backlog."""

    title: str
    detail: str = ""
    source: str = "prompt"          # prompt | research | followup
    state: str = "pending"          # pending | doing | done | abandoned
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "detail": self.detail,
                "source": self.source, "state": self.state, "note": self.note}


@dataclass
class Entry:
    """One line of the journal. Everything that happened, in order."""

    at: str
    kind: str                        # step | held | refused | note | phase
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"at": self.at, "kind": self.kind,
                "message": self.message, "detail": self.detail}


class AutonomySession:
    """One unattended run."""

    def __init__(self, prompt: str, away_minutes: float, work_minutes: float,
                 workspace: Path, allow_shell: bool = False,
                 settings=None, activity=None, audit=None):
        self.prompt = (prompt or "").strip()
        self.away_minutes = max(1.0, float(away_minutes))
        # Working longer than the owner is away defeats the wrap-up: they
        # would walk in on a session still mid-job. Clamped rather than
        # rejected, because the intent is obvious and refusing is unhelpful.
        self.work_minutes = min(float(work_minutes), self.away_minutes,
                                MAX_WORK_MINUTES)

        self.workspace = Path(workspace)
        self.guard = Guard(self.workspace, allow_shell=allow_shell)
        self.settings = settings
        self.activity = activity
        self.audit = audit

        self.phase = Phase.PLANNING
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.jobs: List[Job] = []
        self.journal: List[Entry] = []
        self.held: List[Dict[str, Any]] = []
        self.report: str = ""

        # Building one thing is run differently from doing a list of tasks:
        # the program owns the file and each change is tested before it is
        # kept. What follows is only filled in for a build.
        self.mode = "task"                    # task | build
        self.artifact = ""                    # the app, relative to workspace
        self.preview = ""                     # a screenshot of it, likewise
        self.app: Dict[str, Any] = {}         # lines, bytes, version, tested
        self.steps: List[Dict[str, Any]] = [] # every change tried, and its fate
        self.remarks: List[str] = []          # things the report has to say
        self.searches: List[Dict[str, Any]] = []   # web lookups actually run

        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- the clocks --------------------------------------------------------
    @property
    def wrap_minutes(self) -> float:
        """How much of the budget is kept back for finishing up."""
        proportional = self.work_minutes * WRAP_FRACTION
        # Floor first, then ceiling, then never more than half the budget.
        # Ordering matters: applying the floor last let an eight-minute
        # minimum swallow a five-minute run whole, leaving no time to do
        # anything before tidying up began.
        reserved = max(WRAP_MINIMUM_MINUTES, proportional)
        reserved = min(reserved, WRAP_MAXIMUM_MINUTES)
        return min(reserved, self.work_minutes / 2)

    @property
    def elapsed_minutes(self) -> float:
        if self.started_at is None:
            return 0.0
        return (_now() - self.started_at).total_seconds() / 60.0

    @property
    def remaining_minutes(self) -> float:
        return max(0.0, self.work_minutes - self.elapsed_minutes)

    @property
    def should_wrap_up(self) -> bool:
        return self.remaining_minutes <= self.wrap_minutes

    @property
    def expired(self) -> bool:
        return self.remaining_minutes <= 0

    @property
    def running(self) -> bool:
        return self.phase in (Phase.PLANNING, Phase.WORKING,
                              Phase.RESEARCHING, Phase.WRAPPING_UP)

    # -- journal -----------------------------------------------------------
    def note(self, kind: str, message: str, **detail: Any) -> Entry:
        entry = Entry(at=_now().isoformat(), kind=kind, message=message,
                      detail=detail)
        self.journal.append(entry)
        logger.info("autonomy[%s] %s: %s", self.phase.value, kind, message)

        # Push, rather than letting every screen poll.
        #
        # The point of an unattended run is watching it from somewhere else
        # -- a phone, over the tailnet, while out. Polling every five
        # seconds makes that feel broken in exactly the moments it matters,
        # and the websocket already reaches every connected device.
        try:
            bus.publish("autonomy.progress", {
                "phase": self.phase.value,
                "kind": kind,
                "message": message,
                "elapsed_minutes": round(self.elapsed_minutes, 1),
                "remaining_minutes": round(self.remaining_minutes, 1),
                "jobs_done": len([j for j in self.jobs if j.state == "done"]),
                "jobs_total": len(self.jobs),
                "held_count": len(self.held),
                "running": self.running,
            }, source="autonomy")
        except Exception:
            logger.debug("could not publish progress", exc_info=True)
        return entry

    def hold(self, decision: Decision, what: str) -> None:
        """Record something that was wanted and not done.

        The held list is the part of the report the owner actually has to
        read, so it keeps what was being attempted as well as the rule that
        stopped it -- "held a delete" is useless a day later.
        """
        self.held.append({
            "at": _now().isoformat(),
            "what": what,
            "tool": decision.tool,
            "reason": decision.reason,
            "detail": decision.detail,
            "verdict": decision.verdict.value,
        })
        self.note("held" if decision.verdict is Verdict.HOLD else "refused",
                  what, reason=decision.reason, **decision.detail)

    # -- the backlog -------------------------------------------------------
    def add_jobs(self, titles: List[str], source: str = "prompt") -> int:
        added = 0
        existing = {job.title.strip().lower() for job in self.jobs}
        for title in titles:
            clean = (title or "").strip()
            if not clean or clean.lower() in existing:
                continue
            self.jobs.append(Job(title=clean, source=source))
            existing.add(clean.lower())
            added += 1
        if added:
            self.note("note", f"added {added} job(s) from {source}")
        return added

    @property
    def next_job(self) -> Optional[Job]:
        return next((j for j in self.jobs if j.state == "pending"), None)

    @property
    def backlog_empty(self) -> bool:
        return self.next_job is None

    def finish_job(self, job: Job, note: str = "", done: bool = True) -> None:
        job.state = "done" if done else "abandoned"
        job.note = note
        self.note("step", f"{'finished' if done else 'abandoned'}: {job.title}",
                  note=note)

    # -- what was actually built ------------------------------------------
    def record_step(self, title: str, kept: bool, detail: str = "",
                    **evidence: Any) -> Dict[str, Any]:
        """One change that was tried, and what became of it.

        This list -- not the model's account of itself -- is what the report
        is written from. A step is `kept` only when the new version passed
        every check and replaced the old one on disk, so "kept" and "is in
        the app" are the same statement.
        """
        step = {
            "at": _now().isoformat(),
            "title": title,
            "kept": bool(kept),
            "detail": detail,
            "version": int(self.app.get("version", 0)) if kept else None,
        }
        step.update({key: value for key, value in evidence.items() if value})
        self.steps.append(step)
        return step

    def remark(self, text: str) -> None:
        """Something the owner should be told in the report, once."""
        if text and text not in self.remarks:
            self.remarks.append(text)
            self.note("note", text)

    def save_record(self) -> None:
        """Write the state of the run beside its work.

        The live object dies with the process; a run that takes a day must
        not lose its account of itself to a restart in hour nine. Written to
        a temporary file and moved into place, so a crash mid-write leaves
        the previous record rather than half of a new one.
        """
        try:
            hidden = self.workspace / HIDDEN
            hidden.mkdir(parents=True, exist_ok=True)
            record = self.status()
            record["journal"] = record["journal"][-60:]
            temporary = hidden / (RECORD + ".tmp")
            temporary.write_text(json.dumps(record, indent=1), encoding="utf-8")
            temporary.replace(hidden / RECORD)
        except OSError:
            logger.debug("could not save the run record", exc_info=True)

    # -- control -----------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the run to stop. It finishes the current step first."""
        self._stop.set()
        self.note("note", "stop requested")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- state -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        done = [j for j in self.jobs if j.state == "done"]
        return {
            "phase": self.phase.value,
            "running": self.running,
            "prompt": self.prompt,
            "workspace": str(self.workspace),
            "away_minutes": round(self.away_minutes, 1),
            "work_minutes": round(self.work_minutes, 1),
            "wrap_minutes": round(self.wrap_minutes, 1),
            "elapsed_minutes": round(self.elapsed_minutes, 1),
            "remaining_minutes": round(self.remaining_minutes, 1),
            "should_wrap_up": self.should_wrap_up,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "jobs": [j.as_dict() for j in self.jobs],
            "jobs_done": len(done),
            "jobs_total": len(self.jobs),
            "held": self.held,
            "held_count": len(self.held),
            "journal": [e.as_dict() for e in self.journal[-200:]],
            "report": self.report,
            "stamp": self.workspace.name,
            "mode": self.mode,
            "artifact": self.artifact,
            "preview": self.preview,
            "app": dict(self.app),
            "steps": list(self.steps),
            "remarks": list(self.remarks),
            "searches": list(self.searches),
        }

    def _build_report(self) -> str:
        """The report for a build: what is in the app, and what is not.

        Written from the steps that were kept, which are by construction the
        changes that are in the file. Everything that was tried and thrown
        away is listed too, with the actual reason -- a report that only
        lists successes reads the same whether there were failures or not.
        """
        kept = [s for s in self.steps if s["kept"]]
        dropped = [s for s in self.steps if not s["kept"]]
        pending = [j for j in self.jobs if j.state == "pending"]
        lines: List[str] = []

        if not self.artifact:
            lines.append("No app was produced.")
            if dropped:
                lines.append(f"Every attempt at a first version was rejected "
                             f"({len(dropped)}); the last one because "
                             f"{dropped[-1]['detail']}.")
            lines.append("")
        else:
            app = self.app
            size = f"{app.get('bytes', 0) / 1000:.1f} KB"
            lines.append(f"Built {self.artifact} — {app.get('lines', 0)} lines, "
                         f"{size}, version {app.get('version', 0)}.")
            checked = app.get("checked", "")
            if checked == "browser":
                lines.append("It runs: every version was opened in a real "
                             "browser with the network off and its buttons "
                             "pressed, and only versions with no errors were "
                             "kept.")
            elif checked == "syntax":
                lines.append("It was checked for syntax errors and has none. "
                             "It was NOT run: nothing is executed while "
                             "nobody is here, so try it before relying on it.")
            elif checked == "untested":
                lines.append("It was NOT browser-tested"
                             + (f" ({app['test_detail']})"
                                if app.get("test_detail") else "")
                             + ", so open it and check before relying on it.")

            # What happened when the finished file was actually used. Said
            # here, straight after "it runs", because running and working
            # are different claims and only this one is about working.
            trial = app.get("trial") or {}
            if trial.get("ran"):
                wrong = trial.get("problems") or []
                if wrong:
                    lines.append("")
                    lines.append("Used at the end the way a person would, "
                                 f"and this was STILL WRONG ({len(wrong)}):")
                    lines += [f"  - {problem}" for problem in wrong]
                elif trial.get("saved"):
                    lines.append("Used at the end the way a person would — "
                                 "typed into it, pressed its buttons, "
                                 "reloaded the page: what was added was "
                                 "still there afterwards, and nothing threw "
                                 "an error.")
                else:
                    lines.append("Used at the end the way a person would — "
                                 "its buttons pressed with its fields "
                                 "filled in: nothing threw an error.")
            lines.append("")

            lines.append(f"In the app ({len(kept)}):")
            for step in kept:
                line = f"  - {step['title']}"
                if step.get("detail"):
                    line += f" — {step['detail']}"
                if step.get("source") == "research":
                    line += " · found by looking at comparable apps"
                lines.append(line)
                if step.get("added"):
                    lines.append("      new on the page: "
                                 + ", ".join(f"“{name}”" for name in step["added"]))
                if step.get("fixed"):
                    # Not the model's word for it: each of these was seen
                    # when the app was used before the change, and not seen
                    # when it was used in the same way after.
                    lines.append("      seen before this change and not "
                                 "after it: " + "; ".join(step["fixed"]))
            lines.append("")

        if dropped and self.artifact:
            lines.append(f"Tried, and not kept ({len(dropped)}):")
            lines += [f"  - {s['title']} — {s['detail']}" for s in dropped]
            lines.append("")

        if self.held:
            lines.append(f"Held for you to decide ({len(self.held)}):")
            for item in self.held:
                lines.append(f"  - {item['what']}")
                lines.append(f"      {item['reason']}")
            lines.append("")

        if pending:
            lines.append(f"Not reached ({len(pending)}):")
            lines += [f"  - {j.title}" for j in pending]
            lines.append("")

        if self.searches:
            lines.append("Looked up what comparable apps offer:")
            for search in self.searches:
                if search.get("error"):
                    outcome = f"the search failed ({search['error']})"
                elif not search.get("results"):
                    outcome = "no results"
                else:
                    read = search.get("read") or []
                    outcome = (f"{search['results']} results, "
                               + (f"{len(read)} page(s) read, " if read else "")
                               + f"{search.get('added', 0)} feature(s) taken from them")
                lines.append(f"  - “{search['query']}” — {outcome}")
                for page in search.get("read") or []:
                    lines.append(f"      read: {page['title']}")
            lines.append("")

        lines += self.remarks
        if self.remarks:
            lines.append("")

        lines.append(f"Worked for {self.elapsed_minutes:.0f} of "
                     f"{self.work_minutes:.0f} minutes. Everything is in "
                     f"{self.workspace}.")
        return "\n".join(lines).rstrip()

    def write_report(self) -> str:
        """What the owner reads when they get back.

        Ordered by what they most need to know: what is done, then what was
        held and needs a decision, then the rest. A report that opens with a
        timeline buries the two things that matter.
        """
        if self.mode == "build":
            self.report = self._build_report()
            return self.report

        done = [j for j in self.jobs if j.state == "done"]
        abandoned = [j for j in self.jobs if j.state == "abandoned"]
        pending = [j for j in self.jobs if j.state == "pending"]

        lines = [
            f"Worked for {self.elapsed_minutes:.0f} of {self.work_minutes:.0f} "
            f"minutes in {self.workspace}.",
            "",
        ]

        if done:
            lines.append(f"Finished ({len(done)}):")
            lines += [f"  - {j.title}" + (f" — {j.note}" if j.note else "")
                      for j in done]
            lines.append("")
        else:
            lines += ["Nothing was finished.", ""]

        if self.held:
            lines.append(f"Held for you to decide ({len(self.held)}):")
            for item in self.held:
                lines.append(f"  - {item['what']}")
                lines.append(f"      {item['reason']}")
            lines.append("")

        if abandoned:
            lines.append(f"Abandoned ({len(abandoned)}):")
            lines += [f"  - {j.title}" + (f" — {j.note}" if j.note else "")
                      for j in abandoned]
            lines.append("")

        if pending:
            lines.append(f"Not reached ({len(pending)}):")
            lines += [f"  - {j.title}" for j in pending]
            lines.append("")

        self.report = "\n".join(lines).rstrip()
        return self.report

    async def record(self, message: str) -> None:
        if self.activity is not None:
            try:
                await self.activity.record(message, source="autonomy", level="info")
            except Exception:
                logger.debug("activity log failed", exc_info=True)
        if self.audit is not None:
            try:
                await self.audit.record("autonomy.session", subject=message)
            except Exception:
                logger.debug("audit failed", exc_info=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)
