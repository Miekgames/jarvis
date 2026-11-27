"""v11 -- checklists: a grocery list, a workout, anything to tick off.

Items keep their order; ticking one off keeps it on the list (a workout is
done again next week), and clearing the ticked ones is a separate choice.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V11: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS checklists (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checklist_items (
        id            TEXT PRIMARY KEY,
        checklist_id  TEXT NOT NULL REFERENCES checklists(id) ON DELETE CASCADE,
        text          TEXT NOT NULL,
        done          INTEGER NOT NULL DEFAULT 0,
        position      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_checklist_items ON checklist_items(checklist_id, position)",
]
