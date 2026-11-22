"""
Persistent long-running task engine.

The defining property: a task is a row in the database, not a coroutine tied to
an HTTP request. Closing the dashboard, reloading the page or restarting the
backend does not lose work -- workers pick tasks back up and handlers resume
from their last checkpoint.

    queued -> running -> completed
                 |  \-> failed (after retries are exhausted)
                 |  \-> cancelled
                 \----> paused -> queued
    waiting            (blocked on an unfinished dependency)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import ahead, humanize_duration, new_id, now_iso, parse_iso, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.tasks")


class Status:
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ACTIVE = (QUEUED, RUNNING, PAUSED, WAITING)
    TERMINAL = (COMPLETED, FAILED, CANCELLED)


class Priority:
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    BACKGROUND = "background"

    ORDER = {CRITICAL: 0, HIGH: 1, NORMAL: 2, LOW: 3, BACKGROUND: 4}


class TaskCancelled(Exception):
    """Raised inside a handler when the user cancels."""


class TaskPaused(Exception):
    """Raised inside a handler when the user pauses; the checkpoint is kept."""


class TaskContext:
    """The handle a handler uses to report progress and stay interruptible."""

    def __init__(self, engine: "TaskEngine", record: Dict[str, Any]):
        self.engine = engine
        self.task_id = record["id"]
        self.record = record
        self.params: Dict[str, Any] = loads(record.get("params"), {}) or {}
        self.services = engine.services
        self._started = time.monotonic()
        self._total_steps = int(record.get("total_steps") or 0)

    # -- interruption ---------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self.task_id in self.engine._cancel_requests

    @property
    def paused(self) -> bool:
        return self.task_id in self.engine._pause_requests

    def checkpoint_now(self) -> None:
        """Call at every safe resume point. Raises if the user asked to stop,
        which is what makes cancellation cooperative rather than abrupt."""
        if self.cancelled:
            raise TaskCancelled()
        if self.paused:
            raise TaskPaused()

    # -- progress -------------------------------------------------------
    async def progress(self, fraction: float, step: str = "", step_index: Optional[int] = None) -> None:
        self.checkpoint_now()
        fraction = max(0.0, min(1.0, float(fraction)))
        eta = None
        if fraction > 0.02:
            elapsed = time.monotonic() - self._started
            remaining = elapsed / fraction - elapsed
            eta = ahead(remaining)
        await self.engine._update(
            self.task_id,
            progress=fraction,
            current_step=truncate(step, 200),
            step_index=step_index if step_index is not None else None,
            eta=eta,
            heartbeat=now_iso(),
        )
        bus.publish(
            Events.TASK_PROGRESS,
            {"id": self.task_id, "progress": fraction, "current_step": step, "eta": eta},
            source="tasks",
        )

    async def step(self, index: int, total: int, description: str) -> None:
        self._total_steps = total
        await self.engine._update(self.task_id, total_steps=total)
        await self.progress(index / max(1, total), description, step_index=index)
        await self.log(f"Step {index}/{total}: {description}")

    # -- logging --------------------------------------------------------
    async def log(self, message: str, level: str = "info", **meta: Any) -> None:
        await self.engine.log(self.task_id, message, level, meta)

    # -- checkpointing --------------------------------------------------
    async def save_checkpoint(self, data: Dict[str, Any]) -> None:
        await self.engine._update(self.task_id, checkpoint=dumps(data), heartbeat=now_iso())

    def load_checkpoint(self) -> Dict[str, Any]:
        return loads(self.record.get("checkpoint"), {}) or {}


Handler = Callable[[TaskContext], Awaitable[Any]]


class TaskEngine:
    def __init__(self, db, settings, activity, notifications=None, services: Optional[Dict[str, Any]] = None):
        self.db = db
        self.settings = settings
        self.activity = activity
        self.notifications = notifications
        self.services: Dict[str, Any] = services or {}

        self._handlers: Dict[str, Handler] = {}
        self._resumable: Dict[str, bool] = {}
        self._workers: List[asyncio.Task] = []
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._cancel_requests: set = set()
        self._pause_requests: set = set()
        self._running = False
        self._wake = asyncio.Event()

    # ==================================================================
    # handler registration
    # ==================================================================
    def register(self, task_type: str, handler: Handler, resumable: bool = True) -> None:
        self._handlers[task_type] = handler
        self._resumable[task_type] = resumable
        logger.debug("registered task handler '%s'", task_type)

    def handler_types(self) -> List[str]:
        return sorted(self._handlers)

    # ==================================================================
    # lifecycle
    # ==================================================================
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.recover()
        for index in range(self.settings.WORKER_COUNT):
            worker = asyncio.create_task(self._worker(index), name=f"task-worker-{index}")
            self._workers.append(worker)
        logger.info("task engine started with %s workers", self.settings.WORKER_COUNT)

    async def stop(self, grace: float = 10.0) -> None:
        """Graceful shutdown: ask running tasks to pause so their checkpoints
        survive, then stop the workers."""
        self._running = False
        self._wake.set()

        for task_id in list(self._running_tasks):
            self._pause_requests.add(task_id)

        if self._running_tasks:
            logger.info("waiting up to %ss for %s running task(s)", grace, len(self._running_tasks))
            await asyncio.wait(list(self._running_tasks.values()), timeout=grace)
            # A handler finishing is not the same as its status having been
            # written -- give _run's finally block a moment to land.
            await asyncio.sleep(0.2)

        for task_id, runner in list(self._running_tasks.items()):
            if not runner.done():
                runner.cancel()

        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Final sweep: nothing may be left claiming to run once the engine is
        # down. Anything still marked running goes back in the queue so startup
        # recovery picks it up from its last checkpoint.
        stranded = await self.db.execute(
            "UPDATE tasks SET status = ?, current_step = ? WHERE status = ?",
            (Status.QUEUED, "Interrupted by shutdown; will resume from checkpoint", Status.RUNNING),
        )
        if stranded:
            logger.info("requeued %s task(s) interrupted by shutdown", stranded)
        logger.info("task engine stopped")

    @property
    def running(self) -> bool:
        return self._running

    def worker_status(self) -> List[Dict[str, Any]]:
        return [
            {"name": w.get_name(), "alive": not w.done(), "cancelled": w.cancelled()}
            for w in self._workers
        ]

    # ==================================================================
    # startup recovery
    # ==================================================================
    async def recover(self) -> Dict[str, int]:
        """Anything marked running belonged to a process that no longer exists.
        Resumable work goes back in the queue; the rest is failed honestly."""
        stranded = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE status IN (?, ?)", (Status.RUNNING, Status.WAITING)
        )
        resumed = failed = 0
        for row in stranded:
            task_type = row["type"]
            can_resume = self._resumable.get(task_type, True) and task_type in self._handlers
            if can_resume:
                await self._update(
                    row["id"],
                    status=Status.QUEUED,
                    current_step="Recovered after restart -- resuming from last checkpoint",
                )
                await self.log(row["id"], "Recovered after a backend restart.", "warn")
                resumed += 1
            else:
                await self._update(
                    row["id"],
                    status=Status.FAILED,
                    error="Interrupted by a restart and this task type cannot be resumed safely.",
                    finished_at=now_iso(),
                )
                failed += 1

        if resumed or failed:
            await self.activity.record(
                f"Startup recovery: {resumed} task(s) resumed, {failed} marked unrecoverable.",
                level="warn" if failed else "info",
                source="tasks",
            )
        logger.info("recovery complete: %s resumed, %s failed", resumed, failed)
        return {"resumed": resumed, "failed": failed}

    # ==================================================================
    # creating work
    # ==================================================================
    async def create(
        self,
        name: str,
        task_type: str,
        params: Optional[Dict[str, Any]] = None,
        description: str = "",
        priority: str = Priority.NORMAL,
        conversation_id: Optional[str] = None,
        project_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        depends_on: Optional[List[str]] = None,
        max_retries: Optional[int] = None,
        total_steps: int = 0,
    ) -> Dict[str, Any]:
        if task_type not in self._handlers:
            raise ValueError(
                f"Unknown task type '{task_type}'. Known types: {', '.join(self.handler_types())}"
            )
        priority = priority if priority in Priority.ORDER else Priority.NORMAL
        task_id = new_id("task")

        await self.db.execute(
            "INSERT INTO tasks (id, name, description, type, status, priority, params, created_at,"
            " max_retries, conversation_id, project_id, parent_id, schedule_id, total_steps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, name, description, task_type,
                Status.WAITING if depends_on else Status.QUEUED,
                priority, dumps(params or {}), now_iso(),
                self.settings.TASK_MAX_RETRIES if max_retries is None else max_retries,
                conversation_id, project_id, parent_id, schedule_id, total_steps,
            ),
        )
        if depends_on:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)",
                [(task_id, dep) for dep in depends_on],
            )

        record = await self.get(task_id)
        bus.publish(Events.TASK_CREATED, record, source="tasks")
        await self.activity.record(f"Task queued: {name}", source="tasks", task_id=task_id)
        self._wake.set()
        return record

    # ==================================================================
    # workers
    # ==================================================================
    async def _worker(self, index: int) -> None:
        logger.debug("worker %s online", index)
        while self._running:
            try:
                record = await self._claim_next()
                if record is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await self._run(record)
            except asyncio.CancelledError:
                logger.debug("worker %s cancelled", index)
                raise
            except Exception:
                logger.exception("worker %s crashed -- restarting loop", index)
                await self.activity.record(
                    f"Task worker {index} hit an unexpected error and recovered.", level="error", source="tasks"
                )
                await asyncio.sleep(1.0)

    async def _claim_next(self) -> Optional[Dict[str, Any]]:
        """Atomic claim: the UPDATE only succeeds for whichever worker gets
        there first, so two workers can never run the same task."""
        await self._promote_waiting()

        candidates = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE status = ? AND (eta IS NULL OR eta <= ?)"
            " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
            " WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, created_at ASC LIMIT 5",
            (Status.QUEUED, now_iso()),
        )
        for candidate in candidates:
            claimed = await self.db.execute(
                "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), heartbeat = ?"
                " WHERE id = ? AND status = ?",
                (Status.RUNNING, now_iso(), now_iso(), candidate["id"], Status.QUEUED),
            )
            if claimed:
                return await self.get_raw(candidate["id"])
        return None

    async def _promote_waiting(self) -> None:
        waiting = await self.db.fetch_all(
            "SELECT id FROM tasks WHERE status = ?", (Status.WAITING,)
        )
        for row in waiting:
            unfinished = await self.db.fetch_value(
                "SELECT COUNT(*) FROM task_deps d JOIN tasks t ON t.id = d.depends_on"
                " WHERE d.task_id = ? AND t.status NOT IN ('completed','cancelled','failed')",
                (row["id"],),
                default=0,
            )
            if not unfinished:
                failed_deps = await self.db.fetch_value(
                    "SELECT COUNT(*) FROM task_deps d JOIN tasks t ON t.id = d.depends_on"
                    " WHERE d.task_id = ? AND t.status IN ('failed','cancelled')",
                    (row["id"],),
                    default=0,
                )
                if failed_deps:
                    await self._update(
                        row["id"], status=Status.FAILED,
                        error="A task this one depended on did not complete.", finished_at=now_iso(),
                    )
                else:
                    await self._update(row["id"], status=Status.QUEUED)

    # ==================================================================
    async def _run(self, record: Dict[str, Any]) -> None:
        task_id = record["id"]
        handler = self._handlers.get(record["type"])
        if handler is None:
            await self._finish_failed(task_id, f"No handler registered for type '{record['type']}'.")
            return

        context = TaskContext(self, record)
        runner = asyncio.create_task(handler(context), name=f"task:{task_id}")
        self._running_tasks[task_id] = runner

        bus.publish(Events.TASK_STARTED, await self.get(task_id), source="tasks")
        await self.activity.record(f"Task started: {record['name']}", source="tasks", task_id=task_id)
        started = time.monotonic()

        try:
            result = await runner
            duration = time.monotonic() - started
            await self._update(
                task_id,
                status=Status.COMPLETED,
                progress=1.0,
                result=dumps(result) if result is not None else None,
                finished_at=now_iso(),
                current_step="Completed",
                error=None,
            )
            await self.log(task_id, f"Completed in {humanize_duration(duration)}.")
            final = await self.get(task_id)
            bus.publish(Events.TASK_COMPLETED, final, source="tasks")
            await self.activity.record(
                f"Task completed: {record['name']} ({humanize_duration(duration)})",
                source="tasks", task_id=task_id,
            )
            if self.notifications and record["priority"] in (Priority.CRITICAL, Priority.HIGH):
                await self.notifications.create(
                    title=f"Task finished: {record['name']}",
                    body=truncate(str(result), 300) if result else "",
                    priority="normal",
                    source="tasks",
                    action={"kind": "open_task", "task_id": task_id},
                )

        except TaskPaused:
            await self._update(task_id, status=Status.PAUSED, current_step="Paused")
            await self.log(task_id, "Paused. Progress is checkpointed and will resume where it left off.")
            bus.publish(Events.TASK_PAUSED, await self.get(task_id), source="tasks")

        except (TaskCancelled, asyncio.CancelledError):
            await self._update(
                task_id, status=Status.CANCELLED, finished_at=now_iso(), current_step="Cancelled"
            )
            await self.log(task_id, "Cancelled by the user. Resources cleaned up.", "warn")
            bus.publish(Events.TASK_CANCELLED, await self.get(task_id), source="tasks")
            await self.activity.record(f"Task cancelled: {record['name']}", level="warn",
                                       source="tasks", task_id=task_id)

        except Exception as exc:
            logger.exception("task %s failed", task_id)
            await self._handle_failure(task_id, record, exc)

        finally:
            self._running_tasks.pop(task_id, None)
            self._cancel_requests.discard(task_id)
            self._pause_requests.discard(task_id)
            self._wake.set()

    async def _handle_failure(self, task_id: str, record: Dict[str, Any], exc: Exception) -> None:
        current = await self.get_raw(task_id)
        retries = int(current.get("retries") or 0)
        max_retries = int(current.get("max_retries") or 0)
        message = f"{type(exc).__name__}: {exc}"

        if retries < max_retries:
            backoff = min(60 * (2 ** retries), 900)
            await self._update(
                task_id,
                status=Status.QUEUED,
                retries=retries + 1,
                error=message,
                eta=ahead(backoff),
                current_step=f"Retry {retries + 1}/{max_retries} in {humanize_duration(backoff)}",
            )
            await self.log(
                task_id,
                f"Failed ({message}). Retrying in {humanize_duration(backoff)} "
                f"from the last checkpoint -- attempt {retries + 1} of {max_retries}.",
                "warn",
            )
            bus.publish(Events.TASK_PROGRESS, await self.get(task_id), source="tasks")
        else:
            await self._finish_failed(task_id, message)

    async def _finish_failed(self, task_id: str, message: str) -> None:
        await self._update(
            task_id, status=Status.FAILED, error=message, finished_at=now_iso(), current_step="Failed"
        )
        await self.log(task_id, f"Failed permanently: {message}", "error")
        record = await self.get(task_id)
        bus.publish(Events.TASK_FAILED, record, source="tasks")
        await self.activity.record(
            f"Task failed: {record.get('name')} -- {message}", level="error", source="tasks", task_id=task_id
        )
        if self.notifications:
            await self.notifications.create(
                title=f"Task failed: {record.get('name')}",
                body=message,
                priority="high",
                source="tasks",
                action={"kind": "open_task", "task_id": task_id},
            )

    # ==================================================================
    # control
    # ==================================================================
    async def cancel(self, task_id: str) -> Dict[str, Any]:
        record = await self.get(task_id)
        if not record:
            raise KeyError(task_id)
        if record["status"] in Status.TERMINAL:
            return record
        if not record.get("cancellable", True):
            raise PermissionError(f"Task '{record['name']}' is marked non-cancellable.")

        self._cancel_requests.add(task_id)
        runner = self._running_tasks.get(task_id)
        if runner and not runner.done():
            # Give the handler a moment to notice cooperatively before forcing it.
            await asyncio.sleep(0.35)
            if not runner.done():
                runner.cancel()
        else:
            await self._update(
                task_id, status=Status.CANCELLED, finished_at=now_iso(), current_step="Cancelled"
            )
            bus.publish(Events.TASK_CANCELLED, await self.get(task_id), source="tasks")
        return await self.get(task_id)

    async def pause(self, task_id: str) -> Dict[str, Any]:
        record = await self.get(task_id)
        if not record or record["status"] in Status.TERMINAL:
            return record or {}
        if record["status"] == Status.QUEUED:
            await self._update(task_id, status=Status.PAUSED, current_step="Paused before starting")
            bus.publish(Events.TASK_PAUSED, await self.get(task_id), source="tasks")
        else:
            self._pause_requests.add(task_id)
        return await self.get(task_id)

    async def resume(self, task_id: str) -> Dict[str, Any]:
        record = await self.get(task_id)
        if not record:
            raise KeyError(task_id)
        if record["status"] != Status.PAUSED:
            return record
        self._pause_requests.discard(task_id)
        await self._update(task_id, status=Status.QUEUED, current_step="Resuming", eta=None)
        await self.log(task_id, "Resumed from checkpoint.")
        bus.publish(Events.TASK_RESUMED, await self.get(task_id), source="tasks")
        self._wake.set()
        return await self.get(task_id)

    async def retry(self, task_id: str) -> Dict[str, Any]:
        record = await self.get(task_id)
        if not record:
            raise KeyError(task_id)
        await self._update(
            task_id, status=Status.QUEUED, error=None, retries=0, eta=None,
            finished_at=None, current_step="Requeued by the user",
        )
        await self.log(task_id, "Manually requeued.")
        self._wake.set()
        return await self.get(task_id)

    async def prioritise(self, task_id: str, priority: str) -> Dict[str, Any]:
        if priority not in Priority.ORDER:
            raise ValueError(f"Unknown priority '{priority}'.")
        await self._update(task_id, priority=priority)
        self._wake.set()
        return await self.get(task_id)

    async def stop_all(self) -> Dict[str, Any]:
        """The emergency stop. Cancels everything cancellable, immediately."""
        active = await self.db.fetch_all(
            "SELECT id, name, cancellable FROM tasks WHERE status IN (?,?,?,?)",
            Status.ACTIVE,
        )
        cancelled, skipped = [], []
        for row in active:
            if not row["cancellable"]:
                skipped.append(row["name"])
                continue
            try:
                await self.cancel(row["id"])
                cancelled.append(row["name"])
            except Exception:
                logger.exception("failed to cancel %s", row["id"])
                skipped.append(row["name"])

        await self.activity.record(
            f"EMERGENCY STOP: cancelled {len(cancelled)} task(s).", level="warn", source="tasks"
        )
        return {"cancelled": cancelled, "skipped": skipped, "count": len(cancelled)}

    # ==================================================================
    # persistence helpers
    # ==================================================================
    async def _update(self, task_id: str, **fields: Any) -> None:
        updates = {k: v for k, v in fields.items() if v is not None or k in ("error", "eta", "finished_at")}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self.db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", (*updates.values(), task_id)
        )

    async def log(self, task_id: str, message: str, level: str = "info",
                  meta: Optional[Dict[str, Any]] = None) -> None:
        await self.db.execute(
            "INSERT INTO task_logs (task_id, ts, level, message, meta) VALUES (?,?,?,?,?)",
            (task_id, now_iso(), level, truncate(message, 2000), dumps(meta or {})),
        )
        bus.publish(
            Events.TASK_LOG,
            {"task_id": task_id, "message": message, "level": level, "ts": now_iso()},
            source="tasks",
        )

    # ==================================================================
    # reads
    # ==================================================================
    async def get_raw(self, task_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return dict(row) if row else {}

    async def get(self, task_id: str) -> Dict[str, Any]:
        row = await self.get_raw(task_id)
        return _decode(row) if row else {}

    async def list(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        active_only: bool = False,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        elif active_only:
            sql += " AND status IN ('queued','running','paused','waiting')"
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += (
            " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2"
            " WHEN 'waiting' THEN 3 ELSE 4 END, created_at DESC LIMIT ?"
        )
        params.append(limit)
        return [_decode(r) for r in await self.db.fetch_all(sql, params)]

    async def logs(self, task_id: str, limit: int = 300) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY id ASC LIMIT ?", (task_id, limit)
        )
        for row in rows:
            row["meta"] = loads(row.get("meta"), {})
        return rows

    async def stats(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        counts = {row["status"]: row["n"] for row in rows}
        finished = counts.get(Status.COMPLETED, 0) + counts.get(Status.FAILED, 0)
        return {
            "counts": counts,
            "active": sum(counts.get(s, 0) for s in Status.ACTIVE),
            "queue_depth": counts.get(Status.QUEUED, 0),
            "workers": self.worker_status(),
            "success_rate": round(counts.get(Status.COMPLETED, 0) / finished, 3) if finished else None,
            "running": self._running,
            "handlers": self.handler_types(),
        }

    async def purge(self, keep_days: int = 30) -> int:
        from ..core.util import since

        return await self.db.execute(
            "DELETE FROM tasks WHERE status IN ('completed','failed','cancelled') AND created_at < ?",
            (since(keep_days * 86400),),
        )


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["params"] = loads(item.get("params"), {})
    item["result"] = loads(item.get("result"), None)
    item["checkpoint"] = loads(item.get("checkpoint"), {})
    item["cancellable"] = bool(item.get("cancellable", 1))
    item["progress"] = float(item.get("progress") or 0.0)
    return item
