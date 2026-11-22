"""
Migration 2 — the Intelligence Layer.

Adds the tables that let JARVIS be ambient rather than something you open:
what you are doing, what you usually do, what it noticed, what it decided and
why, and which of your devices are present.
"""
from __future__ import annotations

from typing import List

V2: List[str] = [
    # -- ambient awareness ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS focus_sessions (
        id          TEXT PRIMARY KEY,
        app         TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        category    TEXT NOT NULL DEFAULT 'other',
        started_at  TEXT NOT NULL,
        ended_at    TEXT,
        seconds     INTEGER NOT NULL DEFAULT 0,
        idle        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_focus_started ON focus_sessions(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_focus_app ON focus_sessions(app)",
    """
    CREATE TABLE IF NOT EXISTS app_stats (
        app          TEXT PRIMARY KEY,
        category     TEXT NOT NULL DEFAULT 'other',
        total_seconds INTEGER NOT NULL DEFAULT 0,
        launches     INTEGER NOT NULL DEFAULT 0,
        crashes      INTEGER NOT NULL DEFAULT 0,
        last_seen    TEXT,
        first_seen   TEXT
    )
    """,

    # -- behavioural learning ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS command_patterns (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        normalised   TEXT NOT NULL,
        example      TEXT NOT NULL,
        intent       TEXT NOT NULL DEFAULT '',
        count        INTEGER NOT NULL DEFAULT 1,
        hour_hist    TEXT NOT NULL DEFAULT '{}',
        weekday_hist TEXT NOT NULL DEFAULT '{}',
        last_used    TEXT NOT NULL,
        first_used   TEXT NOT NULL,
        avg_gap_s    REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pattern_norm ON command_patterns(normalised)",
    """
    CREATE TABLE IF NOT EXISTS command_sequences (
        first_id  INTEGER NOT NULL,
        second_id INTEGER NOT NULL,
        count     INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (first_id, second_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vocabulary (
        phrase     TEXT PRIMARY KEY,
        means      TEXT NOT NULL,
        source     TEXT NOT NULL DEFAULT 'learned',
        uses       INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,

    # -- proactive suggestions ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        id          TEXT PRIMARY KEY,
        ts          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        action      TEXT NOT NULL DEFAULT '{}',
        confidence  REAL NOT NULL DEFAULT 0.5,
        reason      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'offered',
        resolved_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status, ts DESC)",

    # -- transparency: why did you do that -----------------------------------
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id              TEXT PRIMARY KEY,
        ts              TEXT NOT NULL,
        subject         TEXT NOT NULL,
        summary         TEXT NOT NULL,
        rationale       TEXT NOT NULL DEFAULT '',
        inputs          TEXT NOT NULL DEFAULT '{}',
        alternatives    TEXT NOT NULL DEFAULT '[]',
        conversation_id TEXT,
        task_id         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts DESC)",

    # -- vision --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS observations (
        id         TEXT PRIMARY KEY,
        ts         TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'screen',
        path       TEXT NOT NULL DEFAULT '',
        summary    TEXT NOT NULL DEFAULT '',
        detail     TEXT NOT NULL DEFAULT '{}',
        app        TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts DESC)",

    # -- goals (agent 2.0) ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS goals (
        id          TEXT PRIMARY KEY,
        objective   TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'planning',
        plan        TEXT NOT NULL DEFAULT '[]',
        result      TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        finished_at TEXT,
        root_task   TEXT
    )
    """,

    # -- connected devices ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS devices (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'desktop',
        platform     TEXT NOT NULL DEFAULT '',
        address      TEXT NOT NULL DEFAULT '',
        token_hash   TEXT NOT NULL DEFAULT '',
        permissions  TEXT NOT NULL DEFAULT '[]',
        last_seen    TEXT,
        paired_at    TEXT NOT NULL,
        is_self      INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        id          TEXT PRIMARY KEY,
        ts          TEXT NOT NULL,
        from_device TEXT NOT NULL,
        to_device   TEXT NOT NULL,
        payload     TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'pending',
        claimed_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_handoffs_target ON handoffs(to_device, status)",

    # -- gaming --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS game_sessions (
        id         TEXT PRIMARY KEY,
        game       TEXT NOT NULL,
        process    TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        ended_at   TEXT,
        seconds    INTEGER NOT NULL DEFAULT 0,
        avg_cpu    REAL,
        avg_gpu    REAL,
        max_gpu_temp REAL,
        crashed    INTEGER NOT NULL DEFAULT 0,
        summary    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_game_sessions ON game_sessions(game, started_at DESC)",
]
