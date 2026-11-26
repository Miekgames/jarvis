"""v10 -- countdowns to events, shown on the main screen.

The day and time are kept as the wall-clock values the owner means ("the
18th", "19:30"), not as an instant: an event on the 18th is on the 18th
whatever the clocks do in between.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V10: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS countdowns (
        id            TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        topic         TEXT DEFAULT '',
        day           TEXT NOT NULL,
        clock         TEXT DEFAULT '',
        origin        TEXT DEFAULT 'owner',
        source_name   TEXT DEFAULT '',
        source_url    TEXT DEFAULT '',
        sources       INTEGER DEFAULT 0,
        checked_at    TEXT,
        created_at    TEXT NOT NULL
    )
    """,
]
