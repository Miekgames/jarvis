"""v9 -- an index of the owner's own documents.

Folders the owner chooses are read, cut into passages and indexed for
full-text search (SQLite FTS5, ranked with BM25), so JARVIS can answer from
their notes, PDFs and documents and say which file each answer came from.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V9: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS doc_folders (
        path          TEXT PRIMARY KEY,
        added_at      TEXT NOT NULL,
        indexed_at    TEXT,
        files         INTEGER DEFAULT 0,
        passages      INTEGER DEFAULT 0,
        status        TEXT DEFAULT 'waiting',
        error         TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_files (
        path          TEXT PRIMARY KEY,
        folder        TEXT NOT NULL,
        mtime         REAL,
        size          INTEGER,
        passages      INTEGER DEFAULT 0,
        indexed_at    TEXT,
        error         TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_files_folder ON doc_files(folder)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS doc_passages USING fts5(
        text, heading, path UNINDEXED, position UNINDEXED,
        tokenize = 'porter unicode61'
    )
    """,
]
