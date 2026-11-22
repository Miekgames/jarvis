"""
In-process event bus.

Every subsystem publishes here; the WebSocket hub, the automation engine and
the activity log all subscribe. Handlers never block the publisher -- each is
dispatched as its own task and a failing handler can't take down the bus.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .util import now_iso

logger = logging.getLogger("jarvis.events")

Handler = Callable[["Event"], Awaitable[None]]


class Event:
    __slots__ = ("type", "data", "ts", "source")

    def __init__(self, type: str, data: Optional[Dict[str, Any]] = None, source: str = "system"):
        self.type = type
        self.data = data or {}
        self.ts = now_iso()
        self.source = source

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts, "source": self.source}

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Event {self.type} {self.data}>"


class EventBus:
    """Topic subscriptions support exact names ('task.completed'), prefix
    wildcards ('task.*') and the global wildcard ('*')."""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._tasks: Set[asyncio.Task] = set()
        self._history: List[Dict[str, Any]] = []
        self._history_limit = 500

    # ------------------------------------------------------------------
    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        self._handlers[pattern].append(handler)

        def unsubscribe() -> None:
            try:
                self._handlers[pattern].remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def _matching(self, event_type: str) -> List[Handler]:
        out: List[Handler] = list(self._handlers.get("*", []))
        out.extend(self._handlers.get(event_type, []))
        for pattern, handlers in self._handlers.items():
            if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
                out.extend(handlers)
        return out

    # ------------------------------------------------------------------
    def publish(self, event_type: str, data: Optional[Dict[str, Any]] = None, source: str = "system") -> Event:
        """Fire-and-forget. Safe to call from sync code inside the event loop."""
        event = Event(event_type, data, source)
        self._remember(event)
        for handler in self._matching(event_type):
            self._spawn(handler, event)
        return event

    async def publish_and_wait(
        self, event_type: str, data: Optional[Dict[str, Any]] = None, source: str = "system"
    ) -> Event:
        """Publish and wait for every handler to finish. Used by tests and by
        shutdown paths that must not drop events."""
        event = Event(event_type, data, source)
        self._remember(event)
        handlers = self._matching(event_type)
        if handlers:
            await asyncio.gather(*(self._guard(h, event) for h in handlers))
        return event

    # ------------------------------------------------------------------
    def _spawn(self, handler: Handler, event: Event) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("event %s dropped: no running loop", event.type)
            return
        task = loop.create_task(self._guard(handler, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event handler failed for %s", event.type)

    def _remember(self, event: Event) -> None:
        self._history.append(event.to_dict())
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._history[-limit:]

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for in-flight handlers (used on graceful shutdown)."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        done, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()


# Module-level singleton -- imported by every subsystem.
bus = EventBus()


# ---------------------------------------------------------------------------
# Canonical event names. Keeping them in one place stops typos from silently
# breaking automations that subscribe to them.
# ---------------------------------------------------------------------------
class Events:
    # lifecycle
    BOOT_STEP = "boot.step"
    BOOT_COMPLETE = "boot.complete"
    SHUTDOWN = "system.shutdown"

    # jarvis presence -- these drive the particle field
    STATE_CHANGED = "jarvis.state"
    SPEAKING_START = "jarvis.speaking.start"
    SPEAKING_END = "jarvis.speaking.end"
    SPEAKING_STOP = "jarvis.speaking.stop"   # stop talking, now
    LISTENING_START = "jarvis.listening.start"
    LISTENING_END = "jarvis.listening.end"
    SLEEP = "jarvis.sleep"
    WAKE_WORD = "jarvis.wakeword.detected"
    WAKE_WORD_STATE = "jarvis.wakeword.state"
    WAKE_COMMAND = "jarvis.wakeword.command"
    WAKE = "jarvis.wake"

    # conversation
    MESSAGE = "conversation.message"
    MESSAGE_DELTA = "conversation.delta"
    TOOL_START = "conversation.tool.start"
    TOOL_END = "conversation.tool.end"

    # tasks
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_LOG = "task.log"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_PAUSED = "task.paused"
    TASK_RESUMED = "task.resumed"

    # scheduling & automation
    SCHEDULE_FIRED = "schedule.fired"
    AUTOMATION_FIRED = "automation.fired"

    # system
    METRICS = "system.metrics"
    HEALTH = "system.health"
    ALERT = "system.alert"

    # user-facing
    NOTIFICATION = "notification.new"
    DESKTOP_NOTIFICATION = "notification.desktop"
    CONFIRMATION_REQUESTED = "confirmation.requested"
    CONFIRMATION_RESOLVED = "confirmation.resolved"
    ACTIVITY = "activity.new"
    MODE_CHANGED = "mode.changed"
    INTEGRATION_CHANGED = "integration.changed"
    CALENDAR_CHANGED = "calendar.changed"
    TIMER_FIRED = "timer.fired"
