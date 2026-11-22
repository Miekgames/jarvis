"""
Minimal 5-field cron parser (minute hour day-of-month month day-of-week).

Supports `*`, `*/step`, `a,b,c`, `a-b` and `a-b/step`, plus the usual named
shortcuts (@daily, @hourly, ...). Written rather than pulled in as a dependency
because this is the only cron surface the app needs and a 150-line parser with
tests beats another install step.

Day-of-week: 0 or 7 = Sunday, matching crontab convention.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Set

FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
FIELD_NAMES = ["minute", "hour", "day of month", "month", "day of week"]

SHORTCUTS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


class CronError(ValueError):
    pass


class CronExpression:
    def __init__(self, expression: str):
        self.raw = (expression or "").strip()
        text = SHORTCUTS.get(self.raw.lower(), self.raw)
        fields = text.split()
        if len(fields) != 5:
            raise CronError(
                f"Cron expression needs 5 fields (minute hour day month weekday), got {len(fields)}: '{expression}'"
            )
        self.fields: List[Set[int]] = [
            _parse_field(field, index) for index, field in enumerate(fields)
        ]

    # ------------------------------------------------------------------
    def matches(self, moment: datetime) -> bool:
        minute, hour, day, month, weekday = self.fields
        # cron weekday: Sunday is 0; python weekday(): Monday is 0
        dow = (moment.weekday() + 1) % 7
        return (
            moment.minute in minute
            and moment.hour in hour
            and moment.month in month
            and self._day_matches(moment, day, weekday, dow)
        )

    @staticmethod
    def _day_matches(moment: datetime, day: Set[int], weekday: Set[int], dow: int) -> bool:
        # crontab semantics: if both day-of-month and day-of-week are
        # restricted, either matching is enough.
        day_restricted = len(day) < 31
        dow_restricted = len(weekday) < 7
        day_hit = moment.day in day
        dow_hit = dow in weekday
        if day_restricted and dow_restricted:
            return day_hit or dow_hit
        return day_hit and dow_hit

    # ------------------------------------------------------------------
    def next_after(self, after: Optional[datetime] = None, horizon_days: int = 400) -> Optional[datetime]:
        """The next matching minute strictly after `after`."""
        moment = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = moment + timedelta(days=horizon_days)
        while moment < limit:
            if self.matches(moment):
                return moment
            # Skip whole days quickly when the date fields can't match.
            if moment.month not in self.fields[3]:
                moment = _start_of_next_month(moment)
                continue
            moment += timedelta(minutes=1)
        return None

    def describe(self) -> str:
        return f"cron({self.raw})"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CronExpression {self.raw}>"


def _start_of_next_month(moment: datetime) -> datetime:
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0)
    return moment.replace(month=moment.month + 1, day=1, hour=0, minute=0)


def _parse_field(field: str, index: int) -> Set[int]:
    low, high = FIELD_RANGES[index]
    values: Set[int] = set()

    for part in field.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"Empty value in the {FIELD_NAMES[index]} field.")

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise CronError(f"Bad step '/{step_text}' in the {FIELD_NAMES[index]} field.")
            if step < 1:
                raise CronError(f"Step must be positive in the {FIELD_NAMES[index]} field.")

        if part in ("*", ""):
            start, end = low, high
        elif "-" in part[1:]:
            start_text, _, end_text = part.partition("-")
            start, end = _to_int(start_text, index), _to_int(end_text, index)
        else:
            start = end = _to_int(part, index)

        if start > end:
            raise CronError(f"Range {start}-{end} is inverted in the {FIELD_NAMES[index]} field.")
        if start < low or end > high:
            raise CronError(
                f"{FIELD_NAMES[index]} value out of range ({low}-{high}): '{field}'"
            )
        values.update(range(start, end + 1, step))

    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return values


def _to_int(text: str, index: int) -> int:
    text = text.strip().lower()
    if index == 3 and text in _MONTHS:
        return _MONTHS[text]
    if index == 4 and text in _DAYS:
        return _DAYS[text]
    try:
        return int(text)
    except ValueError:
        raise CronError(f"'{text}' is not valid in the {FIELD_NAMES[index]} field.")


def next_run(expression: str, after: Optional[datetime] = None) -> Optional[datetime]:
    return CronExpression(expression).next_after(after)


def validate(expression: str) -> Optional[str]:
    """Return None if valid, otherwise the error message."""
    try:
        CronExpression(expression)
        return None
    except CronError as exc:
        return str(exc)
