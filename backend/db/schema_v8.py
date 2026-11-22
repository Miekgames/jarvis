"""v8 -- a running summary of what has scrolled out of a long conversation.

The model can only see so much of a conversation at once. What falls off
the front used to be simply gone; now it is summarised, and the summary
travels with the conversation, so "the budget we agreed earlier" still
means something forty messages later.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V8: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY,
        summary         TEXT NOT NULL,
        covers_until    TEXT NOT NULL,
        messages        INTEGER DEFAULT 0,
        updated_at      TEXT NOT NULL
    )
    """,
]
