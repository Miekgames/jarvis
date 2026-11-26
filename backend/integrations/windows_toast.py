"""A Windows notification -- the kind that appears in the corner of the screen
and in the Notification Centre -- so a reminder reaches the owner even when no
JARVIS tab is open.

Shown through PowerShell, which every Windows 10/11 machine has, using
Windows' own notification API: nothing to install. The text travels as
base64, never pasted into the script, so a reminder's wording can't break out
of it. Windows names the sender "Windows PowerShell"; the title says JARVIS.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import sys

logger = logging.getLogger("jarvis.toast")

# PowerShell's own app id, which Windows already knows how to show.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Text($b64) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) }
$title = [Security.SecurityElement]::Escape((Text '__TITLE__'))
$body = [Security.SecurityElement]::Escape((Text '__BODY__'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual><actions><action content='OK' arguments='dismiss' activationType='system'/></actions><audio src='ms-winsoundevent:Notification.Reminder'/></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APP__').Show($toast)
"""


def _b64(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def script_for(title: str, body: str) -> str:
    return (_SCRIPT.replace("__TITLE__", _b64(title[:120]))
            .replace("__BODY__", _b64(body[:500])).replace("__APP__", _APP_ID))


async def show(title: str, body: str) -> bool:
    """Show it. Returns whether Windows accepted it; never raises."""
    if sys.platform != "win32":
        return False
    from ..core.proc import run

    encoded = base64.b64encode(script_for(title, body).encode("utf-16-le")).decode("ascii")

    def _run() -> bool:
        try:
            done = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                        "Bypass", "-EncodedCommand", encoded],
                       capture_output=True, text=True, timeout=20)
        except Exception as exc:
            logger.info("toast failed to start: %s", exc)
            return False
        if done.returncode != 0:
            logger.info("toast refused: %s", (done.stderr or "")[:300])
        return done.returncode == 0

    return await asyncio.get_event_loop().run_in_executor(None, _run)
