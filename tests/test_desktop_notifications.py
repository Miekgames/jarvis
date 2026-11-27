"""Reading the notifications this computer has received.

Notification text is some of the most sensitive material on a machine --
message previews, two-factor codes, calendar entries -- so most of what
matters here is about restraint: what it refuses to do, what it refuses to
keep, and what it refuses to write down.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.integrations import desktop_notifications as dn


class _Permissions:
    def __init__(self, allowed=True):
        self._allowed = allowed

    def allowed(self, key):
        assert key == "notifications.read"
        return self._allowed


class _Activity:
    def __init__(self):
        self.entries = []

    async def record(self, message, level="info", source="jarvis", meta=None, task_id=None):
        self.entries.append(message)


class _Audit:
    def __init__(self):
        self.entries = []

    async def record(self, event, subject="", detail=None, **kwargs):
        self.entries.append((event, subject, detail))


def _service(allowed=True):
    activity, audit = _Activity(), _Audit()
    service = dn.DesktopNotificationService(object(), _Permissions(allowed), activity, audit)
    return service, activity, audit


# ---------------------------------------------------------------------------
# text coming out of real toasts
# ---------------------------------------------------------------------------
def test_invisible_characters_are_stripped():
    """Real toasts are full of them. The first one read on this machine had a
    U+2068 FIRST STRONG ISOLATE in the middle, which is harmless in a toast
    and breaks everything downstream that assumes printable text."""
    assert dn.clean_text("Sam⁨ sent a message⁩") == "Sam sent a message"
    assert dn.clean_text("zero​width") == "zerowidth"
    assert dn.clean_text("﻿bom at the front") == "bom at the front"


def test_ordinary_text_is_left_alone():
    assert dn.clean_text("Build #42 finished — 3 warnings") == "Build #42 finished — 3 warnings"
    assert dn.clean_text("  padded  ") == "padded"


def test_empty_input_is_handled():
    assert dn.clean_text(None) == ""
    assert dn.clean_text("") == ""


def test_emoji_and_accents_survive():
    """Stripping invisibles must not become stripping anything non-ASCII."""
    assert dn.clean_text("Café ✅ naïve") == "Café ✅ naïve"


# ---------------------------------------------------------------------------
# restraint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_it_does_not_start_without_the_permission():
    service, _activity, _audit = _service(allowed=False)
    if not service.availability()["available"]:
        pytest.skip("windows notification bindings not installed")
    status = await service.start()
    assert status["running"] is False
    assert status["status"] == "denied"
    assert "permission" in status["detail"].lower()


@pytest.mark.asyncio
async def test_stopping_discards_what_it_held():
    """Switching it off has to mean the content goes too. Keeping someone's
    message previews after they stopped watching is the opposite of what
    stopping means."""
    service, _a, _b = _service()
    service._items = [{"id": "1", "app": "Mail", "title": "secret", "body": "", "at": "x"}]
    service._seen = {"1"}

    await service.stop()
    assert service.recent() == []
    assert service._seen == set()
    assert service.status()["count"] == 0


@pytest.mark.asyncio
async def test_nothing_is_logged_but_the_fact():
    """The activity log and the audit trail are written to disk. Notification
    text is not."""
    service, activity, audit = _service()
    service._items = [{"id": "1", "app": "Signal", "title": "my bank code is 123456",
                       "body": "do not share", "at": "x"}]
    await service.stop()

    written = " ".join(activity.entries) + " " + " ".join(
        f"{e}{s}{d}" for e, s, d in audit.entries)
    assert "123456" not in written
    assert "do not share" not in written
    assert "notification" in written.lower(), "the fact itself should be recorded"


def test_the_status_says_where_the_content_lives():
    """A claim the user should be able to read, not have to take on trust."""
    service, _a, _b = _service()
    note = service.status()["note"].lower()
    assert "memory" in note
    assert "disk" in note or "written" in note


def test_an_unsupported_platform_says_so_rather_than_failing():
    service, _a, _b = _service()
    ready = service.availability()
    assert isinstance(ready["available"], bool)
    if not ready["available"]:
        assert ready["detail"], "an unavailable feature must explain why"


def test_history_is_bounded():
    assert dn.KEEP <= 200, "an unbounded buffer of message previews is the wrong instinct"


def test_polling_is_not_hot():
    assert dn.POLL_SECONDS >= 1.0


@pytest.mark.asyncio
async def test_stopping_when_never_started_is_safe():
    service, _a, _b = _service()
    status = await service.stop()
    assert status["running"] is False


def test_the_permission_exists_and_defaults_off():
    """Reading every notification on the machine is not an on-by-default
    kind of capability."""
    from backend.core.permissions import BY_KEY

    assert "notifications.read" in BY_KEY, "notifications.read must be a real permission"
    match = [BY_KEY["notifications.read"]]
    assert match[0].default is False


# ---------------------------------------------------------------------------
# muting a noisy application
#
# One chatty group chat can bury everything else, and a list nobody reads is
# the same as no list at all.
# ---------------------------------------------------------------------------
class _Store:
    def __init__(self, values=None):
        self.values = values or {}
        self.patched = []

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def patch(self, key, updates, actor="user"):
        self.patched.append((key, updates))
        self.values.setdefault(key, {}).update(updates)
        return self.values[key]


def _with_store(values=None):
    store = _Store(values)
    service = dn.DesktopNotificationService(
        object(), _Permissions(True), _Activity(), _Audit(), store)
    return service, store


@pytest.mark.asyncio
async def test_muting_an_app_hides_it():
    service, _store = _with_store()
    assert service.is_muted("Discord") is False
    await service.set_muted("Discord", True)
    assert service.is_muted("Discord") is True
    assert service.is_muted("discord") is True, "matching must ignore case"


@pytest.mark.asyncio
async def test_muting_drops_what_is_already_held():
    """Muting should take effect on what you are looking at, not only on
    what arrives next."""
    service, _store = _with_store()
    service._items = [
        {"id": "1", "app": "Discord", "title": "ping", "body": "", "at": "x"},
        {"id": "2", "app": "Mail", "title": "invoice", "body": "", "at": "x"},
    ]
    await service.set_muted("Discord", True)
    assert [i["app"] for i in service.recent()] == ["Mail"]


@pytest.mark.asyncio
async def test_unmuting_restores_it():
    service, _store = _with_store()
    await service.set_muted("Discord", True)
    await service.set_muted("Discord", False)
    assert service.is_muted("Discord") is False
    assert service.muted == []


@pytest.mark.asyncio
async def test_the_mute_list_is_remembered():
    service, store = _with_store()
    await service.set_muted("Discord", True)
    assert ("notifications", {"muted_apps": ["discord"]}) in store.patched


@pytest.mark.asyncio
async def test_the_mute_list_is_loaded_on_start():
    service, _store = _with_store({"notifications": {"muted_apps": ["Discord", "Steam"]}})
    await service._load_muted()
    assert service.is_muted("discord") and service.is_muted("STEAM")


@pytest.mark.asyncio
async def test_muting_nothing_is_refused():
    service, _store = _with_store()
    with pytest.raises(ValueError):
        await service.set_muted("   ", True)


@pytest.mark.asyncio
async def test_the_mute_list_is_reported():
    service, _store = _with_store()
    await service.set_muted("Discord", True)
    await service.set_muted("Steam", True)
    assert service.status()["muted"] == ["discord", "steam"]


@pytest.mark.asyncio
async def test_muting_works_without_a_settings_store():
    """It should still mute for this session even if it cannot persist."""
    service, _a, _b = _service()
    await service.set_muted("Discord", True)
    assert service.is_muted("Discord")
