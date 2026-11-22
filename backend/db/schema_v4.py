"""
Migration 4 — the developer workspace.

Codebases JARVIS is authorised to read, the symbols it has indexed from them,
and a timeline of development activity.
"""
from __future__ import annotations

from typing import List

V4: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        path        TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL DEFAULT 'unknown',
        vcs         TEXT NOT NULL DEFAULT '',
        indexed_at  TEXT,
        file_count  INTEGER NOT NULL DEFAULT 0,
        symbol_count INTEGER NOT NULL DEFAULT 0,
        added_at    TEXT NOT NULL,
        settings    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_symbols (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL,
        path         TEXT NOT NULL,
        line         INTEGER NOT NULL DEFAULT 0,
        signature    TEXT NOT NULL DEFAULT '',
        parent       TEXT NOT NULL DEFAULT '',
        docstring    TEXT NOT NULL DEFAULT '',
        language     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_symbols_name ON code_symbols(workspace_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_symbols_path ON code_symbols(workspace_id, path)",
    """
    CREATE TABLE IF NOT EXISTS code_files (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        path         TEXT NOT NULL,
        language     TEXT NOT NULL DEFAULT '',
        lines        INTEGER NOT NULL DEFAULT 0,
        size         INTEGER NOT NULL DEFAULT 0,
        imports      TEXT NOT NULL DEFAULT '[]',
        modified     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_code_files ON code_files(workspace_id, path)",
    """
    CREATE TABLE IF NOT EXISTS dev_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        workspace_id TEXT,
        kind         TEXT NOT NULL,
        summary      TEXT NOT NULL,
        detail       TEXT NOT NULL DEFAULT '{}',
        ok           INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dev_events ON dev_events(ts DESC)",
]
