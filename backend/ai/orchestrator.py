"""
The AI orchestration layer.

Responsibilities:
  * provider selection, model routing per purpose, and fallback
  * the full tool-use loop (stream text -> run tools -> feed results back)
  * conversation persistence and context assembly
  * usage/cost accounting
  * presence events that drive the particle field

The loop is deliberately explicit rather than clever: every iteration is
observable in the activity feed and every tool call is gated.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import new_id, now_iso, truncate, utcnow
from ..db.database import dumps, loads
from .providers.anthropic_provider import AnthropicProvider, estimate_cost
from .providers.base import Completion, Provider, ProviderError, RateLimited
from .providers.ollama_provider import OllamaProvider
from .prompt import build_compact_prompt, build_system_prompt
from .telemetry import ReplyTelemetry
from ..services.live_lookup import follow_up_question, needs_live_lookup
from ..systemmon.energy import EnergyMeter

async def _quietly(awaitable) -> None:
    """Await something whose failure does not matter here."""
    try:
        await awaitable
    except Exception:
        pass


logger = logging.getLogger("jarvis.ai")

MAX_TOOL_ITERATIONS = 12
# A reply that ducks a question it could have looked up: "I don't have
# real-time data", "let me search the web for you" (and then stopping), "check
# sports news websites". A small local model says these instead of calling
# the tool it has.
_DODGED = re.compile(
    r"\b(?:don't|do\s+not|can't|cannot)\s+(?:have\s+)?(?:access\s+to\s+)?"
    r"(?:real[-\s]?time|live|current|up[-\s]to[-\s]date)\s+(?:data|information|info|access|results|updates)\b"
    r"|\b(?:can't|cannot|unable\s+to)\s+(?:browse|access\s+the\s+(?:internet|web))\b"
    r"|\blet\s+me\s+(?:search|look\s+(?:that|it)\s+up|check)\b"
    r"|\bI(?:'ll|\s+will)\s+(?:search|look\s+(?:that|it)\s+up)\b"
    r"|\b(?:check|visit|look\s+at)\s+(?:the\s+)?(?:latest\s+|official\s+|local\s+)?(?:\w+\s+)?"
    r"(?:news\s+)?(?:websites?|sites?|sources?|sportsbooks?|odds|news|apps?)\b"
    r"|\bas\s+of\s+my\s+(?:last|latest)\s+(?:update|training)\b|\bknowledge\s+cut-?off\b",
    re.I,
)


def dodged_lookup(reply: str) -> bool:
    """True when the reply put off a question it should have looked up."""
    return bool(_DODGED.search(reply or ""))


# Invented tool names in one turn before the tools are taken away and the
# model is made to answer in words.
MAX_UNKNOWN_TOOL_ATTEMPTS = 2
MAX_RETRIES = 3
# Above this many tools, local models stop emitting tool calls altogether.
LOCAL_TOOL_LIMIT = 8

# Rough bytes-per-token for English prose. Good enough to budget with, and far
# cheaper than running a real tokeniser over every message on every turn.
CHARS_PER_TOKEN = 4


class JarvisState:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WORKING = "working"
    ERROR = "error"
    OFFLINE = "offline"
    SLEEP = "sleep"


_RESEARCH_SAID = re.compile(
    r"(?:(?:can|could|will)\s+you\s+|please\s+|i\s+want\s+you\s+to\s+|go\s+and\s+)?"
    r"(?:do\s+(?:some\s+|a\s+)?(?:deep\s+)?research\s+(?:on|into|about)"
    r"|(?:deep\s+)?research(?:\s+(?:on|into|about))?"
    r"|dig\s+into|look\s+into"
    r"|find\s+out\s+(?:everything|all|more)\s+(?:you\s+can\s+)?about)"
    r"\s+(.{3,200}?)(?:\s+(?:for\s+me|please|properly|in\s+depth))*")


_STOP_SAID = re.compile(
    r"(?:please\s+)?(?:stop|cancel|abort|kill|end)\s+"
    r"(?:the\s+|that\s+|this\s+|my\s+)?(research(?:ing)?(?:\s+(?:on|into|about)\s+.+)?|task|"
    r"background\s+task|job|.+?\s+research)")


def stop_said(message: str) -> Optional[str]:
    """"Stop the research", "cancel that task": which one, in the owner's words."""
    said = " ".join((message or "").lower().strip(" .!?").split())
    said = re.sub(r"^(?:hey\s+)?jarvis[,:]?\s+", "", said)
    said = re.sub(r"\s+(?:please|now|right\s+now)$", "", said)
    m = _STOP_SAID.fullmatch(said)
    return m.group(1).replace("researching", "research") if m else None


def research_topic_in(message: str) -> Optional[str]:
    """The topic of a plain "research X" request, else None."""
    said = " ".join((message or "").strip(" .!?").split())
    said = re.sub(r"^(?:hey\s+)?jarvis[,:]?\s+", "", said, flags=re.I)
    m = _RESEARCH_SAID.fullmatch(said.lower())
    if not m:
        return None
    topic = said[m.start(1):m.end(1)].strip(" ,")
    if topic.lower() in ("it", "that", "this", "them", "those", "this topic", "that topic"):
        return None                                   # needs the conversation: the model's job
    if re.match(r"(?:my|our|why\s+my|what'?s\s+wrong\s+with\s+my)(?:\s|$)", topic, re.I):
        return None                                   # "look into my emails": not web research
    return topic


class Orchestrator:
    def __init__(self, db, settings, settings_store, registry, memory, activity, usage=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.registry = registry
        self.memory = memory
        # Work started after a reply, held so it is not collected mid-flight.
        self._background: set = set()
        self._learner = None
        # Conversations whose oldest messages no longer fit, and where from.
        self._fell_off: Dict[str, str] = {}
        self._summaries = None
        self.activity = activity
        self.usage = usage

        self.providers: Dict[str, Provider] = {}
        # The owner's own API keys, sealed on disk (see ai/cloud.py), and the
        # cloud provider built from the choice made in setup.
        from .cloud import KeyStore

        self.keys = KeyStore(settings.DATA_DIR)
        self._cloud_signature: Optional[tuple] = None
        # In local-only mode the cloud provider is never constructed, so there
        # is no code path that could reach it -- not a flag checked at call
        # time that a future edit might forget.
        if not settings.local_only and settings.ANTHROPIC_API_KEY:
            self.providers["anthropic"] = AnthropicProvider(settings.ANTHROPIC_API_KEY, settings.MODEL_PRIMARY)
        self.providers["ollama"] = OllamaProvider(
            settings.OLLAMA_HOST, settings.OLLAMA_MODEL,
            getattr(settings, "OLLAMA_ROUTING", {}),
            num_ctx=getattr(settings, "OLLAMA_NUM_CTX", 0),
            keep_alive=getattr(settings, "OLLAMA_KEEP_ALIVE", ""),
        )

        self._state = JarvisState.IDLE
        self._state_detail = ""
        # The thread in play. Spoken commands continue this rather than
        # starting a new one each time.
        self.active_conversation_id: Optional[str] = None
        self._provider_health: Dict[str, bool] = {}
        self._active_runs: Dict[str, asyncio.Task] = {}

        # Intelligence-layer collaborators, attached once the container has
        # built them. Optional so the orchestrator still works without them.
        self.awareness = None
        self.patterns = None
        self.reasoning = None

        self.resolver = None

    def attach_intelligence(self, awareness=None, patterns=None, reasoning=None) -> None:
        self.awareness = awareness
        self.patterns = patterns
        self.reasoning = reasoning

    def attach_resolver(self, resolver) -> None:
        """Command interpretation 2.0 -- resolves 'this', 'that', 'yesterday'."""
        self.resolver = resolver

    # ==================================================================
    # presence
    # ==================================================================
    @property
    def state(self) -> str:
        return self._state

    @property
    def owner_busy(self) -> bool:
        """Answering the owner right now. Background work that shares the
        model (long research) waits for this, so a reply is never stuck
        behind twenty minutes of somebody else's questions."""
        return self._state in (JarvisState.THINKING, JarvisState.WORKING)

    def set_state(self, state: str, detail: str = "") -> None:
        if state == self._state and detail == self._state_detail:
            return
        self._state = state
        self._state_detail = detail
        bus.publish(Events.STATE_CHANGED, {"state": state, "detail": detail}, source="ai")
        if state == JarvisState.IDLE:
            # The reply is over: the screen fades back from its skill's colour.
            from ..skills.activity import skill_activity

            skill_activity.reply_done()

    # ==================================================================
    # model routing
    # ==================================================================
    def model_for(self, purpose: str = "conversation") -> str:
        table = {
            "primary": self.settings.MODEL_PRIMARY,
            "fast": self.settings.MODEL_FAST,
            "reasoning": self.settings.MODEL_REASONING,
            "coding": self.settings.MODEL_CODING,
            "vision": self.settings.MODEL_VISION,
        }
        ai_settings = self.settings_store.get_cached("ai", {})
        route = (ai_settings.get("route") or {}).get(purpose, "primary")
        return table.get(route, self.settings.MODEL_PRIMARY)

    async def _compact_prompt(self, extra_context: str = "") -> str:
        """Cached-per-turn compact prompt used whenever a local model answers."""
        personality = await self.settings_store.get("personality", {})
        return await build_compact_prompt(
            settings=self.settings, personality=personality, db=self.db,
            settings_store=self.settings_store, extra_context=extra_context,
        )

    def _tools_for_provider(
        self,
        provider_name: str,
        tools: Optional[List[Dict[str, Any]]],
        query: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Cloud models cope with the full tool surface; local ones do not.

        Measured on this machine: with one tool a 7B local model calls it
        correctly, with 84 it stops calling tools entirely and narrates instead
        -- then invents the answer. So the local provider gets a relevance-
        ranked slice."""
        if not tools or self._is_cloud(provider_name):
            return tools
        limit = int(self.settings_store.get_cached("ai", {}).get("local_tool_limit", LOCAL_TOOL_LIMIT))
        narrowed = self.registry.select_schemas(query, limit=limit, compact=True)
        logger.debug("narrowed tools for %s: %s -> %s", provider_name, len(tools), len(narrowed))
        return narrowed

    async def _model_for_provider(self, provider_name: str, purpose: str) -> Optional[str]:
        """Each provider names its models differently, so translate the purpose
        rather than passing a cloud model id to a local runtime."""
        if provider_name == "anthropic":
            return self.model_for(purpose)
        if provider_name == "cloud":
            config = self.cloud_config()
            if purpose == "fast" and config.get("fast_model"):
                return config["fast_model"]
            return config.get("model") or None
        provider = self.providers.get(provider_name)
        if provider is not None and hasattr(provider, "resolve_for"):
            try:
                return await provider.resolve_for(purpose)
            except Exception:
                logger.debug("local routing failed for %s", purpose, exc_info=True)
        return None

    def _local_server(self) -> Optional[str]:
        """The OpenAI-compatible local server chosen in Settings, if any.

        Built (or rebuilt) from the current settings each time it is asked
        for, so choosing a server in Settings takes effect on the next
        message without a restart. It is on this machine, so it counts as
        local for privacy mode.
        """
        config = (self.settings_store.get_cached("ai", {}) or {}).get("local_server") or {}
        if not config.get("enabled") or not config.get("base_url"):
            self.providers.pop("local_server", None)
            return None
        from .providers.openai_compat_provider import OpenAICompatProvider, api_base

        current = self.providers.get("local_server")
        wanted = (api_base(config.get("base_url", "")), config.get("model", ""))
        if current is None or (getattr(current, "base_url", ""),
                               getattr(current, "model", "")) != wanted:
            self.providers["local_server"] = OpenAICompatProvider(
                wanted[0], model=wanted[1], label=config.get("label") or "Local server")
        return "local_server"

    @staticmethod
    def _is_cloud(provider_name: str) -> bool:
        """Cloud models take the full prompt and the full tool list."""
        return provider_name in ("anthropic", "cloud")

    def cloud_config(self) -> Dict[str, Any]:
        """The provider and model chosen in setup: {provider, model, ...}."""
        return dict((self.settings_store.get_cached("ai", {}) or {}).get("cloud") or {})

    def _cloud(self) -> Optional[str]:
        """The owner's chosen cloud provider, built from current settings.

        Rebuilt only when the provider, model, address or key changes, so a
        new choice in setup takes effect on the next message. Never built in
        local-only mode.
        """
        config = self.cloud_config()
        provider = config.get("provider") or ""
        if self.settings.local_only or not provider or provider == "ollama":
            self.providers.pop("cloud", None)
            self._cloud_signature = None
            return None
        key = self.keys.get(provider)
        import hashlib

        signature = (provider, config.get("model") or "", config.get("base_url") or "",
                     hashlib.sha256(key.encode("utf-8")).hexdigest())
        if signature != self._cloud_signature or "cloud" not in self.providers:
            from .cloud import build

            try:
                self.providers["cloud"] = build(provider, key, config.get("model") or "",
                                                config.get("base_url") or "", name="cloud")
            except ValueError as exc:
                logger.warning("cloud provider not usable: %s", exc)
                self.providers.pop("cloud", None)
                self._cloud_signature = None
                return None
            self._cloud_signature = signature
        return "cloud"

    def _provider_order(self) -> List[str]:
        """Preferred provider first; fallbacks after. Privacy mode pins the
        local providers so nothing leaves the machine."""
        local = ["ollama"]
        server = self._local_server()
        if server:
            # Preferred over Ollama when chosen; Ollama stays as the fallback.
            local = [server, "ollama"]
        if self.settings.local_only:
            return local
        privacy = self.settings_store.get_cached("privacy", {})
        if privacy.get("privacy_mode") or not privacy.get("cloud_ai", True):
            return local
        order = [name for name in ("anthropic",) if name in self.providers]
        cloud = self._cloud()
        if cloud:
            order.insert(0, cloud)
        ai_settings = self.settings_store.get_cached("ai", {})
        if ai_settings.get("fallback_enabled", True) or server:
            order.extend(local)
        return order or local

    async def provider_status(self) -> Dict[str, Any]:
        status = {}
        for name, provider in self.providers.items():
            try:
                alive = await asyncio.wait_for(provider.available(), timeout=3.0)
            except Exception:
                alive = False
            self._provider_health[name] = alive
            status[name] = {"available": alive, "name": name}
        return status

    # ==================================================================
    # conversation storage
    # ==================================================================
    async def current_conversation(self, idle_minutes: int = 180) -> str:
        """The thread to continue when the caller has not named one.

        Spoken commands used to pass no conversation at all, so every one of
        them started a fresh thread with no history -- JARVIS could not
        remember what it had just said, because as far as it was concerned it
        had never said anything. Forty-four of eighty-two conversations on
        this machine were single-turn orphans created that way.

        Voice and typing should be one conversation: saying "what about the
        second one" after typing a question is the obvious thing to do. So an
        unnamed caller continues whatever is current, falling back to the most
        recently touched thread if this is the first command since a restart.

        A long enough silence does start a new thread -- continuing yesterday
        morning's conversation is its own kind of wrong.
        """
        if self.active_conversation_id:
            existing = await self.db.fetch_one(
                "SELECT id FROM conversations WHERE id = ?", (self.active_conversation_id,))
            if existing:
                return self.active_conversation_id
            self.active_conversation_id = None

        # The cutoff is built in Python, not by SQLite's `datetime()`.
        #
        # Timestamps are stored as ISO 8601 -- "2026-11-01T20:16:47+00:00" --
        # while `datetime('now')` yields "2026-11-01 20:07:33". Comparing
        # those as strings matches at the date and then hits 'T' against a
        # space, and 'T' sorts higher, so *every conversation from today*
        # counted as "within the last ten minutes". A like-for-like string is
        # the whole fix.
        cutoff = (utcnow() - timedelta(minutes=int(idle_minutes))).isoformat()
        row = await self.db.fetch_one(
            "SELECT id FROM conversations WHERE archived = 0"
            " AND updated_at > ? ORDER BY updated_at DESC LIMIT 1",
            (cutoff,),
        )
        if row:
            self.active_conversation_id = row["id"]
            return row["id"]

        return await self.ensure_conversation(None)

    async def ensure_conversation(self, conversation_id: Optional[str] = None, project_id: Optional[str] = None) -> str:
        if conversation_id:
            existing = await self.db.fetch_one("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
            if existing:
                return conversation_id
        new = conversation_id or new_id("conv")
        await self.db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, project_id) VALUES (?,?,?,?,?)",
            (new, "New conversation", now_iso(), now_iso(), project_id),
        )
        return new

    async def _save_message(
        self,
        conversation_id: str,
        role: str,
        text: str,
        blocks: List[Dict[str, Any]],
        model: str = "",
        usage: Optional[Dict[str, int]] = None,
        latency_ms: int = 0,
    ) -> str:
        message_id = new_id("msg")
        usage = usage or {}
        await self.db.execute(
            "INSERT INTO messages (id, conversation_id, role, text, blocks, created_at, model,"
            " tokens_in, tokens_out, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                message_id, conversation_id, role, text, dumps(blocks), now_iso(), model,
                int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), latency_ms,
            ),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id)
        )
        return message_id

    def _history_budget(self) -> int:
        """How many characters of conversation can actually be sent.

        Ollama counts the prompt and the reply against one window, so the
        space for history is what is left after the system prompt, the tool
        schemas and whatever the model is allowed to generate. Sending more
        than that is worse than useless: the runtime drops the overflow from
        the *front*, which is where the system prompt lives.
        """
        context = int(getattr(self.settings, "OLLAMA_NUM_CTX", 0) or 8192)
        reply = int(getattr(self.settings, "MAX_TOKENS", 4096))
        # The system prompt plus a narrowed tool set measures around 800
        # tokens on this build; 1800 leaves room for it to grow.
        reserve = 1800
        tokens = max(512, context - reply - reserve)
        return tokens * CHARS_PER_TOKEN

    async def history(self, conversation_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """The conversation so far, newest-biased and trimmed to fit.

        It used to return a flat count of the last 48 messages. In a
        conversation that uses tools that is barely a dozen exchanges -- every
        tool call and every result is a message -- so JARVIS lost the thread
        of anything substantial. The limit is now generous and the real
        constraint is the context window, which is measured rather than
        assumed.
        """
        limit = limit or self.settings.MAX_HISTORY_TURNS * 2
        rows = await self.db.fetch_all(
            "SELECT role, blocks, created_at FROM messages WHERE conversation_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (conversation_id, limit),
        )

        # Walk newest-first and stop at the budget, so what survives is the
        # most recent conversation rather than an arbitrary prefix.
        budget = self._history_budget()
        kept: List[Dict[str, Any]] = []
        used = 0
        oldest_kept = ""
        for row in rows:
            content = loads(row["blocks"], [])
            size = len(dumps(content))
            if kept and used + size > budget:
                break
            used += size
            kept.append({"role": row["role"], "content": content})
            oldest_kept = row.get("created_at") or oldest_kept

        # Something fell off the front -- trimmed to fit, or past the row
        # limit. Remember where, so it can be summarised after this reply.
        if oldest_kept and (len(kept) < len(rows) or len(rows) >= limit):
            self._fell_off[conversation_id] = oldest_kept

        if len(kept) < len(rows):
            logger.debug("history trimmed to %d of %d messages (%d/%d chars)",
                         len(kept), len(rows), used, budget)
        kept.reverse()
        return _repair_tool_pairs(kept)

    async def transcript(self, conversation_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, role, text, blocks, created_at, model, tokens_in, tokens_out, latency_ms"
            " FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (conversation_id, limit),
        )
        for row in rows:
            row["blocks"] = loads(row["blocks"], [])
        return rows

    async def title_conversation(self, conversation_id: str, first_message: str) -> None:
        title = truncate(first_message.strip().replace("\n", " "), 60) or "New conversation"
        await self.db.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND title = 'New conversation'",
            (title, conversation_id),
        )

    # ==================================================================
    # the main entry point
    # ==================================================================
    async def chat(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        purpose: str = "conversation",
        source: str = "text",
        stream: bool = True,
        extra_context: str = "",
    ) -> AsyncIterator[Dict[str, Any]]:
        """Async generator of UI events. Terminal event is always `final` or
        `error`, so the caller can always close its state cleanly."""
        conversation_id = await self.ensure_conversation(conversation_id)
        # "/meeting-notes <notes>": run with that skill's instructions in
        # front of the model. For a small local model, choosing the skill
        # itself is the unreliable part, so the owner can simply say which.
        skill_tools = self._skill_tools(message)
        message, extra_context = self._invoke_skill(message, extra_context)
        # What the tools are chosen by: the request, and any tools the
        # invoked skill works with. "/countdown the Olympics" reaches here
        # as "the Olympics", which asks for no tool by itself.
        tool_query = f"{message} {skill_tools}".strip()
        # Whatever is being talked in is what an unnamed caller -- a spoken
        # command -- should continue.
        self.active_conversation_id = conversation_id
        await self.title_conversation(conversation_id, message)
        await self._record_command(message, source, conversation_id)

        await self._save_message(conversation_id, "user", message, [{"type": "text", "text": message}])
        bus.publish(
            Events.MESSAGE,
            {"conversation_id": conversation_id, "role": "user", "text": message, "source": source},
            source="ai",
        )

        self.set_state(JarvisState.THINKING)
        yield {"type": "start", "conversation_id": conversation_id}

        # "Open settings", "close that": done straight away, without the
        # model -- quick, and it works even while research has the model busy.
        done = self._open_or_close(message)
        if done is None:
            done = await self._plain_setting(message, conversation_id)
        if done is None:
            done = await self._plain_research(message, conversation_id)
        if done is not None:
            await self._save_message(conversation_id, "assistant", done,
                                     [{"type": "text", "text": done}])
            self.set_state(JarvisState.IDLE)
            yield {"type": "delta", "text": done}
            yield {"type": "final", "text": done, "conversation_id": conversation_id}
            return

        # Learn from the command before answering it, so the very next turn can
        # already use the pattern.
        if self.patterns is not None:
            try:
                await self.patterns.record(message, intent=purpose)
            except Exception:
                logger.debug("pattern learning failed", exc_info=True)

        # "Who's projected to win the Bears v Lions game?": today's articles
        # are read first and the model answers from them. Asked cold, a small
        # local model says it has no live data, or answers from six snippets.
        # A short follow-up ("and the over/under?") is looked up as part of
        # the question before it.
        looked_up = False
        lookup_question: Optional[str] = None
        if self.registry.get("live_lookup") is not None:
            lookup_question = (message if needs_live_lookup(message) else
                               follow_up_question(message, await self._last_lookup(conversation_id)))
        if lookup_question:
            looked_up = True
            self.set_state(JarvisState.WORKING, "live_lookup")
            yield {"type": "tool_start", "tool": "live_lookup", "input": {"question": lookup_question}}
            end, found = await self._live_lookup(lookup_question, conversation_id)
            yield end
            if found:
                await self._record_lookup(conversation_id, lookup_question, found)
            self.set_state(JarvisState.THINKING)

        messages = await self.history(conversation_id)
        local_context: List[str] = []
        system_prompt = await self._system_prompt(message, extra_context, conversation_id,
                                                  local_out=local_context)
        compact_prompt = await self._compact_prompt(
            "\n\n".join(block for block in local_context if block))
        tools = self.registry.schemas()
        available_tools = {t.get("name") for t in (tools or [])
                           if isinstance(t, dict)}
        unknown_attempts = 0
        max_iterations = int((self.settings_store.get_cached("ai", {}) or {}).get(
            "max_tool_iterations", MAX_TOOL_ITERATIONS))

        final_text = ""
        # What this reply costs to produce: tokens, timings, GPU energy.
        telemetry = ReplyTelemetry()
        meter: Optional[EnergyMeter] = EnergyMeter()
        try:
            await meter.start()
        except Exception:
            logger.debug("energy meter unavailable", exc_info=True)
            meter = None
        try:
            for iteration in range(max_iterations):
                completion = None
                collected = ""

                if stream and self.settings_store.get_cached("ai", {}).get("streaming", True):
                    async for event in self._stream_with_fallback(
                        messages, system_prompt, tools, purpose, tool_query=tool_query,
                        system_compact=compact_prompt):
                        if event["type"] == "text":
                            telemetry.token()
                            collected += event["text"]
                            bus.publish(
                                Events.MESSAGE_DELTA,
                                {"conversation_id": conversation_id, "text": event["text"]},
                                source="ai",
                            )
                            yield {"type": "delta", "text": event["text"]}
                        elif event["type"] == "done":
                            completion = event["completion"]
                else:
                    completion = await self._complete_with_fallback(
                        messages, system_prompt, tools, purpose, tool_query=tool_query,
                        system_compact=compact_prompt)
                    if completion.text:
                        telemetry.token()
                        yield {"type": "delta", "text": completion.text}

                if completion is None:
                    raise ProviderError("Provider returned no completion.", retryable=False)

                await self._record_usage(completion, purpose)
                telemetry.add(completion)

                # ---- plain answer: we're done ---------------------------
                if not completion.wants_tools:
                    final_text = completion.text or collected
                    # "I don't have real-time data. Let me search the web for
                    # you." -- and then nothing. Look it up and ask again; the
                    # screen shows the real answer when it's done.
                    if (not looked_up and dodged_lookup(final_text)
                            and self.registry.get("live_lookup") is not None):
                        looked_up = True
                        logger.info("reply put off a look-up -- looking it up and asking again")
                        self.set_state(JarvisState.WORKING, "live_lookup")
                        question = lookup_question or message
                        yield {"type": "delta", "text": "\n"}
                        yield {"type": "tool_start", "tool": "live_lookup", "input": {"question": question}}
                        end, found = await self._live_lookup(question, conversation_id)
                        yield end
                        self.set_state(JarvisState.THINKING)
                        if found:
                            await self._record_lookup(conversation_id, question, found, messages)
                            continue
                    await self._save_message(
                        conversation_id, "assistant", final_text, completion.blocks or
                        [{"type": "text", "text": final_text}],
                        completion.model, completion.usage, completion.latency_ms,
                    )
                    break

                # ---- tool round ----------------------------------------
                messages.append({"role": "assistant", "content": completion.blocks})
                await self._save_message(
                    conversation_id, "assistant", completion.text, completion.blocks,
                    completion.model, completion.usage, completion.latency_ms,
                )
                if completion.text:
                    yield {"type": "delta", "text": "\n"}

                self.set_state(JarvisState.WORKING, completion.tool_calls[0]["name"])
                results = []
                for call in completion.tool_calls:
                    if call["name"] not in available_tools:
                        unknown_attempts += 1
                    yield {"type": "tool_start", "tool": call["name"], "input": call["input"]}
                    # perf_counter: monotonic() on Windows ticks every ~16 ms,
                    # which reports most tool calls as taking no time at all.
                    tool_started = time.perf_counter()
                    result = await self.registry.dispatch(
                        call["name"], call["input"], conversation_id=conversation_id
                    )
                    screen = result.meta.get("screen") or {}
                    yield {
                        "type": "tool_end",
                        "tool": call["name"],
                        "ok": result.ok,
                        "summary": truncate(result.message or result.to_model_string(), 200),
                        # Shown on the tool's mark in the conversation, so a
                        # page that tried to give orders is visible as such.
                        "flagged": bool(screen.get("flagged")),
                        "flag_level": screen.get("level", ""),
                        "redacted": sum((screen.get("redacted") or {}).values()),
                        "duration_ms": round((time.perf_counter() - tool_started) * 1000, 1),
                        # Pages a tool actually read, listed under the answer.
                        "sources": result.meta.get("sources") or [],
                    }
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": truncate(result.to_model_string(), 12000),
                            "is_error": not result.ok,
                        }
                    )

                messages.append({"role": "user", "content": results})
                await self._save_message(conversation_id, "user", "", results)

                # Circuit breaker. A small local model that starts inventing
                # tool names will keep inventing them until the round limit,
                # which is a minute of silence and then nothing. After a
                # couple of misses, take the tools away: it then has no choice
                # but to answer in words, which is what the user asked for.
                if unknown_attempts >= MAX_UNKNOWN_TOOL_ATTEMPTS and tools:
                    logger.info(
                        "dropping tools after %d invented tool call(s) -- forcing a "
                        "plain answer", unknown_attempts)
                    tools = None
                    messages.append({"role": "user", "content": [{
                        "type": "text",
                        "text": "You have no tools available. Answer the question directly, "
                                "in plain language, using what you already know.",
                    }]})

                self.set_state(JarvisState.THINKING)
            else:
                final_text = (
                    "I hit my tool-use limit for this request without reaching a final answer. "
                    "Here's where I got to -- ask me to continue if you'd like me to keep going."
                )
                await self._save_message(
                    conversation_id, "assistant", final_text, [{"type": "text", "text": final_text}]
                )

            # An empty answer reaches the user as silence, which is
            # indistinguishable from the app being broken. A small local model
            # produces one whenever it spends its whole turn on a tool call it
            # then cannot summarise.
            if not (final_text or "").strip():
                final_text = (
                    "I didn't manage to put an answer together for that. The local model "
                    "returned nothing usable. Try asking again, or more specifically."
                )
                logger.warning("empty completion for conversation %s -- substituted a "
                               "message so the user is not left with silence", conversation_id)
                await self._save_message(
                    conversation_id, "assistant", final_text,
                    [{"type": "text", "text": final_text}])
                yield {"type": "delta", "text": final_text}

            bus.publish(
                Events.MESSAGE,
                {"conversation_id": conversation_id, "role": "assistant", "text": final_text},
                source="ai",
            )
            await self._summarise_command(conversation_id, final_text)
            energy = None
            if meter is not None:
                try:
                    energy = await meter.stop()
                except Exception:
                    logger.debug("energy reading failed", exc_info=True)
                meter = None
            measured = telemetry.finish(energy)
            await self._record_telemetry(conversation_id, measured)
            # Afterwards, and never in the way of the answer: keep anything
            # durable the owner said about themselves, and fold whatever
            # scrolled out of the model's view into the running summary.
            self._learn_later(message, final_text)
            self._summarise_later(conversation_id)
            self.set_state(JarvisState.IDLE)
            yield {"type": "final", "text": final_text, "conversation_id": conversation_id,
                   "telemetry": measured}

        except asyncio.CancelledError:
            if meter is not None:
                await _quietly(meter.stop())
            self.set_state(JarvisState.IDLE)
            yield {"type": "error", "error": "Cancelled.", "conversation_id": conversation_id}
            raise
        except ProviderError as exc:
            if meter is not None:
                await _quietly(meter.stop())
            logger.error("provider failure: %s", exc)
            self.set_state(JarvisState.ERROR, str(exc))
            await self.activity.record(f"AI request failed: {exc}", level="error", source="ai")
            yield {"type": "error", "error": str(exc), "conversation_id": conversation_id}
            await asyncio.sleep(0)
            self.set_state(JarvisState.IDLE)
        except Exception as exc:
            logger.exception("orchestrator failure")
            self.set_state(JarvisState.ERROR, str(exc))
            await self.activity.record(f"Unexpected AI error: {exc}", level="error", source="ai")
            yield {"type": "error", "error": f"{type(exc).__name__}: {exc}", "conversation_id": conversation_id}
            self.set_state(JarvisState.IDLE)

    # ==================================================================
    # one-shot helper used by tasks, automations and the briefing
    # ==================================================================
    async def ask(
        self,
        prompt: str,
        system: str = "",
        purpose: str = "fast",
        max_tokens: Optional[int] = None,
        use_tools: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """Single-turn completion with no conversation persistence.

        `temperature` overrides the conversational setting for this one call.
        Writing a whole source file wants something close to deterministic;
        the 0.7 that suits chat produces code with creative punctuation.
        """
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        tools = self.registry.schemas() if use_tools else None
        completion = await self._complete_with_fallback(
            messages, system or "You are JARVIS, a precise assistant.", tools, purpose,
            max_tokens, tool_query=prompt, temperature=temperature,
        )
        await self._record_usage(completion, purpose)
        return completion.text

    async def run_agent(
        self,
        objective: str,
        system: str = "",
        purpose: str = "conversation",
        max_iterations: int = 10,
        task_id: Optional[str] = None,
        on_step=None,
        registry=None,
    ) -> Dict[str, Any]:
        """Autonomous tool-using loop for background tasks. Returns the final
        text plus the trace of tools used, so the task log tells the whole story.

        `registry` lets a caller supply a different tool surface for one run.
        Unattended work passes a guarded one: same tools, but every dispatch
        is checked first and anything it will not do alone comes back as a
        refusal the model can read and work around, rather than happening.
        Swapping `self.registry` instead would have leaked that restriction
        into every concurrent conversation.
        """
        registry = registry or self.registry
        messages = [{"role": "user", "content": [{"type": "text", "text": objective}]}]
        tools = registry.schemas()
        trace: List[Dict[str, Any]] = []
        system = system or await self._system_prompt(objective, "")

        for iteration in range(max_iterations):
            completion = await self._complete_with_fallback(
                messages, system, tools, purpose, tool_query=objective,
                system_compact=await self._compact_prompt())
            await self._record_usage(completion, purpose)

            if not completion.wants_tools:
                return {"text": completion.text, "trace": trace, "iterations": iteration + 1}

            messages.append({"role": "assistant", "content": completion.blocks})
            results = []
            for call in completion.tool_calls:
                if on_step:
                    await on_step(call["name"], call["input"])
                result = await registry.dispatch(call["name"], call["input"], task_id=task_id)
                trace.append(
                    {"tool": call["name"], "input": call["input"], "ok": result.ok,
                     "summary": truncate(result.message or result.to_model_string(), 300)}
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": truncate(result.to_model_string(), 12000),
                        "is_error": not result.ok,
                    }
                )
            messages.append({"role": "user", "content": results})

        return {
            "text": "Reached the step limit before finishing.",
            "trace": trace,
            "iterations": max_iterations,
            "incomplete": True,
        }

    # ==================================================================
    # provider plumbing: retry + fallback
    # ==================================================================
    async def _complete_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]],
        purpose: str,
        max_tokens: Optional[int] = None,
        tool_query: str = "",
        system_compact: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> Completion:
        last_error: Optional[Exception] = None
        failures: List[str] = []
        for provider_name in self._provider_order():
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            model = await self._model_for_provider(provider_name, purpose)
            provider_tools = self._tools_for_provider(provider_name, tools, tool_query)
            # Local models need a much smaller prompt to keep calling tools.
            provider_system = (
                system if self._is_cloud(provider_name) or not system_compact
                else system_compact
            )
            for attempt in range(MAX_RETRIES):
                try:
                    return await provider.complete(
                        messages, provider_system, provider_tools, model,
                        max_tokens or self.settings.MAX_TOKENS,
                        float(temperature if temperature is not None else
                              self.settings_store.get_cached("ai", {}).get("temperature", 0.7)),
                    )
                except RateLimited as exc:
                    last_error = exc
                    delay = min(exc.retry_after * (attempt + 1), 30)
                    logger.warning("%s rate limited, retrying in %.1fs", provider_name, delay)
                    await self.activity.record(
                        f"{provider_name} rate limited -- retrying in {delay:.0f}s", level="warn", source="ai"
                    )
                    await asyncio.sleep(delay)
                except ProviderError as exc:
                    last_error = exc
                    if not exc.retryable:
                        break
                    await asyncio.sleep(1.5 * (attempt + 1))
            logger.warning("provider %s exhausted: %s", provider_name, last_error)
            failures.append(f"{provider_name}: {last_error}")
            if len(self._provider_order()) > 1:
                await self.activity.record(
                    f"{provider_name} unavailable -- switching to fallback provider", level="warn", source="ai"
                )
        # Every provider's reason, the first (usually the one chosen) first:
        # a wrong API key must not be hidden behind "Ollama is not running".
        raise ProviderError(
            "No AI provider could handle the request. " + ("; ".join(failures) or str(last_error)),
            retryable=False,
        )

    async def _stream_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]],
        purpose: str,
        tool_query: str = "",
        system_compact: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        last_error: Optional[Exception] = None
        failures: List[str] = []
        for provider_name in self._provider_order():
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            model = await self._model_for_provider(provider_name, purpose)
            provider_tools = self._tools_for_provider(provider_name, tools, tool_query)
            # Local models need a much smaller prompt to keep calling tools.
            provider_system = (
                system if self._is_cloud(provider_name) or not system_compact
                else system_compact
            )
            try:
                produced = False
                async for event in provider.stream(
                    messages, provider_system, provider_tools, model, self.settings.MAX_TOKENS,
                    float(self.settings_store.get_cached("ai", {}).get("temperature", 0.7)),
                ):
                    produced = True
                    yield event
                if produced:
                    return
            except ProviderError as exc:
                last_error = exc
                failures.append(f"{provider_name}: {exc}")
                logger.warning("stream via %s failed: %s", provider_name, exc)
                await self.activity.record(
                    f"{provider_name} stream failed -- trying fallback", level="warn", source="ai"
                )
            except Exception as exc:
                last_error = exc
                logger.exception("stream via %s crashed", provider_name)
        raise ProviderError("No AI provider could answer. " + ("; ".join(failures) or str(last_error)),
                            retryable=False)

    # ==================================================================
    # context, usage, command history
    # ==================================================================
    async def _system_prompt(self, user_message: str, extra_context: str = "",
                             conversation_id: Optional[str] = None,
                             local_out: Optional[List[str]] = None) -> str:
        """The full system prompt -- and, into `local_out`, the context the
        compact prompt for local models must carry too.

        The compact prompt was built separately and given none of this: a
        local model answered without the owner's memories, without the
        summary of a long conversation, without their documents. Everything
        is gathered once here and handed to both.
        """
        personality = await self.settings_store.get("personality", {})
        memory_block = await self.memory.context_block(user_message)
        local: List[str] = [memory_block] if memory_block else []

        # The intelligence layer contributes two things: what the user is doing
        # right now, and how to pitch this particular answer.
        blocks = [extra_context] if extra_context else []

        # The owner's own documents, where they plainly bear on the message.
        documents = self.registry.context.get("documents") if self.registry else None
        if documents is not None and user_message:
            try:
                passages = await documents.context_for(user_message)
                if passages:
                    blocks.append(passages)
                    local.append(passages)
            except Exception:
                logger.debug("document context unavailable", exc_info=True)

        # Skills that fit this request, by name, so the model knows to read one.
        skills = self.registry.context.get("skills") if self.registry else None
        if skills is not None and "The user invoked the skill" not in (extra_context or ""):
            try:
                catalogue = skills.catalogue(user_message)
                if catalogue:
                    blocks.append(catalogue)
                    local.append(catalogue)
            except Exception:
                logger.debug("skill catalogue unavailable", exc_info=True)

        # What has scrolled out of this conversation, in brief.
        if conversation_id:
            try:
                earlier = await self._summaries_store().get(conversation_id)
                if earlier:
                    summary_block = ("Earlier in this conversation (older messages are no "
                                     "longer shown in full -- this is a summary of them):\n"
                                     + earlier)
                    blocks.append(summary_block)
                    local.append(summary_block)
            except Exception:
                logger.debug("conversation summary unavailable", exc_info=True)

        if self.awareness is not None:
            try:
                snapshot = self.awareness.snapshot()
                if snapshot.get("active_app"):
                    blocks.append(
                        f"On screen right now: {snapshot['active_app']} — "
                        f"\"{truncate(snapshot.get('window_title'), 120)}\" "
                        f"({snapshot.get('category')}), focused for "
                        f"{int((snapshot.get('focused_for_seconds') or 0) / 60)} minute(s)."
                    )
            except Exception:
                logger.debug("awareness context unavailable", exc_info=True)

        if self.reasoning is not None:
            try:
                directives = await self.reasoning.style_directives(conversation_id, user_message)
                if directives:
                    blocks.append(directives)
            except Exception:
                logger.debug("style directives unavailable", exc_info=True)

        # "take care of this" / "finish what I was doing yesterday" -- give the
        # model the candidate list rather than letting it invent a referent.
        if self.resolver is not None:
            try:
                referential = await self.resolver.context_block(user_message)
                if referential:
                    blocks.append(referential)
                    local.append(referential)
            except Exception:
                logger.debug("resolver unavailable", exc_info=True)

        if local_out is not None:
            # The caller's own context first: a skill being run, a task.
            local_out.extend(([extra_context] if extra_context else []) + local)
        return await build_system_prompt(
            settings=self.settings,
            personality=personality,
            memory_block=memory_block,
            registry=self.registry,
            db=self.db,
            settings_store=self.settings_store,
            extra_context="\n\n".join(b for b in blocks if b),
        )

    async def _record_usage(self, completion: Completion, purpose: str) -> None:
        tokens_in = int(completion.usage.get("input_tokens", 0))
        tokens_out = int(completion.usage.get("output_tokens", 0))
        cost = estimate_cost(completion.model, tokens_in, tokens_out) if completion.provider == "anthropic" else 0.0
        await self.db.execute(
            "INSERT INTO usage (ts, provider, model, tokens_in, tokens_out, cost, purpose, latency_ms, ok)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), completion.provider, completion.model, tokens_in, tokens_out, cost, purpose,
             completion.latency_ms, 1),
        )

    async def _plain_setting(self, message: str, conversation_id: Optional[str] = None) -> Optional[str]:
        """"Turn on the wake word", "switch to the Ember theme": done, and said.

        Goes through the tool runner like any other change, so it shows on
        screen, is checked against permissions and lands in the activity log.
        """
        from .tools.control_tools import plain_setting, plain_undo

        # "Undo that" / "change it back" straight after one of these.
        if self.registry is not None and plain_undo(message):
            result = await self.registry.dispatch("undo_setting", {}, conversation_id=conversation_id)
            return result.message or ("Done." if result.ok else "That couldn't be undone.")
        found = plain_setting(message)
        if found is None or self.registry is None:
            return None
        setting, value = found
        result = await self.registry.dispatch("change_setting", {"setting": setting, "value": value},
                                              conversation_id=conversation_id)
        return result.message or ("Done." if result.ok else "That setting couldn't be changed.")

    async def _live_lookup(self, question: str, conversation_id: Optional[str]):
        """Run live_lookup through the registry (permissions, audit, screening).

        Returns the tool_end event for the screen, and what the model should
        read -- empty when nothing was found or the look-up wasn't allowed.
        """
        started = time.perf_counter()
        result = await self.registry.dispatch("live_lookup", {"question": question},
                                              conversation_id=conversation_id)
        screen = result.meta.get("screen") or {}
        end = {
            "type": "tool_end", "tool": "live_lookup", "ok": result.ok,
            "summary": truncate(result.message or result.to_model_string(), 200),
            "flagged": bool(screen.get("flagged")), "flag_level": screen.get("level", ""),
            "redacted": sum((screen.get("redacted") or {}).values()),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "sources": result.meta.get("sources") or [],
        }
        return end, (result.to_model_string() if result.ok else "")

    async def _last_lookup(self, conversation_id: str) -> str:
        """The question the previous turn looked up, if it looked one up."""
        try:
            recent = await self.history(conversation_id, limit=6)
        except Exception:
            return ""
        for entry in reversed(recent):
            content = entry.get("content")
            for block in (content if isinstance(content, list) else []):
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "live_lookup"):
                    return str((block.get("input") or {}).get("question") or "")
        return ""

    async def _record_lookup(self, conversation_id: str, question: str, found: str,
                             messages: Optional[List[Dict[str, Any]]] = None) -> None:
        """Keep the look-up in the conversation as a tool call and its result,
        so the answer -- and the next question about it -- can use it."""
        call_id = new_id("lookup")
        blocks = [{"type": "tool_use", "id": call_id, "name": "live_lookup",
                   "input": {"question": question}}]
        results = [{"type": "tool_result", "tool_use_id": call_id,
                    "content": truncate(found, 12000), "is_error": False}]
        await self._save_message(conversation_id, "assistant", "", blocks)
        await self._save_message(conversation_id, "user", "", results)
        if messages is not None:
            messages += [{"role": "assistant", "content": blocks},
                         {"role": "user", "content": results}]

    async def _plain_research(self, message: str, conversation_id: Optional[str] = None) -> Optional[str]:
        """"Research X", "look into X": the 15-20 minute research, started at once.

        The model used to pick the quick lookup for these, which drifted off the
        question. Only "quick research ..." goes to the model now.
        """
        if self.registry is None:
            return None
        stop = stop_said(message)
        if stop is not None:
            result = await self.registry.dispatch("stop_task", {"which": stop},
                                                  conversation_id=conversation_id)
            return result.message or ("Stopped." if result.ok else "Nothing was stopped.")
        topic = research_topic_in(message)
        if topic is None:
            return None
        result = await self.registry.dispatch("research_topic", {"topic": topic},
                                              conversation_id=conversation_id)
        if not result.ok:
            return result.message or "The research couldn't be started."
        minutes = (result.data or {}).get("minutes", 17) if isinstance(result.data, dict) else 17
        return (f"Researching “{topic}” now. It takes about {minutes} minutes: I'll put the report "
                "here (what I found, and what's next) and send a notification. Say “stop the "
                "research” to stop it.")

    def _open_or_close(self, message: str) -> Optional[str]:
        """The reply to "open <part of JARVIS>" / "close that", having done it;
        None for anything else, which goes to the model as usual."""
        from .tools.ui_tools import PANELS, _screens, which_panel

        said = " ".join((message or "").lower().strip(" .!?").split())
        said = re.sub(r"^(?:jarvis,?\s+)?(?:please\s+|can you\s+|could you\s+)?", "", said)
        said = re.sub(r"\s+please$", "", said)
        hub = self.registry.context.get("hub") if self.registry else None
        nowhere = hub is not None and _screens(hub) == 0
        if re.fullmatch(r"(?:close|hide|dismiss)(?:\s+(?:it|that|this|the\s+panel|the\s+menu|panel))?", said):
            if nowhere:
                return None
            bus.publish("ui.open_panel", {"panel": None}, source="ui")
            return "Closed."
        m = re.fullmatch(r"(?:open|show(?:\s+me)?|go\s+to|bring\s+up|pull\s+up|take\s+me\s+to)\s+(.+)", said)
        if not m:
            return None
        key = which_panel(m.group(1), exact=True)
        if key is None:
            return None
        if nowhere:
            return (f"There's no JARVIS screen open to show {PANELS[key][0]} on. Open JARVIS in "
                    "a browser and ask again.")
        bus.publish("ui.open_panel", {"panel": key}, source="ui")
        return f"Opening {PANELS[key][0]}."

    def _skill_tools(self, message: str) -> str:
        """The tools named in the skill a "/name" message runs."""
        text = (message or "").strip()
        skills = self.registry.context.get("skills") if self.registry else None
        if not text.startswith("/") or skills is None:
            return ""
        skill = skills.get(text[1:].partition(" ")[0])
        if skill is None or not skill.enabled:
            return ""
        return " ".join(dict.fromkeys(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", skill.body)))

    def _invoke_skill(self, message: str, extra_context: str):
        text = (message or "").strip()
        if not text.startswith("/"):
            return message, extra_context
        name, _, rest = text[1:].partition(" ")
        skills = self.registry.context.get("skills") if self.registry else None
        skill = skills.get(name) if skills is not None and name else None
        if skill is None or not skill.enabled:
            return message, extra_context
        from ..skills.activity import skill_activity

        skill_activity.replying_with(skill)          # the screen wears its colour
        block = (f"The user invoked the skill '{skill.name}' ({skill.description}). "
                 f"Follow its instructions for this request:\n\n{skill.body}")
        request = rest.strip() or f"Run the {skill.name} skill."
        context = f"{extra_context}\n\n{block}" if extra_context else block
        return request, context

    def _summaries_store(self):
        if self._summaries is None:
            from .compression import ConversationSummaries

            self._summaries = ConversationSummaries(self.db, self)
        return self._summaries

    def _summarise_later(self, conversation_id: str) -> None:
        oldest_kept = self._fell_off.pop(conversation_id, None)
        if not oldest_kept:
            return
        try:
            task = asyncio.create_task(
                self._summaries_store().refresh(conversation_id, oldest_kept))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        except Exception:
            logger.debug("could not schedule a summary", exc_info=True)

    def _learn_later(self, user_text: str, reply_text: str) -> None:
        if self.memory is None:
            return
        try:
            from .learning import FactLearner

            if getattr(self, "_learner", None) is None:
                self._learner = FactLearner(self.memory, self, self.settings_store)
            task = asyncio.create_task(self._learner.learn(user_text, reply_text))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        except Exception:
            logger.debug("could not schedule learning", exc_info=True)

    async def _record_telemetry(self, conversation_id: str, measured: Dict[str, Any]) -> None:
        try:
            await self.db.execute(
                "INSERT INTO reply_telemetry (ts, conversation_id, provider, model, calls,"
                " input_tokens, output_tokens, tokens_per_sec, ttft_ms, total_ms, load_ms,"
                " energy_j, avg_power_w, energy_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), conversation_id, measured["provider"], measured["model"],
                 measured["calls"], measured["input_tokens"], measured["output_tokens"],
                 measured["tokens_per_sec"], measured["ttft_ms"], measured["total_ms"],
                 measured["load_ms"], measured["energy_j"], measured["avg_power_w"],
                 measured["energy_method"]),
            )
        except Exception:
            logger.debug("could not store reply telemetry", exc_info=True)

    async def efficiency_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Totals for the efficiency readout: tokens, energy, time, cost avoided."""
        from ..core.util import since
        from .telemetry import REFERENCE_LABEL, REFERENCE_PRICE

        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS replies, SUM(input_tokens) AS tokens_in,"
            " SUM(output_tokens) AS tokens_out, SUM(energy_j) AS energy_j,"
            " SUM(CASE WHEN energy_j IS NOT NULL THEN output_tokens ELSE 0 END) AS metered_out,"
            " AVG(tokens_per_sec) AS speed, AVG(ttft_ms) AS ttft, AVG(total_ms) AS total"
            " FROM reply_telemetry WHERE ts > ?", (since(hours * 3600),)) or {}
        tokens_in = int(row.get("tokens_in") or 0)
        tokens_out = int(row.get("tokens_out") or 0)
        energy = float(row.get("energy_j") or 0.0)
        metered = int(row.get("metered_out") or 0)
        models = await self.db.fetch_all(
            "SELECT model, provider, COUNT(*) AS replies, SUM(output_tokens) AS tokens_out,"
            " AVG(tokens_per_sec) AS speed, SUM(energy_j) AS energy_j"
            " FROM reply_telemetry WHERE ts > ? GROUP BY model, provider"
            " ORDER BY replies DESC", (since(hours * 3600),))
        return {
            "hours": hours,
            "replies": int(row.get("replies") or 0),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "energy_j": round(energy, 1),
            "energy_wh": round(energy / 3600.0, 3),
            "tokens_per_joule": round(metered / energy, 3) if energy > 0 else None,
            "avg_tokens_per_sec": round(row["speed"], 1) if row.get("speed") else None,
            "avg_ttft_ms": int(row["ttft"]) if row.get("ttft") else None,
            "avg_total_ms": int(row["total"]) if row.get("total") else None,
            "cloud_cost_avoided_usd": round(
                tokens_in / 1e6 * REFERENCE_PRICE["in"]
                + tokens_out / 1e6 * REFERENCE_PRICE["out"], 4),
            "cloud_reference": REFERENCE_LABEL,
            "models": models,
        }

    async def _record_command(self, text: str, source: str, conversation_id: str) -> None:
        await self.db.execute(
            "INSERT INTO commands (ts, text, source, conversation_id) VALUES (?,?,?,?)",
            (now_iso(), truncate(text, 500), source, conversation_id),
        )

    async def _summarise_command(self, conversation_id: str, result: str) -> None:
        await self.db.execute(
            "UPDATE commands SET result_summary = ? WHERE id = (SELECT MAX(id) FROM commands WHERE conversation_id = ?)",
            (truncate(result, 200), conversation_id),
        )

    async def usage_summary(self, hours: int = 24) -> Dict[str, Any]:
        from ..core.util import since

        rows = await self.db.fetch_all(
            "SELECT provider, model, COUNT(*) AS requests, SUM(tokens_in) AS tokens_in,"
            " SUM(tokens_out) AS tokens_out, SUM(cost) AS cost, AVG(latency_ms) AS avg_latency"
            " FROM usage WHERE ts > ? GROUP BY provider, model",
            (since(hours * 3600),),
        )
        total_cost = sum(float(r["cost"] or 0) for r in rows)
        return {
            "window_hours": hours,
            "by_model": rows,
            "requests": sum(int(r["requests"] or 0) for r in rows),
            "tokens_in": sum(int(r["tokens_in"] or 0) for r in rows),
            "tokens_out": sum(int(r["tokens_out"] or 0) for r in rows),
            "estimated_cost_usd": round(total_cost, 4),
        }

    async def close(self) -> None:
        for provider in self.providers.values():
            await provider.close()


# ---------------------------------------------------------------------------
def _repair_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A truncated window can start mid tool-round, which providers reject.

    Drop any leading tool_result that has no matching tool_use, and any
    trailing tool_use whose results were cut off."""
    while messages:
        content = messages[0].get("content") or []
        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            messages.pop(0)
            continue
        break

    while messages:
        content = messages[-1].get("content") or []
        if isinstance(content, list) and any(b.get("type") == "tool_use" for b in content):
            messages.pop()
            continue
        break

    return [m for m in messages if m.get("content")]
