"""
Tool registry.

A tool declares the permission it needs and its risk tier. Dispatch is uniform:

    permission gate  ->  confirmation (if the risk tier demands it)  ->  run
                     ->  audit (for sensitive tools)  ->  structured result

Handlers receive a `ToolContext` carrying every service they might need, so no
tool module has to reach for a global.
"""
from __future__ import annotations

import difflib
import asyncio
import inspect
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ...core.events import Events, bus
from ...core.permissions import PermissionDenied, Risk
from ...core.util import truncate

logger = logging.getLogger("jarvis.tools")

Handler = Callable[..., Awaitable[Any]]


class ToolContext:
    """Everything a tool handler can reach. Populated once at startup."""

    def __init__(self, **services: Any):
        self._services = services
        for key, value in services.items():
            setattr(self, key, value)

    def get(self, name: str, default: Any = None) -> Any:
        return self._services.get(name, default)

    def with_scope(self, **extra: Any) -> "ToolContext":
        """A per-call view carrying conversation/task identifiers."""
        return ToolContext(**{**self._services, **extra})


class ToolResult:
    __slots__ = ("ok", "data", "message", "meta")

    def __init__(self, ok: bool = True, data: Any = None, message: str = "", meta: Optional[Dict[str, Any]] = None):
        self.ok = ok
        self.data = data
        self.message = message
        self.meta = meta or {}

    def to_model_string(self) -> str:
        """What the model actually sees. Deliberately compact.

        After dispatch this is the screened version -- credentials removed,
        and outside content that tries to give instructions wrapped in a
        warning (see `core.screening`) -- so every caller that hands a
        result to a model gets that without having to remember to ask.
        """
        screened = self.meta.get("model_text") if self.meta else None
        if isinstance(screened, str):
            return screened
        return self.raw_model_string()

    def raw_model_string(self) -> str:
        import json

        if not self.ok:
            return f"ERROR: {self.message}"
        if self.data is None:
            return self.message or "OK"
        if isinstance(self.data, str):
            return f"{self.message}\n{self.data}".strip() if self.message else self.data
        try:
            body = json.dumps(self.data, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(self.data)
        return f"{self.message}\n{body}".strip() if self.message else body

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "message": self.message, "meta": self.meta}


def ok(data: Any = None, message: str = "", **meta: Any) -> ToolResult:
    return ToolResult(True, data, message, meta)


def fail(message: str, data: Any = None, **meta: Any) -> ToolResult:
    return ToolResult(False, data, message, meta)


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Handler,
        permission: str = "",
        risk: str = Risk.LOW,
        confirm_summary: Optional[Callable[[Dict[str, Any]], str]] = None,
        category: str = "general",
        requires_integration: str = "",
        timeout: float = 120.0,
        prepare: Optional[Callable[..., Any]] = None,
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.permission = permission
        self.risk = risk
        self.confirm_summary = confirm_summary
        self.category = category
        self.requires_integration = requires_integration
        self.timeout = timeout
        # Runs before the confirmation: `prepare(ctx, arguments)` returns the
        # arguments to ask about and run with -- resolved, so the owner
        # approves what will actually happen ("sam@example.com", not "Sam")
        # -- or a ToolResult to stop with, before anyone is asked anything.
        self.prepare = prepare

    def schema(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "risk": self.risk,
            "category": self.category,
            "requires_integration": self.requires_integration,
        }


class ToolRegistry:
    def __init__(self, context: ToolContext):
        self.context = context
        self._tools: Dict[str, Tool] = {}

    # ------------------------------------------------------------------
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            logger.warning("tool %s registered twice -- overwriting", tool.name)
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> bool:
        """Remove a tool -- a plug-in's, when its server disconnects."""
        return self._tools.pop(name, None) is not None

    def add(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        permission: str = "",
        risk: str = Risk.LOW,
        confirm_summary: Optional[Callable[[Dict[str, Any]], str]] = None,
        category: str = "general",
        requires_integration: str = "",
        timeout: float = 120.0,
        prepare: Optional[Callable[..., Any]] = None,
    ):
        """Decorator form: @registry.add("name", "desc", schema, ...)"""

        def decorator(handler: Handler) -> Handler:
            self.register(
                Tool(
                    name, description, input_schema, handler, permission, risk,
                    confirm_summary, category, requires_integration, timeout,
                    prepare,
                )
            )
            return handler

        return decorator

    # ------------------------------------------------------------------
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def describe_all(self) -> List[Dict[str, Any]]:
        return [t.describe() for t in self.all()]

    def schemas(self, include_denied: bool = False) -> List[Dict[str, Any]]:
        """Tool definitions handed to the model.

        Tools whose permission is disabled are withheld entirely -- the model
        should not be tempted by a capability the user turned off. The gate at
        dispatch is still enforced regardless.
        """
        permissions = self.context.get("permissions")
        out = []
        for tool in self.all():
            if not include_denied and permissions and tool.permission:
                if not permissions.allowed(tool.permission):
                    continue
            out.append(tool.schema())
        return out

    @staticmethod
    def _compact_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
        """Strip a tool definition down to what a small model needs.

        Measured: a 7B local model reliably calls one full-fat schema, but
        stops calling anything once a dozen verbose ones are in context --
        while a dozen *terse* schemas still work. The bulk (usage examples in
        descriptions, per-property prose, enums) is what costs us, so it goes.
        """
        properties = {}
        for name, spec in (schema.get("input_schema", {}).get("properties") or {}).items():
            if not isinstance(spec, dict):
                continue
            trimmed: Dict[str, Any] = {"type": spec.get("type", "string")}
            enum = spec.get("enum")
            if enum and len(enum) <= 5:
                trimmed["enum"] = enum
            properties[name] = trimmed

        description = (schema.get("description") or "").split(". ")[0].strip()
        return {
            "name": schema["name"],
            "description": description[:110],
            "input_schema": {
                "type": "object",
                "properties": properties,
                **({"required": schema["input_schema"]["required"]}
                   if schema.get("input_schema", {}).get("required") else {}),
            },
        }

    def _called_for_by(self, query: str) -> List[str]:
        """Tools a request plainly means, that no keyword could find.

        "I'm back" is the case. Every word of it is scaffolding, so it
        scores nothing against anything, and the one thing it is asking for
        -- hand over what was done while I was out -- was never offered.

        Deliberately narrow: only that phrasing, and only while there is
        unattended work waiting to be handed over. `welcome_back` ends a
        run that is still going, so it is not something to put in front of
        a small model on the strength of the word "back" in "go back a
        track".
        """
        context = getattr(self, "context", None)
        wanted: List[str] = []

        # A skill fits the request: make sure the tool that reads it is
        # in front of the model, whatever the keyword ranking thought.
        skills = context.get("skills") if context is not None else None
        if skills is not None:
            try:
                if skills.match(query or "", limit=1):
                    wanted.append("use_skill")
            except Exception:
                logger.debug("skill matching failed", exc_info=True)

        # Named outright: a skill being run lists the tools it works with.
        for word in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", query or ""):
            if word in self._tools and word not in wanted:
                wanted.append(word)

        if _SETTING.search(query or ""):
            wanted.append("change_setting")
        if _AWAY.search(query or ""):
            wanted += ["start_away_work", "stop_away_work"]
        if _OPEN.search(query or ""):
            wanted.append("open_panel")
        if _REMIND.search(query or ""):
            wanted += ["schedule_notification", "list_reminders", "cancel_reminder"]
        if _RESEARCH.search(query or ""):
            wanted.append("deep_research")
        if re.search(r"\b(?:stop|cancel|abort)\b.*\b(?:research\w*|task|job)\b", query or "", re.I):
            wanted.append("stop_task")

        # "How many days until..." is nearly all scaffolding words.
        if _COUNTDOWN.search(query or ""):
            wanted += ["add_countdown", "list_countdowns", "remove_countdown"]
        if _HOW_LONG.search(query or ""):
            wanted.append("how_long_until")

        autonomy = context.get("autonomy") if context is not None else None
        if autonomy is not None and getattr(autonomy, "current", None) is not None \
                and _ARRIVAL.search(query or ""):
            wanted += ["welcome_back", "away_work_report"]
        return wanted

    def select_schemas(
        self,
        query: str,
        limit: int = 12,
        include_denied: bool = False,
        compact: bool = False,
    ) -> List[Dict[str, Any]]:
        """The most relevant tools for one request, capped.

        Small local models degrade sharply as the tool list grows -- past
        roughly a dozen they stop emitting tool calls and start narrating
        ("I will use tool_44"), which is worse than having no tools at all
        because the model then invents the answer. Cloud models get the full
        set; local ones get a focused slice chosen by keyword overlap.
        """
        available = self.schemas(include_denied=include_denied)
        if len(available) <= limit:
            return [self._compact_schema(s) for s in available] if compact else available

        # Scaffolding words are how a request is phrased, not what it is
        # about, and they were deciding the whole selection. "List unofficial
        # PS5 emulators" scored `list_apps`, `list_folder`, `list_processes`,
        # `list_schedules` and `list_tasks` on the strength of the word
        # "list", filled every slot with them, and left no web search -- so
        # the model said "let me check the web for you" and had nothing to
        # check it with.
        terms = set()
        for word in _words(query):
            if len(word) > 2 and word not in _SCAFFOLDING:
                terms |= _variants(word)
        by_name = {schema["name"]: schema for schema in available}

        scored: List[tuple] = []
        for tool in self.all():
            schema = by_name.get(tool.name)
            if schema is None:
                continue
            haystack = _words(f"{tool.name} {tool.description} {tool.category}")
            overlap = len(terms & haystack)
            # Name matches are worth more than a passing mention in prose.
            name_hit = 2 if terms & _words(tool.name) else 0
            category_hit = 1 if terms & _words(tool.category) else 0
            scored.append((overlap + name_hit + category_hit, tool.name, schema))

        scored.sort(key=lambda entry: entry[0], reverse=True)

        # Looking something up is the universal fallback, so it is reserved a
        # place rather than left to win a keyword contest it cannot win: a
        # question about the outside world rarely contains the word "search".
        # Reading a page comes with it, because finding a link and not being
        # able to open it is no better than not searching.
        chosen: List[Dict[str, Any]] = []
        for reserved in (*_ALWAYS_OFFERED, *self._called_for_by(query)):
            schema = by_name.get(reserved)
            if schema is not None and schema not in chosen:
                chosen.append(schema)

        for score, _name, schema in scored:
            if len(chosen) >= limit:
                break
            if score > 0 and schema not in chosen:
                chosen.append(schema)

        # A small spine, so the model can still orient itself when nothing
        # matched the wording at all.
        for essential in ("system_status", "create_task", "list_tasks",
                          "what_am_i_doing", "recall"):
            if len(chosen) >= limit:
                break
            schema = by_name.get(essential)
            if schema is not None and schema not in chosen:
                chosen.append(schema)

        # Fill any remaining room. An under-filled slice wastes capacity the
        # model is perfectly able to use.
        if len(chosen) < limit:
            for schema in available:
                if len(chosen) >= limit:
                    break
                if schema not in chosen:
                    chosen.append(schema)

        if not chosen:
            chosen = available[:limit]
        chosen = chosen[:limit]
        return [self._compact_schema(s) for s in chosen] if compact else chosen

    # ------------------------------------------------------------------
    async def dispatch(
        self,
        name: str,
        arguments: Dict[str, Any],
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        skip_confirmation: bool = False,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            # This result goes back to the model, so it has to be instruction
            # enough to recover from. A bare "unknown tool" leaves a small
            # local model repeating the same invented call until the round
            # limit, and the user gets nothing.
            close = difflib.get_close_matches(name, list(self._tools), n=3, cutoff=0.5)
            hint = (" Did you mean: " + ", ".join(close) + "?") if close else ""
            return fail(
                f"There is no tool called '{name}'.{hint} Do not call it again. "
                f"If you can answer from what you already know, reply to the user "
                f"directly in plain language instead of calling a tool."
            )

        arguments = arguments if isinstance(arguments, dict) else {}
        permissions = self.context.get("permissions")
        confirmations = self.context.get("confirmations")
        audit = self.context.get("audit")

        bus.publish(
            Events.TOOL_START,
            {
                "tool": name,
                "detail": _summarise(tool, arguments),
                "risk": tool.risk,
                "conversation_id": conversation_id,
                "task_id": task_id,
            },
            source="tools",
        )
        if task_id is None:
            # A reply calling a skill's own tool is using that skill: the
            # screen fades to its colour. Background work never repaints it.
            try:
                from ...skills.activity import skill_activity

                skill_activity.tool_used(name, arguments, self.context.get("skills"))
            except Exception:
                logger.debug("skill colour not updated", exc_info=True)
        started = time.monotonic()

        try:
            # 1. permission gate ----------------------------------------
            if tool.permission and permissions:
                permissions.require(tool.permission)

            # 2. integration availability -------------------------------
            if tool.requires_integration:
                integrations = self.context.get("integrations")
                if integrations and not integrations.is_connected(tool.requires_integration):
                    return fail(
                        f"The '{tool.requires_integration}' integration is not connected. "
                        f"Add its credentials in Settings -> Integrations and I'll be able to do this."
                    )

            # 2b. preparation: settle what the owner will be asked about --
            # and what will run -- before asking them anything.
            if tool.prepare is not None:
                prepared = tool.prepare(self.context, dict(arguments))
                if inspect.isawaitable(prepared):
                    prepared = await prepared
                if isinstance(prepared, ToolResult):
                    return prepared
                if isinstance(prepared, dict):
                    arguments = prepared

            # 3. confirmation -------------------------------------------
            if not skip_confirmation and confirmations and tool.risk in (Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL):
                summary = tool.confirm_summary(arguments) if tool.confirm_summary else _summarise(tool, arguments)
                approved = await confirmations.request(
                    tool=name,
                    summary=summary,
                    detail={"arguments": arguments, "category": tool.category},
                    risk=tool.risk,
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
                if not approved:
                    return fail("The user did not approve this action, so it was not performed.")

            # 4. run -----------------------------------------------------
            scoped = self.context.with_scope(conversation_id=conversation_id, task_id=task_id)
            result = await asyncio.wait_for(_invoke(tool.handler, scoped, arguments), timeout=tool.timeout)
            if not isinstance(result, ToolResult):
                result = ok(result)

            # 5. screen what is going back to the model -----------------
            await self._screen(name, result, conversation_id, audit)

            # 6. audit ---------------------------------------------------
            if audit and tool.risk in (Risk.HIGH, Risk.CRITICAL):
                await audit.record(
                    "tool.sensitive",
                    subject=name,
                    detail={"arguments": arguments, "risk": tool.risk, "ok": result.ok},
                    result="ok" if result.ok else "error",
                )
            return result

        except PermissionDenied as exc:
            logger.info("tool %s denied: %s", name, exc.key)
            return fail(str(exc))
        except asyncio.TimeoutError:
            logger.warning("tool %s timed out after %ss", name, tool.timeout)
            return fail(f"'{name}' timed out after {int(tool.timeout)}s and was stopped.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return fail(f"{type(exc).__name__}: {exc}")
        finally:
            bus.publish(
                Events.TOOL_END,
                {
                    "tool": name,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                },
                source="tools",
            )


    async def _screen(self, name: str, result: ToolResult,
                      conversation_id: Optional[str], audit: Any) -> None:
        """Screen a result before any model reads it. Never fails the call."""
        try:
            from ...core.screening import guard_for_model, screener

            cleaned, screen = screener.screen(result.raw_model_string())
            result.meta["model_text"] = guard_for_model(cleaned, name, screen)
            if not (screen.flagged or screen.redacted):
                return
            result.meta["screen"] = screen.as_dict()
            if screen.flagged:
                logger.warning("tool %s returned content flagged as %s: %s", name,
                               screen.level, [f.name for f in screen.findings])
                bus.publish("security.flagged", {
                    "tool": name, "level": screen.level,
                    "findings": [f.as_dict() for f in screen.findings[:5]],
                    "conversation_id": conversation_id,
                }, source="screening")
                if audit:
                    await audit.record(
                        "security.injection_flagged", subject=name,
                        detail=screen.as_dict(), result="flagged")
        except Exception:
            logger.debug("screening %s failed", name, exc_info=True)


async def _invoke(handler: Handler, context: ToolContext, arguments: Dict[str, Any]) -> Any:
    """Call `handler(ctx, **arguments)`, dropping any argument the handler
    doesn't declare. Models occasionally invent an extra field; that should not
    turn into a TypeError the user sees."""
    result = handler(context, **_bind(handler, arguments))
    if inspect.isawaitable(result):
        return await result
    return result


def _bind(handler: Handler, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return dict(arguments)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return dict(arguments)
    accepted = {name for name, p in parameters.items() if p.kind in
                (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    accepted.discard(next(iter(parameters), ""))  # the context parameter
    dropped = set(arguments) - accepted
    if dropped:
        logger.debug("dropping unexpected tool arguments: %s", sorted(dropped))
    return {k: v for k, v in arguments.items() if k in accepted}


# Words that describe how a request is phrased rather than what it is about.
# They match tool *names* -- "list", "find", "show", "what" -- and so used to
# decide the entire selection: asking to "list unofficial PS5 emulators"
# returned every tool whose name begins with "list_".
_SCAFFOLDING = frozenset("""
    list show tell give find get fetch look check see display
    what which who whom whose where when why how
    can could would will shall should may might must
    please thanks thank just only also some any all
    the and for with from about into onto than then that this these those
    are was were been being have has had does did done
    you your yours use using used want need know think make made
    there here now today currently right actually really very
    new old best top good great bad more most less least
    out off down over under again back
""".split())

# Looking something up is the universal fallback, so these are offered on
# every turn rather than left to win a keyword contest they cannot win: a
# question about the outside world rarely contains the word "search".
_ALWAYS_OFFERED = ("web_search", "read_webpage")

_SETTING = re.compile(r"\b(?:turn|switch)\s+(?:on|off)\b|\b(?:turn|switch)\s+.+\s+(?:on|off)\b|"
                      r"\b(?:enable|disable|mute|unmute)\b|\btheme\b|\bset\s+(?:the\s+)?\w+\s+to\b|"
                      r"\bspeak\s+(?:faster|slower|louder|quieter)\b|\b(?:shorter|longer)\s+(?:answers|replies)\b|"
                      r"\b(?:answers|replies)\s+(?:shorter|longer)\b", re.I)
_AWAY = re.compile(r"\bwhile\s+i(?:'m|\s+am)\s+(?:away|out|gone)\b|\bi'?m\s+(?:going\s+out|heading\s+out|leaving)\b|"
                   r"\baway\s+work\b|\bwhen\s+i(?:'m|\s+am)\s+(?:away|out)\b", re.I)
_OPEN = re.compile(r"^\s*(?:please\s+)?(?:open|show(?:\s+me)?|go\s+to|bring\s+up|pull\s+up|"
                   r"take\s+me\s+to|close)", re.I)
_REMIND = re.compile(r"\b(?:remind(?:er|ers)?|notify\s+me|alert\s+me|ping\s+me|"
                     r"schedule\s+a\s+notification|notifications?\s+(?:at|every|in))\b", re.I)
_RESEARCH = re.compile(r"\b(?:research|dig\s+(?:in|into)|look\s+into|investigate|"
                       r"find\s+out\s+(?:everything|all|more)\s+about)\b", re.I)
_COUNTDOWN = re.compile(r"\bcount(?:ing)?\s?-?down", re.I)
_HOW_LONG = re.compile(r"\bhow\s+(?:long|much\s+longer|many\s+(?:days|weeks|months|sleeps))\b"
                       r".*\b(?:until|till|til|before|left)\b", re.I)

# "I'm back", "im home", "I am back", "I've returned" -- said on walking in.
_ARRIVAL = re.compile(
    r"\b(?:i\s*'?m|i\s+am|we\s*'?re|we\s+are)\s+(?:back|home)\b"
    r"|\bi\s*(?:'ve|\s+have)\s+(?:returned|come\s+back|got\s+back)\b",
    re.IGNORECASE)


def _variants(term: str) -> set:
    """A word and its obvious singular/plural forms.

    Not a stemmer -- just enough that asking about "the file called invoice"
    still reaches `find_files`, and "processes" still reaches
    `list_processes`. Exact matching missed both, which reads as the model
    being dim when it was simply never offered the tool.
    """
    forms = {term}
    if term.endswith("es") and len(term) > 4:
        forms.add(term[:-2])
    if term.endswith("s") and len(term) > 3:
        forms.add(term[:-1])
    else:
        forms.add(term + "s")
    return forms


def _words(text: str) -> set:
    import re

    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _summarise(tool: Tool, arguments: Dict[str, Any]) -> str:
    if not arguments:
        return tool.name
    parts = [f"{k}={truncate(v, 80)}" for k, v in list(arguments.items())[:4]]
    return f"{tool.name}({', '.join(parts)})"
