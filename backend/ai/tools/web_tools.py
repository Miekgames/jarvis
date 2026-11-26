"""Web search, page reading and weather."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.util import truncate
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "web_search",
        "Search the web. Returns titles, URLs and snippets. For anything needing more than a quick "
        "look-up, queue a 'research' background task instead.",
        {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        }, "required": ["query"]},
        permission="web.search",
        category="web",
        timeout=45.0,
    )
    async def web_search(ctx, query: str, max_results: int = 6):
        web = ctx.get("web")
        try:
            # Small models ask for one result and then answer from it; one
            # result is how a question gets answered by the wrong page.
            results = await web.search(query, max(6, min(int(max_results or 6), 12)))
        except RuntimeError as exc:
            return fail(str(exc))
        if not results:
            return ok({"results": []}, f"No results for '{query}'.")
        return ok({"query": query, "results": results}, f"{len(results)} result(s).")

    # ==================================================================
    @registry.add(
        "news_search",
        "Search recent news articles on a topic.",
        {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        }, "required": ["query"]},
        permission="web.search",
        category="web",
        timeout=45.0,
    )
    async def news_search(ctx, query: str, max_results: int = 8):
        try:
            results = await ctx.get("web").news(query, min(max_results, 15))
        except RuntimeError as exc:
            return fail(str(exc))
        return ok({"query": query, "articles": results}, f"{len(results)} article(s).")

    # ==================================================================
    @registry.add(
        "read_webpage",
        "Fetch a web page and return its readable text. Use after web_search to actually read a source.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer"},
        }, "required": ["url"]},
        permission="web.fetch",
        category="web",
        timeout=60.0,
    )
    async def read_webpage(ctx, url: str, max_chars: int = 8000):
        result = await ctx.get("web").fetch(url, min(max_chars, 20000))
        if not result.get("ok"):
            return fail(f"Couldn't read {url}: {result.get('error')}")
        return ok(
            {"url": result["url"], "title": result["title"], "text": result["text"],
             "truncated": result["truncated"]},
            f"Read '{result['title']}' ({result['chars']} characters).",
        )

    # ==================================================================
    @registry.add(
        "weather",
        "Current conditions and a five-day forecast for any location. Works with no account.",
        {"type": "object", "properties": {
            "location": {"type": "string", "description": "City name; omit to use the configured default"},
            "include_forecast": {"type": "boolean"},
        }},
        permission="web.fetch",
        category="information",
        timeout=30.0,
    )
    async def weather(ctx, location: str = "", include_forecast: bool = False):
        service = ctx.get("weather")
        if service is None:
            return fail("Weather service unavailable.")
        result = await service.current(location or None)
        if not result.get("ok"):
            return fail(result.get("message", "Weather unavailable."))
        payload = {
            "location": result["location"],
            "conditions": result["conditions"],
            "temperature": result["temperature"],
            "feels_like": result["feels_like"],
            "humidity": result["humidity"],
            "wind_speed": result["wind_speed"],
            "unit": result["unit"],
        }
        if include_forecast:
            payload["forecast"] = result["forecast"]
            payload["sunrise"] = result["sunrise"]
            payload["sunset"] = result["sunset"]
        return ok(payload, result["summary"])

    # ==================================================================
    @registry.add(
        "calculate",
        "Evaluate a mathematical expression precisely. Use this instead of doing arithmetic in your head.",
        {"type": "object", "properties": {
            "expression": {"type": "string", "description": "e.g. '(1920*1080)/1e6' or 'sqrt(2)*17'"}
        }, "required": ["expression"]},
        category="information",
    )
    async def calculate(ctx, expression: str):
        import ast
        import math
        import operator

        operators = {
            ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
            ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
        }
        functions = {
            name: getattr(math, name)
            for name in ("sqrt", "sin", "cos", "tan", "log", "log10", "log2", "exp", "floor",
                         "ceil", "fabs", "degrees", "radians", "atan", "asin", "acos")
        }
        functions.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
        constants = {"pi": math.pi, "e": math.e, "tau": math.tau}

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                raise ValueError("Only numbers are allowed.")
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
                return operators[type(node.op)](evaluate(node.operand))
            if isinstance(node, ast.Name) and node.id in constants:
                return constants[node.id]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                function = functions.get(node.func.id)
                if function is None:
                    raise ValueError(f"Unknown function '{node.func.id}'.")
                return function(*[evaluate(a) for a in node.args])
            if isinstance(node, (ast.Tuple, ast.List)):
                return [evaluate(e) for e in node.elts]
            raise ValueError("Unsupported expression.")

        try:
            tree = ast.parse(expression, mode="eval")
            value = evaluate(tree)
        except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
            return fail(f"Couldn't evaluate that: {exc}")
        return ok({"expression": expression, "result": value}, f"{expression} = {value}")
