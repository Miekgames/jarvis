"""
Migration 3 — the AI operating environment.

Goals gain structure (milestones, complexity, adaptation history), agents
become first-class runtime objects, research becomes a resumable project with
tracked sources, and memory gains scope and provenance.
"""
from __future__ import annotations

from typing import List

V3: List[str] = [
    # -- goals 2.0 -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS milestones (
        id          TEXT PRIMARY KEY,
        goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
        title       TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        position    INTEGER NOT NULL DEFAULT 0,
        due_at      TEXT,
        status      TEXT NOT NULL DEFAULT 'pending',
        depends_on  TEXT NOT NULL DEFAULT '[]',
        task_ids    TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_milestones_goal ON milestones(goal_id, position)",

    # Extra goal columns. SQLite has no IF NOT EXISTS for ADD COLUMN, so the
    # migration runner tolerates duplicate-column errors on these.
    "ALTER TABLE goals ADD COLUMN project_id TEXT",
    "ALTER TABLE goals ADD COLUMN deadline TEXT",
    "ALTER TABLE goals ADD COLUMN complexity TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE goals ADD COLUMN confidence REAL NOT NULL DEFAULT 0",
    "ALTER TABLE goals ADD COLUMN questions TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE goals ADD COLUMN adaptations TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE goals ADD COLUMN report TEXT",

    # -- agent runtime -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id           TEXT PRIMARY KEY,
        agent        TEXT NOT NULL,
        objective    TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'queued',
        task_id      TEXT,
        goal_id      TEXT,
        priority     TEXT NOT NULL DEFAULT 'normal',
        time_limit_s INTEGER NOT NULL DEFAULT 900,
        started_at   TEXT,
        finished_at  TEXT,
        progress     REAL NOT NULL DEFAULT 0,
        current_step TEXT NOT NULL DEFAULT '',
        result       TEXT,
        error        TEXT,
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        from_run   TEXT NOT NULL,
        to_run     TEXT,
        kind       TEXT NOT NULL DEFAULT 'note',
        body       TEXT NOT NULL DEFAULT '',
        payload    TEXT NOT NULL DEFAULT '{}',
        consumed   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_run, consumed)",

    # -- deep research -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS research_projects (
        id          TEXT PRIMARY KEY,
        question    TEXT NOT NULL,
        depth       TEXT NOT NULL DEFAULT 'standard',
        status      TEXT NOT NULL DEFAULT 'active',
        summary     TEXT NOT NULL DEFAULT '',
        report      TEXT NOT NULL DEFAULT '',
        conflicts   TEXT NOT NULL DEFAULT '[]',
        queries     TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        task_id     TEXT,
        goal_id     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_sources (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
        n           INTEGER NOT NULL DEFAULT 0,
        url         TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        domain      TEXT NOT NULL DEFAULT '',
        excerpt     TEXT NOT NULL DEFAULT '',
        published   TEXT,
        retrieved_at TEXT NOT NULL,
        stale       INTEGER NOT NULL DEFAULT 0,
        stale_reason TEXT NOT NULL DEFAULT '',
        reachable   INTEGER NOT NULL DEFAULT 1,
        error       TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_research_sources ON research_sources(project_id, n)",

    # -- memory 2.0 ----------------------------------------------------------
    "ALTER TABLE memory ADD COLUMN scope TEXT NOT NULL DEFAULT 'long_term'",
    "ALTER TABLE memory ADD COLUMN project_id TEXT",
    "ALTER TABLE memory ADD COLUMN conversation_id TEXT",
    "ALTER TABLE memory ADD COLUMN expires_at TEXT",
    "ALTER TABLE memory ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE memory ADD COLUMN last_used TEXT",
    "ALTER TABLE memory ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope, expires_at)",

    # -- continuity ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS continuity (
        id           TEXT PRIMARY KEY,
        device_id    TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'session',
        state        TEXT NOT NULL DEFAULT '{}',
        updated_at   TEXT NOT NULL
    )
    """,
]
