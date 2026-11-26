"""Everyday lookups: convert units and money, the time abroad, Wikipedia,
definitions, coins and dice. Every number comes from `services.quickfacts`,
never from the model's memory.

Conversions, the world clock and the dice work offline and need no permission;
currency, Wikipedia and the dictionary read the web, so they sit behind
"Open web pages" (web.fetch), which is on by default."""
from __future__ import annotations

from typing import List

from ...services import quickfacts as qf
from .registry import ToolRegistry, fail, ok


def _service(ctx):
    return ctx.get("quickfacts") or qf.QuickFacts()


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "convert_units",
        "Convert a measurement between units exactly: length (km, miles, feet, inches), weight "
        "(kg, pounds, ounces, stone), volume (litres, gallons, cups), temperature (Celsius, "
        "Fahrenheit, Kelvin), speed (mph, km/h), area, data (GB, MB), time, energy (calories). "
        "Use for 'how many X in Y', 'convert 5 miles to km', '70 F in C'. Never convert in your head.",
        {"type": "object", "properties": {
            "amount": {"type": "number", "description": "The number the owner said"},
            "from_unit": {"type": "string", "description": "e.g. 'miles', 'lb', 'fahrenheit'"},
            "to_unit": {"type": "string", "description": "e.g. 'km', 'kg', 'celsius'"},
        }, "required": ["amount", "from_unit", "to_unit"]},
        category="convert conversion units measurement miles kilometers km pounds kg feet inches "
                 "celsius fahrenheit temperature gallons litres cups ounces",
    )
    async def convert_units(ctx, amount, from_unit: str, to_unit: str):
        try:
            answer = qf.convert_units(amount, from_unit, to_unit)
        except qf.QuickFactError as exc:
            return fail(str(exc))
        return ok(answer, answer["text"] + " Say exactly this result.")

    @registry.add(
        "convert_currency",
        "Convert money between currencies at today's published exchange rate (European Central "
        "Bank). Use for 'how much is 50 dollars in euros', '100 GBP to USD', 'exchange rate'. "
        "Never state an exchange rate from memory.",
        {"type": "object", "properties": {
            "amount": {"type": "number", "description": "How much money"},
            "from_currency": {"type": "string", "description": "e.g. 'USD', 'dollars', 'pounds'"},
            "to_currency": {"type": "string", "description": "e.g. 'EUR', 'euros', 'yen'"},
        }, "required": ["amount", "from_currency", "to_currency"]},
        category="currency money exchange rate convert dollars euros pounds yen usd eur gbp forex",
        permission="web.fetch",
    )
    async def convert_currency(ctx, amount, from_currency: str, to_currency: str):
        try:
            answer = await _service(ctx).convert_currency(amount, from_currency, to_currency)
        except qf.QuickFactError as exc:
            return fail(str(exc))
        return ok(answer, answer["text"])

    @registry.add(
        "world_clock",
        "The current local time and date in another city or country, and how many hours ahead "
        "or behind it is. Use for 'what time is it in Tokyo', 'time in London', 'is it night in "
        "Sydney'. Read from the time zone database, never guessed.",
        {"type": "object", "properties": {
            "place": {"type": "string", "description": "City or country, e.g. 'Tokyo', 'New York'"},
        }, "required": ["place"]},
        category="world clock time zone timezone local time in city country abroad hours ahead behind",
    )
    async def world_clock(ctx, place: str):
        try:
            answer = qf.world_time(place)
        except qf.QuickFactError as exc:
            return fail(str(exc))
        return ok(answer, answer["text"])

    @registry.add(
        "wikipedia_lookup",
        "Look a person, place, thing or topic up on Wikipedia and get its summary. Use for 'who "
        "is X', 'what is X', 'tell me about X', 'look up X on Wikipedia', 'wiki X'.",
        {"type": "object", "properties": {
            "topic": {"type": "string", "description": "What to look up, e.g. 'Ada Lovelace'"},
        }, "required": ["topic"]},
        category="wikipedia wiki encyclopedia who is what is tell me about biography history",
        permission="web.fetch",
    )
    async def wikipedia_lookup(ctx, topic: str):
        try:
            answer = await _service(ctx).wikipedia(topic)
        except qf.QuickFactError as exc:
            return fail(str(exc))
        said = f"Wikipedia, “{answer['title']}”"
        if answer["description"]:
            said += f" ({answer['description']})"
        said += f": {answer['summary']} Source: {answer['url']}"
        if answer["disambiguation"]:
            said += " That name means several things; ask the owner which one they meant."
        return ok(answer, said)

    @registry.add(
        "define_word",
        "What a word means: dictionary definitions, part of speech and an example, from "
        "Wiktionary. Use for 'define X', 'what does X mean', 'meaning of X', 'synonym for X'.",
        {"type": "object", "properties": {
            "word": {"type": "string", "description": "One English word"},
        }, "required": ["word"]},
        category="dictionary define definition meaning word synonym spelling pronunciation vocabulary",
        permission="web.fetch",
    )
    async def define_word(ctx, word: str):
        try:
            answer = await _service(ctx).define(word)
        except qf.QuickFactError as exc:
            return fail(str(exc))
        lines = [f"{answer['word']} (Wiktionary):"]
        for number, sense in enumerate(answer["senses"], 1):
            line = f"{number}. ({sense['part_of_speech']}) {sense['definition']}"
            if sense["example"]:
                line += f" Example: “{sense['example']}”"
            lines.append(line)
        return ok(answer, "\n".join(lines))

    @registry.add(
        "flip_coin_roll_dice",
        "Flip a coin, roll dice (like 2d6 or d20), pick one of several options at random, or a "
        "random number in a range. Use for 'flip a coin', 'heads or tails', 'roll a die', 'roll "
        "2d6', 'pick one: pizza or tacos', 'random number between 1 and 10'. Truly random.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["coin", "dice", "pick", "number"]},
            "dice": {"type": "string",
                     "description": "For dice: e.g. '1d6', '2d6', 'd20' ('roll a d20' -> kind 'dice', dice 'd20')"},
            "choices": {"type": "array", "items": {"type": "string"},
                        "description": "For pick: the options the owner gave"},
            "count": {"type": "integer", "description": "For dice: how many dice (or write it in dice, '3d6')"},
            "low": {"type": "integer", "description": "For number: lowest"},
            "high": {"type": "integer", "description": "For number: highest"},
        }, "required": ["kind"]},
        category="random coin flip heads tails dice roll die d20 pick choose decide random number",
    )
    async def flip_coin_roll_dice(ctx, kind: str = "", dice: str = "",
                                  choices: List[str] = None, low: int = None, high: int = None,
                                  count: int = None, num: int = None, times: int = None):
        # Small models invent their own name for "how many dice": num, times.
        how_many = next((v for v in (count, num, times) if v not in (None, "")), None)
        try:
            answer = qf.roll(kind, dice=dice, choices=choices, low=low, high=high,
                             count=int(how_many) if how_many is not None else None)
        except (qf.QuickFactError, ValueError) as exc:
            return fail(str(exc))
        return ok(answer, answer["text"] + " Report exactly this result.")
