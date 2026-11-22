"""
Migration 5 -- the personal operating layer.

Notes, clipboard, snippets, focus sessions, habits, wellbeing, file tags,
bookmarks, feeds, custom themes and saved layouts.

Everything here is local to this machine. Nothing in this migration is synced,
uploaded, or shared.
"""
from __future__ import annotations

from typing import List

V5: List[str] = [
    # -- notes ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS notes (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL DEFAULT 'note',
        pinned      INTEGER NOT NULL DEFAULT 0,
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind, archived)",
    """
    CREATE TABLE IF NOT EXISTS note_links (
        note_id     TEXT NOT NULL,
        target      TEXT NOT NULL,
        resolved_id TEXT,
        PRIMARY KEY (note_id, target)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target)",

    # -- clipboard and snippets -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS clips (
        id          TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        preview     TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL DEFAULT 'text',
        bytes       INTEGER NOT NULL DEFAULT 0,
        pinned      INTEGER NOT NULL DEFAULT 0,
        source_app  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS snippets (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        trigger     TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL,
        tags        TEXT NOT NULL DEFAULT '',
        uses        INTEGER NOT NULL DEFAULT 0,
        last_used   TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_snippets_trigger ON snippets(trigger) WHERE trigger <> ''",

    # -- focus and time ---------------------------------------------------
    # Named pomodoro_sessions, not focus_sessions: v2 already uses that name
    # for the awareness layer's foreground-window sessions, and CREATE TABLE
    # IF NOT EXISTS would silently no-op against the wrong schema.
    """
    CREATE TABLE IF NOT EXISTS pomodoro_sessions (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL DEFAULT 'focus',
        goal          TEXT NOT NULL DEFAULT '',
        planned_min   INTEGER NOT NULL DEFAULT 25,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        interruptions INTEGER NOT NULL DEFAULT 0,
        notes         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_focus_started ON pomodoro_sessions(started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS time_blocks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        day         TEXT NOT NULL,
        app         TEXT NOT NULL,
        category    TEXT NOT NULL DEFAULT 'other',
        seconds     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (day, app)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_time_blocks_day ON time_blocks(day)",
    """
    CREATE TABLE IF NOT EXISTS distractions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT,
        app         TEXT NOT NULL,
        at          TEXT NOT NULL,
        seconds     INTEGER NOT NULL DEFAULT 0
    )
    """,

    # -- habits and wellbeing ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS habits (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        cadence     TEXT NOT NULL DEFAULT 'daily',
        target      REAL NOT NULL DEFAULT 1,
        unit        TEXT NOT NULL DEFAULT '',
        colour      TEXT NOT NULL DEFAULT '#5ad2ff',
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS habit_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        habit_id    TEXT NOT NULL,
        day         TEXT NOT NULL,
        value       REAL NOT NULL DEFAULT 1,
        note        TEXT NOT NULL DEFAULT '',
        UNIQUE (habit_id, day)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_habit_entries_day ON habit_entries(day DESC)",
    """
    CREATE TABLE IF NOT EXISTS wellbeing (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        day         TEXT NOT NULL,
        kind        TEXT NOT NULL,
        value       REAL NOT NULL,
        note        TEXT NOT NULL DEFAULT '',
        UNIQUE (day, kind)
    )
    """,

    # -- files -------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS file_tags (
        path        TEXT NOT NULL,
        tag         TEXT NOT NULL,
        added_at    TEXT NOT NULL,
        PRIMARY KEY (path, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag)",

    # -- reading -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS bookmarks (
        id          TEXT PRIMARY KEY,
        url         TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        summary     TEXT NOT NULL DEFAULT '',
        added_at    TEXT NOT NULL,
        read_at     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bookmarks_added ON bookmarks(added_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS feeds (
        id            TEXT PRIMARY KEY,
        url           TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL DEFAULT '',
        last_fetched  TEXT,
        last_error    TEXT NOT NULL DEFAULT '',
        item_count    INTEGER NOT NULL DEFAULT 0,
        added_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feed_items (
        id          TEXT PRIMARY KEY,
        feed_id     TEXT NOT NULL,
        guid        TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        url         TEXT NOT NULL DEFAULT '',
        published   TEXT,
        summary     TEXT NOT NULL DEFAULT '',
        read        INTEGER NOT NULL DEFAULT 0,
        UNIQUE (feed_id, guid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_feed_items_pub ON feed_items(published DESC)",

    # -- interface ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS custom_themes (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        colours     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS layouts (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        config      TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
]
