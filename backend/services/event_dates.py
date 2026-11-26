"""Working out when something happens, from evidence.

A countdown to the wrong day is worse than no countdown, and the model is the
worst place to get the day from: a small local one will state a release date
that moved two years ago with complete confidence. So a date comes from one of
three places, in this order, and never from the model's memory:

  * **the owner's own words** -- "countdown to my holiday on 5 June";
  * **the calendar** -- Christmas does not need looking up;
  * **the web** -- several independent sites that agree with each other.

Reading dates off the web has one big trap: pages are full of dates that are
not the date. Search snippets open with when the article was written, carry
bylines, mention trailers, presales and the day it was *going* to come out
before it was delayed. So each mention is weighed by the words around it, each
site gets one vote per date however often it repeats itself, and an answer is
only certain when sites agree and nothing rivals it. Otherwise the owner is
asked, with what was found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = ("(January|February|March|April|May|June|July|August|September|October|"
          "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)")
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b\.?,?(?:\s+(\d{{4}})\b)?", re.I)
_MONTH_DAY = re.compile(
    rf"\b{_MONTH}\b\.?\s+(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b(?!:)(?:\s*,?\s+(\d{{4}})\b)?", re.I)
# "July 14-30, 2028" and "14th to 30th July 2028": something that lasts, and
# the day to count down to is the first.
_TO = r"\s*(?:-|–|—|to|through|until|till)\s*"
_MONTH_RANGE = re.compile(
    rf"\b{_MONTH}\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?{_TO}(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*{_MONTH})"
    r"(?:\s*,?\s+(?P<year>\d{4})\b)?", re.I)
_RANGE_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?{_TO}\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b\.?,?"
    r"(?:\s+(\d{4})\b)?", re.I)
# Between two dates, this makes the second the end of the first.
_BETWEEN = re.compile(
    r"^\s*,?\s*(?:-|–|—|to|through|until|till|and\s+ends?(?:\s+on)?|and\s+runs\s+until)\s*(?:on\s+)?"
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s*)?(?:the\s+)?$", re.I)
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2}|\d{2})\b")

# Dates that say when a page was written, not when anything happens.
_WRITTEN = [
    re.compile(rf"^\s*(?:{_MONTH}\s+\d{{1,2}},\s+\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|"
               r"\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)\s*[·—–|]\s*", re.I),
    re.compile(rf"(?:last\s+)?(?:updated|published|posted)(?:\s+on)?:?\s*(?:{_MONTH}\.?\s+\d{{1,2}}"
               rf"(?:,?\s+\d{{4}})?|\d{{1,2}}\s+{_MONTH}\.?(?:\s+\d{{4}})?)", re.I),
    re.compile(rf"(?:{_MONTH}\s+\d{{1,2}},\s+\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}})\s*\|\s*By\b", re.I),
]

_IS_THE_DATE = re.compile(
    r"release\s+date|releas(?:e|es|ed|ing)|arriv(?:e|es|ing)|in\s+(?:theaters|theatres|cinemas)|"
    r"premier|scheduled|kick\s?-?off|takes?\s+place|taking\s+place|(?:be|being)\s+(?:held|played)|"
    r"set\s+(?:for|to)|opens?\b|opening|launch|comes?\s+out|coming\s+out|coming\s+to|hits\b|begins?|"
    r"starts?|\bheld\b|falls\s+on|will\s+be\s+on|\bis\s+on\b|out\s+on|due\s+(?:out|on)|"
    r"celebrated|occurs?|lands?\s+on|runs?\s+from|\bdate\b", re.I)
_IS_ANOTHER_DATE = re.compile(
    r"trailer|teaser|pre-?sale|pre-?order|tickets?|booking|delay(?:ed)?\s+from|original(?:ly)?|"
    r"previous(?:ly)?|was\s+(?:set|scheduled|slated|due)|moved\s+from|"
    r"push(?:ed)?\s+(?:back\s+)?from|announced|announcement|leak|born|died|re-?release|"
    r"anniversary|early\s+access|\bbeta\b|filming|production|world\s+premiere|red\s+carpet|"
    r"\bdigital|blu-?ray|\b4k\b|\bdvd\b|\bvod\b|closing|ends?\s+on|final(?:e)?\s+(?:is\s+)?on", re.I)

_FILLER = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "at", "is", "it", "my", "our",
    "next", "upcoming", "when", "date", "release", "released", "coming", "out", "movie", "film",
    "event", "countdown", "until", "till", "with", "from",
}

_FIXED_DAYS = {
    "christmas": (12, 25), "christmas day": (12, 25), "xmas": (12, 25),
    "christmas eve": (12, 24), "boxing day": (12, 26),
    "new year": (1, 1), "new years": (1, 1), "new year's": (1, 1), "new years day": (1, 1),
    "new year's day": (1, 1), "new years eve": (12, 31), "new year's eve": (12, 31),
    "halloween": (10, 31), "valentines": (2, 14), "valentine's": (2, 14),
    "valentines day": (2, 14), "valentine's day": (2, 14),
    "april fools": (4, 1), "april fool's day": (4, 1), "april fools day": (4, 1),
    "st patricks day": (3, 17), "st patrick's day": (3, 17), "saint patrick's day": (3, 17),
    "independence day": (7, 4), "fourth of july": (7, 4), "4th of july": (7, 4),
    "july 4th": (7, 4), "bonfire night": (11, 5), "earth day": (4, 22),
}

_PERSONAL = re.compile(
    r"^\s*(?:my|our|his|her|their|mum's|mom's|dad's)\b|"
    r"\b\w+['’]s\s+(?:birthday|wedding|party|graduation|anniversary)\b|"
    r"^\s*(?:the\s+)?(?:wedding|holiday|vacation|trip|exam|exams|graduation|birthday|party|"
    r"moving day|interview|appointment)\s*$", re.I)


def tidy(event: str) -> str:
    """An event's name, without the words the request came wrapped in."""
    name = " ".join((event or "").replace("\n", " ").split()).strip(" .!?\"'“”")
    name = re.sub(r"^(?:a\s+|the\s+)?count\s?-?down\s+(?:to|for|until|till)\s+", "", name, flags=re.I)
    name = re.sub(r"(?:['’]s)?\s+(?:count\s?-?down|release(?:\s+(?:date|day))?|date)$", "", name, flags=re.I)
    name = name[:70].strip()
    if name and name == name.lower():
        small = {"of", "the", "and", "to", "in", "on", "for", "a", "an", "at", "vs"}
        words = name.split()
        name = " ".join(w if (i and w in small) else w[:1].upper() + w[1:]
                        for i, w in enumerate(words))
    return name


def is_personal(event: str) -> bool:
    """Something only the owner knows the date of."""
    return bool(_PERSONAL.search(event or ""))


def significant(event: str) -> List[str]:
    """The words that identify an event."""
    words = re.findall(r"[a-z0-9]+", (event or "").lower().replace("'", "").replace("’", ""))
    kept = [w for w in words if w not in _FILLER and (len(w) > 1 or w.isdigit())]
    return kept or words


# ---------------------------------------------------------------------------
# reading dates
# ---------------------------------------------------------------------------
@dataclass
class Mention:
    day: date
    start: int
    end: int
    year_given: bool
    ends_a_range: bool = False


def _safe(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next(month: int, day: int, today: date) -> Optional[date]:
    """The next time a day of the year comes round, today included."""
    for year in range(today.year, today.year + 9):      # 29 February
        found = _safe(year, month, day)
        if found and found >= today:
            return found
    return None


def find_dates(text: str, today: date, day_first: Optional[bool] = None,
               year: Optional[int] = None) -> List[Mention]:
    """Every calendar date written in some text, in order.

    A date with no year takes it from the date it opens ("July 14 to July 30,
    2028"), else `year` if the caller knows it, else the next time that day
    comes round after `today`.
    """
    found: List[Mention] = []
    taken: List[Tuple[int, int]] = []
    known_year = year

    def add(match, year: Optional[str], month: int, day: int) -> None:
        if any(match.start() < end and match.end() > start for start, end in taken):
            return
        when = (_safe(int(year), month, day) if year
                else _safe(known_year, month, day) if known_year
                else _next(month, day, today))
        if when is None:
            return
        taken.append((match.start(), match.end()))
        found.append(Mention(when, match.start(), match.end(), bool(year)))

    for m in _ISO.finditer(text):
        add(m, m.group(1), int(m.group(2)), int(m.group(3)))
    for m in _RANGE_MONTH.finditer(text):
        add(m, m.group(3), _MONTHS[m.group(2).lower()], int(m.group(1)))
    for m in _MONTH_RANGE.finditer(text):
        add(m, m.group("year"), _MONTHS[m.group(1).lower()], int(m.group(2)))
    for m in _DAY_MONTH.finditer(text):
        add(m, m.group(3), _MONTHS[m.group(2).lower()], int(m.group(1)))
    for m in _MONTH_DAY.finditer(text):
        add(m, m.group(3), _MONTHS[m.group(1).lower()], int(m.group(2)))
    if day_first is not None:
        for m in _NUMERIC.finditer(text):
            a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
            year = year if len(year) == 4 else f"20{year}"
            first_is_day = a > 12 or (day_first and b <= 12)
            day, month = (a, b) if first_is_day else (b, a)
            if 1 <= month <= 12:
                add(m, year, month, day)
    found.sort(key=lambda mention: mention.start)
    for first, second in zip(found, found[1:]):
        if not _BETWEEN.match(text[first.end:second.start]):
            continue
        second.ends_a_range = True
        if second.year_given and not first.year_given:
            year_of = second.day.year
            same = _safe(year_of, first.day.month, first.day.day)
            if same and same > second.day:          # 28 December to 3 January 2027
                same = _safe(year_of - 1, first.day.month, first.day.day)
            if same:
                first.day, first.year_given = same, True
    return found


_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def add_months(start: date, months: int) -> date:
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    for day in range(start.day, 27, -1):
        found = _safe(year, month, day)
        if found:
            return found
    return date(year, month, min(start.day, 28))


def date_in_words(message: str, today: date, day_first: bool = False) -> Optional[date]:
    """A date the owner gave, however they said it. None if they gave none."""
    text = message or ""
    mentions = [m for m in find_dates(text, today, day_first=day_first) if not m.ends_a_range]
    if mentions:
        return mentions[-1].day
    lower = text.lower()
    if re.search(r"\bday after tomorrow\b", lower):
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1)
    m = re.search(r"\bin\s+(\d{1,4}|" + "|".join(_NUMBER_WORDS) + r")\s+(day|week|month|year)s?\b", lower)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMBER_WORDS[m.group(1)]
        unit = m.group(2)
        if unit == "day":
            return today + timedelta(days=n)
        if unit == "week":
            return today + timedelta(weeks=n)
        return add_months(today, n if unit == "month" else n * 12)
    m = re.search(r"\b(?:on|this|next|coming)\s+(" + "|".join(_WEEKDAYS) + r")\b", lower)
    if m:
        ahead = (_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        if "next " + m.group(1) in lower and ahead < 7 and today.weekday() + ahead < 7:
            ahead += 7              # "next Friday" said on a Monday: not this one
        return today + timedelta(days=ahead)
    return None


def time_in_words(message: str) -> str:
    """A time of day the owner gave, as HH:MM, or ''."""
    lower = (message or "").lower()
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])", lower)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if 1 <= hour <= 12 and minute < 60:
            hour = hour % 12 + (12 if m.group(3).startswith("p") else 0)
            return f"{hour:02d}:{minute:02d}"
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", lower)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if re.search(r"\bat noon\b|\bat midday\b", lower):
        return "12:00"
    return ""


def _thanksgiving(year: int) -> date:
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def _easter(year: int) -> date:
    """Western Easter Sunday (the anonymous Gregorian computus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


_MOVING_DAYS = {
    "thanksgiving": _thanksgiving, "thanksgiving day": _thanksgiving,
    "black friday": lambda year: _thanksgiving(year) + timedelta(days=1),
    "easter": _easter, "easter sunday": _easter,
    "good friday": lambda year: _easter(year) - timedelta(days=2),
}


def fixed_day(event: str, today: date) -> Optional[date]:
    """A day the calendar knows, with nothing to look up."""
    name = re.sub(r"\s+20\d{2}$", "", (event or "").lower().replace("’", "'")).strip()
    name = re.sub(r"^(?:the|next|this)\s+", "", name)
    given = re.search(r"\b(20\d{2})$", (event or "").strip())
    if name in _FIXED_DAYS:
        known = _FIXED_DAYS[name]
        return _safe(int(given.group(1)), *known) if given else _next(known[0], known[1], today)
    if name in _MOVING_DAYS:
        rule = _MOVING_DAYS[name]
        if given:
            return rule(int(given.group(1)))
        return rule(today.year) if rule(today.year) >= today else rule(today.year + 1)
    return None


# ---------------------------------------------------------------------------
# weighing the evidence
# ---------------------------------------------------------------------------
@dataclass
class Found:
    """What the web says about when something is."""
    status: str                      # sure | unsure | past | none
    day: Optional[date] = None
    sources: int = 0                 # independent sites that say so
    source_name: str = ""
    source_url: str = ""
    others: List[Tuple[date, int]] = field(default_factory=list)


def site(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_AGO = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_AGO_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _strip_written(text: str, today: Optional[date] = None) -> Tuple[str, Optional[date]]:
    """Text without the dates saying when it was written, and that date."""
    today = today or date.today()
    written: Optional[date] = None
    opening = _WRITTEN[0].match(text)
    if opening:
        ago = _AGO.search(opening.group(0))
        if ago:
            written = today - timedelta(days=int(ago.group(1)) * _AGO_DAYS[ago.group(2).lower()])
        else:
            dates = find_dates(opening.group(0), today)
            written = dates[0].day if dates else None
    for pattern in _WRITTEN:
        text = pattern.sub(" ", text)
    return text, written


def _about(event_words: List[str], text: str) -> bool:
    """Whether a result is about this event and not merely near it."""
    words = set(re.findall(r"[a-z0-9]+", text.lower().replace("'", "").replace("’", "")))
    hits = sum(1 for w in event_words if w in words or (w + "s") in words
               or (w.endswith("s") and w[:-1] in words))
    # Numbers are what tell GTA 6 from GTA 5: they all have to be there,
    # except a year, which pages often leave out.
    for word in event_words:
        if word.isdigit() and len(word) < 4 and word not in words:
            roman = {"2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi", "7": "vii",
                     "8": "viii", "9": "ix", "10": "x"}.get(word)
            if not (roman and roman in words):
                return False
    needed = max(1, (2 * len(event_words) + 2) // 3)
    return hits >= min(needed, len(event_words))


def weigh(event: str, results: List[Dict[str, Any]], today: date) -> Found:
    """The date several independent sites agree on, or an honest shrug."""
    words = significant(event)
    wanted_year = next((int(w) for w in words if re.fullmatch(r"20\d{2}", w)), None)
    naming = [w for w in words if not re.fullmatch(r"20\d{2}", w)] or words

    votes: Dict[date, Dict[str, float]] = {}
    where: Dict[date, Tuple[float, str]] = {}
    for result in results:
        host = site(result.get("url", ""))
        title = str(result.get("title") or "")
        body, written = _strip_written(str(result.get("snippet") or ""), today)
        if not host or not _about(naming, f"{title} {body}"):
            continue
        # A bare "July 31" in an article from July 2026 is that July.
        since = written - timedelta(days=3) if written and written <= today else today
        for part in (title, body):
            for mention in find_dates(part, since, year=wanted_year):
                if mention.ends_a_range:
                    continue
                if wanted_year and mention.day.year != wanted_year:
                    continue
                before = part[max(0, mention.start - 70):mention.start]
                after = part[mention.end:mention.end + 30]
                # The closest words decide: "...the trailer. Release Date.
                # December 18" is a release date.
                says = [m.end() for m in _IS_THE_DATE.finditer(before)]
                other = [m.end() for m in _IS_ANOTHER_DATE.finditer(before)]
                weight = 1.0
                if says and (not other or says[-1] > other[-1]):
                    weight = 2.0
                elif other:
                    weight = 0.3
                elif _IS_THE_DATE.search(after):
                    weight = 1.5
                if not mention.year_given and not wanted_year:
                    # The year from when the page was written is nearly as
                    # good as given. With nothing to go on it is a guess: it
                    # can lean towards a date other sites give in full, but
                    # is kept under 1.0 so it never counts as a site agreeing.
                    weight *= 0.8 if written else 0.45
                if wanted_year and mention.year_given:
                    weight += 0.5
                mine = votes.setdefault(mention.day, {})
                if weight > mine.get(host, 0.0):
                    mine[host] = weight
                if weight > where.get(mention.day, (0.0, ""))[0]:
                    where[mention.day] = (weight, result.get("url", ""))

    if not votes:
        return Found("none")

    def total(day: date) -> float:
        return sum(votes[day].values())

    def backing(day: date) -> int:
        return sum(1 for weight in votes[day].values() if weight >= 1.0)

    ranked = sorted(votes, key=lambda day: (total(day), backing(day)), reverse=True)
    ahead = [d for d in ranked if d >= today]
    # "The next eclipse" is not the one that has been.
    coming = re.search(r"\b(?:next|upcoming)\b", event or "", re.I)
    behind = [] if coming else [d for d in ranked if d < today]

    def make(status: str, day: date) -> Found:
        url = where.get(day, (0.0, ""))[1]
        others = [(d, backing(d)) for d in ahead if d != day and backing(d) >= 1][:3]
        return Found(status, day, backing(day), site(url), url, others)

    best_behind = behind[0] if behind else None
    if not ahead:
        return (make("past", best_behind) if best_behind and backing(best_behind) >= 2
                else Found("none"))
    best = ahead[0]
    if best_behind and backing(best_behind) >= 2 and (
            backing(best) < 2 or total(best) < total(best_behind) / 2):
        return make("past", best_behind)
    rival = total(ahead[1]) if len(ahead) > 1 else 0.0
    if backing(best) >= 2 and total(best) >= 1.5 * rival:
        return make("sure", best)
    return make("unsure", best) if backing(best) >= 1 else Found("none")


# ---------------------------------------------------------------------------
# saying how long is left
# ---------------------------------------------------------------------------
def target_moment(day: str, clock: str = "") -> datetime:
    """The local moment a countdown reaches zero."""
    return datetime.strptime(f"{day} {clock or '00:00'}", "%Y-%m-%d %H:%M")


def left(now: datetime, target: datetime) -> Dict[str, int]:
    """Whole months, then what is left of each smaller unit."""
    if target <= now:
        return {"months": 0, "days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    months = (target.year - now.year) * 12 + target.month - now.month

    def anchor(n: int) -> datetime:
        return datetime.combine(add_months(now.date(), n), now.time())

    while months > 0 and anchor(months) > target:
        months -= 1
    rest = target - anchor(months)
    seconds = int(rest.total_seconds())
    return {"months": months, "days": seconds // 86400, "hours": seconds % 86400 // 3600,
            "minutes": seconds % 3600 // 60, "seconds": seconds % 60}


def in_words(now: datetime, target: datetime) -> str:
    """"1 month, 3 days and 4 hours" -- the two or three units that matter."""
    if target <= now:
        return "it has already arrived"
    parts = left(now, target)
    units = [(parts[k], k) for k in ("months", "days", "hours", "minutes")]
    while units and units[0][0] == 0:
        units.pop(0)
    said = [f"{n} {name[:-1] if n == 1 else name}" for n, name in units[:3] if n]
    if not said:
        return "less than a minute"
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
