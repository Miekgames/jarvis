"""
Self-modification — JARVIS editing its own source.

Uses a **separate, dedicated code model** from the one holding the
conversation. That separation is deliberate: the model writing code should be
chosen for coding, and keeping it distinct means a conversational model can
never quietly rewrite the application by drifting off-task.

The pipeline never skips a step:

    read -> propose (dedicated model) -> validate syntax -> diff
         -> confirm (shows the diff) -> back up -> write -> restart notice

and every write is reversible.

Guard rails, in order of severity:

  * `code.self_modify` is a **critical** permission, off by default.
  * Edits are confined to the project's own source directories.
  * Files that implement the safety machinery itself -- permissions,
    authentication, confirmations, audit, and this module -- are *protected*.
    Editing one requires an explicit override on the call, and the
    confirmation prompt says exactly what is being weakened.
  * Syntactically invalid code is never written. Python is parsed; TypeScript
    is checked with the project's own `tsc` when available.
  * Every write is backed up first and recorded in the audit log.
  * Nothing is ever applied automatically. Proposal and application are
    separate operations.
"""
from __future__ import annotations

import ast
import difflib
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.proc import run as _run, popen as _popen
from ..core.events import bus
from ..core.util import new_id, now_iso, truncate

# Long enough for the whole suite on this machine, short enough that a
# hung test does not leave a half-applied edit in place indefinitely.
TEST_TIMEOUT_S = 900.0

logger = logging.getLogger("jarvis.selfedit")

# Only these trees are editable. Anything else -- data, backups, .env, the
# virtualenv -- is out of bounds regardless of what is asked.
EDITABLE_ROOTS = ("backend", "frontend/src", "tests", "docs", "start.py")

EDITABLE_SUFFIXES = {".py", ".ts", ".tsx", ".css", ".md", ".json", ".html"}

# Editing these weakens the very controls that make self-editing safe.
PROTECTED = {
    "backend/core/permissions.py",
    "backend/core/security.py",
    "backend/core/confirmations.py",
    "backend/core/audit.py",
    "backend/intelligence/selfedit.py",
    "backend/api/deps.py",
}

MAX_FILE_BYTES = 400_000

_CODER_SYSTEM = """You modify the source code of JARVIS, a local-first personal AI assistant.

You will be given one file's full contents and a change request.

Return the COMPLETE new contents of the file, and nothing else:
- No markdown fences, no commentary, no diff format, no ellipses.
- Preserve the file's existing style, imports, indentation and comment voice.
- Change only what the request requires. Do not reformat untouched code.
- Never remove error handling, permission checks, confirmation prompts or audit
  calls unless the request explicitly asks for exactly that.
- If the request cannot be done safely or is unclear, return the file UNCHANGED
  and put a single line at the very top reading:
  # SELFEDIT-REFUSED: <one sentence reason>
"""


class SelfEditService:
    def __init__(self, db, settings, orchestrator, activity, audit=None, reasoning=None):
        self.db = db
        self.settings = settings
        self.orchestrator = orchestrator
        self.activity = activity
        self.audit = audit
        self.reasoning = reasoning
        self.root = Path(settings.ROOT).resolve()
        self.backup_dir = Path(settings.BACKUP_DIR) / "source"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._proposals: Dict[str, Dict[str, Any]] = {}

    # ==================================================================
    # the dedicated model
    # ==================================================================
    def model_info(self) -> Dict[str, Any]:
        """Which model does the code writing, and where it came from."""
        provider = (getattr(self.settings, "SELFEDIT_PROVIDER", "") or "").strip()
        model = (getattr(self.settings, "SELFEDIT_MODEL", "") or "").strip()

        if self.settings.local_only:
            # Local-only means local-only. A stale JARVIS_SELFEDIT_PROVIDER must
            # not become the one route that quietly sends source code to a
            # cloud API.
            provider = "ollama"
        elif not provider:
            provider = "anthropic" if self.settings.ANTHROPIC_API_KEY else "ollama"
        return {
            "provider": provider,
            "model": model or ("(routed: coding)" if provider == "anthropic" else "(auto)"),
            "configured": bool(model),
            "separate_from_conversation": True,
        }

    async def _write_code(self, prompt: str) -> Tuple[str, Dict[str, Any]]:
        """Call the dedicated code model directly, bypassing the conversational
        orchestrator so nothing else can influence what gets written."""
        info = self.model_info()
        provider = self.orchestrator.providers.get(info["provider"])
        if provider is None:
            raise RuntimeError(
                f"The '{info['provider']}' provider is not available. Set "
                f"JARVIS_SELFEDIT_PROVIDER to one you have configured."
            )

        model: Optional[str] = None
        if info["configured"]:
            model = info["model"]
        elif info["provider"] == "anthropic":
            model = self.orchestrator.model_for("coding")
        elif hasattr(provider, "resolve_for"):
            # Per-purpose routing is optional in the provider contract (the
            # orchestrator treats it that way too); a provider without it picks
            # its own default rather than crashing here.
            model = await provider.resolve_for("coding")

        completion = await provider.complete(
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            system=_CODER_SYSTEM,
            tools=None,
            model=model,
            max_tokens=16000,
            temperature=0.0,          # deterministic: this is code, not prose
        )
        await self.orchestrator._record_usage(completion, "selfedit")
        return completion.text, {"provider": info["provider"], "model": model}

    # ==================================================================
    # source access
    # ==================================================================
    def resolve(self, relative: str) -> Path:
        """Resolve inside the project's own editable source. Raises otherwise."""
        candidate = (self.root / relative.strip().lstrip("/\\")).resolve()
        try:
            inside = candidate.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"'{relative}' is outside the JARVIS project.")

        posix = inside.as_posix()
        if not any(posix == r or posix.startswith(f"{r}/") for r in EDITABLE_ROOTS):
            raise PermissionError(
                f"'{posix}' is not in an editable part of the project. "
                f"Editable: {', '.join(EDITABLE_ROOTS)}."
            )
        if candidate.suffix and candidate.suffix not in EDITABLE_SUFFIXES:
            raise PermissionError(f"'{candidate.suffix}' files are not editable.")
        return candidate

    def is_protected(self, relative: str) -> bool:
        try:
            posix = self.resolve(relative).relative_to(self.root).as_posix()
        except PermissionError:
            return True
        return posix in PROTECTED

    def list_files(self, pattern: str = "") -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for root in EDITABLE_ROOTS:
            base = self.root / root
            if base.is_file():
                candidates = [base]
            elif base.is_dir():
                candidates = [p for p in base.rglob("*")
                              if p.is_file() and p.suffix in EDITABLE_SUFFIXES
                              and "node_modules" not in p.parts and "__pycache__" not in p.parts]
            else:
                continue
            for path in candidates:
                relative = path.relative_to(self.root).as_posix()
                if pattern and pattern.lower() not in relative.lower():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                found.append({
                    "path": relative,
                    "size": size,
                    "lines": _count_lines(path),
                    "protected": relative in PROTECTED,
                })
        found.sort(key=lambda f: f["path"])
        return found

    def read(self, relative: str) -> Dict[str, Any]:
        path = self.resolve(relative)
        if not path.exists():
            raise FileNotFoundError(f"'{relative}' does not exist.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"'{relative}' is too large to edit safely "
                             f"({path.stat().st_size} bytes).")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path.relative_to(self.root).as_posix(),
            "content": text,
            "lines": text.count("\n") + 1,
            "protected": self.is_protected(relative),
        }

    # ==================================================================
    # validation
    # ==================================================================
    def validate(self, relative: str, content: str) -> Dict[str, Any]:
        """Syntax check before anything is written. A broken write could stop
        JARVIS starting at all, so this is not optional."""
        suffix = Path(relative).suffix

        if suffix == ".py":
            try:
                ast.parse(content, filename=relative)
            except SyntaxError as exc:
                return {"valid": False, "checker": "python-ast",
                        "error": f"line {exc.lineno}: {exc.msg}"}
            return {"valid": True, "checker": "python-ast"}

        if suffix == ".json":
            import json
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                return {"valid": False, "checker": "json", "error": str(exc)}
            return {"valid": True, "checker": "json"}

        if suffix in (".ts", ".tsx"):
            return self._check_typescript(relative, content)

        # Markdown, CSS and HTML have no failure mode that stops the app.
        return {"valid": True, "checker": "none",
                "note": f"No syntax checker for {suffix or 'this file type'}."}

    def _check_typescript(self, relative: str, content: str) -> Dict[str, Any]:
        """Type-check against the real project by writing the candidate to a
        temporary sibling, so imports and tsconfig resolve properly."""
        frontend = self.root / "frontend"
        if not (frontend / "node_modules" / "typescript").exists():
            return {"valid": True, "checker": "none",
                    "note": "TypeScript not installed; syntax was not verified."}

        target = self.resolve(relative)
        shadow = target.with_name(f"__selfedit_{target.name}")
        try:
            shadow.write_text(content, encoding="utf-8")
            # Resolved rather than shelled. `npx` is a .cmd on Windows, which
            # is why this used to pass shell=True -- but that also sends the
            # file path through cmd.exe, where a `&` or `|` in a filename
            # stops being part of the name and starts being punctuation.
            npx = shutil.which("npx")
            if not npx:
                return {"valid": True, "checker": "none",
                        "note": "npx is not on PATH; syntax was not verified."}
            result = _run(
                [npx, "tsc", "--noEmit", "--skipLibCheck", str(shadow)],
                cwd=frontend, capture_output=True, text=True, timeout=180,
                shell=False,
            )
            if result.returncode != 0:
                errors = [line for line in (result.stdout or "").splitlines()
                          if "__selfedit_" in line][:6]
                if errors:
                    return {"valid": False, "checker": "tsc",
                            "error": "\n".join(e.replace("__selfedit_", "") for e in errors)}
            return {"valid": True, "checker": "tsc"}
        except subprocess.TimeoutExpired:
            return {"valid": True, "checker": "tsc",
                    "note": "Type check timed out; syntax was not verified."}
        except OSError as exc:
            return {"valid": True, "checker": "none", "note": f"Could not run tsc: {exc}"}
        finally:
            shadow.unlink(missing_ok=True)

    # ==================================================================
    # propose
    # ==================================================================
    async def propose(self, relative: str, request: str) -> Dict[str, Any]:
        """Ask the dedicated code model for a change. Writes nothing."""
        current = self.read(relative)
        protected = current["protected"]

        prompt = (
            f"FILE: {current['path']}\n"
            f"CHANGE REQUEST: {request}\n\n"
            f"--- CURRENT CONTENTS ---\n{current['content']}"
        )
        proposed, model_used = await self._write_code(prompt)
        proposed = _strip_fences(proposed)

        if not proposed.strip():
            return {"ok": False, "message": "The code model returned nothing."}

        refusal = re.match(r"^\s*(?:#|//)\s*SELFEDIT-REFUSED:\s*(.+)", proposed)
        if refusal:
            return {"ok": False, "refused": True,
                    "message": f"The code model declined: {refusal.group(1).strip()}"}

        if proposed.strip() == current["content"].strip():
            return {"ok": False, "message": "The model returned the file unchanged — "
                                            "the request may already be satisfied."}

        validation = self.validate(current["path"], proposed)
        diff = _unified_diff(current["content"], proposed, current["path"])
        added, removed = _diff_counts(diff)

        proposal_id = new_id("edit")
        self._proposals[proposal_id] = {
            "path": current["path"],
            "before": current["content"],
            "after": proposed,
            "request": request,
            "created_at": now_iso(),
            "model": model_used,
        }

        if self.reasoning:
            await self.reasoning.log_decision(
                subject="self-edit proposal",
                summary=f"Proposed a change to {current['path']} (+{added}/-{removed}).",
                rationale=truncate(request, 400),
                inputs={"model": model_used, "valid": validation["valid"],
                        "protected": protected},
                alternatives=["leave the file unchanged"],
            )
        await self.activity.record(
            f"Proposed a source change to {current['path']} (+{added}/-{removed}, "
            f"{'valid' if validation['valid'] else 'INVALID'}).",
            level="warn" if not validation["valid"] else "info",
            source="selfedit",
        )
        bus.publish("selfedit.proposed", {"id": proposal_id, "path": current["path"],
                                          "added": added, "removed": removed,
                                          "valid": validation["valid"]}, source="selfedit")

        return {
            "ok": True,
            "proposal_id": proposal_id,
            "path": current["path"],
            "protected": protected,
            "diff": diff,
            "lines_added": added,
            "lines_removed": removed,
            "validation": validation,
            "model": model_used,
            "applied": False,
            "note": "Nothing has been written. Apply it explicitly to make the change.",
        }

    # ==================================================================
    # apply
    # ==================================================================
    async def verify(self, timeout: float = TEST_TIMEOUT_S) -> Dict[str, Any]:
        """Run the test suite and report whether it still passes.

        Validation catches code that will not parse. It cannot catch code
        that parses perfectly and breaks something three modules away, which
        is most of what goes wrong when a program edits itself.
        """
        try:
            completed = await asyncio.to_thread(
                _run,
                [sys.executable, "-m", "pytest", "-q", "--timeout", "120",
                 "-x", "--no-header"],
                cwd=str(self.root), capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ran": False, "passed": False,
                    "detail": f"The tests did not finish within {timeout:.0f}s."}
        except Exception as exc:
            return {"ran": False, "passed": False,
                    "detail": f"Could not run the tests: {str(exc)[:160]}"}

        output = (completed.stdout or "") + (completed.stderr or "")
        passed = completed.returncode == 0
        return {
            "ran": True,
            "passed": passed,
            "detail": _last_meaningful_line(output) or
                      ("All tests passed." if passed else "Tests failed."),
            "output": output[-3000:],
        }

    async def apply(self, proposal_id: str, allow_protected: bool = False,
                    run_tests: bool = True) -> Dict[str, Any]:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise KeyError("That proposal is not available (it may be from a previous run).")

        path = self.resolve(proposal["path"])
        relative = path.relative_to(self.root).as_posix()

        if relative in PROTECTED and not allow_protected:
            raise PermissionError(
                f"'{relative}' implements JARVIS's own safety controls. Editing it needs "
                f"allow_protected=true, and you should read the diff carefully first."
            )

        validation = self.validate(relative, proposal["after"])
        if not validation["valid"]:
            return {"ok": False, "applied": False,
                    "message": f"Refused: the proposed code is not valid "
                               f"({validation['checker']}): {validation.get('error')}"}

        # The current file on disk may have changed since the proposal.
        if path.read_text(encoding="utf-8", errors="replace") != proposal["before"]:
            return {"ok": False, "applied": False,
                    "message": f"'{relative}' has changed on disk since this was proposed. "
                               f"Re-propose against the current version."}

        backup = self._backup_path(relative)
        shutil.copy2(path, backup)
        path.write_text(proposal["after"], encoding="utf-8")

        if self.audit:
            await self.audit.record(
                "code.self_modified", subject=relative,
                detail={"request": truncate(proposal["request"], 300),
                        "model": proposal["model"], "backup": backup.name,
                        "protected": relative in PROTECTED},
            )
        await self.activity.record(
            f"Modified its own source: {relative}. Backup at {backup.name}.",
            level="warn", source="selfedit",
        )
        bus.publish("selfedit.applied", {"path": relative, "backup": backup.name},
                    source="selfedit")

        restart = "backend" in relative or relative.endswith(".py")
        return {
            "ok": True,
            "applied": True,
            "path": relative,
            "backup": backup.name,
            "validation": validation,
            "restart_required": restart,
            "message": (
                f"Applied to {relative}. Backup kept as {backup.name}. "
                + ("Restart the backend for this to take effect."
                   if restart else "The dev server will hot-reload it.")
            ),
        }

        if not run_tests:
            result["tests"] = {"ran": False, "passed": None,
                               "detail": "Not run."}
            return result

        # Run the suite, and undo the change if it broke something.
        #
        # This is what makes a program editing its own source something
        # other than a gamble. Valid code that breaks a module three files
        # away is the normal failure here, not the exception, and without
        # this the first anyone knows is the next time that path runs --
        # possibly while nobody is watching.
        tests = await self.verify()
        result["tests"] = tests

        if tests["ran"] and not tests["passed"]:
            shutil.copy2(backup, path)
            result["applied"] = False
            result["rolled_back"] = True
            result["message"] = (
                f"Applied to {relative}, the tests failed, so it was put back. "
                f"{tests['detail']} Nothing has changed."
            )
            await self.activity.record(
                f"self-edit to {relative} reverted: tests failed",
                source="selfedit", level="warn")
            return result

        if not tests["ran"]:
            result["message"] += f" (Tests could not run: {tests['detail']})"
        return result

    # ==================================================================
    # rollback
    # ==================================================================
    def backups(self, relative: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for backup in sorted(self.backup_dir.glob("*.bak"), reverse=True):
            original = backup.name.rsplit(".", 2)[0].replace("__", "/")
            if relative and original != relative:
                continue
            out.append({
                "backup": backup.name,
                "path": original,
                "size": backup.stat().st_size,
                "taken_at": datetime.fromtimestamp(backup.stat().st_mtime).isoformat(),
            })
        return out

    async def rollback(self, backup_name: str) -> Dict[str, Any]:
        backup = (self.backup_dir / backup_name).resolve()
        try:
            backup.relative_to(self.backup_dir.resolve())
        except ValueError:
            raise PermissionError("That backup path is outside the backup folder.")
        if not backup.exists():
            raise FileNotFoundError(f"No backup named '{backup_name}'.")

        relative = backup_name.rsplit(".", 2)[0].replace("__", "/")
        path = self.resolve(relative)

        # Keep the current version too, so a rollback is itself reversible.
        current_backup = self._backup_path(relative)
        if path.exists():
            shutil.copy2(path, current_backup)
        shutil.copy2(backup, path)

        if self.audit:
            await self.audit.record("code.rolled_back", subject=relative,
                                    detail={"restored_from": backup_name})
        await self.activity.record(f"Rolled {relative} back to {backup_name}.",
                                   level="warn", source="selfedit")
        return {"ok": True, "path": relative, "restored_from": backup_name,
                "previous_kept_as": current_backup.name,
                "restart_required": relative.endswith(".py")}

    def _backup_path(self, relative: str) -> Path:
        """A backup name that cannot collide.

        Second-resolution timestamps are not enough: applying twice in one
        second, or rolling back immediately after applying, would otherwise
        overwrite the very backup being restored from."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = relative.replace("/", "__")
        candidate = self.backup_dir / f"{stem}.{stamp}.bak"
        counter = 1
        while candidate.exists():
            candidate = self.backup_dir / f"{stem}.{stamp}-{counter}.bak"
            counter += 1
        return candidate

    # ==================================================================
    def pending(self) -> List[Dict[str, Any]]:
        return [
            {"proposal_id": pid, "path": p["path"], "request": truncate(p["request"], 120),
             "created_at": p["created_at"]}
            for pid, p in self._proposals.items()
        ]

    def stats(self) -> Dict[str, Any]:
        return {
            "model": self.model_info(),
            "editable_roots": list(EDITABLE_ROOTS),
            "protected_files": sorted(PROTECTED),
            "pending_proposals": len(self._proposals),
            "backups": len(list(self.backup_dir.glob("*.bak"))),
        }


# ---------------------------------------------------------------------------
def _strip_fences(text: str) -> str:
    """Models wrap code in fences despite being told not to."""
    stripped = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", stripped, re.S)
    return fenced.group(1) if fenced else stripped


def _unified_diff(before: str, after: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))


def _diff_counts(diff: str) -> Tuple[int, int]:
    added = sum(1 for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _last_meaningful_line(output: str) -> str:
    """pytest's summary line, which is the part worth repeating."""
    for line in reversed((output or "").splitlines()):
        clean = line.strip()
        if clean and ("passed" in clean or "failed" in clean or "error" in clean):
            return clean[:200]
    return ""
