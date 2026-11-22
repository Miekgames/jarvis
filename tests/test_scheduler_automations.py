"""Cron parsing, the scheduler, and the automation engine."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.automations.engine import AutomationEngine
from backend.core.events import Event, bus
from backend.scheduler.cron import CronError, CronExpression, next_run, validate
from backend.scheduler.service import Kind


# ===========================================================================
# cron
# ===========================================================================
def test_cron_every_minute():
    expression = CronExpression("* * * * *")
    base = datetime(2026, 6, 1, 12, 0)
    assert expression.next_after(base) == datetime(2026, 6, 1, 12, 1)


def test_cron_daily_at_eight():
    expression = CronExpression("0 8 * * *")
    assert expression.next_after(datetime(2026, 6, 1, 12, 0)) == datetime(2026, 6, 2, 8, 0)
    assert expression.next_after(datetime(2026, 6, 1, 7, 0)) == datetime(2026, 6, 1, 8, 0)


def test_cron_monday_mornings():
    # 1 June 2026 is a Monday.
    expression = CronExpression("0 8 * * 1")
    following = expression.next_after(datetime(2026, 6, 1, 9, 0))
    assert following == datetime(2026, 6, 8, 8, 0)
    assert following.weekday() == 0


def test_cron_step_and_list_and_range():
    assert CronExpression("*/15 * * * *").next_after(datetime(2026, 6, 1, 12, 1)) == \
        datetime(2026, 6, 1, 12, 15)
    assert CronExpression("0 9,17 * * *").next_after(datetime(2026, 6, 1, 10, 0)) == \
        datetime(2026, 6, 1, 17, 0)
    assert CronExpression("0 9 * * 1-5").next_after(datetime(2026, 6, 5, 10, 0)) == \
        datetime(2026, 6, 8, 9, 0)   # Friday 10:00 -> Monday 09:00


def test_cron_named_days_and_months():
    assert CronExpression("0 8 * * mon").next_after(datetime(2026, 6, 2, 9, 0)) == \
        datetime(2026, 6, 8, 8, 0)
    assert CronExpression("0 0 1 jan *").next_after(datetime(2026, 6, 1)) == \
        datetime(2027, 1, 1, 0, 0)


def test_cron_shortcuts():
    assert CronExpression("@daily").next_after(datetime(2026, 6, 1, 12, 0)) == \
        datetime(2026, 6, 2, 0, 0)
    assert CronExpression("@hourly").next_after(datetime(2026, 6, 1, 12, 30)) == \
        datetime(2026, 6, 1, 13, 0)


def test_cron_sunday_is_zero_or_seven():
    assert CronExpression("0 0 * * 0").fields[4] == CronExpression("0 0 * * 7").fields[4]


@pytest.mark.parametrize("expression", [
    "* * * *",              # too few fields
    "60 * * * *",           # minute out of range
    "* 24 * * *",           # hour out of range
    "* * * * 8",            # weekday out of range
    "5-1 * * * *",          # inverted range
    "*/0 * * * *",          # zero step
    "abc * * * *",          # nonsense
])
def test_cron_rejects_invalid_expressions(expression):
    assert validate(expression) is not None
    with pytest.raises(CronError):
        CronExpression(expression)


# ===========================================================================
# scheduler
# ===========================================================================
@pytest.mark.asyncio
async def test_one_shot_schedule_fires_once(scheduler, notifications):
    from backend.core.util import ahead

    await scheduler.create(
        name="Test reminder",
        action={"kind": "notify", "title": "Ping", "body": "time is up"},
        kind=Kind.ONCE,
        run_at=ahead(-1),          # already due
    )

    fired = await scheduler.tick()
    assert fired == 1

    items = await notifications.list()
    assert any(item["title"] == "Ping" for item in items)

    # A one-shot must not fire twice.
    assert await scheduler.tick() == 0


@pytest.mark.asyncio
async def test_interval_schedule_reschedules_itself(scheduler, db):
    from backend.core.util import ahead

    record = await scheduler.create(
        name="Every minute",
        action={"kind": "notify", "title": "tick"},
        kind=Kind.INTERVAL,
        interval_seconds=60,
        run_at=ahead(-1),
    )
    await scheduler.tick()

    updated = await scheduler.get(record["id"])
    assert updated["enabled"] is True
    assert updated["next_run"] is not None
    assert updated["fire_count"] == 1


@pytest.mark.asyncio
async def test_missed_occurrence_beyond_grace_is_skipped(scheduler, notifications, db):
    from backend.core.util import since

    record = await scheduler.create(
        name="Long overdue",
        action={"kind": "notify", "title": "should not appear"},
        kind=Kind.INTERVAL,
        interval_seconds=3600,
        misfire_grace=60,
    )
    # Pretend the machine was asleep for two hours.
    await db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (since(7200), record["id"]))

    await scheduler.tick()

    items = await notifications.list()
    assert not any(item["title"] == "should not appear" for item in items), \
        "a badly-missed occurrence should be skipped, not fired late"
    assert (await scheduler.get(record["id"]))["next_run"] is not None


@pytest.mark.asyncio
async def test_cron_schedule_validation_at_creation(scheduler):
    with pytest.raises(ValueError):
        await scheduler.create("Bad", {"kind": "notify"}, kind=Kind.CRON, cron="not a cron")


@pytest.mark.asyncio
async def test_one_shot_without_time_is_rejected(scheduler):
    with pytest.raises(ValueError, match="run_at"):
        await scheduler.create("No time", {"kind": "notify"}, kind=Kind.ONCE)


@pytest.mark.asyncio
async def test_schedule_can_create_a_task(scheduler, engine, notifications, db):
    async def handler(ctx):
        return "ran from a schedule"

    engine.register("scheduled_work", handler)
    await engine.start()
    scheduler.actions.services["tasks"] = engine

    from backend.core.util import ahead

    await scheduler.create(
        name="Nightly job",
        action={"kind": "create_task", "type": "scheduled_work", "name": "Nightly"},
        kind=Kind.ONCE,
        run_at=ahead(-1),
    )
    await scheduler.tick()
    await asyncio.sleep(0.6)

    tasks = await engine.list()
    assert any(task["name"] == "Nightly" for task in tasks)


# ===========================================================================
# automations
# ===========================================================================
@pytest.mark.asyncio
async def test_automation_fires_on_matching_event(automations, notifications):
    await automations.create(
        name="Alert on failure",
        trigger={"kind": "event", "event": "task.failed"},
        actions=[{"kind": "notify", "title": "A task failed", "priority": "high"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.failed", {"name": "Research"})
    await asyncio.sleep(0.3)

    items = await notifications.list()
    assert any(item["title"] == "A task failed" for item in items)


@pytest.mark.asyncio
async def test_automation_ignores_non_matching_event(automations, notifications):
    await automations.create(
        name="Only failures",
        trigger={"kind": "event", "event": "task.failed"},
        actions=[{"kind": "notify", "title": "should not fire"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.completed", {"name": "Research"})
    await asyncio.sleep(0.3)

    items = await notifications.list()
    assert not any(item["title"] == "should not fire" for item in items)


@pytest.mark.asyncio
async def test_wildcard_trigger_matches_prefix(automations, notifications):
    await automations.create(
        name="Any task event",
        trigger={"kind": "event", "event": "task.*"},
        actions=[{"kind": "notify", "title": "task event"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.completed", {"name": "X"})
    await asyncio.sleep(0.3)
    assert any(i["title"] == "task event" for i in await notifications.list())


@pytest.mark.asyncio
async def test_conditions_block_the_action(automations, notifications, settings_store):
    await settings_store.set("active_mode", "gaming")

    await automations.create(
        name="Work-mode only",
        trigger={"kind": "event", "event": "task.failed"},
        conditions=[{"kind": "mode_is", "modes": ["work"]}],
        actions=[{"kind": "notify", "title": "should be blocked"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.failed", {"name": "X"})
    await asyncio.sleep(0.3)

    assert not any(i["title"] == "should be blocked" for i in await notifications.list())


@pytest.mark.asyncio
async def test_skipped_runs_are_recorded_with_a_reason(automations, settings_store):
    await settings_store.set("active_mode", "gaming")
    record = await automations.create(
        name="Blocked",
        trigger={"kind": "event", "event": "task.failed"},
        conditions=[{"kind": "mode_is", "modes": ["work"]}],
        actions=[{"kind": "notify", "title": "nope"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.failed", {"name": "X"})
    await asyncio.sleep(0.3)

    runs = await automations.runs(record["id"])
    assert runs and runs[0]["status"] == "skipped"
    assert "conditions" in runs[0]["detail"]["reason"]


@pytest.mark.asyncio
async def test_disabled_automation_does_not_fire(automations, notifications):
    await automations.create(
        name="Off",
        trigger={"kind": "event", "event": "task.failed"},
        actions=[{"kind": "notify", "title": "disabled"}],
        enabled=False,
    )
    await automations.start()

    await bus.publish_and_wait("task.failed", {"name": "X"})
    await asyncio.sleep(0.3)
    assert not any(i["title"] == "disabled" for i in await notifications.list())


@pytest.mark.asyncio
async def test_automation_requires_at_least_one_action(automations):
    with pytest.raises(ValueError):
        await automations.create(
            name="Empty",
            trigger={"kind": "event", "event": "task.failed"},
            actions=[],
        )


@pytest.mark.asyncio
async def test_preview_is_human_readable(automations):
    record = await automations.create(
        name="Evening wind-down",
        trigger={"kind": "event", "event": "calendar.upcoming"},
        conditions=[{"kind": "time_between", "start": "17:00", "end": "23:00"}],
        actions=[
            {"kind": "notify", "title": "Wrap up", "priority": "normal"},
            {"kind": "set_mode", "mode": "away"},
        ],
    )
    preview = AutomationEngine.preview(record)

    assert "calendar.upcoming" in preview["when"]
    assert any("17:00" in condition for condition in preview["conditions"])
    assert any("notification" in action for action in preview["actions"])
    assert any("away mode" in action for action in preview["actions"])
    assert preview["summary"]


@pytest.mark.asyncio
async def test_failed_action_is_reported_not_swallowed(automations):
    record = await automations.create(
        name="Broken action",
        trigger={"kind": "event", "event": "task.failed"},
        actions=[{"kind": "definitely_not_a_real_action"}],
    )
    await automations.start()

    await bus.publish_and_wait("task.failed", {"name": "X"})
    await asyncio.sleep(0.3)

    runs = await automations.runs(record["id"])
    assert runs[0]["status"] == "failed"
