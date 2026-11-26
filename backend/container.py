"""
Service container: builds every subsystem, wires them together, and runs the
startup and shutdown sequences.

The boot sequence publishes a `boot.step` event per stage, which is what the
frontend renders as:

    SYSTEM INITIALISING...
    DATABASE ........ ONLINE
    AI CORE ......... ONLINE
    TASK ENGINE ..... ONLINE
    ...
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ai.memory import MemoryStore
from .ai.orchestrator import Orchestrator
from .ai.tools import build_registry
from .automations.actions import ActionRunner
from .automations.engine import AutomationEngine
from .config import settings as default_settings
from .core.audit import AuditLog
from .core.confirmations import ConfirmationService
from .core.events import Events, bus
from .core.logs import ActivityLog
from .core.notifications import NotificationService
from .core.permissions import PermissionService
from .core.security import AuthService
from .core.util import humanize_duration, now_iso
from .core.ws import hub
from .db.database import Database
from .db.settings_store import SettingsStore
from .integrations.registry import IntegrationRegistry
from .intelligence.agents import AgentOrchestrator
from .intelligence.awareness import AwarenessService
from .intelligence.goals2 import GoalPlanner
from .intelligence.memory2 import MemoryManager
from .intelligence.research import ResearchService
from .intelligence.code import CodeIntelligence
from .intelligence.devtools import DevTools
from .intelligence.selfedit import SelfEditService
from .intelligence.resolver import ContextResolver
from .intelligence.gaming import GamingService
from .intelligence.goals import GoalService
from .intelligence.patterns import PatternLearner
from .intelligence.planning import PlanningService
from .intelligence.proactive import ProactiveEngine
from .intelligence.reasoning import ReasoningService
from .intelligence.vision import VisionService
from .intelligence.watchers import WatcherService
from .intelligence.codeaudit import CodeAudit
from .scheduler.service import SchedulerService
from .services.clipboard import ClipboardService
from .services.customise import CustomiseService
from .services.devices import DeviceService
from .services.fileops import FileOpsService
from .services.focus import FocusService
from .services.notes import NotesService
from .services.netdiag import NetDiagService
from .services.reading import ReadingService
from .services.search import SearchService
from .services import themes as themes_module
from .services.calendar import CalendarService
from .services.files import FileService
from .services.modes import ModeService
from .services.presence import PresenceService
from .services.weather import WeatherService
from .services.web import WebService
from .systemmon.health import HealthService
from .systemmon.metrics import MetricsService
from .tasks.engine import TaskEngine
from .tasks.handlers import register_all as register_task_handlers
from .business.service import BusinessService
from .integrations.desktop_notifications import DesktopNotificationService
from .voice.hotword import HotwordService
from .voice.service import VoiceService

logger = logging.getLogger("jarvis.container")


BOOT_STEPS = [
    ("database", "DATABASE"),
    ("security", "SECURITY"),
    ("ai", "AI CORE"),
    ("tools", "TOOL SYSTEM"),
    ("tasks", "TASK ENGINE"),
    ("scheduler", "SCHEDULER"),
    ("automations", "AUTOMATION"),
    ("monitoring", "MONITORING"),
    ("awareness", "AWARENESS"),
    ("intelligence", "INTELLIGENCE"),
    ("voice", "VOICE"),
    ("integrations", "INTEGRATIONS"),
]


class Container:
    def __init__(self, settings=None):
        self.settings = settings or default_settings
        self.services: Dict[str, Any] = {}
        self.started = False
        # Identifies this run of the backend. The interface compares it on
        # every reconnect: a page left open across a restart is running code
        # older than the server, and in this project that has already meant a
        # stale tab submitting a command the backend now handles itself.
        self.instance_id = now_iso()
        self.boot_log: List[Dict[str, Any]] = []
        self._boot_started = 0.0
        self._last_step_at = 0.0

    # ==================================================================
    def __getattr__(self, name: str) -> Any:
        services = self.__dict__.get("services", {})
        if name in services:
            return services[name]
        raise AttributeError(name)

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    # ==================================================================
    def _step(self, key: str, label: str, status: str, detail: str = "") -> None:
        now = time.monotonic()
        elapsed_ms = int((now - (self._last_step_at or self._boot_started)) * 1000)
        self._last_step_at = now

        entry = {"key": key, "label": label, "status": status, "detail": detail,
                 "elapsed_ms": elapsed_ms, "ts": now_iso()}
        self.boot_log.append(entry)
        bus.publish(Events.BOOT_STEP, entry, source="boot")
        symbol = {"online": "ONLINE", "degraded": "DEGRADED",
                  "offline": "OFFLINE"}.get(status, status.upper())
        logger.info("%-16s %s  %5dms%s", label, symbol, elapsed_ms,
                    f"  ({detail})" if detail else "")

    # ==================================================================
    async def start(self) -> Dict[str, Any]:
        if self.started:
            return {"already_running": True}
        self._boot_started = time.monotonic()
        self._last_step_at = self._boot_started
        logger.info("SYSTEM INITIALISING...")

        # ---- database ------------------------------------------------
        db = Database(self.settings.DB_PATH)
        await db.connect()
        self.services["db"] = db
        self.services["settings"] = self.settings

        settings_store = SettingsStore(db)
        await settings_store.bootstrap()
        self.services["settings_store"] = settings_store

        activity = ActivityLog(db)
        self.services["activity"] = activity
        version = await db.schema_version()
        self._step("database", "DATABASE", "online", f"schema v{version}")

        # ---- security ------------------------------------------------
        audit = AuditLog(db)
        auth = AuthService(db, self.settings, audit)
        await auth.ensure_owner()
        await auth.purge_expired()

        permissions = PermissionService(db, audit)
        await permissions.bootstrap()

        confirmations = ConfirmationService(db, audit, settings_store)
        await confirmations.expire_stale()

        self.services.update({"audit": audit, "auth": auth, "permissions": permissions,
                              "confirmations": confirmations})
        enabled = sum(1 for p in await permissions.list() if p["enabled"])
        self._step("security", "SECURITY", "online",
                   f"{self.settings.AUTH_MODE} auth, {enabled} permissions enabled")

        # ---- supporting services ------------------------------------
        notifications = NotificationService(db, settings_store)
        files = FileService(self.settings)
        web = WebService(self.settings)
        weather = WeatherService(self.settings, settings_store)
        calendar = CalendarService(db, activity, notifications)
        memory = MemoryStore(db, settings_store)
        self.services.update({
            "notifications": notifications, "files": files, "web": web,
            "weather": weather, "calendar": calendar, "memory": memory,
        })

        # ---- AI ------------------------------------------------------
        registry = build_registry(self.services)
        self.services["registry"] = registry

        orchestrator = Orchestrator(db, self.settings, settings_store, registry, memory, activity)
        self.services["orchestrator"] = orchestrator
        # The tool context is built from the same dict, so late additions are visible.
        registry.context._services.update(self.services)
        for key, value in self.services.items():
            setattr(registry.context, key, value)

        if self.settings.local_only:
            model = self.settings.OLLAMA_MODEL or "auto-selected per purpose"
            self._step("ai", "AI CORE", "online", f"local only via Ollama ({model})")

            # Load the weights now, in the background. Ollama reads 4.4 GB
            # off disk the first time a model is used, which measured 12.0s
            # against 0.04s once resident -- and without this that cost lands
            # on whatever the user happens to ask first.
            async def _warm() -> None:
                try:
                    provider = orchestrator.providers.get("ollama")
                    if provider is None:
                        return
                    result = await provider.warm()
                    logger.info("model warmup: %s", result.get("detail", ""))
                except Exception:
                    logger.debug("model warmup failed", exc_info=True)

            asyncio.create_task(_warm(), name="ollama-warmup")
        else:
            # The provider chosen in setup (with the owner's own key) comes
            # first; an ANTHROPIC_API_KEY in .env is the older route.
            chosen = orchestrator.cloud_config()
            provider = chosen.get("provider") or ""
            if provider and provider != "ollama" and (
                    orchestrator.keys.get(provider) or provider == "custom"):
                self._step("ai", "AI CORE", "online",
                           f"{provider} · {chosen.get('model') or 'no model chosen'}")
            elif provider == "ollama":
                self._step("ai", "AI CORE", "online", "local models (Ollama)")
            elif self.settings.has_ai_provider():
                self._step("ai", "AI CORE", "online", f"primary: {self.settings.MODEL_PRIMARY}")
            else:
                self._step("ai", "AI CORE", "degraded",
                           "no AI chosen yet -- finish setup, or install Ollama for local models")

        self._step("tools", "TOOL SYSTEM", "online", f"{len(registry.names())} tools registered")

        # ---- task engine --------------------------------------------
        tasks = TaskEngine(db, self.settings, activity, notifications, self.services)
        register_task_handlers(tasks)
        self.services["tasks"] = tasks
        await tasks.start()
        recovery = await tasks.stats()
        self._step("tasks", "TASK ENGINE", "online",
                   f"{self.settings.WORKER_COUNT} workers, {recovery['active']} active")

        # ---- actions, scheduler, automations ------------------------
        actions = ActionRunner(self.services)
        self.services["actions"] = actions

        scheduler = SchedulerService(db, self.settings, activity, actions, notifications)
        self.services["scheduler"] = scheduler
        await scheduler.start()
        schedule_stats = await scheduler.stats()
        self._step("scheduler", "SCHEDULER", "online", f"{schedule_stats['enabled']} enabled")

        metrics = MetricsService(db, self.settings, settings_store, activity, notifications)
        self.services["metrics"] = metrics

        automations = AutomationEngine(db, activity, actions, settings_store, metrics)
        self.services["automations"] = automations
        await automations.start()
        automation_count = int(await db.fetch_value(
            "SELECT COUNT(*) FROM automations WHERE enabled = 1", default=0))
        self._step("automations", "AUTOMATION", "online", f"{automation_count} enabled")

        # ---- modes & presence ----------------------------------------
        modes = ModeService(db, settings_store, activity, actions)
        await modes.bootstrap()
        self.services["modes"] = modes

        presence = PresenceService(db, self.settings, settings_store, activity, orchestrator, metrics)
        self.services["presence"] = presence

        # ---- monitoring ----------------------------------------------
        await metrics.start()
        await calendar.start()
        self._step("monitoring", "MONITORING", "online" if metrics.running else "degraded",
                   f"sampling every {self.settings.METRICS_INTERVAL_SECONDS}s"
                   if metrics.running else "psutil unavailable")

        # ---- ambient awareness ----------------------------------------
        awareness = AwarenessService(db, self.settings, settings_store, activity,
                                     notifications, metrics)
        self.services["awareness"] = awareness
        await awareness.start()

        watchers = WatcherService(db, self.settings, settings_store, activity, files, metrics)
        self.services["watchers"] = watchers
        await watchers.start()

        gaming = GamingService(db, settings_store, activity, awareness, metrics, modes,
                               notifications, orchestrator)
        self.services["gaming"] = gaming
        await gaming.start()

        self._step(
            "awareness", "AWARENESS", "online" if awareness.running else "degraded",
            f"tracking focus, {len(await watchers.watched_folders())} watched folder(s)"
            if awareness.running else "psutil unavailable",
        )

        # ---- intelligence ---------------------------------------------
        patterns = PatternLearner(db, settings_store, activity)
        reasoning = ReasoningService(db, settings_store, activity, awareness, patterns)
        vision = VisionService(db, self.settings, settings_store, orchestrator, activity, awareness)
        planning = PlanningService(db, settings_store, calendar, tasks, orchestrator,
                                   awareness, notifications)
        goals = GoalService(db, orchestrator, tasks, activity, reasoning)
        devices = DeviceService(db, self.settings, activity, audit)
        await devices.bootstrap()

        self.services.update({
            "patterns": patterns, "reasoning": reasoning, "vision": vision,
            "planning": planning, "goals": goals, "devices": devices,
        })

        # ---- Update 3: the operating environment -----------------------
        agent_orchestrator = AgentOrchestrator(db, orchestrator, registry, permissions,
                                               activity, notifications, reasoning)
        await agent_orchestrator.recover()

        research = ResearchService(db, orchestrator, web, activity, tasks, notifications)
        memory2 = MemoryManager(db, memory, orchestrator, activity)
        await memory2.expire()

        goal_planner = GoalPlanner(db, orchestrator, tasks, activity, goals, scheduler,
                                   notifications, reasoning, planning)
        resolver = ContextResolver(db, awareness, tasks, goals, calendar, planning, research)
        selfedit = SelfEditService(db, self.settings, orchestrator, activity, audit, reasoning)
        code = CodeIntelligence(db, self.settings, orchestrator, activity, files)
        devtools = DevTools(db, self.settings, orchestrator, activity, code, audit)

        self.services.update({
            "agent_orchestrator": agent_orchestrator, "research": research,
            "memory2": memory2, "goal_planner": goal_planner, "resolver": resolver, "selfedit": selfedit,
            "code": code, "devtools": devtools,
        })
        orchestrator.attach_resolver(resolver)

        proactive = ProactiveEngine(db, settings_store, activity, notifications, self.services)
        self.services["proactive"] = proactive
        await proactive.expire_stale()
        await proactive.start()

        # The orchestrator gained context sources, so let it see them.
        orchestrator.attach_intelligence(awareness=awareness, patterns=patterns,
                                         reasoning=reasoning)

        awareness_settings = await settings_store.get("awareness", {})
        self._step(
            "intelligence", "INTELLIGENCE", "online",
            f"learning {'on' if awareness_settings.get('learning', True) else 'off'}, "
            f"proactive {'on' if awareness_settings.get('proactive') else 'off'}, "
            f"{len(await devices.list())} device(s)",
        )

        # ---- voice ---------------------------------------------------
        voice = VoiceService(self.settings, settings_store, activity)
        self.services["voice"] = voice
        voice_health = await voice.health()
        self._step("voice", "VOICE", voice_health["status"], voice_health["detail"])

        # Local always-on wake word. Built here but deliberately NOT started:
        # continuous microphone capture waits for the user to ask for it.
        hotword = HotwordService(self.settings, settings_store, voice, activity, audit)
        hotword.run_command = self._run_spoken_command
        self.services["hotword"] = hotword

        # Reading the machine's own notifications. Built, never auto-started:
        # it needs a permission that defaults to off, and Windows asks the
        # user separately on top of that.
        desktop = DesktopNotificationService(self.settings, permissions, activity,
                                             audit, settings_store)
        self.services["desktop_notifications"] = desktop

        # The workshop: generating wallpapers and packaging them. Entirely
        # local -- it writes images into its own directory and reaches no
        # network at all, which a test enforces. Cheap to construct, so
        # there is nothing to defer here.
        self.services["business"] = BusinessService(self.settings, activity, audit)

        # Sending files to the owner's other devices over Tailscale. Not part
        # of the workshop: it is simply how a wallpaper gets onto a phone.
        from .integrations.taildrop import Taildrop
        self.services["taildrop"] = Taildrop(self.settings, activity, audit)

        # Working unattended. Every permission is granted now, so the hold
        # queue in here is the principal safeguard rather than a backstop.
        from .autonomy.service import AutonomyService
        self.services["autonomy"] = AutonomyService(
            self.settings, orchestrator, registry, activity, audit)

        # Skills: SKILL.md know-how, bundled and the owner's own.
        from .skills.library import SkillLibrary
        self.services["skills"] = SkillLibrary(
            Path(self.settings.DATA_DIR) / "skills", web=self.services.get("web"))

        # The owner's documents, searchable. Kept current in the background.
        from .documents.index import DocumentIndex
        documents = DocumentIndex(db, self.services.get("files"), activity)
        self.services["documents"] = documents
        asyncio.create_task(documents.refresh_all(), name="documents-refresh")

        # The owner's mailbox, if they connected one (IMAP/SMTP with an app
        # password, sealed with DPAPI). Nothing touches the network here.
        from .integrations.email_account import EmailAccount
        self.services["email"] = EmailAccount(Path(self.settings.DATA_DIR), activity, audit)

        # Countdowns on the main screen, and the news behind each.
        from .services.countdowns import Countdowns
        self.services["countdowns"] = Countdowns(
            db, web=self.services.get("web"), activity=activity, settings=self.settings)

        # MCP plug-ins: connected in the background, so a slow or broken
        # server never holds up start-up.
        from .integrations.mcp_manager import MCPManager
        mcp = MCPManager(Path(self.settings.DATA_DIR), registry, activity)
        self.services["mcp"] = mcp
        asyncio.create_task(mcp.start(), name="mcp-start")

        # Off the loop: the probe is an import, and `openwakeword` takes the
        # better part of fifteen seconds to load from this disk. Done inline
        # it blocks uvicorn from answering its own health check, which is how
        # a backend that was starting perfectly well got killed by the
        # launcher for "failing to start".
        wake_ready = await asyncio.get_running_loop().run_in_executor(
            None, hotword.availability)
        voice_prefs = await settings_store.get("voice", {}) or {}
        if wake_ready["available"] and voice_prefs.get("wake_word_enabled"):
            # Warm speech-to-text in the background. The first command after
            # the wake word would otherwise stall on a model load, which from
            # the outside is indistinguishable from the wake word not working.
            if voice_prefs.get("local_command_capture", True):
                asyncio.create_task(voice.preload())
            await hotword.start()
            state = hotword.status()
            self._step("hotword", "WAKE WORD",
                       "online" if state["phase"] in ("listening", "starting") else "degraded",
                       state["detail"])
        else:
            self._step("hotword", "WAKE WORD",
                       "offline" if wake_ready["available"] else "degraded",
                       "Ready, not listening. Turn it on in Settings -> Voice."
                       if wake_ready["available"] else wake_ready["detail"])

        # ---- integrations --------------------------------------------
        integrations = IntegrationRegistry(db, self.settings, activity, audit)
        integrations.build(weather_service=weather, web_service=web,
                           email_account=self.services.get("email"))
        self.services["integrations"] = integrations
        # DevTools was built before the integration registry existed; give it
        # the reference now so CI status works.
        devtools.integrations = integrations
        health_summary = await integrations.bootstrap(probe=True)
        self._step(
            "integrations", "INTEGRATIONS", "online",
            f"{len(integrations.keys())} registered, {health_summary['available']} need credentials "
            f"(probing in the background)",
        )

        # ---- personal operating layer --------------------------------
        notes = NotesService(db, activity, orchestrator)
        self.services["notes"] = notes

        clipboard = ClipboardService(db, settings_store, activity)
        self.services["clipboard"] = clipboard
        await clipboard.start()

        focus = FocusService(db, activity, notifications)
        self.services["focus"] = focus
        awareness.focus = focus

        fileops = FileOpsService(db, self.settings, activity, confirmations)
        self.services["fileops"] = fileops

        netdiag = NetDiagService(self.settings, activity)
        self.services["netdiag"] = netdiag

        reading = ReadingService(db, web, orchestrator, activity)
        self.services["reading"] = reading

        customise = CustomiseService(db, settings_store, themes_module)
        self.services["customise"] = customise







        codeaudit = CodeAudit(code, activity, orchestrator)
        self.services["codeaudit"] = codeaudit

        search = SearchService(self.services, db)
        self.services["search"] = search

        habit_count = len(await focus.habits())
        note_count = (await notes.stats())["notes"]
        self._step(
            "personal", "PERSONAL LAYER", "online",
            f"{note_count} note(s), {habit_count} habit(s), "
            f"clipboard {'capturing' if clipboard.running else 'idle'}",
        )

        # ---- websocket + health --------------------------------------
        hub.start()
        self.services["hub"] = hub

        health = HealthService(self.services, self.settings)
        self.services["health"] = health
        await health.start()

        # Late wiring: services created after the registry context was built.
        registry.context._services.update(self.services)
        for key, value in self.services.items():
            setattr(registry.context, key, value)

        # ---- done -----------------------------------------------------
        elapsed = time.monotonic() - self._boot_started
        self.started = True
        summary = {
            "steps": self.boot_log,
            "elapsed_seconds": round(elapsed, 2),
            "tools": len(registry.names()),
            "task_types": tasks.handler_types(),
        }
        bus.publish(Events.BOOT_COMPLETE, summary, source="boot")
        await activity.record(
            f"JARVIS online in {elapsed:.1f}s ({len(registry.names())} tools, "
            f"{self.settings.WORKER_COUNT} workers).",
            source="boot",
        )
        logger.info("JARVIS ........... ONLINE  (%.1fs)", elapsed)
        return summary

    # ==================================================================
    async def _run_spoken_command(self, text: str) -> None:
        """Run a command the wake word heard, here rather than in the browser.

        `chat()` is an async generator that publishes each message to the
        event bus as it goes, so draining it is enough: every connected page
        renders the exchange, and a page does not have to be open at all.
        """
        orchestrator = self.get("orchestrator")
        if orchestrator is None:
            logger.warning("spoken command dropped: no orchestrator")
            return

        # Continue the conversation already in play. Without this every
        # spoken command opened a fresh thread with no history, so JARVIS
        # repeated itself and could not refer back to anything -- including
        # what it had just said a moment earlier.
        conversation_id = await orchestrator.current_conversation()
        async for _event in orchestrator.chat(
                text, conversation_id=conversation_id, source="wake_word"):
            pass

    async def stop(self) -> None:
        if not self.started:
            return
        logger.info("shutting down...")
        bus.publish(Events.SHUTDOWN, {"at": now_iso()}, source="system")

        desktop = self.get("desktop_notifications")
        if desktop is not None:
            try:
                await desktop.stop()
            except Exception:
                logger.exception("error stopping the notification watcher")

        mcp = self.get("mcp")
        if mcp is not None:
            try:
                await mcp.stop()
            except Exception:
                logger.exception("error stopping MCP plug-ins")

        hotword = self.get("hotword")
        if hotword is not None:
            try:
                await hotword.shutdown()
            except Exception:
                logger.exception("error stopping the wake word detector")

        confirmations = self.get("confirmations")
        if confirmations:
            confirmations.cancel_all()

        for name in ("proactive", "gaming", "watchers", "awareness",
                     "health", "calendar", "metrics", "automations", "scheduler"):
            service = self.get(name)
            if service is not None:
                try:
                    await service.stop()
                except Exception:
                    logger.exception("error stopping %s", name)

        tasks = self.get("tasks")
        if tasks is not None:
            await tasks.stop(grace=8.0)

        await hub.close_all()
        hub.stop()
        await bus.drain(timeout=3.0)

        for name in ("orchestrator", "web", "weather", "integrations"):
            service = self.get(name)
            if service is not None:
                try:
                    await service.close()
                except Exception:
                    logger.exception("error closing %s", name)

        db = self.get("db")
        if db is not None:
            await db.close()

        self.started = False
        logger.info("shutdown complete")

    # ==================================================================
    async def status(self) -> Dict[str, Any]:
        health = self.get("health")
        report = health.last() if health else {"checks": {}, "overall": "unknown"}
        tasks = self.get("tasks")
        orchestrator = self.get("orchestrator")
        presence = self.get("presence")

        return {
            "online": self.started,
            "state": orchestrator.state if orchestrator else "offline",
            "presence": await presence.status() if presence else {},
            "health": report,
            "tasks": await tasks.stats() if tasks else {},
            "boot": self.boot_log,
            "clients": hub.client_count,
            "version": "1.0.0",
            "instance_id": self.instance_id,
        }


container = Container()
