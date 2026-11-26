"""Everyday answers every assistant is asked for, worked out rather than recalled.

Unit and currency conversion, the time somewhere else, a Wikipedia summary,
what a word means, a coin or a die. A small local model gets every one of
these wrong from memory -- it multiplies badly, remembers last year's
exchange rate, forgets daylight saving -- so each is computed here or read
from a free public source, and the answer says where it came from.

Sources (no keys): exchange rates from frankfurter.app (European Central
Bank reference rates), summaries from the Wikipedia REST API, definitions
from Wiktionary's REST API. Times come from the IANA zone database on this PC.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

USER_AGENT = "JARVIS/1.0 (personal assistant; local)"


class QuickFactError(Exception):
    """Something the owner should be told plainly."""


# ---------------------------------------------------------------------------
# Units. Each quantity is converted through one base unit.
# ---------------------------------------------------------------------------
_UNITS: Dict[str, Tuple[str, float]] = {}


def _define(kind: str, factor: float, *names: str) -> None:
    for name in names:
        _UNITS[name] = (kind, factor)


# length, base metre
_define("length", 1e-3, "mm", "millimeter", "millimetre", "millimeters", "millimetres")
_define("length", 1e-2, "cm", "centimeter", "centimetre", "centimeters", "centimetres")
_define("length", 1.0, "m", "meter", "metre", "meters", "metres")
_define("length", 1e3, "km", "kilometer", "kilometre", "kilometers", "kilometres", "kms")
_define("length", 0.0254, "in", "inch", "inches", '"')
_define("length", 0.3048, "ft", "foot", "feet", "'")
_define("length", 0.9144, "yd", "yard", "yards")
_define("length", 1609.344, "mi", "mile", "miles")
_define("length", 1852.0, "nmi", "nautical mile", "nautical miles")
# mass, base kilogram
_define("mass", 1e-6, "mg", "milligram", "milligrams")
_define("mass", 1e-3, "g", "gram", "grams", "gramme", "grammes")
_define("mass", 1.0, "kg", "kilogram", "kilograms", "kilo", "kilos", "kgs")
_define("mass", 1000.0, "t", "tonne", "tonnes", "metric ton", "metric tons")
_define("mass", 0.028349523125, "oz", "ounce", "ounces")
_define("mass", 0.45359237, "lb", "lbs", "pound", "pounds")
_define("mass", 6.35029318, "st", "stone", "stones")
_define("mass", 907.18474, "short ton", "short tons", "us ton", "us tons", "ton", "tons")
# volume, base litre
_define("volume", 1e-3, "ml", "milliliter", "millilitre", "milliliters", "millilitres")
_define("volume", 1e-2, "cl", "centiliter", "centilitre")
_define("volume", 1.0, "l", "liter", "litre", "liters", "litres")
_define("volume", 3.785411784, "gal", "gallon", "gallons", "us gallon", "us gallons")
_define("volume", 4.54609, "uk gallon", "uk gallons", "imperial gallon", "imperial gallons")
_define("volume", 0.946352946, "qt", "quart", "quarts")
_define("volume", 0.473176473, "pt", "pint", "pints", "us pint", "us pints")
_define("volume", 0.56826125, "uk pint", "uk pints", "imperial pint", "imperial pints")
_define("volume", 0.2365882365, "cup", "cups")
_define("volume", 0.0295735295625, "fl oz", "fluid ounce", "fluid ounces")
_define("volume", 0.01478676478125, "tbsp", "tablespoon", "tablespoons")
_define("volume", 0.00492892159375, "tsp", "teaspoon", "teaspoons")
# speed, base metre per second
_define("speed", 1.0, "m/s", "meters per second", "metres per second")
_define("speed", 1 / 3.6, "km/h", "kph", "kmh", "kilometers per hour", "kilometres per hour")
_define("speed", 0.44704, "mph", "miles per hour")
_define("speed", 0.514444, "knot", "knots", "kn")
# area, base square metre
_define("area", 1.0, "m2", "sq m", "square meter", "square metre", "square meters", "square metres")
_define("area", 1e6, "km2", "sq km", "square kilometer", "square kilometre", "square kilometers",
        "square kilometres")
_define("area", 0.09290304, "ft2", "sq ft", "square foot", "square feet")
_define("area", 4046.8564224, "acre", "acres")
_define("area", 1e4, "ha", "hectare", "hectares")
_define("area", 2589988.110336, "mi2", "sq mi", "square mile", "square miles")
# data, base byte
_define("data", 1.0, "b", "byte", "bytes")
_define("data", 1e3, "kb", "kilobyte", "kilobytes")
_define("data", 1e6, "mb", "megabyte", "megabytes")
_define("data", 1e9, "gb", "gigabyte", "gigabytes")
_define("data", 1e12, "tb", "terabyte", "terabytes")
_define("data", 1024.0, "kib", "kibibyte", "kibibytes")
_define("data", 1024.0 ** 2, "mib", "mebibyte", "mebibytes")
_define("data", 1024.0 ** 3, "gib", "gibibyte", "gibibytes")
_define("data", 1024.0 ** 4, "tib", "tebibyte", "tebibytes")
# time, base second
_define("time", 1.0, "s", "sec", "secs", "second", "seconds")
_define("time", 60.0, "min", "mins", "minute", "minutes")
_define("time", 3600.0, "h", "hr", "hrs", "hour", "hours")
_define("time", 86400.0, "d", "day", "days")
_define("time", 604800.0, "wk", "week", "weeks")
_define("time", 31557600.0, "yr", "year", "years")
# energy, base joule
_define("energy", 1.0, "j", "joule", "joules")
_define("energy", 1e3, "kj", "kilojoule", "kilojoules")
_define("energy", 4.184, "cal", "calorie", "calories")
_define("energy", 4184.0, "kcal", "kilocalorie", "kilocalories")
_define("energy", 3.6e6, "kwh", "kilowatt hour", "kilowatt hours")

_TEMPERATURES = {
    "c": "C", "°c": "C", "celsius": "C", "centigrade": "C", "degrees c": "C", "degrees celsius": "C",
    "f": "F", "°f": "F", "fahrenheit": "F", "degrees f": "F", "degrees fahrenheit": "F",
    "k": "K", "kelvin": "K",
}
_TEMP_NAMES = {"C": "°C", "F": "°F", "K": "K"}


def _unit_key(text: str) -> str:
    key = re.sub(r"\s+", " ", (text or "").strip().lower().replace("²", "2"))
    key = re.sub(r"^(?:square|sq\.?)\s+(\w+)$", lambda m: f"square {m.group(1)}", key) \
        if key not in _UNITS else key
    return key


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise QuickFactError(f"“{value}” is not a number. Ask the owner how much to convert.")


def _pretty(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e15 or magnitude < 1e-6:
        return f"{value:.6g}"
    if magnitude >= 1000:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:.6g}" if magnitude < 1 else f"{value:.4f}".rstrip("0").rstrip(".")


def convert_units(amount: Any, from_unit: str, to_unit: str) -> Dict[str, Any]:
    """amount in from_unit -> to_unit, exactly. Raises QuickFactError."""
    value = _number(amount)
    src, dst = _unit_key(from_unit), _unit_key(to_unit)
    if src in _TEMPERATURES or dst in _TEMPERATURES:
        if src not in _TEMPERATURES or dst not in _TEMPERATURES:
            raise QuickFactError(f"Cannot convert {from_unit} to {to_unit}: only a temperature "
                                 "converts to a temperature.")
        a, b = _TEMPERATURES[src], _TEMPERATURES[dst]
        kelvin = {"C": value + 273.15, "F": (value - 32) * 5 / 9 + 273.15, "K": value}[a]
        if kelvin < -1e-9:
            raise QuickFactError(f"{_pretty(value)} {_TEMP_NAMES[a]} is below absolute zero.")
        result = {"C": kelvin - 273.15, "F": (kelvin - 273.15) * 9 / 5 + 32, "K": kelvin}[b]
        return {"amount": value, "from": _TEMP_NAMES[a], "to": _TEMP_NAMES[b],
                "result": round(result, 6), "kind": "temperature",
                "text": f"{_pretty(value)} {_TEMP_NAMES[a]} = {_pretty(round(result, 2))} {_TEMP_NAMES[b]}"}
    if src not in _UNITS:
        raise QuickFactError(f"I don't know the unit “{from_unit}”.")
    if dst not in _UNITS:
        raise QuickFactError(f"I don't know the unit “{to_unit}”.")
    (kind_a, fa), (kind_b, fb) = _UNITS[src], _UNITS[dst]
    if kind_a != kind_b:
        raise QuickFactError(f"Cannot convert {from_unit} ({kind_a}) to {to_unit} ({kind_b}).")
    result = value * fa / fb
    return {"amount": value, "from": from_unit.strip(), "to": to_unit.strip(),
            "result": float(f"{result:.10g}"), "kind": kind_a,
            "text": f"{_pretty(value)} {from_unit.strip()} = {_pretty(result)} {to_unit.strip()}"}


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------
# Places people say that are not the city an IANA zone is named after.
_PLACES = {
    "uk": "Europe/London", "england": "Europe/London", "britain": "Europe/London",
    "scotland": "Europe/London", "wales": "Europe/London", "ireland": "Europe/Dublin",
    "france": "Europe/Paris", "germany": "Europe/Berlin", "spain": "Europe/Madrid",
    "italy": "Europe/Rome", "netherlands": "Europe/Amsterdam", "holland": "Europe/Amsterdam",
    "belgium": "Europe/Brussels", "switzerland": "Europe/Zurich", "austria": "Europe/Vienna",
    "sweden": "Europe/Stockholm", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki", "poland": "Europe/Warsaw", "greece": "Europe/Athens",
    "portugal": "Europe/Lisbon", "turkey": "Europe/Istanbul", "russia": "Europe/Moscow",
    "ukraine": "Europe/Kyiv", "egypt": "Africa/Cairo", "south africa": "Africa/Johannesburg",
    "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi", "israel": "Asia/Jerusalem",
    "uae": "Asia/Dubai", "saudi arabia": "Asia/Riyadh", "india": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata", "pakistan": "Asia/Karachi",
    "china": "Asia/Shanghai", "beijing": "Asia/Shanghai", "japan": "Asia/Tokyo",
    "korea": "Asia/Seoul", "south korea": "Asia/Seoul", "vietnam": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Bangkok", "thailand": "Asia/Bangkok", "philippines": "Asia/Manila",
    "indonesia": "Asia/Jakarta", "malaysia": "Asia/Kuala_Lumpur", "taiwan": "Asia/Taipei",
    "australia": "Australia/Sydney", "canberra": "Australia/Sydney",
    "new zealand": "Pacific/Auckland", "nz": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "hawaii": "Pacific/Honolulu", "alaska": "America/Anchorage", "brazil": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires", "buenos aires": "America/Argentina/Buenos_Aires",
    "mexico": "America/Mexico_City", "canada": "America/Toronto", "ottawa": "America/Toronto",
    "montreal": "America/Toronto", "quebec": "America/Toronto",
    "usa": "America/New_York", "us": "America/New_York", "east coast": "America/New_York",
    "nyc": "America/New_York", "washington dc": "America/New_York", "dc": "America/New_York",
    "boston": "America/New_York", "miami": "America/New_York", "atlanta": "America/New_York",
    "philadelphia": "America/New_York", "florida": "America/New_York", "orlando": "America/New_York",
    "west coast": "America/Los_Angeles", "california": "America/Los_Angeles",
    "la": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "las vegas": "America/Los_Angeles",
    "san diego": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "texas": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago",
    "austin": "America/Chicago", "minneapolis": "America/Chicago", "new orleans": "America/Chicago",
    "colorado": "America/Denver", "salt lake city": "America/Denver", "arizona": "America/Phoenix",
    "utc": "UTC", "gmt": "Etc/GMT", "zulu": "UTC",
}
_ZONES: Optional[Dict[str, str]] = None


def _zones() -> Dict[str, str]:
    global _ZONES
    if _ZONES is None:
        from zoneinfo import available_timezones

        found: Dict[str, str] = {}
        for zone in sorted(available_timezones()):
            if zone.startswith(("Etc/", "SystemV/", "posix/", "right/")):
                continue
            found.setdefault(zone.lower(), zone)
            city = zone.rsplit("/", 1)[-1].replace("_", " ").lower()
            found.setdefault(city, zone)
        _ZONES = found
    return _ZONES


def find_zone(place: str) -> str:
    key = re.sub(r"\s+", " ", re.sub(r"[^\w/ +-]", " ", (place or "").lower())).strip()
    key = re.sub(r"^(?:the|in)\s+", "", key)
    if not key:
        raise QuickFactError("Ask the owner which city or country they mean.")
    if key in _PLACES:
        return _PLACES[key]
    zones = _zones()
    if key in zones:
        return zones[key]
    # "Tokyo, Japan" / "London England": try each part on its own.
    for part in re.split(r"[,/]| in ", key):
        part = part.strip()
        if part in _PLACES:
            return _PLACES[part]
        if part in zones:
            return zones[part]
    words = key.split()
    for size in range(len(words), 0, -1):
        for start in range(0, len(words) - size + 1):
            chunk = " ".join(words[start:start + size])
            if chunk in _PLACES:
                return _PLACES[chunk]
            if chunk in zones:
                return zones[chunk]
    raise QuickFactError(f"I don't know which time zone “{place}” is in. Ask the owner for a "
                         "bigger nearby city.")


def world_time(place: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    from zoneinfo import ZoneInfo

    zone = find_zone(place)
    here = (now or datetime.now()).astimezone()
    there = here.astimezone(ZoneInfo(zone))
    offset = there.utcoffset()
    diff_hours = ((there.utcoffset().total_seconds() - here.utcoffset().total_seconds()) / 3600
                  if there.utcoffset() is not None and here.utcoffset() is not None else 0.0)
    if diff_hours == 0:
        relative = "the same time as here"
    else:
        amount = f"{abs(diff_hours):g} hour{'s' if abs(diff_hours) != 1 else ''}"
        relative = f"{amount} {'ahead of' if diff_hours > 0 else 'behind'} here"
    day_note = ""
    if there.date() != here.date():
        day_note = " (tomorrow)" if there.date() > here.date() else " (yesterday)"
    total = int(offset.total_seconds() // 60) if offset is not None else 0
    utc = f"UTC{'+' if total >= 0 else '-'}{abs(total) // 60:02d}:{abs(total) % 60:02d}"
    clock = there.strftime("%I:%M %p").lstrip("0")
    return {"place": place.strip(), "zone": zone, "time": there.strftime("%H:%M"), "clock": clock,
            "date": there.date().isoformat(), "weekday": there.strftime("%A"), "utc_offset": utc,
            "hours_from_here": diff_hours, "abbreviation": there.tzname() or "",
            "text": f"It is {clock} on {there.strftime('%A')}{day_note} in {place.strip()} "
                    f"({zone}, {utc}), {relative}."}


# ---------------------------------------------------------------------------
# Random
# ---------------------------------------------------------------------------
_RANDOM = secrets.SystemRandom()
_DICE = re.compile(r"^\s*(\d*)\s*d\s*(\d+)\s*([+-]\s*\d+)?\s*$", re.I)


def roll(kind: str = "", dice: str = "", choices: Optional[List[str]] = None,
         low: Optional[int] = None, high: Optional[int] = None,
         rng: Any = None, count: Optional[int] = None) -> Dict[str, Any]:
    rng = rng or _RANDOM
    kind, dice = _kind_of(kind, dice, choices, low, high)
    if kind == "dice" and count is not None:
        # "3 dice" given as a count beside "d6": the count is how many.
        written = _DICE.match(dice or "1d6")
        if written and written.group(1) in ("", "1") and int(count) > 1:
            dice = f"{int(count)}d{written.group(2)}{(written.group(3) or '').replace(' ', '')}"
    if kind == "coin":
        side = rng.choice(["heads", "tails"])
        return {"kind": "coin", "result": side, "text": f"The coin landed on {side}."}
    if kind == "dice":
        match = _DICE.match(dice or "1d6")
        if not match:
            raise QuickFactError(f"“{dice}” is not dice notation; use something like 2d6 or d20.")
        count, sides = int(match.group(1) or 1), int(match.group(2))
        bonus = int(re.sub(r"\s", "", match.group(3) or "0"))
        if not 1 <= count <= 100 or not 2 <= sides <= 1000:
            raise QuickFactError("Roll between 1 and 100 dice with 2 to 1000 sides each.")
        rolls = [rng.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + bonus
        shown = f"{count}d{sides}{'+' if bonus > 0 else ''}{bonus if bonus else ''}"
        detail = f" ({' + '.join(map(str, rolls))}{f' {bonus:+d}' if bonus else ''})" \
            if count > 1 or bonus else ""
        return {"kind": "dice", "dice": shown, "rolls": rolls, "bonus": bonus, "result": total,
                "text": f"Rolled {shown}: {total}{detail}."}
    if kind == "pick":
        options = [c.strip() for c in (choices or []) if str(c).strip()]
        if len(options) < 2:
            raise QuickFactError("Ask the owner for at least two things to choose between.")
        chosen = rng.choice(options)
        return {"kind": "pick", "options": options, "result": chosen,
                "text": f"Picked at random from {len(options)}: {chosen}."}
    if kind == "number":
        a = 1 if low is None else int(low)
        b = 100 if high is None else int(high)
        if a > b:
            a, b = b, a
        value = rng.randint(a, b)
        return {"kind": "number", "low": a, "high": b, "result": value,
                "text": f"Random number from {a} to {b}: {value}."}
    raise QuickFactError("Choose coin, dice, pick or number. Nothing was rolled: do not make up a result.")


_KINDS = {
    "coin": "coin", "flip": "coin", "coin flip": "coin", "flip a coin": "coin", "heads": "coin",
    "tails": "coin", "heads or tails": "coin", "toss": "coin",
    "dice": "dice", "die": "dice", "roll": "dice", "dice roll": "dice", "roll dice": "dice",
    "pick": "pick", "choose": "pick", "choice": "pick", "decide": "pick", "random choice": "pick",
    "number": "number", "random number": "number", "random": "number", "range": "number",
}


def _kind_of(kind: str, dice: str, choices: Optional[List[str]], low: Optional[int],
             high: Optional[int]) -> Tuple[str, str]:
    """What a small model meant, however it put it: kind="d20" is a d20."""
    said = re.sub(r"\s+", " ", str(kind or "").strip().lower())
    if _DICE.match(said):
        return "dice", dice if (dice or "").strip() else said
    mapped = _KINDS.get(said)
    # Dice written out ("3d6") are the most specific thing said; a missing or
    # default kind ("coin") must not turn them into a coin flip.
    if (dice or "").strip() and mapped in (None, "coin", "dice"):
        return "dice", dice
    if mapped == "dice" and high is not None:
        return "dice", f"1d{int(high)}"              # "roll a d20" as kind=dice, 1..20
    if mapped:
        return mapped, dice
    if choices:
        return "pick", dice
    if low is not None or high is not None:
        return "number", dice
    return said or "coin", dice


# ---------------------------------------------------------------------------
# The service: the parts that need the internet.
# ---------------------------------------------------------------------------
_CURRENCY_NAMES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD", "us dollars": "USD",
    "bucks": "USD", "$": "USD", "euro": "EUR", "euros": "EUR", "eur": "EUR", "€": "EUR",
    "pound": "GBP", "pounds": "GBP", "quid": "GBP", "sterling": "GBP", "gbp": "GBP", "£": "GBP",
    "yen": "JPY", "jpy": "JPY", "¥": "JPY", "yuan": "CNY", "renminbi": "CNY", "rmb": "CNY",
    "rupee": "INR", "rupees": "INR", "canadian dollar": "CAD", "canadian dollars": "CAD",
    "australian dollar": "AUD", "australian dollars": "AUD", "aussie dollars": "AUD",
    "franc": "CHF", "swiss franc": "CHF", "swiss francs": "CHF", "peso": "MXN", "pesos": "MXN",
    "won": "KRW", "real": "BRL", "reais": "BRL", "krona": "SEK", "kronor": "SEK", "krone": "NOK",
    "zloty": "PLN", "rand": "ZAR", "lira": "TRY", "baht": "THB", "shekel": "ILS", "shekels": "ILS",
    "nz dollar": "NZD", "new zealand dollar": "NZD", "hong kong dollar": "HKD",
    "singapore dollar": "SGD", "ringgit": "MYR", "rupiah": "IDR", "forint": "HUF", "koruna": "CZK",
}


def currency_code(text: str) -> str:
    key = re.sub(r"\s+", " ", (text or "").strip().lower())
    if key in _CURRENCY_NAMES:
        return _CURRENCY_NAMES[key]
    if re.fullmatch(r"[a-z]{3}", key):
        return key.upper()
    raise QuickFactError(f"I don't know the currency “{text}”. Use its three-letter code, e.g. USD.")


FetchJson = Callable[[str], Awaitable[Tuple[int, Any]]]


class QuickFacts:
    def __init__(self, fetch_json: Optional[FetchJson] = None, timeout: float = 12.0):
        self._fetch = fetch_json
        self._timeout = timeout
        self._client = None
        self._rates: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def _get(self, url: str) -> Tuple[int, Any]:
        if self._fetch is not None:
            return await self._fetch(url)
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True,
                                             headers={"User-Agent": USER_AGENT,
                                                      "Accept": "application/json"})
        try:
            response = await self._client.get(url)
        except Exception as exc:
            raise QuickFactError(f"Could not reach {url.split('/')[2]}: {type(exc).__name__}. "
                                 "Check the internet connection.")
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- currency --------------------------------------------------------
    async def convert_currency(self, amount: Any, from_currency: str, to_currency: str) -> Dict[str, Any]:
        import time

        value = _number(amount)
        src, dst = currency_code(from_currency), currency_code(to_currency)
        if src == dst:
            return {"amount": value, "from": src, "to": dst, "rate": 1.0, "result": value,
                    "date": "", "source": "", "text": f"{_pretty(value)} {src} = {_pretty(value)} {dst}"}
        cached = self._rates.get(src)
        if cached and time.monotonic() - cached[0] < 3600:
            data = cached[1]
        else:
            status, data = await self._get(f"https://api.frankfurter.app/latest?from={quote(src)}")
            if status == 404 or not isinstance(data, dict) or "rates" not in data:
                raise QuickFactError(f"No exchange rate is published for {src}. The source "
                                     "(European Central Bank rates) covers about 30 major currencies.")
            self._rates[src] = (time.monotonic(), data)
        rate = data["rates"].get(dst)
        if rate is None:
            raise QuickFactError(f"No exchange rate is published for {dst}. The source "
                                 "(European Central Bank rates) covers about 30 major currencies.")
        result = round(value * float(rate), 2 if value * float(rate) >= 1 else 6)
        when = str(data.get("date", ""))
        return {"amount": value, "from": src, "to": dst, "rate": float(rate), "result": result,
                "date": when, "source": "European Central Bank reference rate via frankfurter.app",
                "text": f"{_pretty(value)} {src} = {_pretty(result)} {dst} "
                        f"(1 {src} = {float(rate):g} {dst}, ECB rate for {when})."}

    # -- wikipedia -------------------------------------------------------
    async def wikipedia(self, topic: str) -> Dict[str, Any]:
        topic = re.sub(r"\s+", " ", (topic or "").strip())
        if not topic:
            raise QuickFactError("Ask the owner what to look up.")
        title = topic
        status, data = await self._get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe=""))
        if status == 404 or not isinstance(data, dict) or not data.get("extract"):
            status, found = await self._get(
                "https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&namespace=0&format=json"
                "&search=" + quote(topic))
            if not isinstance(found, list) or len(found) < 2 or not found[1]:
                raise QuickFactError(f"Wikipedia has no article on “{topic}”.")
            title = found[1][0]
            status, data = await self._get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe=""))
            if not isinstance(data, dict) or not data.get("extract"):
                raise QuickFactError(f"Wikipedia has no article on “{topic}”.")
        url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") \
            or "https://en.wikipedia.org/wiki/" + quote(str(data.get("title", title)).replace(" ", "_"))
        result = {"title": data.get("title", title), "description": data.get("description", ""),
                  "summary": data.get("extract", ""), "url": url,
                  "disambiguation": data.get("type") == "disambiguation"}
        return result

    # -- dictionary -------------------------------------------------------
    async def define(self, word: str) -> Dict[str, Any]:
        word = (word or "").strip().strip(".?!\"'").lower()
        if not word or len(word) > 60:
            raise QuickFactError("Ask the owner which word to define.")
        status, data = await self._get(
            "https://en.wiktionary.org/api/rest_v1/page/definition/" + quote(word, safe=""))
        if status == 404:
            raise QuickFactError(f"The dictionary has no entry for “{word}”. Check the spelling.")
        if status != 200 or not isinstance(data, dict):
            raise QuickFactError(f"The dictionary (Wiktionary) did not answer (HTTP {status}). "
                                 "Try again in a moment.")
        senses: List[Dict[str, str]] = []
        for part in data.get("en", []):
            for definition in part.get("definitions", []):
                text = _plain(definition.get("definition", ""))
                if not text:
                    continue
                examples = [_plain(e) for e in definition.get("examples", []) or [] if _plain(e)]
                senses.append({"part_of_speech": str(part.get("partOfSpeech", "")).lower(),
                               "definition": text, "example": examples[0] if examples else ""})
                if len(senses) >= 5:
                    break
            if len(senses) >= 5:
                break
        if not senses:
            raise QuickFactError(f"The dictionary has no English entry for “{word}”. Check the spelling.")
        return {"word": word, "senses": senses,
                "url": "https://en.wiktionary.org/wiki/" + quote(word, safe=""),
                "source": "Wiktionary"}


def _plain(html: Any) -> str:
    import html as _html

    text = re.sub(r"<[^>]+>", "", str(html or ""))
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()
