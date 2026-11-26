"""Everyday lookups: conversions, world clock, Wikipedia, dictionary, random."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.services import quickfacts as qf


# -- units --------------------------------------------------------------
@pytest.mark.parametrize("amount, a, b, expected", [
    (5, "miles", "km", 8.04672),
    (1, "inch", "cm", 2.54),
    (10, "kg", "lb", 22.0462262),
    (1, "US gallon", "litres", 3.785411784),
    (2, "cups", "ml", 473.176473),
    (60, "mph", "km/h", 96.56064),
    (1, "GB", "MB", 1000),
    (1, "GiB", "MiB", 1024),
    (90, "minutes", "hours", 1.5),
    (1, "acre", "square feet", 43560),
    (11, "stone", "kg", 69.85322498),
])
def test_units_convert_exactly(amount, a, b, expected):
    assert qf.convert_units(amount, a, b)["result"] == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("amount, a, b, expected", [
    (100, "celsius", "fahrenheit", 212), (32, "F", "C", 0), (0, "K", "C", -273.15),
    (98.6, "fahrenheit", "celsius", 37), (-40, "C", "F", -40),
])
def test_temperatures(amount, a, b, expected):
    assert qf.convert_units(amount, a, b)["result"] == pytest.approx(expected, abs=1e-6)


def test_conversions_that_make_no_sense_say_so():
    with pytest.raises(qf.QuickFactError, match="Cannot convert"):
        qf.convert_units(3, "kg", "miles")
    with pytest.raises(qf.QuickFactError, match="only a temperature"):
        qf.convert_units(3, "celsius", "kg")
    with pytest.raises(qf.QuickFactError, match="don't know the unit"):
        qf.convert_units(3, "smoots", "m")
    with pytest.raises(qf.QuickFactError, match="absolute zero"):
        qf.convert_units(-500, "C", "F")
    with pytest.raises(qf.QuickFactError, match="not a number"):
        qf.convert_units("lots", "kg", "lb")


# -- time zones -----------------------------------------------------------
@pytest.mark.parametrize("place, zone", [
    ("Tokyo", "Asia/Tokyo"), ("new york", "America/New_York"), ("London, England", "Europe/London"),
    ("San Francisco", "America/Los_Angeles"), ("India", "Asia/Kolkata"), ("Sydney", "Australia/Sydney"),
    ("the UK", "Europe/London"), ("Paris France", "Europe/Paris"), ("Asia/Dubai", "Asia/Dubai"),
])
def test_places_find_their_zone(place, zone):
    assert qf.find_zone(place) == zone


def test_unknown_places_are_not_guessed():
    with pytest.raises(qf.QuickFactError, match="time zone"):
        qf.find_zone("Narnia")


def test_world_time_is_computed_from_the_zone_database():
    noon_utc = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    tokyo = qf.world_time("Tokyo", now=noon_utc)
    assert tokyo["time"] == "21:00" and tokyo["utc_offset"] == "UTC+09:00"
    london = qf.world_time("London", now=noon_utc)
    assert london["time"] == "13:00" and london["utc_offset"] == "UTC+01:00"  # summer time
    winter = qf.world_time("London", now=datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc))
    assert winter["utc_offset"] == "UTC+00:00"


# -- random ---------------------------------------------------------------
class _Fixed:
    def choice(self, items):
        return items[-1]

    def randint(self, a, b):
        return b


def test_coins_dice_and_picks():
    assert qf.roll("coin", rng=_Fixed())["result"] == "tails"
    dice = qf.roll("dice", dice="3d6+2", rng=_Fixed())
    assert dice["rolls"] == [6, 6, 6] and dice["result"] == 20
    assert qf.roll("dice", dice="d20", rng=_Fixed())["result"] == 20
    assert qf.roll("pick", choices=["pizza", " ", "tacos"], rng=_Fixed())["result"] == "tacos"
    assert qf.roll("number", low=10, high=1, rng=_Fixed())["result"] == 10
    with pytest.raises(qf.QuickFactError):
        qf.roll("dice", dice="lots")
    with pytest.raises(qf.QuickFactError):
        qf.roll("pick", choices=["only one"])


@pytest.mark.parametrize("args, kind, result", [
    ({"kind": "d20", "low": 1, "high": 20}, "dice", 20),      # what llama3.2 sent for "roll a d20"
    ({"kind": "2d6"}, "dice", 12),
    ({"kind": "die"}, "dice", 6),
    ({"kind": "dice", "low": 1, "high": 20}, "dice", 20),
    ({"kind": "heads or tails"}, "coin", "tails"),
    ({"kind": "choose", "choices": ["a", "b"]}, "pick", "b"),
    ({"kind": "random number", "low": 1, "high": 10}, "number", 10),
    ({"kind": "", "dice": "d8"}, "dice", 8),
    ({"dice": "3d6"}, "dice", 18),                           # what qwen2.5 sent for "roll 3d6"
    ({"kind": "coin", "dice": "d20"}, "dice", 20),
    ({}, "coin", "tails"),
])
def test_what_a_small_model_sends_is_understood(args, kind, result):
    answer = qf.roll(rng=_Fixed(), **args)
    assert answer["kind"] == kind and answer["result"] == result


def test_how_many_dice_however_it_is_said():
    import asyncio as _asyncio

    registry = _registry()
    handler = registry.get("flip_coin_roll_dice").handler
    # What llama3.2 sent for "roll 3d6".
    sent = {"kind": "dice", "low": 2, "high": 6, "num": 3}
    result = _asyncio.run(handler(registry.context, **sent))
    assert result.ok and result.data["dice"] == "3d6" and len(result.data["rolls"]) == 3
    assert qf.roll("dice", dice="d8", count=2, rng=_Fixed())["result"] == 16
    assert qf.roll("dice", dice="2d8", count=5, rng=_Fixed())["result"] == 16   # written count wins


def test_a_kind_nobody_knows_says_not_to_invent_a_result():
    with pytest.raises(qf.QuickFactError, match="do not make up a result"):
        qf.roll("tarot")


def test_real_randomness_stays_in_range():
    rolls = {qf.roll("dice", dice="1d6")["result"] for _ in range(300)}
    assert rolls == {1, 2, 3, 4, 5, 6}


# -- network lookups with a fake internet -----------------------------------
def _fake(routes):
    calls = []

    async def fetch(url):
        calls.append(url)
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                return answer
        return 404, None
    return fetch, calls


def test_currency_uses_the_published_rate_and_caches_it():
    fetch, calls = _fake({"https://api.frankfurter.app/latest?from=USD":
                          (200, {"base": "USD", "date": "2026-11-24", "rates": {"EUR": 0.9, "GBP": 0.8}})})
    facts = qf.QuickFacts(fetch_json=fetch)
    answer = asyncio.run(facts.convert_currency(50, "dollars", "euros"))
    assert answer["result"] == 45.0 and answer["date"] == "2026-11-24" and "ECB" in answer["text"]
    asyncio.run(facts.convert_currency(10, "USD", "pounds"))
    assert len(calls) == 1
    with pytest.raises(qf.QuickFactError, match="No exchange rate"):
        asyncio.run(facts.convert_currency(10, "USD", "XYZ"))
    with pytest.raises(qf.QuickFactError, match="don't know the currency"):
        asyncio.run(facts.convert_currency(10, "USD", "space credits"))


def test_wikipedia_falls_back_to_search():
    summary = (200, {"title": "Ada Lovelace", "description": "English mathematician",
                     "extract": "Augusta Ada King was an English mathematician.", "type": "standard",
                     "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}}})
    fetch, calls = _fake({
        "https://en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace": summary,
        "https://en.wikipedia.org/w/api.php": (200, ["ada lovelace", ["Ada Lovelace"], [], []]),
    })
    answer = asyncio.run(qf.QuickFacts(fetch_json=fetch).wikipedia("ada lovelace"))
    assert answer["title"] == "Ada Lovelace" and answer["url"].endswith("Ada_Lovelace")
    assert len(calls) == 3  # direct miss, search, summary
    nothing, _ = _fake({"https://en.wikipedia.org/w/api.php": (200, ["x", [], [], []])})
    with pytest.raises(qf.QuickFactError, match="no article"):
        asyncio.run(qf.QuickFacts(fetch_json=nothing).wikipedia("zzqxv"))


def test_dictionary_definitions():
    entry = {"en": [{"partOfSpeech": "Noun", "language": "English", "definitions": [
        {"definition": "The <a href=\"/wiki/phenomenon\">phenomenon</a> of a fortunate discovery.",
         "examples": ["It was pure <b>serendipity</b>."]}]}]}
    fetch, _ = _fake({"https://en.wiktionary.org/api/rest_v1/page/definition/serendipity": (200, entry)})
    answer = asyncio.run(qf.QuickFacts(fetch_json=fetch).define("Serendipity?"))
    assert answer["senses"][0] == {"part_of_speech": "noun",
                                   "definition": "The phenomenon of a fortunate discovery.",
                                   "example": "It was pure serendipity."}
    with pytest.raises(qf.QuickFactError, match="no entry"):
        asyncio.run(qf.QuickFacts(fetch_json=fetch).define("qwertyuiop"))
    refused, _ = _fake({"https://en.wiktionary.org": (403, None)})
    with pytest.raises(qf.QuickFactError, match="did not answer"):
        asyncio.run(qf.QuickFacts(fetch_json=refused).define("word"))


# -- the tools, as the model sees them ---------------------------------------
def _registry():
    from backend.ai.tools import build_registry

    return build_registry({})


def test_tools_are_registered_and_answer():
    registry = _registry()
    for name in ("convert_units", "convert_currency", "world_clock", "wikipedia_lookup",
                 "define_word", "flip_coin_roll_dice"):
        assert registry.get(name) is not None
    result = asyncio.run(registry.get("convert_units").handler(registry.context, 5, "miles", "km"))
    assert result.ok and "8.0467" in result.message


@pytest.mark.parametrize("query, tool", [
    ("convert 5 miles to km", "convert_units"),
    ("what is 70 fahrenheit in celsius", "convert_units"),
    ("how much is 50 dollars in euros", "convert_currency"),
    ("what time is it in Tokyo", "world_clock"),
    ("look up Ada Lovelace on wikipedia", "wikipedia_lookup"),
    ("define serendipity", "define_word"),
    ("flip a coin", "flip_coin_roll_dice"),
    ("roll 2d6", "flip_coin_roll_dice"),
])
def test_a_small_model_is_offered_the_right_tool(query, tool):
    names = [s["name"] for s in _registry().select_schemas(query, limit=8)]
    assert tool in names, names


def test_only_the_lookups_that_go_online_need_a_permission():
    registry = _registry()
    for name in ("convert_currency", "wikipedia_lookup", "define_word"):
        assert registry.get(name).permission == "web.fetch"
    for name in ("convert_units", "world_clock", "flip_coin_roll_dice"):
        assert not registry.get(name).permission
    from backend.core.permissions import BY_KEY
    assert BY_KEY["web.fetch"].default is True  # they work out of the box
