"""What may happen while nobody is watching.

Every permission is granted now, and risky tool calls normally stop and ask.
Unattended, there is nobody to ask -- so this decides, per action, between
three answers rather than two:

    ALLOW   do it, write it down
    HOLD    do not do it; record what was wanted and why it was held;
            carry on with everything else
    REFUSE  never, under any circumstances, unattended

HOLD is the whole idea. The obvious designs are both bad: blocking on a
confirmation nobody will answer means the run stops at the first interesting
moment, and auto-approving means the confirmation was never a safeguard at
all, just a speed bump that happened to be absent. Holding lets the work
continue and leaves an honest list of what it would not do alone.

The workspace is the other half. A run is given a directory it owns, and
inside it the agent is free -- create, edit, delete, restructure. That is
the point of leaving it alone for a day. Outside that directory it can read
(within the file roots the rest of JARVIS already enforces) but every change
is held. The distinction is not about trust, it is about blast radius: a
mistake inside the workspace is one folder to throw away.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.autonomy.guard")


class Verdict(str, Enum):
    ALLOW = "allow"
    HOLD = "hold"
    REFUSE = "refuse"


# Tools that are never run unattended, whatever the prompt said.
#
# Not a risk ranking -- these are the ones whose consequences reach outside
# the machine or cannot be undone by deleting a folder. A held action costs
# a few minutes of the owner's time when they get back. An unheld one of
# these can cost an account, a relationship or a disk.
NEVER_UNATTENDED = frozenset({
    # Anything that speaks as the owner.
    "send_email", "send_message", "post_update", "publish",
    # Anything that spends.
    "make_purchase", "transfer_money",
    # Anything that changes who can get in.
    "change_password", "revoke_sessions", "logout_all",
    # Anything that reaches the machine's own configuration.
    "system_shutdown", "system_restart", "system_sleep", "lock_screen",
    "install_package", "uninstall_application",
    # Editing its own source while running unsupervised: the one failure
    # that can stop it being stoppable.
    "selfedit_apply", "selfedit_write", "selfedit_revert",
    # Its own controls. A run that can announce that the owner is back has
    # a way to end itself and clear its own report; one that can send files
    # to the owner's devices is deciding what lands on their phone.
    "welcome_back", "send_built_app_to_device",
})

# Tools whose effects are confined and reversible, and which a run needs in
# order to do anything useful at all.
ALWAYS_SAFE = frozenset({
    "read_file", "list_files", "search_files", "file_preview",
    "web_search", "read_webpage", "live_lookup", "remember", "recall",
    "system_metrics", "list_processes", "wallpaper_options",
    "wallpaper_packs", "wallpaper_estimate",
})

# Paths that are never written to unattended, even inside a workspace that
# somehow contains them. A run that "tidies up" one of these is a run that
# breaks the machine or the assistant.
PROTECTED = (
    "windows", "program files", "programdata", "system32",
    "appdata/roaming/microsoft", "/etc", "/usr", "/bin", "/boot",
    ".ssh", ".aws", ".gnupg", "credentials", "id_rsa",
)

# Files whose contents are secrets. Readable by the owner, not something an
# unattended run should be copying around.
SECRET_NAMES = re.compile(
    r"(^\.env|\.env\.|secrets?\.|credentials?\.|\.pem$|\.key$|token)", re.IGNORECASE)


@dataclass
class Decision:
    verdict: Verdict
    reason: str = ""
    tool: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict.value, "reason": self.reason,
                "tool": self.tool, "detail": self.detail}


class Guard:
    """Decides what an unattended run may do.

    Constructed per session, because the workspace is what most of the
    decisions turn on.
    """

    def __init__(self, workspace: Path, allow_shell: bool = False):
        self.workspace = Path(workspace).resolve()
        # Shell is off unattended unless explicitly turned on for the run.
        # It is the one capability that can do anything at all, which makes
        # it the one where "it was in the workspace" stops being a limit.
        self.allow_shell = allow_shell

    # -- paths -------------------------------------------------------------
    def inside_workspace(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.workspace)
            return True
        except (ValueError, OSError):
            return False

    def is_protected(self, path: Path) -> bool:
        text = str(Path(path)).replace("\\", "/").lower()
        return any(marker in text for marker in PROTECTED)

    def is_secret(self, path: Path) -> bool:
        return bool(SECRET_NAMES.search(Path(path).name))

    # -- the decision ------------------------------------------------------
    def check(self, tool: str, arguments: Optional[Dict[str, Any]] = None,
              risk: str = "medium") -> Decision:
        arguments = arguments or {}
        name = (tool or "").strip()

        if name in NEVER_UNATTENDED:
            return Decision(Verdict.REFUSE, tool=name,
                            reason="This is never done unattended — its "
                                   "consequences reach outside this machine "
                                   "or cannot be undone.")

        # Path checks come before the safe list, not after.
        #
        # `read_file` is on the safe list, and reading is safe -- but only
        # of ordinary files. Checking the list first meant a read of
        # `.env` or `id_rsa` matched "read_file is always safe" and was
        # allowed before anything looked at what was being read. The safe
        # list is about the *verb*; the path still has to be judged.
        path = _path_argument(arguments)
        if path is not None:
            return self._check_path(name, path, arguments)

        if name in ALWAYS_SAFE:
            return Decision(Verdict.ALLOW, tool=name, reason="Read-only or confined.")

        # A plug-in's tool does whatever its author wrote it to, outside
        # anything the workspace confines. Not a call to make alone.
        if name.startswith("mcp_"):
            return Decision(Verdict.HOLD, tool=name,
                            reason="Plug-in tools are not run while nobody is here.")

        if _is_shell(name):
            if not self.allow_shell:
                return Decision(Verdict.HOLD, tool=name,
                                reason="Running shell commands unattended is off "
                                       "for this session.")
            return Decision(Verdict.ALLOW, tool=name,
                            reason="Shell was enabled for this session.")

        if risk in ("high", "critical"):
            return Decision(Verdict.HOLD, tool=name,
                            reason=f"{risk.title()}-risk and nobody is here to confirm it.")

        return Decision(Verdict.ALLOW, tool=name, reason="Ordinary action.")

    def _check_path(self, tool: str, path: Path, arguments: Dict[str, Any]) -> Decision:
        writes = _is_write(tool)
        deletes = _is_delete(tool)

        if self.is_protected(path):
            if writes or deletes:
                return Decision(Verdict.REFUSE, tool=tool,
                                detail={"path": str(path)},
                                reason="That path is part of the operating system "
                                       "or holds credentials.")
            return Decision(Verdict.ALLOW, tool=tool, reason="Reading only.")

        if self.is_secret(path) and not writes and not deletes:
            # Reading a secret is not refused -- plenty of legitimate work
            # needs a config file -- but it is held, because an unattended
            # run has no reason to need one and every reason not to.
            return Decision(Verdict.HOLD, tool=tool, detail={"path": str(path)},
                            reason="That file looks like it holds secrets.")

        if not (writes or deletes):
            return Decision(Verdict.ALLOW, tool=tool, reason="Reading only.")

        if self.inside_workspace(path):
            return Decision(Verdict.ALLOW, tool=tool, detail={"path": str(path)},
                            reason="Inside this session's own workspace.")

        return Decision(
            Verdict.HOLD, tool=tool, detail={"path": str(path)},
            reason=("Deleting" if deletes else "Changing")
                   + " a file outside the session's workspace.")


# ---------------------------------------------------------------------------
_WRITE_HINTS = ("write", "create", "edit", "save", "append", "patch",
                "rename", "move", "copy", "mkdir")
_DELETE_HINTS = ("delete", "remove", "trash", "rmdir", "clear")
_SHELL_HINTS = ("shell", "command", "powershell", "bash", "exec", "terminal")

_PATH_KEYS = ("path", "file", "filename", "target", "destination", "source",
              "directory", "folder")


def _is_write(tool: str) -> bool:
    lowered = (tool or "").lower()
    return any(hint in lowered for hint in _WRITE_HINTS)


def _is_delete(tool: str) -> bool:
    lowered = (tool or "").lower()
    return any(hint in lowered for hint in _DELETE_HINTS)


def _is_shell(tool: str) -> bool:
    lowered = (tool or "").lower()
    return any(hint in lowered for hint in _SHELL_HINTS)


def _path_argument(arguments: Dict[str, Any]) -> Optional[Path]:
    """The filesystem path an action is about, if it names one."""
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value.strip())
    return None
