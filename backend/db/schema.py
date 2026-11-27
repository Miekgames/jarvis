"""
Versioned schema migrations.

Each entry in MIGRATIONS is (version, description, [sql statements]). They run
in order inside a transaction and the applied version is recorded in
`schema_version`, so upgrading an existing install never loses data.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List, Tuple

V1: List[str] = [
    # -- identity & security -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        salt          TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT 'owner',
        display_name  TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        last_login    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        client     TEXT NOT NULL DEFAULT '',
        ip         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    """
    CREATE TABLE IF NOT EXISTS permissions (
        key         TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 0,
        label       TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        risk        TEXT NOT NULL DEFAULT 'low',
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        user_id INTEGER,
        event   TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        detail  TEXT NOT NULL DEFAULT '{}',
        result  TEXT NOT NULL DEFAULT 'ok',
        ip      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        id              TEXT PRIMARY KEY,
        ts              TEXT NOT NULL,
        tool            TEXT NOT NULL,
        summary         TEXT NOT NULL,
        detail          TEXT NOT NULL DEFAULT '{}',
        risk            TEXT NOT NULL DEFAULT 'medium',
        status          TEXT NOT NULL DEFAULT 'pending',
        resolved_at     TEXT,
        conversation_id TEXT,
        task_id         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_confirm_status ON confirmations(status)",

    # -- activity & logging --------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL DEFAULT 'info',
        source  TEXT NOT NULL DEFAULT 'system',
        message TEXT NOT NULL,
        meta    TEXT NOT NULL DEFAULT '{}',
        task_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_activity_task ON activity_log(task_id)",

    # -- conversation --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL DEFAULT 'New conversation',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        project_id TEXT,
        archived   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL,
        text            TEXT NOT NULL DEFAULT '',
        blocks          TEXT NOT NULL DEFAULT '[]',
        created_at      TEXT NOT NULL,
        model           TEXT NOT NULL DEFAULT '',
        tokens_in       INTEGER NOT NULL DEFAULT 0,
        tokens_out      INTEGER NOT NULL DEFAULT 0,
        latency_ms      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS commands (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        text            TEXT NOT NULL,
        source          TEXT NOT NULL DEFAULT 'text',
        conversation_id TEXT,
        pinned          INTEGER NOT NULL DEFAULT 0,
        favorite        INTEGER NOT NULL DEFAULT 0,
        label           TEXT NOT NULL DEFAULT '',
        result_summary  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts DESC)",

    # -- task engine ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        type            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'queued',
        priority        TEXT NOT NULL DEFAULT 'normal',
        progress        REAL NOT NULL DEFAULT 0,
        current_step    TEXT NOT NULL DEFAULT '',
        step_index      INTEGER NOT NULL DEFAULT 0,
        total_steps     INTEGER NOT NULL DEFAULT 0,
        params          TEXT NOT NULL DEFAULT '{}',
        result          TEXT,
        error           TEXT,
        checkpoint      TEXT NOT NULL DEFAULT '{}',
        retries         INTEGER NOT NULL DEFAULT 0,
        max_retries     INTEGER NOT NULL DEFAULT 3,
        created_at      TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        eta             TEXT,
        heartbeat       TEXT,
        cancellable     INTEGER NOT NULL DEFAULT 1,
        parent_id       TEXT,
        conversation_id TEXT,
        project_id      TEXT,
        schedule_id     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS task_logs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL,
        meta    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id)",
    """
    CREATE TABLE IF NOT EXISTS task_deps (
        task_id    TEXT NOT NULL,
        depends_on TEXT NOT NULL,
        PRIMARY KEY (task_id, depends_on)
    )
    """,

    # -- scheduling & automation --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        kind             TEXT NOT NULL DEFAULT 'once',
        cron             TEXT NOT NULL DEFAULT '',
        interval_seconds INTEGER NOT NULL DEFAULT 0,
        run_at           TEXT,
        next_run         TEXT,
        last_run         TEXT,
        action           TEXT NOT NULL DEFAULT '{}',
        enabled          INTEGER NOT NULL DEFAULT 1,
        created_at       TEXT NOT NULL,
        fire_count       INTEGER NOT NULL DEFAULT 0,
        misfire_grace    INTEGER NOT NULL DEFAULT 300
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(enabled, next_run)",
    """
    CREATE TABLE IF NOT EXISTS automations (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        enabled     INTEGER NOT NULL DEFAULT 1,
        trigger     TEXT NOT NULL DEFAULT '{}',
        conditions  TEXT NOT NULL DEFAULT '[]',
        actions     TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        last_fired  TEXT,
        fire_count  INTEGER NOT NULL DEFAULT 0,
        cooldown    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS automation_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
        ts            TEXT NOT NULL,
        status        TEXT NOT NULL,
        detail        TEXT NOT NULL DEFAULT '{}'
    )
    """,

    # -- notifications -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id            TEXT PRIMARY KEY,
        ts            TEXT NOT NULL,
        priority      TEXT NOT NULL DEFAULT 'normal',
        title         TEXT NOT NULL,
        body          TEXT NOT NULL DEFAULT '',
        source        TEXT NOT NULL DEFAULT 'jarvis',
        read          INTEGER NOT NULL DEFAULT 0,
        dismissed     INTEGER NOT NULL DEFAULT 0,
        snoozed_until TEXT,
        action        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notif_ts ON notifications(ts DESC)",

    # -- memory --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS memory (
        id         TEXT PRIMARY KEY,
        category   TEXT NOT NULL DEFAULT 'general',
        key        TEXT NOT NULL DEFAULT '',
        value      TEXT NOT NULL,
        importance INTEGER NOT NULL DEFAULT 3,
        source     TEXT NOT NULL DEFAULT 'jarvis',
        pinned     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_cat ON memory(category)",

    # -- projects ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'active',
        deadline    TEXT,
        color       TEXT NOT NULL DEFAULT '#4dd8ff',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL,
        ref_id     TEXT NOT NULL DEFAULT '',
        title      TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,

    # -- people & comms ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        phone        TEXT NOT NULL DEFAULT '',
        email        TEXT NOT NULL DEFAULT '',
        organization TEXT NOT NULL DEFAULT '',
        notes        TEXT NOT NULL DEFAULT '',
        tags         TEXT NOT NULL DEFAULT '[]',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS communications (
        id         TEXT PRIMARY KEY,
        ts         TEXT NOT NULL,
        contact_id TEXT,
        channel    TEXT NOT NULL,
        direction  TEXT NOT NULL DEFAULT 'outbound',
        subject    TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'draft',
        meta       TEXT NOT NULL DEFAULT '{}'
    )
    """,

    # -- calendar (local store; integrations sync into this) ------------------
    """
    CREATE TABLE IF NOT EXISTS calendar_events (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        start_at    TEXT NOT NULL,
        end_at      TEXT,
        all_day     INTEGER NOT NULL DEFAULT 0,
        location    TEXT NOT NULL DEFAULT '',
        link        TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        attendees   TEXT NOT NULL DEFAULT '[]',
        source      TEXT NOT NULL DEFAULT 'local',
        external_id TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cal_start ON calendar_events(start_at)",

    # -- timers --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS timers (
        id         TEXT PRIMARY KEY,
        label      TEXT NOT NULL DEFAULT 'Timer',
        kind       TEXT NOT NULL DEFAULT 'timer',
        duration   INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        ends_at    TEXT,
        paused_at  TEXT,
        status     TEXT NOT NULL DEFAULT 'running'
    )
    """,

    # -- integrations & settings --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS integrations (
        key        TEXT PRIMARY KEY,
        connected  INTEGER NOT NULL DEFAULT 0,
        account    TEXT NOT NULL DEFAULT '',
        config     TEXT NOT NULL DEFAULT '{}',
        last_sync  TEXT,
        status     TEXT NOT NULL DEFAULT 'not_configured',
        error      TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS modes (
        key        TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        icon       TEXT NOT NULL DEFAULT '',
        config     TEXT NOT NULL DEFAULT '{}',
        builtin    INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,

    # -- telemetry -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS metrics (
        ts       TEXT PRIMARY KEY,
        cpu      REAL NOT NULL DEFAULT 0,
        mem      REAL NOT NULL DEFAULT 0,
        disk     REAL NOT NULL DEFAULT 0,
        net_up   REAL NOT NULL DEFAULT 0,
        net_down REAL NOT NULL DEFAULT 0,
        gpu      REAL,
        gpu_mem  REAL,
        battery  REAL,
        temps    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        provider   TEXT NOT NULL,
        model      TEXT NOT NULL,
        tokens_in  INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        cost       REAL NOT NULL DEFAULT 0,
        purpose    TEXT NOT NULL DEFAULT '',
        latency_ms INTEGER NOT NULL DEFAULT 0,
        ok         INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS backups (
        id       TEXT PRIMARY KEY,
        ts       TEXT NOT NULL,
        path     TEXT NOT NULL,
        size     INTEGER NOT NULL DEFAULT 0,
        verified INTEGER NOT NULL DEFAULT 0,
        note     TEXT NOT NULL DEFAULT ''
    )
    """,
]


from .schema_v2 import V2  # noqa: E402
from .schema_v3 import V3  # noqa: E402
from .schema_v4 import V4  # noqa: E402
from .schema_v5 import V5  # noqa: E402
from .schema_v6 import V6  # noqa: E402
from .schema_v7 import V7  # noqa: E402
from .schema_v8 import V8  # noqa: E402
from .schema_v9 import V9  # noqa: E402
from .schema_v10 import V10  # noqa: E402
from .schema_v11 import V11  # noqa: E402

MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (1, "initial schema", V1),
    (2, "intelligence layer", V2),
    (3, "operating environment", V3),
    (4, "developer workspace", V4),
    (5, "personal operating layer", V5),
    (6, "retire calling and phone messaging", V6),
    (7, "reply telemetry", V7),
    (8, "conversation summaries", V8),
    (9, "document index", V9),
    (10, "countdowns", V10),
    (11, "checklists", V11),
]
