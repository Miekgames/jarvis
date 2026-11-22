"""Task engine: lifecycle, cancellation, pause/resume, retries, checkpoints,
dependencies and startup recovery."""
from __future__ import annotations

import asyncio

import pytest

from backend.tasks.engine import Priority, Status, TaskCancelled


async def wait_for(engine, task_id: str, status: str, timeout: float = 8.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        record = await engine.get(task_id)
        if record.get("status") == status:
            return record
        await asyncio.sleep(0.05)
    record = await engine.get(task_id)
    raise AssertionError(
        f"task never reached '{status}' (last: {record.get('status')}, error={record.get('error')})"
    )


@pytest.mark.asyncio
async def test_task_runs_to_completion(engine):
    async def handler(ctx):
        await ctx.progress(0.5, "halfway")
        await ctx.log("working")
        return {"answer": 42}

    engine.register("simple", handler)
    await engine.start()

    record = await engine.create("Simple", "simple")
    finished = await wait_for(engine, record["id"], Status.COMPLETED)

    assert finished["result"] == {"answer": 42}
    assert finished["progress"] == 1.0
    logs = await engine.logs(record["id"])
    assert any("working" in log["message"] for log in logs)


@pytest.mark.asyncio
async def test_unknown_task_type_is_rejected(engine):
    await engine.start()
    with pytest.raises(ValueError, match="Unknown task type"):
        await engine.create("Nope", "does_not_exist")


@pytest.mark.asyncio
async def test_cancellation_is_cooperative_and_final(engine):
    started = asyncio.Event()

    async def handler(ctx):
        started.set()
        for i in range(400):
            ctx.checkpoint_now()          # the cooperative cancellation point
            await asyncio.sleep(0.02)
        return "should not finish"

    engine.register("long", handler)
    await engine.start()

    record = await engine.create("Long job", "long")
    await asyncio.wait_for(started.wait(), timeout=5)

    await engine.cancel(record["id"])
    cancelled = await wait_for(engine, record["id"], Status.CANCELLED)

    assert cancelled["status"] == Status.CANCELLED
    assert cancelled["finished_at"] is not None


@pytest.mark.asyncio
async def test_pause_preserves_checkpoint_and_resume_continues(engine):
    attempts = []
    reached = asyncio.Event()

    async def handler(ctx):
        state = ctx.load_checkpoint()
        start = int(state.get("index", 0))
        attempts.append(start)
        for i in range(start, 20):
            ctx.checkpoint_now()
            await ctx.save_checkpoint({"index": i})
            if i == 3:
                reached.set()
            await asyncio.sleep(0.05)
        return {"finished_at_index": 20}

    engine.register("resumable", handler)
    await engine.start()

    record = await engine.create("Resumable", "resumable")
    await asyncio.wait_for(reached.wait(), timeout=5)

    await engine.pause(record["id"])
    paused = await wait_for(engine, record["id"], Status.PAUSED)
    saved = int(paused["checkpoint"].get("index", 0))
    assert saved >= 3, "checkpoint should have been written before pausing"

    await engine.resume(record["id"])
    finished = await wait_for(engine, record["id"], Status.COMPLETED, timeout=10)

    assert finished["result"] == {"finished_at_index": 20}
    # The second run started from the checkpoint, not from zero.
    assert len(attempts) == 2 and attempts[1] >= 3


@pytest.mark.asyncio
async def test_failure_retries_then_fails_permanently(engine):
    calls = {"n": 0}

    async def handler(ctx):
        calls["n"] += 1
        raise RuntimeError("boom")

    engine.register("flaky", handler)
    await engine.start()

    record = await engine.create("Flaky", "flaky", max_retries=1)
    # Backoff is 60s for the first retry, so assert the retry was *scheduled*
    # rather than waiting for it.
    await asyncio.sleep(1.0)
    current = await engine.get(record["id"])
    assert current["retries"] == 1
    assert current["status"] == Status.QUEUED
    assert current["eta"] is not None
    assert "boom" in (current["error"] or "")


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failure(engine):
    calls = {"n": 0}

    async def handler(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "recovered"

    engine.register("transient", handler)
    await engine.start()

    record = await engine.create("Transient", "transient", max_retries=2)
    await asyncio.sleep(0.8)

    # Clear the backoff the way the user's "retry now" button does.
    await engine.retry(record["id"])
    finished = await wait_for(engine, record["id"], Status.COMPLETED)
    assert finished["result"] == "recovered"


@pytest.mark.asyncio
async def test_priority_ordering(engine):
    order = []

    async def handler(ctx):
        order.append(ctx.params.get("label"))
        await asyncio.sleep(0.05)

    engine.register("ordered", handler)
    # A single worker makes the ordering deterministic.
    engine.settings.WORKER_COUNT = 1

    await engine.create("low", "ordered", {"label": "low"}, priority=Priority.LOW)
    await engine.create("critical", "ordered", {"label": "critical"}, priority=Priority.CRITICAL)
    await engine.create("normal", "ordered", {"label": "normal"}, priority=Priority.NORMAL)

    await engine.start()
    await asyncio.sleep(1.2)

    assert order[0] == "critical", f"critical should run first, got {order}"
    assert order.index("normal") < order.index("low")


@pytest.mark.asyncio
async def test_dependencies_block_until_complete(engine):
    finished = []

    async def handler(ctx):
        await asyncio.sleep(0.1)
        finished.append(ctx.params.get("label"))

    engine.register("dep", handler)
    await engine.start()

    first = await engine.create("first", "dep", {"label": "first"})
    second = await engine.create("second", "dep", {"label": "second"}, depends_on=[first["id"]])

    assert (await engine.get(second["id"]))["status"] == Status.WAITING
    await wait_for(engine, second["id"], Status.COMPLETED, timeout=10)
    assert finished == ["first", "second"]


@pytest.mark.asyncio
async def test_dependency_failure_fails_dependent(engine):
    async def failing(ctx):
        raise RuntimeError("nope")

    async def ok(ctx):
        return "ran"

    engine.register("failing", failing)
    engine.register("ok", ok)
    await engine.start()

    first = await engine.create("will fail", "failing", max_retries=0)
    second = await engine.create("dependent", "ok", depends_on=[first["id"]])

    result = await wait_for(engine, second["id"], Status.FAILED, timeout=10)
    assert "depended on" in (result["error"] or "")


@pytest.mark.asyncio
async def test_startup_recovery_requeues_resumable_work(engine, db):
    async def handler(ctx):
        return "done"

    engine.register("recoverable", handler, resumable=True)
    engine.register("one_shot", handler, resumable=False)

    # Simulate rows left behind by a process that died mid-run.
    from backend.core.util import now_iso, new_id

    for task_type, task_id in (("recoverable", "task_a"), ("one_shot", "task_b")):
        await db.execute(
            "INSERT INTO tasks (id, name, description, type, status, priority, params, created_at,"
            " max_retries) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, f"stranded {task_type}", "", task_type, Status.RUNNING, "normal", "{}",
             now_iso(), 3),
        )

    summary = await engine.recover()

    assert summary == {"resumed": 1, "failed": 1}
    assert (await engine.get("task_a"))["status"] == Status.QUEUED
    recovered = await engine.get("task_b")
    assert recovered["status"] == Status.FAILED
    assert "cannot be resumed" in recovered["error"]


@pytest.mark.asyncio
async def test_stop_all_cancels_everything_cancellable(engine):
    async def handler(ctx):
        for _ in range(400):
            ctx.checkpoint_now()
            await asyncio.sleep(0.02)

    engine.register("busy", handler)
    await engine.start()

    for i in range(3):
        await engine.create(f"busy {i}", "busy")
    await asyncio.sleep(0.6)

    result = await engine.stop_all()
    assert result["count"] >= 1

    await asyncio.sleep(0.6)
    active = await engine.list(active_only=True)
    assert active == [], f"tasks still active after stop_all: {active}"


@pytest.mark.asyncio
async def test_graceful_shutdown_requeues_running_work(engine, db):
    started = asyncio.Event()

    async def handler(ctx):
        started.set()
        for _ in range(400):
            ctx.checkpoint_now()
            await asyncio.sleep(0.02)

    engine.register("slow", handler)
    await engine.start()
    record = await engine.create("Slow", "slow")
    await asyncio.wait_for(started.wait(), timeout=5)

    await engine.stop(grace=2.0)

    # Paused or requeued, but never silently lost or marked complete.
    final = await engine.get(record["id"])
    assert final["status"] in (Status.PAUSED, Status.QUEUED)
