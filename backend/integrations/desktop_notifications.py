"""
Reading the notifications Windows itself has received.

What the user sees in the Action Center -- a message, a calendar reminder, a
build finishing -- read through `UserNotificationListener`, the API Windows
provides for exactly this. Access is granted by the user through Windows'
own permission prompt, not by this application deciding it may look.

Three things this deliberately does not do.

**It does not store them.** Notification text is some of the most sensitive
material on a machine: message previews, two-factor codes, calendar entries,
anything an app chooses to put in a toast. They are held in memory while they
are current and discarded on shutdown. Nothing reaches the database, nothing
reaches a log file, and the audit trail records *that* notifications were
read, never *what* they said.

**It does not dismiss or act on them.** It reads. Clearing someone's Action
Center as a side effect of looking at it would be its own kind of rude.

**It does not run until asked.** Off by default, behind a permission that
defaults to off, and stoppable at any moment.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any, Callable, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso

logger = logging.getLogger("jarvis.desktop_notifications")

IS_WINDOWS = sys.platform == "win32"

# How often to look. The listener has a change event, but wiring it up needs
# a COM message pump that does not sit comfortably inside an asyncio
# application; polling costs almost nothing and cannot wedge the loop.
POLL_SECONDS = 4.0

# Nobody needs an unbounded history of notifications in memory, and this is
# the kind of content where "unbounded" is the wrong instinct anyway.
KEEP = 60

# Characters Windows and the apps feeding it sprinkle through toast text:
# directional isolates, zero-width joiners, the lot. They are invisible, they
# break terminals, and they are no use to anyone reading the result.
_INVISIBLE = re.compile(r"[​-‏ -‮⁠-⁤⁦-⁩﻿]")


def clean_text(text: Optional[str]) -> str:
    """Strip the invisible characters out of notification text.

    Real toasts are full of them -- the first one read on this machine had a
    U+2068 FIRST STRONG ISOLATE in the middle of it, which is harmless in a
    toast and breaks everything downstream that assumes printable text.
    """
    if not text:
        return ""
    return _INVISIBLE.sub("", text).strip()


class DesktopNotificationService:
    """Watches the Windows Action Center and reports what arrives."""

    def __init__(self, settings, permissions, activity, audit=None, settings_store=None):
        self.settings = settings
        self.permissions = permissions
        self.activity = activity
        self.audit = audit
        self.settings_store = settings_store
        # Apps whose notifications are not worth surfacing. One chatty group
        # chat can bury everything else, and a list nobody reads is the same
        # as no list at all.
        self._muted: set = set()

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._items: List[Dict[str, Any]] = []
        self._seen: set = set()
        self._status = "off"
        self._detail = "Not started."
        self._listener = None

    # -- state -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "status": self._status,
            "detail": self._detail,
            "supported": IS_WINDOWS,
            "available": self.availability()["available"],
            "permitted": self._permitted(),
            "count": len(self._items),
            "muted": self.muted,
            "poll_seconds": POLL_SECONDS,
            "note": ("Read from the Windows Action Center and held in memory only. "
                     "Nothing is written to disk and no notification text is logged."),
        }

    def availability(self) -> Dict[str, Any]:
        if not IS_WINDOWS:
            return {"available": False,
                    "detail": "Reading desktop notifications needs Windows."}
        try:
            import winrt.windows.ui.notifications.management  # noqa: F401
        except ImportError:
            return {
                "available": False,
                "detail": "Needs the Windows notification bindings. Install with: "
                          "pip install winrt-Windows.UI.Notifications.Management "
                          "winrt-Windows.UI.Notifications winrt-Windows.ApplicationModel",
            }
        return {"available": True, "detail": "Ready."}

    def _permitted(self) -> bool:
        try:
            return bool(self.permissions.allowed("notifications.read"))
        except Exception:
            return False

    def recent(self, limit: int = 40) -> List[Dict[str, Any]]:
        return self._items[:limit]

    # -- muting ------------------------------------------------------------
    @property
    def muted(self) -> List[str]:
        return sorted(self._muted)

    def is_muted(self, app: str) -> bool:
        return (app or "").strip().lower() in self._muted

    async def set_muted(self, app: str, muted: bool) -> Dict[str, Any]:
        """Mute or unmute one application.

        Muting drops its notifications on the way in, so nothing from a muted
        app is held in memory at all -- it is not a display filter over
        content kept anyway.
        """
        key = (app or "").strip().lower()
        if not key:
            raise ValueError("No application given.")

        if muted:
            self._muted.add(key)
            # Drop what is already held, so muting takes effect on what you
            # are looking at rather than only on what arrives next.
            self._items = [i for i in self._items if i["app"].strip().lower() != key]
        else:
            self._muted.discard(key)

        if self.settings_store is not None:
            try:
                await self.settings_store.patch(
                    "notifications", {"muted_apps": sorted(self._muted)}, actor="user")
            except Exception:
                logger.debug("could not persist the mute list", exc_info=True)
        await self._record("{} notifications from {}".format(
            "muted" if muted else "unmuted", app))
        return self.status()

    async def _load_muted(self) -> None:
        if self.settings_store is None:
            return
        try:
            stored = await self.settings_store.get("notifications", {}) or {}
            self._muted = {str(a).strip().lower()
                           for a in stored.get("muted_apps", []) if str(a).strip()}
        except Exception:
            logger.debug("could not read the mute list", exc_info=True)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> Dict[str, Any]:
        if self._running:
            return self.status()

        ready = self.availability()
        if not ready["available"]:
            self._status, self._detail = "unavailable", ready["detail"]
            return self.status()

        if not self._permitted():
            self._status = "denied"
            self._detail = ("The 'Read desktop notifications' permission is off. "
                            "Turn it on in Settings -> Permissions.")
            return self.status()

        await self._load_muted()
        granted, detail = await self._request_access()
        if not granted:
            self._status, self._detail = "denied", detail
            return self.status()

        self._running = True
        self._status = "watching"
        self._detail = "Watching the Windows Action Center."
        self._task = asyncio.create_task(self._loop(), name="desktop-notifications")

        await self._record("started watching desktop notifications")
        return self.status()

    async def stop(self) -> Dict[str, Any]:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

        # The content goes with the watching. Keeping someone's message
        # previews around after they switched it off would be the opposite of
        # what switching it off means.
        self._items = []
        self._seen = set()
        self._status = "off"
        self._detail = "Stopped."
        await self._record("stopped watching desktop notifications")
        return self.status()

    # -- the windows side --------------------------------------------------
    async def _request_access(self) -> "tuple[bool, str]":
        """Ask Windows. The user answers this, not us."""
        try:
            from winrt.windows.ui.notifications.management import UserNotificationListener

            self._listener = UserNotificationListener.current
            status = await self._listener.request_access_async()
            name = getattr(status, "name", str(status)).upper()
            if "ALLOWED" in name:
                return True, "Windows granted access."
            return False, (
                "Windows did not grant access to notifications ({}). Allow it under "
                "Settings -> Privacy & security -> Notifications.".format(name))
        except Exception as exc:
            return False, "Could not reach the Windows notification listener: {}".format(exc)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient failure here should not take the watcher down;
                # it should say so and keep trying.
                self._status = "degraded"
                self._detail = "Last read failed: {}".format(str(exc)[:160])
                logger.debug("notification poll failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    async def _poll(self) -> None:
        from winrt.windows.ui.notifications import NotificationKinds

        found = await self._listener.get_notifications_async(NotificationKinds.TOAST)
        current = []
        fresh = []

        for item in found:
            parsed = self._parse(item)
            if parsed is None:
                continue
            if self.is_muted(parsed["app"]):
                continue          # dropped on the way in, never held
            current.append(parsed["id"])
            if parsed["id"] in self._seen:
                continue
            self._seen.add(parsed["id"])
            fresh.append(parsed)

        # Forget ids that have left the Action Center, so a notification the
        # user dismissed and then received again is reported again.
        self._seen &= set(current)

        if not fresh:
            if self._status == "degraded":
                self._status, self._detail = "watching", "Watching the Windows Action Center."
            return

        self._items = fresh + self._items
        del self._items[KEEP:]
        self._status, self._detail = "watching", "Watching the Windows Action Center."

        for entry in fresh:
            # The event carries the content to the interface over the local
            # websocket. The activity log gets the fact and the source only --
            # it is written to disk, and notification text is not.
            bus.publish(Events.DESKTOP_NOTIFICATION, entry, source="desktop")
        await self._record(
            "{} desktop notification(s) from {}".format(
                len(fresh), ", ".join(sorted({e["app"] for e in fresh}) )))

    def _parse(self, item) -> Optional[Dict[str, Any]]:
        """One notification, reduced to what can safely be shown."""
        try:
            app = "Unknown app"
            try:
                info = item.app_info
                if info is not None:
                    app = clean_text(info.display_info.display_name) or app
            except Exception:
                pass

            texts: List[str] = []
            try:
                binding = item.notification.visual.get_binding("ToastGeneric")
                if binding is not None:
                    for element in binding.get_text_elements():
                        cleaned = clean_text(element.text)
                        if cleaned:
                            texts.append(cleaned)
            except Exception:
                pass

            if not texts:
                return None

            when = now_iso()
            try:
                when = item.creation_time.isoformat()
            except Exception:
                pass

            return {
                "id": str(getattr(item, "id", "")) or f"{app}:{texts[0][:40]}:{when}",
                "app": app,
                "title": texts[0][:200],
                "body": " ".join(texts[1:])[:600],
                "at": when,
            }
        except Exception:
            logger.debug("could not parse a notification", exc_info=True)
            return None

    # -- logging -----------------------------------------------------------
    async def _record(self, message: str) -> None:
        """Deliberately records the fact, never the content."""
        if self.activity is not None:
            try:
                await self.activity.record(message, source="desktop", level="info")
            except Exception:
                logger.debug("activity log failed", exc_info=True)
        if self.audit is not None:
            try:
                await self.audit.record("notifications.desktop", subject=message)
            except Exception:
                logger.debug("audit failed", exc_info=True)
