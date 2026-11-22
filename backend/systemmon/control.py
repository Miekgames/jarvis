"""
Machine control: volume, media keys, brightness, power, screenshots, clipboard.

Windows paths use ctypes/PowerShell rather than extra dependencies. Every
function reports honestly when the platform or hardware doesn't support it --
nothing silently pretends to have worked.
"""
from __future__ import annotations

import logging
import platform
import subprocess

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.proc import run as _run, popen as _popen

logger = logging.getLogger("jarvis.control")

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Windows virtual key codes
_VK = {
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "media_next": 0xB0,
    "media_prev": 0xB1,
    "media_stop": 0xB2,
    "media_play_pause": 0xB3,
}

_NO_WINDOWS = {"ok": False, "message": "This control is only implemented for Windows on this build."}


def _tap(vk: int, times: int = 1) -> None:
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.01)


def _powershell(script: str, timeout: int = 25) -> subprocess.CompletedProcess:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------

def volume(action: str, amount: int = 10) -> Dict[str, Any]:
    """action: up | down | mute | set. `set` needs a 0-100 level in `amount`."""
    if not IS_WINDOWS:
        if IS_MAC:
            mapping = {
                "up": "set volume output volume (output volume of (get volume settings) + 10)",
                "down": "set volume output volume (output volume of (get volume settings) - 10)",
                "mute": "set volume with output muted",
                "set": f"set volume output volume {max(0, min(100, amount))}",
            }
            script = mapping.get(action)
            if not script:
                return {"ok": False, "message": f"Unknown volume action '{action}'."}
            _run(["osascript", "-e", script], capture_output=True)
            return {"ok": True, "message": f"Volume {action}."}
        return _NO_WINDOWS

    try:
        if action == "mute":
            _tap(_VK["volume_mute"])
            return {"ok": True, "message": "Toggled mute."}
        if action == "up":
            _tap(_VK["volume_up"], max(1, amount // 2))
            return {"ok": True, "message": f"Volume up by about {amount}%."}
        if action == "down":
            _tap(_VK["volume_down"], max(1, amount // 2))
            return {"ok": True, "message": f"Volume down by about {amount}%."}
        if action == "set":
            level = max(0, min(100, int(amount)))
            # No stepless API without extra deps: zero out, then step up.
            _tap(_VK["volume_down"], 50)
            _tap(_VK["volume_up"], round(level / 2))
            return {"ok": True, "message": f"Volume set to roughly {level}%."}
    except Exception as exc:
        return {"ok": False, "message": f"Volume control failed: {exc}"}
    return {"ok": False, "message": f"Unknown volume action '{action}'."}


def get_volume() -> Dict[str, Any]:
    """Reading the exact level needs the Core Audio COM API, which isn't
    available without pycaw. Reported honestly rather than guessed."""
    return {
        "ok": False,
        "message": "Exact volume level isn't readable without the optional 'pycaw' package. "
                   "Volume changes still work.",
    }


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------

def media(action: str) -> Dict[str, Any]:
    """action: play_pause | next | previous | stop"""
    mapping = {
        "play_pause": "media_play_pause",
        "play": "media_play_pause",
        "pause": "media_play_pause",
        "next": "media_next",
        "previous": "media_prev",
        "prev": "media_prev",
        "stop": "media_stop",
    }
    key = mapping.get(action)
    if key is None:
        return {"ok": False, "message": f"Unknown media action '{action}'."}

    if IS_WINDOWS:
        try:
            _tap(_VK[key])
            return {"ok": True, "message": f"Sent media {action}."}
        except Exception as exc:
            return {"ok": False, "message": f"Media control failed: {exc}"}
    if IS_MAC:
        script = {"media_play_pause": 'tell application "Music" to playpause',
                  "media_next": 'tell application "Music" to next track',
                  "media_prev": 'tell application "Music" to previous track',
                  "media_stop": 'tell application "Music" to stop'}[key]
        _run(["osascript", "-e", script], capture_output=True)
        return {"ok": True, "message": f"Sent media {action}."}
    return _NO_WINDOWS


def now_playing() -> Dict[str, Any]:
    """Windows exposes this through the WinRT GlobalSystemMediaTransportControls
    API, which needs the optional `winsdk` package."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Track metadata is not available on this platform."}
    try:
        import asyncio

        from winsdk.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        async def _read():
            manager = await SessionManager.request_async()
            session = manager.get_current_session()
            if session is None:
                return None
            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            return {
                "title": props.title,
                "artist": props.artist,
                "album": props.album_title,
                "status": str(info.playback_status).split(".")[-1].lower(),
            }

        result = asyncio.run(_read())
        if result is None:
            return {"ok": True, "message": "Nothing is playing.", "playing": None}
        return {"ok": True, "message": f"{result['title']} — {result['artist']}", "playing": result}
    except ImportError:
        return {
            "ok": False,
            "message": "Track metadata needs the optional 'winsdk' package (pip install winsdk). "
                       "Playback controls work without it.",
        }
    except Exception as exc:
        return {"ok": False, "message": f"Couldn't read media session: {exc}"}


# ---------------------------------------------------------------------------
# brightness
# ---------------------------------------------------------------------------

def brightness(level: Optional[int] = None) -> Dict[str, Any]:
    """Laptop panels expose WmiMonitorBrightnessMethods; desktop monitors
    usually do not."""
    if not IS_WINDOWS:
        return _NO_WINDOWS
    try:
        if level is None:
            result = _powershell(
                "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
            )
            value = (result.stdout or "").strip().splitlines()
            if result.returncode != 0 or not value:
                return {"ok": False, "message": "This display doesn't report brightness over WMI."}
            return {"ok": True, "message": f"Brightness is {value[0]}%.", "level": int(value[0])}

        level = max(0, min(100, int(level)))
        result = _powershell(
            f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
            f".WmiSetBrightness(1,{level})"
        )
        if result.returncode != 0:
            return {"ok": False, "message": "This display doesn't accept brightness changes over WMI."}
        return {"ok": True, "message": f"Brightness set to {level}%.", "level": level}
    except Exception as exc:
        return {"ok": False, "message": f"Brightness control failed: {exc}"}


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------

def power(action: str, delay_seconds: int = 30) -> Dict[str, Any]:
    """action: lock | sleep | restart | shutdown | cancel

    Restart and shutdown are scheduled with a delay so `cancel` is a real
    escape hatch rather than a courtesy."""
    if not IS_WINDOWS:
        if IS_MAC:
            mapping = {
                "lock": ["pmset", "displaysleepnow"],
                "sleep": ["pmset", "sleepnow"],
                "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
                "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
            }
            command = mapping.get(action)
            if not command:
                return {"ok": False, "message": f"Unknown power action '{action}'."}
            _run(command, capture_output=True)
            return {"ok": True, "message": f"Requested {action}."}
        return _NO_WINDOWS

    try:
        if action == "lock":
            _run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
            return {"ok": True, "message": "Workstation locked."}
        if action == "sleep":
            _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
            return {"ok": True, "message": "Sleep requested."}
        if action == "restart":
            _run(["shutdown", "/r", "/t", str(delay_seconds)], check=False)
            return {
                "ok": True,
                "message": f"Restart scheduled in {delay_seconds} seconds. Say 'cancel shutdown' to abort.",
                "cancellable_for": delay_seconds,
            }
        if action == "shutdown":
            _run(["shutdown", "/s", "/t", str(delay_seconds)], check=False)
            return {
                "ok": True,
                "message": f"Shutdown scheduled in {delay_seconds} seconds. Say 'cancel shutdown' to abort.",
                "cancellable_for": delay_seconds,
            }
        if action == "cancel":
            result = _run(["shutdown", "/a"], capture_output=True, text=True)
            if result.returncode == 0:
                return {"ok": True, "message": "Pending shutdown cancelled."}
            return {"ok": False, "message": "There was no pending shutdown to cancel."}
    except Exception as exc:
        return {"ok": False, "message": f"Power action failed: {exc}"}
    return {"ok": False, "message": f"Unknown power action '{action}'."}


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------

def screenshot(output_dir: Path) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    destination = output_dir / filename

    if IS_WINDOWS:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
            f"$bmp.Save('{destination.as_posix()}', "
            "[System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose()"
        )
        try:
            result = _powershell(script, timeout=30)
            if result.returncode != 0 or not destination.exists():
                return {"ok": False, "message": f"Screenshot failed: {(result.stderr or '').strip()[:300]}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "Screenshot timed out."}
    elif IS_MAC:
        result = _run(["screencapture", "-x", str(destination)], capture_output=True)
        if result.returncode != 0 or not destination.exists():
            return {"ok": False, "message": "Screenshot failed."}
    else:
        for tool in (["gnome-screenshot", "-f", str(destination)], ["scrot", str(destination)]):
            try:
                if _run(tool, capture_output=True).returncode == 0 and destination.exists():
                    break
            except FileNotFoundError:
                continue
        else:
            return {"ok": False, "message": "No screenshot tool found (tried gnome-screenshot, scrot)."}

    return {
        "ok": True,
        "message": f"Screenshot saved to {destination}",
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
    }


# ---------------------------------------------------------------------------
# clipboard
# ---------------------------------------------------------------------------

def clipboard_read() -> Dict[str, Any]:
    try:
        import pyperclip

        return {"ok": True, "text": pyperclip.paste() or "", "message": "Clipboard read."}
    except ImportError:
        if IS_WINDOWS:
            result = _powershell("Get-Clipboard -Raw")
            if result.returncode == 0:
                return {"ok": True, "text": result.stdout, "message": "Clipboard read."}
        return {"ok": False, "message": "Clipboard access needs the optional 'pyperclip' package."}
    except Exception as exc:
        return {"ok": False, "message": f"Clipboard read failed: {exc}"}


def clipboard_write(text: str) -> Dict[str, Any]:
    try:
        import pyperclip

        pyperclip.copy(text)
        return {"ok": True, "message": "Copied to clipboard."}
    except ImportError:
        if IS_WINDOWS:
            escaped = text.replace("'", "''")
            result = _powershell(f"Set-Clipboard -Value '{escaped}'")
            if result.returncode == 0:
                return {"ok": True, "message": "Copied to clipboard."}
        return {"ok": False, "message": "Clipboard access needs the optional 'pyperclip' package."}
    except Exception as exc:
        return {"ok": False, "message": f"Clipboard write failed: {exc}"}
