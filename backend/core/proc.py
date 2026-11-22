"""Running external commands without a console window flashing up.

On Windows, starting a console program from a process that has no console of
its own gives it a brand new one. JARVIS normally runs under `pythonw.exe`
precisely so it has no window, so every such call opened a console, drew it,
and closed it again.

That is invisible for a one-off command and intolerable for a repeating one.
GPU metrics shell out to `nvidia-smi` every three seconds, which meant a
window flashing on the desktop every three seconds for as long as JARVIS was
running -- reported, reasonably, as "the terminal keeps opening and closing
really fast".

`CREATE_NO_WINDOW` suppresses it. The flag exists only on Windows, so these
wrappers add it there and change nothing anywhere else, and everything in
the backend that runs a command should go through them rather than
remembering to pass it.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict

IS_WINDOWS = os.name == "nt"

# 0x08000000. Defined on the subprocess module only on Windows, so it is
# read defensively rather than assumed.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window() -> Dict[str, Any]:
    """Keyword arguments that stop a console appearing. Empty off Windows."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


def run(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess":
    """`subprocess.run`, windowless.

    An explicit `creationflags` passed by the caller wins: some callers
    genuinely want a different set, and silently overriding them would be
    worse than the flashing this exists to prevent.
    """
    if IS_WINDOWS and "creationflags" not in kwargs:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)


def popen(*args: Any, **kwargs: Any) -> "subprocess.Popen":
    """`subprocess.Popen`, windowless.

    Not used for launching applications the user is meant to see -- those
    want their window, and pass their own flags.
    """
    if IS_WINDOWS and "creationflags" not in kwargs:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.Popen(*args, **kwargs)


async def exec_async(*args: Any, **kwargs: Any):
    """`asyncio.create_subprocess_exec`, windowless."""
    import asyncio

    if IS_WINDOWS and "creationflags" not in kwargs:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return await asyncio.create_subprocess_exec(*args, **kwargs)
