"""
Heartbeat / health monitoring.

Checks every subsystem and reports ONLINE / DEGRADED / OFFLINE / ERROR. This is
what powers the status strip, the `run diagnostics` command, and the
self-monitoring loop that notices when a worker has stopped responding.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso, since, utcnow

logger = logging.getLogger("jarvis.health")


class Status:
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    ERROR = "error"


def _check(status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"status": status, "detail": detail, "checked_at": now_iso(), **extra}


class HealthService:
    def __init__(self, services: Dict[str, Any], settings):
        self.services = services
        self.settings = settings
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last: Dict[str, Any] = {}
        self._online = True
        self._restart_attempts: Dict[str, int] = {}

    # ==================================================================
    async def start(self, interval: int = 30) -> None:
        if self._running:
            return
        self._running = True
        # Run one check inline so /status is meaningful the moment boot finishes.
        try:
            self._last = await self.quick_check()
        except Exception:
            logger.exception("initial health check failed")
        self._task = asyncio.create_task(self._loop(interval), name="health-monitor")
        logger.info("health monitor started (overall: %s)", self._last.get("overall", "unknown"))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        while self._running:
            try:
                report = await self.quick_check()
                self._last = report
                bus.publish(Events.HEALTH, report, source="health")
                await self._self_heal(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("health check failed")
            await asyncio.sleep(interval)

    # ==================================================================
    async def _self_heal(self, report: Dict[str, Any]) -> None:
        """Restart our own stopped services. Deliberately limited to JARVIS's
        own subsystems -- never anything on the wider machine."""
        activity = self.services.get("activity")

        for name, key in (("task engine", "tasks"), ("scheduler", "scheduler"),
                          ("metrics sampler", "metrics"), ("calendar watcher", "calendar")):
            check = report["checks"].get(key, {})
            if check.get("status") != Status.OFFLINE:
                self._restart_attempts.pop(key, None)
                continue

            attempts = self._restart_attempts.get(key, 0)
            if attempts >= 3:
                continue
            service = self.services.get(key)
            if service is None or not hasattr(service, "start"):
                continue

            self._restart_attempts[key] = attempts + 1
            logger.warning("%s stopped responding -- restarting (attempt %s)", name, attempts + 1)
            if activity:
                await activity.record(
                    f"{name.capitalize()} stopped responding. Restarting it.", level="warn", source="health"
                )
            try:
                await service.start()
            except Exception:
                logger.exception("failed to restart %s", name)

    # ==================================================================
    async def quick_check(self) -> Dict[str, Any]:
        checks: Dict[str, Any] = {}

        # -- database ------------------------------------------------------
        db = self.services.get("db")
        try:
            started = time.monotonic()
            await db.fetch_value("SELECT 1")
            latency = (time.monotonic() - started) * 1000
            checks["database"] = _check(
                Status.ONLINE if latency < 200 else Status.DEGRADED,
                f"responding in {latency:.0f}ms",
                latency_ms=round(latency, 1),
            )
        except Exception as exc:
            checks["database"] = _check(Status.ERROR, f"query failed: {exc}")

        # -- task engine ---------------------------------------------------
        engine = self.services.get("tasks")
        if engine is None:
            checks["tasks"] = _check(Status.OFFLINE, "not initialised")
        else:
            workers = engine.worker_status()
            alive = sum(1 for w in workers if w["alive"])
            if not engine.running or alive == 0:
                checks["tasks"] = _check(Status.OFFLINE, "no workers running")
            elif alive < len(workers):
                checks["tasks"] = _check(Status.DEGRADED, f"{alive}/{len(workers)} workers alive")
            else:
                checks["tasks"] = _check(Status.ONLINE, f"{alive} workers alive", workers=alive)

        # -- scheduler -----------------------------------------------------
        scheduler = self.services.get("scheduler")
        checks["scheduler"] = (
            _check(Status.ONLINE, "ticking") if scheduler and scheduler.running
            else _check(Status.OFFLINE, "not running")
        )

        # -- automations ---------------------------------------------------
        automations = self.services.get("automations")
        checks["automations"] = (
            _check(Status.ONLINE, "subscribed to the event bus") if automations and automations.running
            else _check(Status.OFFLINE, "not running")
        )

        # -- metrics -------------------------------------------------------
        metrics = self.services.get("metrics")
        if metrics is None:
            checks["metrics"] = _check(Status.OFFLINE, "not initialised")
        elif not metrics.running:
            checks["metrics"] = _check(Status.OFFLINE, "sampler stopped")
        else:
            latest = metrics.latest()
            checks["metrics"] = _check(
                Status.ONLINE if latest else Status.DEGRADED,
                f"sampling every {self.settings.METRICS_INTERVAL_SECONDS}s",
            )

        # -- calendar ------------------------------------------------------
        calendar = self.services.get("calendar")
        checks["calendar"] = (
            _check(Status.ONLINE, "watching for upcoming events") if calendar and calendar.running
            else _check(Status.OFFLINE, "watcher stopped")
        )

        # -- websocket -----------------------------------------------------
        hub = self.services.get("hub")
        if hub is not None:
            count = hub.client_count
            checks["realtime"] = _check(
                Status.ONLINE if count else Status.DEGRADED,
                f"{count} client(s) connected" if count else "no dashboard connected",
                clients=count,
            )

        # -- the AI itself --------------------------------------------------
        #
        # This used to be checked only by `run diagnostics`, so the status
        # strip could read a confident "online" for a system whose entire
        # purpose -- answering -- was unavailable. Every other check here is
        # for plumbing; this is the one the user would actually notice.
        orchestrator = self.services.get("orchestrator")
        if orchestrator is not None:
            try:
                providers = await asyncio.wait_for(
                    orchestrator.provider_status(), timeout=6.0)
                live = [name for name, state in providers.items() if state.get("available")]
                if live:
                    checks["ai"] = _check(Status.ONLINE, "via " + ", ".join(live),
                                          providers=live)
                else:
                    named = ", ".join(providers) or "none configured"
                    checks["ai"] = _check(
                        Status.OFFLINE,
                        f"no AI provider is reachable ({named}). Is Ollama running?")
            except asyncio.TimeoutError:
                checks["ai"] = _check(Status.DEGRADED, "the provider did not answer in time")
            except Exception as exc:
                checks["ai"] = _check(Status.ERROR, f"provider check failed: {exc}")

        # -- voice ----------------------------------------------------------
        voice = self.services.get("voice")
        if voice is not None:
            try:
                checks["voice"] = await voice.health()
            except Exception as exc:
                checks["voice"] = _check(Status.ERROR, f"voice check failed: {exc}")

        # -- wake word ------------------------------------------------------
        #
        # Reported only when it is meant to be running: "off" is a choice,
        # not a fault, and showing it as a red light would train the user to
        # ignore the row.
        hotword = self.services.get("hotword")
        if hotword is not None:
            state = hotword.status()
            if state["running"]:
                phase = state["phase"]
                if phase in ("listening", "recording", "thinking"):
                    checks["wake_word"] = _check(
                        Status.ONLINE, state["detail"], phase=phase)
                elif phase == "silent":
                    # Silence is ambiguous, and the two cases deserve
                    # different answers. A device that has delivered sound
                    # this session and has gone quiet is a quiet room --
                    # marking that degraded would leave the strip amber every
                    # quiet evening until nobody looked at it. A device that
                    # has never delivered anything is deaf, and that is the
                    # failure this subsystem exists to make visible.
                    if state.get("heard_anything"):
                        checks["wake_word"] = _check(
                            Status.ONLINE, "listening (the room is quiet)", phase=phase)
                    else:
                        checks["wake_word"] = _check(
                            Status.DEGRADED, state["detail"], phase=phase)
                elif phase == "failed":
                    checks["wake_word"] = _check(Status.ERROR, state["detail"], phase=phase)
                else:
                    checks["wake_word"] = _check(Status.DEGRADED, state["detail"], phase=phase)

        return {"ts": now_iso(), "checks": checks, "overall": _overall(checks)}

    # ==================================================================
    async def full_check(self) -> Dict[str, Any]:
        """The deeper pass used by `run diagnostics`: also probes the network,
        the AI providers and every configured integration."""
        report = await self.quick_check()
        checks = report["checks"]

        # -- network -------------------------------------------------------
        metrics = self.services.get("metrics")
        online = True
        if metrics is not None:
            try:
                online = await metrics.network_reachable()
            except Exception:
                online = False
        self._online = online
        checks["network"] = (
            _check(Status.ONLINE, "internet reachable") if online
            else _check(Status.OFFLINE, "no internet -- running in degraded/offline mode")
        )

        # -- AI providers --------------------------------------------------
        orchestrator = self.services.get("orchestrator")
        if orchestrator is not None:
            try:
                status = await orchestrator.provider_status()
                available = [name for name, info in status.items() if info["available"]]
                if available:
                    # Local-only is a complete configuration, not a degraded
                    # one: Ollama reachable means the AI layer is fully up.
                    healthy = (self.settings.local_only and "ollama" in available) \
                        or "anthropic" in available
                    checks["ai"] = _check(
                        Status.ONLINE if healthy else Status.DEGRADED,
                        f"available: {', '.join(available)}",
                        providers=status,
                    )
                else:
                    checks["ai"] = _check(Status.OFFLINE, "no AI provider is reachable", providers=status)
            except Exception as exc:
                checks["ai"] = _check(Status.ERROR, f"provider probe failed: {exc}")

        # -- voice ---------------------------------------------------------
        voice = self.services.get("voice")
        if voice is not None:
            checks["voice"] = await voice.health()

        # -- integrations --------------------------------------------------
        integrations = self.services.get("integrations")
        if integrations is not None:
            summary = await integrations.health()
            checks["integrations"] = _check(
                Status.ONLINE if summary["connected"] else Status.DEGRADED,
                f"{summary['connected']} connected, {summary['available']} available but not configured",
                detail_by_key=summary["by_key"],
            )

        # -- disk headroom -------------------------------------------------
        if metrics is not None:
            latest = metrics.latest()
            free = latest.get("disk_free_gb")
            if free is not None:
                checks["storage"] = _check(
                    Status.ONLINE if free > 10 else Status.DEGRADED,
                    f"{free} GB free",
                    free_gb=free,
                )

        report["overall"] = _overall(checks)
        report["checks"] = checks
        return report

    # ==================================================================
    @property
    def online(self) -> bool:
        return self._online

    def last(self) -> Dict[str, Any]:
        return self._last or {"ts": now_iso(), "checks": {}, "overall": "unknown"}


def _overall(checks: Dict[str, Any]) -> str:
    statuses = [c["status"] for c in checks.values()]
    if any(s == Status.ERROR for s in statuses):
        return Status.ERROR
    if any(s == Status.OFFLINE for s in statuses):
        return Status.DEGRADED
    if any(s == Status.DEGRADED for s in statuses):
        return Status.DEGRADED
    return Status.ONLINE
