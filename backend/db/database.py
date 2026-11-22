"""
SQLite persistence layer.

All database work is funnelled through a single dedicated worker thread. That
serialises access (SQLite writes are serialised anyway), removes every
cross-thread connection hazard, and keeps the public API cleanly async.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .schema import MIGRATIONS

logger = logging.getLogger("jarvis.db")


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> Dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-db")
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect_sync(self) -> None:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        conn.row_factory = _dict_factory
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        self._migrate()

    async def connect(self) -> None:
        await self._run(self.connect_sync)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _close() -> None:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                self._conn = None

        try:
            await self._run(_close)
        finally:
            self._pool.shutdown(wait=True)

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)"
        )
        self._conn.commit()
        cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        current = cur.fetchone()["v"]

        from datetime import datetime, timezone

        for version, description, statements in MIGRATIONS:
            if version <= current:
                continue
            logger.info("applying migration %s: %s", version, description)
            try:
                for stmt in statements:
                    try:
                        cur.execute(stmt)
                    except sqlite3.OperationalError as exc:
                        # SQLite has no ADD COLUMN IF NOT EXISTS. Re-running a
                        # migration on a database that already has the column
                        # is not an error worth aborting for.
                        if "duplicate column name" in str(exc).lower():
                            logger.debug("column already present, skipping: %s", stmt[:80])
                            continue
                        raise
                cur.execute(
                    "INSERT INTO schema_version (version, description, applied_at) VALUES (?,?,?)",
                    (version, description, datetime.now(timezone.utc).isoformat()),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                logger.exception("migration %s failed", version)
                raise

    async def schema_version(self) -> int:
        row = await self.fetch_one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        return int(row["v"]) if row else 0

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _run(self, fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    # ------------------------------------------------------------------
    # query API
    # ------------------------------------------------------------------
    def execute_sync(self, sql: str, params: Sequence[Any] = ()) -> int:
        conn = self._require()
        with self._lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await self._run(self.execute_sync, sql, params)

    def execute_many_sync(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        conn = self._require()
        with self._lock:
            cur = conn.executemany(sql, [tuple(r) for r in rows])
            conn.commit()
            return cur.rowcount

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await self._run(self.execute_many_sync, sql, list(rows))

    def fetch_all_sync(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        conn = self._require()
        with self._lock:
            return conn.execute(sql, tuple(params)).fetchall()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return await self._run(self.fetch_all_sync, sql, params)

    def fetch_one_sync(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        conn = self._require()
        with self._lock:
            return conn.execute(sql, tuple(params)).fetchone()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        return await self._run(self.fetch_one_sync, sql, params)

    def fetch_value_sync(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.fetch_one_sync(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        return await self._run(self.fetch_value_sync, sql, params, default)

    def transaction_sync(self, operations: Iterable[Sequence[Any]]) -> None:
        """operations: iterable of (sql, params). All-or-nothing."""
        conn = self._require()
        with self._lock:
            try:
                for sql, params in operations:
                    conn.execute(sql, tuple(params))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    async def transaction(self, operations: Iterable[Sequence[Any]]) -> None:
        await self._run(self.transaction_sync, list(operations))

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    async def backup_to(self, dest: Path) -> int:
        """Consistent online backup using SQLite's own backup API."""

        def _backup() -> int:
            conn = self._require()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                target = sqlite3.connect(str(dest))
                try:
                    conn.backup(target)
                finally:
                    target.close()
            return dest.stat().st_size

        return await self._run(_backup)

    async def vacuum(self) -> None:
        await self._run(lambda: self._require().execute("VACUUM"))

    async def integrity_check(self) -> str:
        row = await self.fetch_one("PRAGMA integrity_check")
        return str(next(iter(row.values()))) if row else "unknown"

    async def table_counts(self) -> Dict[str, int]:
        tables = await self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        out: Dict[str, int] = {}
        for t in tables:
            name = t["name"]
            try:
                out[name] = int(await self.fetch_value(f'SELECT COUNT(*) FROM "{name}"', default=0))
            except Exception:
                out[name] = -1
        return out


# ---------------------------------------------------------------------------
# JSON column helpers -- SQLite has no native JSON type, so subsystems store
# JSON text and use these to move in and out safely.
# ---------------------------------------------------------------------------

def dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def row_json(row: Optional[Dict[str, Any]], *fields: str) -> Optional[Dict[str, Any]]:
    """Return a copy of `row` with the named columns decoded from JSON text."""
    if row is None:
        return None
    out = dict(row)
    for field in fields:
        if field in out:
            out[field] = loads(out[field], {} if field != "actions" else [])
    return out


def rows_json(rows: List[Dict[str, Any]], *fields: str) -> List[Dict[str, Any]]:
    return [row_json(r, *fields) or {} for r in rows]
