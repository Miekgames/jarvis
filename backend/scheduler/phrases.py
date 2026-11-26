"""When a reminder should go off, from the way people say it.

"Remind me at 7pm", "in 20 minutes", "tomorrow morning", "every Monday at 9",
"every weekday at 7:30", "every 2 hours", "on the 1st of every month".

The schedule comes from these words, never from a model's arithmetic: a small
model asked for "every weekday at 7:30" writes cron that fires on Sundays.
Anything this can't read is returned as None, and the owner is asked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from ..services import event_dates

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_SHORT = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
          "sat": 5, "sun": 6}
_CRON_DAY = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}     # Python weekday -> cron
_PARTS = {"noon": "12:00", "midday": "12:00", "midnight": "00:00", "morning": "09:00",
          "afternoon": "15:00", "evening": "18:00", "tonight": "20:00", "night": "21:00"}
_COUNT = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "half an": 0.5}
_UNIT_S = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
           "day": 86400, "week": 604800}


@dataclass
class When:
    kind: str                         # once | cron | interval
    run_at: Optional[datetime] = None  # first (or only) time, local
    cron: str = ""
    interval_seconds: int = 0
    says: str = ""                    # how to say it back
    assumed: str = ""                 # what was filled in, to mention


def _clock(text: str) -> Optional[str]:
    said = event_dates.time_in_words(text)
    if said:
        return said
    m = re.search(r"\bat\s+(\d{1,2})(?![\d:.])(?!\s*(?:day|week|month|minute|hour|min|hr))", text)
    if m and 0 <= int(m.group(1)) <= 23:
        hour = int(m.group(1))
        # "at 7" with no am/pm. For a routine, the waking-hours reading; for
        # a one-off, `_soonest` picks whichever of 07:00 and 19:00 comes next
        # (the "?" marks it as either).
        bare = 1 <= hour <= 11
        if 1 <= hour <= 6:
            hour += 12
        return f"{hour:02d}:00" + ("?" if bare else "")
    for word, clock in _PARTS.items():
        if re.search(rf"\b{word}\b", text):
            return clock
    return None


def _hm(clock: str):
    hour, minute = clock.rstrip("?").split(":")
    return int(hour), int(minute)


def _soonest(now: datetime, clock: str) -> datetime:
    """The next time a clock reading comes round: "7" with no am/pm is either."""
    h, mi = _hm(clock)
    hours = sorted({h % 12, h % 12 + 12}) if clock.endswith("?") else [h]
    options = []
    for hour in hours:
        at = now.replace(hour=hour, minute=mi)
        options.append(at if at > now else at + timedelta(days=1))
    return min(options)


def _days_named(text: str) -> List[int]:
    found = []
    for n, day in enumerate(_DAYS):
        if re.search(rf"\b{day}s?\b", text):
            found.append(n)
    for short, n in _SHORT.items():
        if re.search(rf"\b{short}\b", text) and n not in found:
            found.append(n)
    return sorted(found)


def _amount(word: str) -> Optional[float]:
    word = word.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", word):
        return float(word)
    return _COUNT.get(word)


def _nice_time(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _nice_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return day.strftime("%A %d %B").replace(" 0", " ")


def parse(text: str, now: Optional[datetime] = None) -> Optional[When]:
    """The schedule a phrase describes, or None if it doesn't describe one."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    today = now.date()
    said = " ".join((text or "").lower().replace("’", "'").split())
    if not said:
        return None
    clock = _clock(said)

    repeating = re.search(r"\bevery\b|\beach\b|\bdaily\b|\bweekly\b|\bhourly\b|\bmonthly\b|"
                          r"\bweekdays\b|\bweekends\b|\byearly\b|\bannually\b", said)
    if repeating:
        # every N minutes / hours / days
        m = re.search(r"\bevery\s+(?:(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|ten|twelve|"
                      r"fifteen|twenty|thirty|half an)\s+)?(second|sec|minute|min|hour|hr|day|week)s?\b",
                      said)
        if m and not (m.group(2) in ("day", "week") and m.group(1) in (None, "a", "one")):
            n = _amount(m.group(1) or "1") or 1
            seconds = int(n * _UNIT_S[m.group(2)])
            if seconds < 60:
                return None
            first = now + timedelta(seconds=seconds)
            if clock and m.group(2) in ("day", "week"):
                h, mi = _hm(clock)
                first = now.replace(hour=h, minute=mi)
                if first <= now:
                    first += timedelta(days=1)
            unit = {"sec": "second", "min": "minute", "hr": "hour"}.get(m.group(2), m.group(2))
            n_text = f"{int(n) if n == int(n) else n} {unit}s" if n != 1 else unit
            return When("interval", run_at=first, interval_seconds=seconds,
                        says=f"every {n_text}" + (f" from {_nice_time(first)}" if seconds >= 3600 else ""))
        if re.search(r"\bhourly\b|\bevery hour\b", said):
            return When("interval", run_at=now + timedelta(hours=1), interval_seconds=3600,
                        says="every hour")

        assumed = ""
        if not clock:
            clock, assumed = "09:00", "You didn't say a time, so it's set for 09:00."
        h, mi = _hm(clock)

        if re.search(r"\bweekdays?\b|\bwork ?days?\b", said) and not re.search(r"\bweekends?\b", said):
            return When("cron", cron=f"{mi} {h} * * 1-5", says=f"every weekday at {clock.rstrip('?')}",
                        assumed=assumed)
        if re.search(r"\bweekends?\b", said):
            return When("cron", cron=f"{mi} {h} * * 0,6", says=f"every weekend day at {clock.rstrip('?')}",
                        assumed=assumed)
        days = _days_named(said)
        if days:
            names = [_DAYS[d].capitalize() for d in days]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            return When("cron", cron=f"{mi} {h} * * {','.join(str(_CRON_DAY[d]) for d in days)}",
                        says=f"every {listed} at {clock.rstrip('?')}", assumed=assumed)
        if re.search(r"\bmonth(?:ly)?\b", said):
            m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", said)
            day = int(m.group(1)) if m else today.day
            if not 1 <= day <= 31:
                return None
            note = assumed + (" Months without that day are skipped." if day > 28 else "")
            return When("cron", cron=f"{mi} {h} {day} * *",
                        says=f"on the {day}{_ordinal(day)} of every month at {clock.rstrip('?')}",
                        assumed=note.strip())
        if re.search(r"\byear(?:ly)?\b|\bannually\b", said):
            mentions = event_dates.find_dates(said, today)
            if not mentions:
                return None
            d = mentions[0].day
            return When("cron", cron=f"{mi} {h} {d.day} {d.month} *",
                        says=f"every year on {d.strftime('%d %B').lstrip('0')} at {clock.rstrip('?')}",
                        assumed=assumed)
        if re.search(r"\bweek(?:ly)?\b", said):
            d = today.weekday()
            return When("cron", cron=f"{mi} {h} * * {_CRON_DAY[d]}",
                        says=f"every {_DAYS[d].capitalize()} at {clock.rstrip('?')}", assumed=assumed)
        if re.search(r"\bday\b|\bdaily\b|\bmorning\b|\bevening\b|\bnight\b|\bafternoon\b", said):
            return When("cron", cron=f"{mi} {h} * * *", says=f"every day at {clock.rstrip('?')}", assumed=assumed)
        return None

    # -- once --------------------------------------------------------------------
    m = re.search(r"\bin\s+(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|ten|twelve|fifteen|"
                  r"twenty|thirty|half an)\s+(second|sec|minute|min|hour|hr|day|week)s?\b", said)
    if m and m.group(2) not in ("day", "week"):
        seconds = int((_amount(m.group(1)) or 0) * _UNIT_S[m.group(2)])
        if seconds < 30:
            return None
        at = now + timedelta(seconds=seconds)
        mins = round(seconds / 60)
        span = f"{mins} minute{'s' if mins != 1 else ''}" if seconds < 5400 else \
            f"{round(seconds / 3600, 1):g} hours"
        return When("once", run_at=at, says=f"in {span} (at {_nice_time(at)})")
    if re.search(r"\bin\s+half\s+an\s+hour\b", said):
        at = now + timedelta(minutes=30)
        return When("once", run_at=at, says=f"in 30 minutes (at {_nice_time(at)})")

    day = event_dates.date_in_words(said, today)
    if day is None:
        named = _days_named(said)
        if named:
            ahead = (named[0] - today.weekday()) % 7 or 7
            day = today + timedelta(days=ahead)
    assumed = ""
    if day is not None:
        if not clock:
            if day == today:
                return None                  # "today" with no time: ask
            clock, assumed = "09:00", "You didn't say a time, so it's set for 09:00."
        h, mi = _hm(clock)
        at = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=mi)
        if at <= now:
            return None
        return When("once", run_at=at, says=f"{_nice_day(day, today)} at {clock.rstrip('?')}", assumed=assumed)
    if clock:
        at = _soonest(now, clock)
        return When("once", run_at=at, says=f"{_nice_day(at.date(), today)} at {_nice_time(at)}")
    return None


def _ordinal(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
