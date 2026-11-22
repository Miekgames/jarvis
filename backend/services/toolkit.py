"""
Developer and everyday conversions: JSON, base64, hashes, UUIDs, regex, diff,
CSV, units, colours and cron.

All pure functions over their input. No model is involved, so the answers are
exact -- which is the entire point: asking a language model to base64-decode a
string or convert 3.5 miles to kilometres is a good way to get a confident wrong
answer.

Every function returns an explanation on failure rather than raising into the
tool layer, because "that is not valid JSON, line 3 column 12" is more useful
than a stack trace.
"""
from __future__ import annotations

import base64
import binascii
import colorsys
import csv
import difflib
import hashlib
import io
import json
import re
import statistics
import uuid as uuid_module
from datetime import datetime
from typing import Any, Dict, List, Optional

# ======================================================================
# JSON
# ======================================================================
def format_json(text: str, indent: int = 2, sort_keys: bool = False) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False,
                "error": "{} at line {}, column {}".format(exc.msg, exc.lineno, exc.colno),
                "line": exc.lineno, "column": exc.colno}
    return {
        "ok": True,
        "formatted": json.dumps(parsed, indent=max(0, min(indent, 8)),
                                sort_keys=sort_keys, ensure_ascii=False),
        "minified": json.dumps(parsed, separators=(",", ":"), ensure_ascii=False),
        "type": type(parsed).__name__,
        "keys": list(parsed)[:50] if isinstance(parsed, dict) else None,
        "length": len(parsed) if isinstance(parsed, (list, dict)) else None,
    }


def json_paths(text: str, limit: int = 200) -> Dict[str, Any]:
    """Every leaf path in a JSON document -- useful for finding one field."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": str(exc)}

    paths: List[Dict[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if len(paths) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, "{}.{}".format(path, key) if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node[:20]):
                walk(value, "{}[{}]".format(path, index))
        else:
            paths.append({"path": path, "value": node, "type": type(node).__name__})

    walk(parsed, "")
    return {"ok": True, "paths": paths, "truncated": len(paths) >= limit}


# ======================================================================
# encoding and hashing
# ======================================================================
def base64_encode(text: str, url_safe: bool = False) -> Dict[str, Any]:
    raw = text.encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw) if url_safe else base64.b64encode(raw)
    return {"ok": True, "result": encoded.decode("ascii"), "input_bytes": len(raw)}


def base64_decode(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"\s+", "", text or "")
    # Tolerate missing padding -- it is the most common way this fails.
    padded = cleaned + "=" * (-len(cleaned) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            return {"ok": True, "result": raw.decode("utf-8"), "bytes": len(raw)}
        except UnicodeDecodeError:
            return {"ok": True, "result": raw.hex(), "bytes": len(raw),
                    "note": "The result is binary, shown as hex."}
    return {"ok": False, "error": "That is not valid base64."}


def hash_text(text: str, algorithm: str = "sha256") -> Dict[str, Any]:
    algorithm = (algorithm or "sha256").lower().replace("-", "")
    available = {"md5", "sha1", "sha256", "sha384", "sha512", "blake2b", "blake2s"}
    if algorithm not in available:
        return {"ok": False,
                "error": "Unknown algorithm '{}'. Available: {}".format(
                    algorithm, ", ".join(sorted(available)))}
    digest = hashlib.new(algorithm, (text or "").encode("utf-8")).hexdigest()
    result = {"ok": True, "algorithm": algorithm, "hash": digest}
    if algorithm in ("md5", "sha1"):
        result["warning"] = ("{} is broken for security use. Fine as a checksum, "
                             "never for passwords or signatures.".format(algorithm))
    return result


def make_uuid(version: int = 4, count: int = 1) -> Dict[str, Any]:
    count = max(1, min(int(count), 100))
    if version == 4:
        values = [str(uuid_module.uuid4()) for _ in range(count)]
    elif version == 1:
        values = [str(uuid_module.uuid1()) for _ in range(count)]
    else:
        return {"ok": False, "error": "Only versions 1 and 4 are supported here."}
    return {"ok": True, "version": version, "uuids": values,
            "note": "Version 1 embeds the time and MAC address." if version == 1 else ""}


# ======================================================================
# regex and diff
# ======================================================================
def test_regex(pattern: str, text: str, flags: str = "") -> Dict[str, Any]:
    bits = 0
    for flag in (flags or "").lower():
        bits |= {"i": re.IGNORECASE, "m": re.MULTILINE,
                 "s": re.DOTALL, "x": re.VERBOSE}.get(flag, 0)
    try:
        compiled = re.compile(pattern, bits)
    except re.error as exc:
        return {"ok": False, "error": "Invalid pattern: {}".format(exc)}

    matches = []
    for match in list(compiled.finditer(text or ""))[:100]:
        matches.append({
            "match": match.group(0),
            "start": match.start(),
            "end": match.end(),
            "groups": list(match.groups()),
            "named": match.groupdict(),
        })
    return {"ok": True, "pattern": pattern, "matches": matches, "count": len(matches),
            "groups_defined": compiled.groups,
            "group_names": list(compiled.groupindex)}


def diff_text(left: str, right: str, label_a: str = "before",
              label_b: str = "after") -> Dict[str, Any]:
    a = (left or "").splitlines()
    b = (right or "").splitlines()
    unified = list(difflib.unified_diff(a, b, lineterm="",
                                        fromfile=label_a, tofile=label_b))
    added = len([line for line in unified if line.startswith("+") and not line.startswith("+++")])
    removed = len([line for line in unified if line.startswith("-") and not line.startswith("---")])
    ratio = difflib.SequenceMatcher(None, left or "", right or "").ratio()
    return {
        "ok": True,
        "diff": "\n".join(unified),
        "added": added,
        "removed": removed,
        "identical": not unified,
        "similarity": round(ratio, 4),
    }


# ======================================================================
# CSV
# ======================================================================
def analyse_csv(text: str, max_rows: int = 5000) -> Dict[str, Any]:
    """Parse CSV and describe each column -- type, nulls, and real statistics."""
    if not (text or "").strip():
        return {"ok": False, "error": "No CSV data."}
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = [row for _, row in zip(range(max_rows + 1), reader)]
    except csv.Error as exc:
        return {"ok": False, "error": "Could not parse: {}".format(exc)}
    if not rows:
        return {"ok": False, "error": "No rows."}

    header, data = rows[0], rows[1:]
    columns = []
    for index, name in enumerate(header):
        values = [r[index].strip() for r in data if index < len(r) and r[index].strip()]
        numbers: List[float] = []
        for value in values:
            try:
                numbers.append(float(value.replace(",", "")))
            except ValueError:
                pass

        column: Dict[str, Any] = {
            "name": name or "column_{}".format(index + 1),
            "filled": len(values),
            "empty": len(data) - len(values),
            "unique": len(set(values)),
        }
        if numbers and len(numbers) >= max(1, len(values) * 0.8):
            column["type"] = "number"
            column["min"] = min(numbers)
            column["max"] = max(numbers)
            column["mean"] = round(statistics.fmean(numbers), 4)
            column["median"] = statistics.median(numbers)
            column["sum"] = round(sum(numbers), 4)
        else:
            column["type"] = "text"
            common = {}
            for value in values:
                common[value] = common.get(value, 0) + 1
            column["most_common"] = sorted(
                common.items(), key=lambda kv: -kv[1])[:5]
        columns.append(column)

    return {
        "ok": True,
        "delimiter": delimiter,
        "rows": len(data),
        "truncated": len(rows) > max_rows,
        "columns": columns,
        "preview": [dict(zip(header, r)) for r in data[:5]],
    }


# ======================================================================
# units
# ======================================================================
# Everything converts through a base unit per dimension, so adding a unit is one
# line and cannot introduce an inconsistent pair.
UNITS: Dict[str, Dict[str, float]] = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254,
               "inch": 0.0254, "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
               "yd": 0.9144, "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
               "nmi": 1852.0},
    "mass": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "oz": 0.028349523125,
             "lb": 0.45359237, "lbs": 0.45359237, "stone": 6.35029318},
    "volume": {"ml": 0.001, "l": 1.0, "litre": 1.0, "liter": 1.0, "gal": 3.785411784,
               "gallon": 3.785411784, "pint": 0.473176473, "cup": 0.2365882365,
               "floz": 0.0295735295625},
    "time": {"ms": 0.001, "s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0,
             "hr": 3600.0, "hour": 3600.0, "day": 86400.0, "week": 604800.0},
    "data": {"b": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2, "gb": 1024.0 ** 3,
             "tb": 1024.0 ** 4},
    "speed": {"mps": 1.0, "kph": 0.277777778, "kmh": 0.277777778,
              "mph": 0.44704, "knot": 0.514444},
}


def convert_units(value: float, source: str, target: str) -> Dict[str, Any]:
    source = (source or "").strip().lower().rstrip(".")
    target = (target or "").strip().lower().rstrip(".")

    # Temperature is affine, not a simple ratio, so it gets its own path.
    temps = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}
    if source in temps or target in temps:
        return _convert_temperature(value, source, target)

    for dimension, table in UNITS.items():
        if source in table and target in table:
            result = value * table[source] / table[target]
            return {"ok": True, "value": value, "from": source, "to": target,
                    "result": round(result, 6), "dimension": dimension}

    known = sorted({u for table in UNITS.values() for u in table} | temps)
    if source not in known:
        return {"ok": False, "error": "I do not know the unit '{}'.".format(source),
                "known": known}
    if target not in known:
        return {"ok": False, "error": "I do not know the unit '{}'.".format(target),
                "known": known}
    return {"ok": False,
            "error": "'{}' and '{}' measure different things.".format(source, target)}


def _convert_temperature(value: float, source: str, target: str) -> Dict[str, Any]:
    normalise = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f",
                 "k": "k", "kelvin": "k"}
    source_key = normalise.get(source)
    target_key = normalise.get(target)
    if not source_key or not target_key:
        return {"ok": False,
                "error": "Temperature converts only between C, F and K."}

    celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[source_key]
    result = {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[target_key]
    return {"ok": True, "value": value, "from": source_key, "to": target_key,
            "result": round(result, 4), "dimension": "temperature"}


# ======================================================================
# colour
# ======================================================================
def convert_colour(value: str) -> Dict[str, Any]:
    text = (value or "").strip().lower()
    rgb: Optional[tuple] = None

    hex_match = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{6})", text)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        rgb = tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))

    rgb_match = re.fullmatch(r"rgba?\(\s*(\d+)\D+(\d+)\D+(\d+)[^)]*\)", text)
    if rgb_match:
        rgb = tuple(int(g) for g in rgb_match.groups())

    if rgb is None:
        return {"ok": False,
                "error": "Give a colour as #rrggbb, #rgb or rgb(r, g, b)."}
    if any(c > 255 for c in rgb):
        return {"ok": False, "error": "RGB components must be 0-255."}

    r, g, b = [c / 255 for c in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    hue, saturation, brightness = colorsys.rgb_to_hsv(r, g, b)

    # WCAG relative luminance, so the contrast advice is a real calculation.
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    on_white = (1.05) / (luminance + 0.05)
    on_black = (luminance + 0.05) / 0.05

    return {
        "ok": True,
        "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
        "rgb": "rgb({}, {}, {})".format(*rgb),
        "rgb_values": list(rgb),
        "hsl": "hsl({:.0f}, {:.0f}%, {:.0f}%)".format(h * 360, s * 100, l * 100),
        "hsv": "hsv({:.0f}, {:.0f}%, {:.0f}%)".format(
            hue * 360, saturation * 100, brightness * 100),
        "luminance": round(luminance, 4),
        "contrast_on_white": round(on_white, 2),
        "contrast_on_black": round(on_black, 2),
        "readable_on": ("white" if on_white >= on_black else "black"),
        "passes_aa": max(on_white, on_black) >= 4.5,
    }


# ======================================================================
# cron
# ======================================================================
_CRON_FIELDS = [
    ("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31),
    ("month", 1, 12), ("day of week", 0, 6),
]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def explain_cron(expression: str) -> Dict[str, Any]:
    """Turn a cron expression into a sentence, validating each field."""
    parts = (expression or "").split()
    if len(parts) != 5:
        return {"ok": False,
                "error": "A cron expression has 5 fields "
                         "(minute hour day-of-month month day-of-week); got {}.".format(
                             len(parts))}

    described = []
    for value, (name, low, high) in zip(parts, _CRON_FIELDS):
        problem = _validate_cron_field(value, low, high)
        if problem:
            return {"ok": False, "error": "{} field: {}".format(name, problem)}
        described.append(_describe_cron_field(value, name))

    minute, hour, dom, month, dow = parts
    if minute == "*" and hour == "*":
        when = "every minute"
    elif hour == "*":
        when = "every hour at {}".format(described[0])
    elif minute.isdigit() and hour.isdigit():
        when = "at {:02d}:{:02d}".format(int(hour), int(minute))
    else:
        when = "{}, {}".format(described[1], described[0])

    scope = []
    if dom != "*":
        scope.append(described[2])
    if month != "*":
        scope.append(described[3])
    if dow != "*":
        scope.append(described[4])

    summary = when + (", " + ", ".join(scope) if scope else ", every day")
    return {"ok": True, "expression": expression, "summary": summary,
            "fields": {name: text for (name, _, _), text in zip(_CRON_FIELDS, described)}}


def _validate_cron_field(value: str, low: int, high: int) -> Optional[str]:
    for token in value.split(","):
        step = 1
        if "/" in token:
            token, _, step_text = token.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                return "'{}' is not a valid step.".format(step_text)
            step = int(step_text)
        if token in ("*", ""):
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            if not (start.isdigit() and end.isdigit()):
                return "'{}' is not a valid range.".format(token)
            if not (low <= int(start) <= high and low <= int(end) <= high):
                return "range {} is outside {}-{}.".format(token, low, high)
            if int(start) > int(end):
                return "range {} runs backwards.".format(token)
            continue
        if not token.isdigit():
            return "'{}' is not a number.".format(token)
        if not low <= int(token) <= high:
            return "{} is outside {}-{}.".format(token, low, high)
    return None


def _describe_cron_field(value: str, name: str) -> str:
    if value == "*":
        return "every {}".format(name)
    if value.startswith("*/"):
        return "every {} {}s".format(value[2:], name)
    if name == "month" and value.isdigit():
        return "in {}".format(_MONTHS[int(value) - 1])
    if name == "day of week" and value.isdigit():
        return "on {}".format(_DAYS[int(value) % 7])
    if name == "minute" and value.isdigit():
        return "minute {}".format(value)
    if name == "hour" and value.isdigit():
        return "at {:02d}:00".format(int(value))
    return "{} {}".format(name, value)


# ======================================================================
# timestamps
# ======================================================================
def convert_timestamp(value: str) -> Dict[str, Any]:
    """Epoch seconds/milliseconds to a readable date, and back."""
    text = (value or "").strip()
    if not text:
        return {"ok": False, "error": "Give an epoch timestamp or an ISO date."}

    if re.fullmatch(r"\d{9,14}", text):
        number = int(text)
        # 13 digits is milliseconds; 10 is seconds.
        seconds = number / 1000 if len(text) > 11 else number
        try:
            moment = datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return {"ok": False, "error": "That number is not a valid timestamp."}
        return {"ok": True, "input": text, "unit": "milliseconds" if len(text) > 11 else "seconds",
                "local": moment.isoformat(timespec="seconds"),
                "utc": datetime.utcfromtimestamp(seconds).isoformat(timespec="seconds") + "Z",
                "relative": _relative(moment)}

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return {"ok": False, "error": "Not an epoch number or an ISO-8601 date."}
    return {"ok": True, "input": text, "epoch_seconds": int(moment.timestamp()),
            "epoch_ms": int(moment.timestamp() * 1000),
            "iso": moment.isoformat(timespec="seconds"),
            "relative": _relative(moment.replace(tzinfo=None))}


def _relative(moment: datetime) -> str:
    delta = (datetime.now() - moment).total_seconds()
    future = delta < 0
    delta = abs(delta)
    for size, label in ((86400 * 365, "year"), (86400 * 30, "month"), (86400, "day"),
                        (3600, "hour"), (60, "minute")):
        if delta >= size:
            count = int(delta // size)
            plural = "" if count == 1 else "s"
            return "in {} {}{}".format(count, label, plural) if future \
                else "{} {}{} ago".format(count, label, plural)
    return "just now"


def word_count(text: str) -> Dict[str, Any]:
    words = re.findall(r"\b[\w'-]+\b", text or "")
    sentences = [s for s in re.split(r"[.!?]+", text or "") if s.strip()]
    lines = (text or "").splitlines()
    return {
        "ok": True,
        "characters": len(text or ""),
        "characters_no_spaces": len(re.sub(r"\s", "", text or "")),
        "words": len(words),
        "sentences": len(sentences),
        "lines": len(lines),
        "paragraphs": len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()]),
        "unique_words": len({w.lower() for w in words}),
        "reading_minutes": round(len(words) / 200, 1),
        "speaking_minutes": round(len(words) / 130, 1),
    }


def slugify(text: str) -> Dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return {"ok": True, "slug": slug or "untitled",
            "snake": slug.replace("-", "_") or "untitled",
            "camel": _camel(slug), "pascal": _camel(slug, upper_first=True)}


def _camel(slug: str, upper_first: bool = False) -> str:
    parts = [p for p in slug.split("-") if p]
    if not parts:
        return "untitled"
    head = parts[0].capitalize() if upper_first else parts[0]
    return head + "".join(p.capitalize() for p in parts[1:])
