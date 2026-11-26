"""Reminders by asking: "remind me at 7pm to call Dad", "every Monday at 9...".

When it goes off comes from the owner's own words (`scheduler.phrases`); what
the model put in the call is used only when the owner's latest message gives no
time at all (a follow-up like "make it 5pm").
"""
from __future__ import annotations

from .countdown_tools import _said
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:
    from ...scheduler import phrases
    from ...services.reminders import ReminderError

    async def _make(ctx, message: str, when: str):
        service = ctx.get("reminders")
        if service is None:
            return fail("Reminders are not available.")
        said, _ = await _said(ctx)
        timing = said if said and phrases.parse(said) is not None else when
        try:
            item = await service.add(message, timing)
        except ReminderError as exc:
            return fail(f"{exc} Nothing was scheduled. Ask the owner when.")
        lead = ("Repeating reminder set" if item["repeats"] else "Reminder set")
        text = (f"{lead}: “{item['message']}” — {item['says']}. "
                f"Next: {item['next_said']}. It will pop up as a notification in JARVIS and in "
                "Windows.")
        if item["assumed"]:
            text += " " + item["assumed"]
        return ok({"id": item["id"], "next": item["next"], "repeats": item["repeats"]}, text)

    @registry.add(
        "schedule_notification",
        "Schedule a notification / reminder, once or repeating: 'remind me at 7pm to call Dad', "
        "'remind me in 20 minutes', 'every weekday at 7:30 remind me to take my tablets', "
        "'notify me every 2 hours to stretch'. Pass what it should say and when, in the owner's words.",
        {"type": "object", "properties": {
            "message": {"type": "string", "description": "What the reminder should say"},
            "when": {"type": "string", "description": "When, in the owner's words, e.g. "
                                                      "'every Monday at 9am' or 'in 20 minutes'"},
        }, "required": ["message", "when"]},
        permission="tasks.create",
        category="reminder remind notification notify schedule alert every",
    )
    async def schedule_notification(ctx, message: str, when: str):
        return await _make(ctx, message, when)

    # The old name, kept so a model that reaches for it gets the same result.
    @registry.add(
        "set_reminder",
        "Create a reminder. Same as schedule_notification.",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "What to remind the user about"},
            "when": {"type": "string", "description": "When, in the owner's words"},
        }, "required": ["text", "when"]},
        permission="tasks.create",
        category="reminder remind scheduling",
    )
    async def set_reminder(ctx, text: str, when: str, priority: str = "normal"):
        return await _make(ctx, text, when)

    @registry.add(
        "list_reminders",
        "The reminders and scheduled notifications coming up, soonest first.",
        {"type": "object", "properties": {}},
        permission="tasks.create",
        category="reminder remind notification list upcoming",
    )
    async def list_reminders(ctx):
        service = ctx.get("reminders")
        if service is None:
            return fail("Reminders are not available.")
        items = await service.list()
        if not items:
            return ok([], "There are no reminders scheduled.")
        rows = [{"n": n, "says": i["message"], "when": i["says"], "next": i["next_said"]}
                for n, i in enumerate(items, 1)]
        return ok(rows, f"{len(rows)} reminder(s). Quote the 'next' times exactly.")

    @registry.add(
        "cancel_reminder",
        "Cancel a reminder or scheduled notification.",
        {"type": "object", "properties": {
            "which": {"type": "string", "description": "Words from it, or its number"},
        }, "required": ["which"]},
        permission="tasks.create",
        category="reminder remind notification cancel delete stop",
    )
    async def cancel_reminder(ctx, which: str):
        service = ctx.get("reminders")
        if service is None:
            return fail("Reminders are not available.")
        item = await service.find(which)
        if item is None:
            names = "; ".join(f"{n}. {i['message']} ({i['says']})"
                              for n, i in enumerate(await service.list(), 1))
            return fail(f"No single reminder matches “{which}”. "
                        + (f"There are: {names}." if names else "There are none."))
        await service.cancel(item["id"])
        return ok({"cancelled": item["message"]}, f"Cancelled the reminder “{item['message']}”.")
