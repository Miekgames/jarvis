"""The one unattended run at a time, and its history.

One at a time on purpose. Two agents editing the same machine while nobody
watches is a class of problem with no good outcome and no way to debug it
afterwards, and the owner asked for a session they leave running, not a
fleet.

Finished runs are kept in memory and written to the workspace, so the report
survives this process restarting even though the live object does not.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runner import Runner
from .session import HIDDEN, MAX_WORK_MINUTES, RECORD, AutonomySession, Phase

logger = logging.getLogger("jarvis.autonomy.service")

# How many finished runs to keep for the interface.
KEEP = 20

PAGES = (".html", ".htm")


def _visible(workspace: Path) -> List[Path]:
    """The files a run made, without the scaffolding it made them with.

    Earlier versions, the candidate under test and the saved record live in
    a dot-directory. They are how the app got built, not what was built, and
    listing them beside it buries the one file the owner came for.
    """
    try:
        return sorted(
            path for path in workspace.rglob("*")
            if path.is_file() and not any(
                part.startswith(".") for part in path.relative_to(workspace).parts))
    except OSError:
        return []


class AutonomyService:
    """Starts, watches and stops unattended work."""

    def __init__(self, settings, orchestrator=None, registry=None,
                 activity=None, audit=None):
        self.settings = settings
        self.orchestrator = orchestrator
        self.registry = registry
        self.activity = activity
        self.audit = audit

        self.root = Path(settings.DATA_DIR) / "autonomy"
        self.current: Optional[AutonomySession] = None
        self.history: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None

    # -- state -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.orchestrator is not None,
            "busy": self.busy,
            "max_work_minutes": MAX_WORK_MINUTES,
            "current": self.current.status() if self.current else None,
            "history": self.history[:KEEP],
            "root": str(self.root),
        }

    # -- lifecycle ---------------------------------------------------------
    async def start(self, prompt: str, away_minutes: float,
                    work_minutes: float, allow_shell: bool = False
                    ) -> Dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("The AI core is not available.")
        if self.busy:
            raise RuntimeError(
                f"Already working on: {self.current.prompt[:80]}. "
                "Stop that first.")
        if not (prompt or "").strip():
            raise ValueError("Say what you want done while you are out.")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        workspace = self.root / stamp

        session = AutonomySession(
            prompt=prompt, away_minutes=away_minutes, work_minutes=work_minutes,
            workspace=workspace, allow_shell=allow_shell,
            settings=self.settings, activity=self.activity, audit=self.audit)
        self.current = session

        runner = Runner(self.orchestrator, self.registry, session)
        self._task = asyncio.create_task(self._run(runner), name=f"autonomy-{stamp}")
        return session.status()

    async def _run(self, runner: Runner) -> None:
        try:
            await runner.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("unattended run crashed")
        finally:
            session = self.current
            if session is not None:
                self.history.insert(0, session.status())
                del self.history[KEEP:]

    async def welcome_back(self) -> Dict[str, Any]:
        """The owner is back. Hand over, and clear the way for the next run.

        Separate from stopping, because the two mean different things: stop
        interrupts work, this acknowledges finished work. Without it a
        completed run sat as `current` forever, so the panel kept showing a
        report from yesterday and refused to start anything new.
        """
        session = self.current
        if session is None:
            return {"acknowledged": False, "detail": "Nothing to catch up on."}

        if session.running:
            # Back early. Wrap up rather than discarding what is in flight.
            session.request_stop()
            return {
                "acknowledged": False,
                "running": True,
                "detail": "It is still working — asked it to wrap up. "
                          "Say you are back again once it has.",
            }

        report = session.report or session.write_report()
        done = len([j for j in session.jobs if j.state == "done"])
        held = len(session.held)

        self.current = None
        await session.record("owner returned; run acknowledged")

        if session.mode == "build":
            kept = len([s for s in session.steps if s["kept"]])
            dropped = len(session.steps) - kept
            if session.artifact:
                detail = (
                    f"Welcome back. {session.artifact} is built: "
                    f"{session.app.get('lines', 0)} lines, {kept} change(s) "
                    "kept"
                    + (f", {dropped} tried and thrown away" if dropped else "")
                    + ". Open it from the While away tab."
                )
            else:
                detail = ("Welcome back. No app came out of that run — the "
                          "report says why.")
        else:
            detail = (
                f"Welcome back. {done} job(s) finished"
                + (f", {held} thing(s) waiting on you" if held else "")
                + f". The work is in {session.workspace}."
            )

        return {
            "acknowledged": True,
            "report": report,
            "jobs_done": done,
            "held_count": held,
            "workspace": str(session.workspace),
            "stamp": session.workspace.name,
            "mode": session.mode,
            "artifact": session.artifact,
            "preview": session.preview,
            "detail": detail,
        }

    # -- getting the work out ---------------------------------------------
    def _session_for(self, stamp: str = "") -> Optional[Path]:
        """The workspace of a run, by its folder name.

        Runs are addressed by the directory they wrote into rather than by
        an index, because the live session is lost when this process
        restarts while the files are not.
        """
        if stamp:
            candidate = (self.root / stamp).resolve()
            try:
                candidate.relative_to(self.root.resolve())
            except ValueError:
                return None          # outside the autonomy root entirely
            return candidate if candidate.is_dir() else None
        if self.current is not None:
            return self.current.workspace
        runs = self.runs()
        return Path(runs[0]["workspace"]) if runs else None

    def _record_for(self, workspace: Path) -> Dict[str, Any]:
        """What is known about the run that wrote a workspace.

        The live session while there is one, and otherwise the record it
        saved beside its work -- which is the only account left of a run
        once this process has restarted.
        """
        current = self.current
        if current is not None and Path(current.workspace) == Path(workspace):
            return current.status()
        try:
            record = json.loads(
                (workspace / HIDDEN / RECORD).read_text(encoding="utf-8"))
            return record if isinstance(record, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _main_file(workspace: Path, record: Dict[str, Any],
                   files: List[Path]) -> str:
        """The thing that was built, as opposed to the notes about it."""
        named = str(record.get("artifact") or "")
        if named and (workspace / named).is_file():
            return named
        # A run from before records were kept: if it made exactly one page,
        # that page is the answer.
        pages = [f for f in files if f.suffix.lower() in PAGES]
        if len(pages) == 1:
            return pages[0].relative_to(workspace).as_posix()
        return ""

    def runs(self) -> List[Dict[str, Any]]:
        """Every run that left something behind, newest first."""
        if not self.root.exists():
            return []
        out = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            files = _visible(directory)
            if not files:
                continue
            record = self._record_for(directory)
            steps = record.get("steps") or []
            preview = str(record.get("preview") or "")
            out.append({
                "stamp": directory.name,
                "workspace": str(directory),
                "files": len(files),
                "bytes": sum(f.stat().st_size for f in files),
                "prompt": record.get("prompt", ""),
                "mode": record.get("mode", "task"),
                "phase": record.get("phase", ""),
                "running": bool(record.get("running")),
                "artifact": self._main_file(directory, record, files),
                "preview": preview if preview and (directory / preview).is_file() else "",
                "kept": len([s for s in steps if s.get("kept")]),
                "dropped": len([s for s in steps if not s.get("kept")]),
                "jobs_done": record.get("jobs_done", 0),
                "jobs_total": record.get("jobs_total", 0),
                "held_count": record.get("held_count", 0),
                "elapsed_minutes": record.get("elapsed_minutes", 0),
            })
        return out[:KEEP]

    def produced(self, stamp: str = "") -> Dict[str, Any]:
        """What a run actually made, as a list a person can act on.

        Leads with the app. The first version of this returned a flat list
        in which the thing that was built sat alphabetically between its own
        screenshot and its notes, and the owner had to work out which of
        three files was the result.
        """
        workspace = self._session_for(stamp)
        if workspace is None or not workspace.exists():
            return {"files": [], "workspace": "", "detail": "Nothing produced yet."}

        paths = _visible(workspace)
        record = self._record_for(workspace)
        main = self._main_file(workspace, record, paths)
        preview = str(record.get("preview") or "")
        if not (preview and (workspace / preview).is_file()):
            preview = ""

        files = []
        for path in paths:
            relative = path.relative_to(workspace).as_posix()
            files.append({
                "path": relative,
                "name": path.name,
                "bytes": path.stat().st_size,
                # Only a page can be opened as one; everything else is a
                # download, and offering "open" on a .py would be a lie.
                "openable": path.suffix.lower() in PAGES,
                "main": relative == main,
            })
        files.sort(key=lambda item: (not item["main"], item["path"]))

        return {
            "workspace": str(workspace),
            "stamp": workspace.name,
            "files": files,
            "detail": f"{len(files)} file(s).",
            "mode": record.get("mode", "task"),
            "prompt": record.get("prompt", ""),
            "artifact": main,
            "preview": preview,
            "app": record.get("app") or {},
            "steps": record.get("steps") or [],
            "searches": record.get("searches") or [],
            "remarks": record.get("remarks") or [],
            "report": record.get("report") or "",
            "running": bool(record.get("running")),
        }

    def resolve_file(self, relative: str, stamp: str = "") -> Optional[Path]:
        """One produced file, confined to its own workspace.

        The path comes from a client, and joining client text onto a
        directory is how traversal happens -- so the result is resolved and
        checked to be inside the workspace rather than trusted.
        """
        workspace = self._session_for(stamp)
        if workspace is None:
            return None
        candidate = (workspace / relative).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def archive(self, stamp: str = "") -> Optional[Path]:
        """Zip the whole run, so it can be taken away in one piece."""
        import zipfile

        workspace = self._session_for(stamp)
        if workspace is None or not workspace.exists():
            return None

        bundle = workspace.parent / f"{workspace.name}.zip"
        # Rebuilt each time: a run that is still working will have more in
        # it a minute from now, and a stale zip is worse than a slow one.
        if bundle.exists():
            bundle.unlink()
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in _visible(workspace):
                archive.write(path, path.relative_to(workspace))
        return bundle

    async def stop(self) -> Dict[str, Any]:
        """Ask the run to stop. It finishes its current step and wraps up."""
        if self.current is None or not self.current.running:
            return {"stopped": False, "detail": "Nothing is running."}
        self.current.request_stop()
        return {"stopped": True,
                "detail": "Asked it to stop. It will finish the step it is on "
                          "and write its report."}

    async def cancel(self) -> Dict[str, Any]:
        """Stop now, without wrapping up. The blunt one."""
        if self._task is None or self._task.done():
            return {"cancelled": False, "detail": "Nothing is running."}
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        return {"cancelled": True, "detail": "Stopped immediately."}
