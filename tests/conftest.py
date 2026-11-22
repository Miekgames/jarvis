"""Shared test fixtures.

Every test runs against a real SQLite database in a temporary directory and the
real service classes — no mocking of JARVIS's own machinery, because the things
worth testing here (task recovery, permission gating, cron arithmetic) are
exactly the things a mock would paper over.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.ai.tools.registry import ToolContext, ToolRegistry  # noqa: E402
from backend.automations.actions import ActionRunner  # noqa: E402
from backend.automations.engine import AutomationEngine  # noqa: E402
from backend.core.audit import AuditLog  # noqa: E402
from backend.core.confirmations import ConfirmationService  # noqa: E402
from backend.core.logs import ActivityLog  # noqa: E402
from backend.core.notifications import NotificationService  # noqa: E402
from backend.core.permissions import PermissionService  # noqa: E402
from backend.core.security import AuthService  # noqa: E402
from backend.db.database import Database  # noqa: E402
from backend.db.settings_store import SettingsStore  # noqa: E402
from backend.scheduler.service import SchedulerService  # noqa: E402
from backend.tasks.engine import TaskEngine  # noqa: E402


class Settings:
    """Minimal settings object for tests."""

    def __init__(self, tmp_path: Path):
        self.ROOT = tmp_path
        self.DATA_DIR = tmp_path
        self.BACKUP_DIR = tmp_path / "backups"
        self.BACKUP_DIR.mkdir(exist_ok=True)
        self.DB_PATH = tmp_path / "test.db"
        self.HOST = "127.0.0.1"
        self.PORT = 8000
        self.FRONTEND_PORT = 3000
        self.WORKER_COUNT = 2
        self.TASK_MAX_RETRIES = 2
        self.SCHEDULER_TICK_SECONDS = 1
        self.METRICS_INTERVAL_SECONDS = 60
        self.METRICS_RETENTION_HOURS = 1
        self.AUTH_MODE = "password"
        self.PASSWORD = "test-password"
        self.SESSION_HOURS = 1
        self.OWNER_NAME = "Tester"
        self.ASSISTANT_NAME = "JARVIS"
        self.MAX_TOKENS = 512
        self.MAX_HISTORY_TURNS = 5
        self.FILE_ROOTS = [str(tmp_path)]
        self.AI_PROVIDER = "ollama"
        self.ANTHROPIC_API_KEY = ""
        self.OPENAI_API_KEY = ""
        self.OLLAMA_HOST = "http://127.0.0.1:11434"
        self.OLLAMA_MODEL = ""
        self.MODEL_PRIMARY = "test-model"
        self.MODEL_FAST = "test-model"
        self.MODEL_REASONING = "test-model"
        self.MODEL_CODING = "test-model"
        self.MODEL_VISION = "test-model"

    @property
    def local_only(self) -> bool:
        return self.AI_PROVIDER != "auto"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path)


@pytest.fixture
async def db(settings):
    database = Database(settings.DB_PATH)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def settings_store(db):
    store = SettingsStore(db)
    await store.bootstrap()
    return store


@pytest.fixture
def activity(db):
    return ActivityLog(db)


@pytest.fixture
def audit(db):
    return AuditLog(db)


@pytest.fixture
async def permissions(db, audit):
    service = PermissionService(db, audit)
    await service.bootstrap()
    return service


@pytest.fixture
async def auth(db, settings, audit):
    service = AuthService(db, settings, audit)
    await service.ensure_owner()
    return service


@pytest.fixture
async def confirmations(db, audit, settings_store):
    return ConfirmationService(db, audit, settings_store)


@pytest.fixture
async def notifications(db, settings_store):
    return NotificationService(db, settings_store)


@pytest.fixture
async def engine(db, settings, activity, notifications):
    task_engine = TaskEngine(db, settings, activity, notifications, {})
    yield task_engine
    if task_engine.running:
        await task_engine.stop(grace=1.0)


@pytest.fixture
async def scheduler(db, settings, activity, notifications):
    services = {"notifications": notifications}
    service = SchedulerService(db, settings, activity, ActionRunner(services), notifications)
    yield service
    if service.running:
        await service.stop()


@pytest.fixture
async def automations(db, activity, settings_store, notifications):
    services = {"notifications": notifications}
    engine = AutomationEngine(db, activity, ActionRunner(services), settings_store)
    yield engine
    if engine.running:
        await engine.stop()


@pytest.fixture
def registry(permissions, confirmations, audit):
    context = ToolContext(permissions=permissions, confirmations=confirmations, audit=audit)
    return ToolRegistry(context)
