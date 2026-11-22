"""v7 -- what each reply cost to produce.

One row per finished reply: which model, how many tokens each way, how long
it took to start and to finish, and how much energy the GPU used while it
was being produced. It is what the reply's stats line shows, and what the
efficiency readout adds up.

Ported idea: OpenJarvis treats energy, latency and tokens as first-class
measurements of an assistant ("intelligence per watt"). The schema is ours.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V7: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS reply_telemetry (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        conversation_id TEXT,
        provider        TEXT,
        model           TEXT,
        calls           INTEGER DEFAULT 1,
        input_tokens    INTEGER DEFAULT 0,
        output_tokens   INTEGER DEFAULT 0,
        tokens_per_sec  REAL,
        ttft_ms         INTEGER,
        total_ms        INTEGER,
        load_ms         INTEGER,
        energy_j        REAL,
        avg_power_w     REAL,
        energy_method   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reply_telemetry_ts ON reply_telemetry(ts)",
]
