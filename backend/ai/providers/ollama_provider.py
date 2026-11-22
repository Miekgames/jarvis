"""
Ollama provider -- the local, offline-capable fallback.

Speaks the OpenAI-ish /api/chat shape, so message translation is needed in
both directions. Tool calling works on models that support it (llama3.1,
qwen2.5, mistral-nemo, ...); on models that don't, tool calls simply never
appear and the orchestrator handles the plain-text turn normally.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from .base import Completion, Provider, ProviderError, normalise_content

# Keep the model resident between requests. "-1" means indefinitely; a
# duration string such as "30m" also works. See the note in __init__ for the
# measurements behind this.
DEFAULT_KEEP_ALIVE = "-1"


def _keep_alive(value):
    """Ollama's keep_alive, in the one shape it actually accepts.

    It takes either a number of seconds or a Go duration string, and the two
    are not interchangeable: the string "-1" is parsed as a duration and
    rejected outright --

        HTTP 400 {"error":"time: missing unit in duration \"-1\""}

    -- while the integer -1 means "hold it indefinitely". Since the value
    arrives from configuration as text, something has to make that
    distinction, and getting it wrong does not degrade performance, it fails
    every single request.
    """
    text = str(value).strip()
    if not text:
        return -1
    try:
        return int(text)
    except ValueError:
        return text          # a duration like "30m" or "24h"

logger = logging.getLogger("jarvis.ai.ollama")


# Preferred local models, best first.
#
# Ordered by observed tool-calling reliability, not size. Measured on this
# machine against the real JARVIS tool set: llama3.2 (3.2B) and qwen3 (2.0B)
# emitted correct tool calls on every query, while mistral (7.2B) narrated
# "I will call system_status" as prose every time and then invented the
# numbers. For a tool-driven assistant a smaller reliable caller beats a
# larger one that hallucinates, so family rank outranks parameter count.
_PREFERRED = ("llama3.3", "llama3.2", "llama3.1", "qwen3", "qwen2.5", "mistral-nemo",
              "gemma3", "phi4", "mistral", "phi3", "deepseek-r1")

# What each purpose actually needs from a local model. Ollama reports
# capabilities per model, so routing is derived rather than guessed -- this
# works on any machine without knowing which models are installed.
# 'thinking' models are avoided rather than preferred. Under a token budget
# they routinely spend the whole allowance inside <think> and return an empty
# message, and they narrate tool use ("I'm checking that now") instead of
# emitting a tool call. For an assistant that needs structured output and
# working tools, a smaller non-thinking model is strictly better.
_PURPOSE_REQUIREMENTS: Dict[str, Dict[str, Any]] = {
    "vision":       {"require": ["vision"], "prefer_large": True},
    "reasoning":    {"require": ["tools"], "avoid": ["thinking"], "prefer_large": True},
    "coding":       {"require": ["tools"], "avoid": ["thinking"], "prefer_large": True},
    "conversation": {"require": ["tools"], "avoid": ["thinking"], "prefer_large": True},
    "primary":      {"require": ["tools"], "avoid": ["thinking"], "prefer_large": True},
    "fast":         {"require": ["tools"], "avoid": ["thinking"], "prefer_large": False},
}


def _parameter_billions(text: str) -> float:
    """'8.2B' -> 8.2, '1.7B' -> 1.7. Unknown sizes sort last."""
    try:
        return float(str(text).upper().rstrip("B").strip())
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------
# Inline tool calls
# ----------------------------------------------------------------------
# Smaller local models frequently ignore the structured tool-call channel and
# emit the call as JSON in the message content instead. Left alone that JSON is
# rendered to the user as the assistant's answer, which is both useless and
# looks broken. Recognise the common shapes and promote them to real calls.
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("parameters", "arguments", "input", "args")


def _balanced_object(text: str, start: int) -> Optional[str]:
    """The complete JSON object beginning at `start`, or None.

    Brace counting has to respect strings and escapes, or a brace inside a
    value ends the object early and the parse fails on text that was in fact
    a perfectly good tool call.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _candidate_json(text: str) -> List[Dict[str, Any]]:
    """JSON objects worth inspecting, with whether each one opens the message.

    `exclusive` means "this is an attempted call, not quoted material". An
    object that *starts* the message qualifies even when prose follows it:
    models routinely emit the call and then keep talking ("...} whats next"),
    and an answer to a user almost never opens with a raw JSON object. The
    same object quoted inside prose does not qualify and is left alone.
    """
    body = text or ""
    stripped = body.strip()
    found: List[Dict[str, Any]] = []

    if stripped.startswith("{"):
        offset = body.index("{")
        blob = _balanced_object(body, offset)
        if blob:
            trailing = body[offset + len(blob):].strip()
            found.append({"blob": blob, "exclusive": True, "trailing": trailing})

    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.DOTALL):
        remainder = body.replace(match.group(0), "").strip()
        found.append({"blob": match.group(1), "exclusive": not remainder,
                      "trailing": remainder})
    return found


def parse_inline_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """A tool call written into the message content, if there is one.

    Returns the name, the arguments, and whether the call was the entire
    message. Does not judge whether the tool exists -- that is the caller's
    decision, because an unknown name is still a call attempt and is better
    repaired than shown to the user as an answer.
    """
    for candidate in _candidate_json(text):
        try:
            parsed = json.loads(candidate["blob"])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue

        name = next((parsed[key] for key in _NAME_KEYS
                     if isinstance(parsed.get(key), str) and parsed[key]), None)
        if not name:
            continue

        # Find the argument key that is actually present. Choosing a shape on
        # the strength of its name key alone silently drops the arguments and
        # dispatches the tool with nothing.
        present = next((key for key in _ARG_KEYS if key in parsed), None)
        if present is None:
            # A bare {"name": ...} is far more likely to be data than a call.
            continue

        arguments = parsed[present]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                continue
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            continue
        return {"name": name, "input": arguments, "exclusive": candidate["exclusive"]}
    return None


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> Optional[Set[str]]:
    """Names of the tools actually offered on this call."""
    if not tools:
        return None
    names = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = (function or {}).get("name") if function else (
            tool.get("name") if isinstance(tool, dict) else None)
        if name:
            names.add(name)
    return names or None


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, host: str = "http://127.0.0.1:11434", default_model: str = "",
                 routing: Optional[Dict[str, str]] = None, num_ctx: int = 0,
                 keep_alive: str = ""):
        self.host = host.rstrip("/")
        # How much conversation the model is allowed to see, in tokens.
        #
        # Ollama does not use the model's full context unless asked: left
        # alone it applies its own modest default and silently truncates
        # anything past it. qwen2.5 advertises 32768 and was being given
        # roughly an eighth of that, which is why JARVIS lost the thread of a
        # conversation no matter how much history was sent -- the history was
        # arriving and being thrown away at the other end.
        #
        # It is not free: the key/value cache grows linearly with this, so it
        # is a setting rather than simply "as much as the model allows".
        self.num_ctx = int(num_ctx or 0)

        # How long Ollama keeps the model in memory between requests.
        #
        # This is the single biggest thing affecting how fast JARVIS feels,
        # and it has nothing to do with the model's speed. Measured on this
        # machine: a request arriving while the model is resident takes
        # **0.30s** end to end. The same request after an idle gap takes
        # **12.18s**, of which 11.98s is `load_duration` -- reading 4.4 GB
        # off disk again. Generation itself was 0.06s in both cases.
        #
        # Ollama's default is five minutes, so any ordinary pause in a
        # conversation puts the next reply on the wrong side of that cliff.
        # Holding the model costs VRAM and nothing else; on a 12 GB card
        # with a 4.4 GB model that is a trade worth making by default, and
        # JARVIS_OLLAMA_KEEP_ALIVE exists for when it is not.
        self.keep_alive = _keep_alive(keep_alive or DEFAULT_KEEP_ALIVE)
        self._configured_model = default_model
        self.default_model = default_model or "llama3.2"
        self._resolved = bool(default_model)
        self._client = None
        # Explicit per-purpose overrides from configuration; anything not named
        # here is derived from reported capabilities.
        self._routing_overrides = {k: v for k, v in (routing or {}).items() if v}
        self._capabilities: Dict[str, Dict[str, Any]] = {}
        self._purpose_cache: Dict[str, str] = {}

    def _options(self, temperature: float, max_tokens: int) -> Dict[str, Any]:
        """Generation options, including how much context to actually use."""
        options: Dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens}
        if self.num_ctx > 0:
            options["num_ctx"] = self.num_ctx
        return options

    async def warm(self, model: str = "") -> Dict[str, Any]:
        """Load the model now, so the first real request does not pay for it.

        Costs nothing but a few seconds of startup in the background, and
        saves twelve seconds off whatever the user asks first -- which is
        the reply most likely to form their impression of how fast this is.
        """
        import time

        target = model or self.default_model
        started = time.monotonic()
        try:
            # An empty prompt with no tokens requested loads the weights and
            # generates nothing.
            response = await self._http().post("/api/chat", json={
                "model": target,
                "messages": [{"role": "user", "content": "."}],
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": {"num_predict": 1, "temperature": 0.0},
            })
            response.raise_for_status()
        except Exception as exc:
            return {"ok": False, "model": target,
                    "detail": f"Could not warm {target}: {str(exc)[:160]}"}

        seconds = time.monotonic() - started
        return {"ok": True, "model": target, "seconds": round(seconds, 1),
                "detail": f"{target} loaded in {seconds:.1f}s and will stay resident."}

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(base_url=self.host, timeout=300.0)
        return self._client

    # ------------------------------------------------------------------
    async def available(self) -> bool:
        try:
            response = await self._http().get("/api/tags", timeout=2.0)
            if response.status_code != 200:
                return False
            await self._resolve_model()
            return True
        except Exception:
            return False

    async def models(self) -> List[str]:
        try:
            response = await self._http().get("/api/tags", timeout=5.0)
            response.raise_for_status()
            return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            return []

    async def _resolve_model(self) -> str:
        """Pick a model that is actually pulled.

        Hardcoding a default guarantees a confusing "model not found" the first
        time the fallback is needed, so the installed list decides instead."""
        if self._resolved:
            return self.default_model
        installed = await self.models()
        if installed:
            chosen = next(
                (name for prefix in _PREFERRED for name in installed if name.startswith(prefix)),
                installed[0],
            )
            self.default_model = chosen
            logger.info("local model resolved to '%s'", chosen)
        self._resolved = True
        return self.default_model

    # ------------------------------------------------------------------
    # capability-aware routing
    # ------------------------------------------------------------------
    async def capabilities(self, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Ask Ollama what each installed model can actually do.

        `/api/show` reports capabilities like 'tools', 'vision' and 'thinking',
        so JARVIS never has to guess whether a local model can see an image or
        drive a tool call."""
        if self._capabilities and not refresh:
            return self._capabilities

        installed = await self.models()
        found: Dict[str, Dict[str, Any]] = {}
        for name in installed:
            try:
                response = await self._http().post("/api/show", json={"model": name}, timeout=20.0)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.debug("could not inspect '%s': %s", name, exc)
                found[name] = {"capabilities": [], "parameters": 0.0, "family": ""}
                continue
            details = data.get("details") or {}
            found[name] = {
                "capabilities": list(data.get("capabilities") or []),
                "parameters": _parameter_billions(details.get("parameter_size", "")),
                "family": details.get("family", ""),
            }
        self._capabilities = found
        self._purpose_cache.clear()
        logger.info(
            "local models inspected: %s",
            ", ".join(f"{n}({'/'.join(v['capabilities']) or 'basic'})" for n, v in found.items()),
        )
        return found

    async def resolve_for(self, purpose: str) -> Optional[str]:
        """Best installed model for a purpose, or None if nothing qualifies.

        Returning None matters: for vision it lets the caller say 'no local
        model can see images, pull one' instead of sending an image to a model
        that will silently ignore it."""
        if purpose in self._routing_overrides:
            return self._routing_overrides[purpose]
        if purpose in self._purpose_cache:
            return self._purpose_cache[purpose]

        requirements = _PURPOSE_REQUIREMENTS.get(purpose, _PURPOSE_REQUIREMENTS["conversation"])
        catalogue = await self.capabilities()
        if not catalogue:
            return await self._resolve_model()

        required = set(requirements.get("require", []))
        bonus = set(requirements.get("bonus", []))
        avoid = set(requirements.get("avoid", []))
        prefer_large = requirements.get("prefer_large", True)

        eligible = [
            (name, info) for name, info in catalogue.items()
            if required.issubset(set(info["capabilities"]))
        ]
        if not eligible:
            # 'tools' is a nice-to-have; a model without it can still hold a
            # conversation. 'vision' is not substitutable, so stay strict.
            if "vision" in required:
                self._purpose_cache[purpose] = ""
                return None
            eligible = list(catalogue.items())
        if not eligible:
            return await self._resolve_model()

        def score(entry) -> tuple:
            name, info = entry
            capabilities = set(info["capabilities"])
            # Penalty first: a model we want to avoid loses regardless of size.
            clean = 0 if avoid & capabilities else 1
            has_bonus = 1 if bonus & capabilities else 0
            size = info["parameters"]
            preference = next(
                (len(_PREFERRED) - i for i, prefix in enumerate(_PREFERRED)
                 if name.startswith(prefix)), 0,
            )
            # Family rank before size: reliability at emitting tool calls is
            # what matters, and it does not track parameter count.
            if "tools" in required:
                return (clean, has_bonus, preference, size if prefer_large else -size)
            return (clean, has_bonus, size if prefer_large else -size, preference)

        chosen = max(eligible, key=score)[0]
        self._purpose_cache[purpose] = chosen
        logger.debug("local routing: %s -> %s", purpose, chosen)
        return chosen

    async def vision_model(self) -> Optional[str]:
        return await self.resolve_for("vision")

    async def routing_table(self) -> Dict[str, Optional[str]]:
        return {
            purpose: await self.resolve_for(purpose)
            for purpose in ("conversation", "fast", "reasoning", "coding", "vision")
        }

    # ------------------------------------------------------------------
    def _prepare(self, messages: List[Dict[str, Any]], system: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for message in messages:
            role = message.get("role", "user")
            blocks = normalise_content(message.get("content"))

            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            images: List[str] = []
            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    text_parts.append(block.get("text", ""))
                elif kind == "image":
                    # Anthropic nests base64 under source; Ollama wants a bare
                    # list of base64 strings on the message itself.
                    source = block.get("source") or {}
                    data = source.get("data") if isinstance(source, dict) else None
                    if data:
                        images.append(data)
                elif kind == "tool_use":
                    tool_calls.append(
                        {"function": {"name": block.get("name", ""), "arguments": block.get("input", {})}}
                    )
                elif kind == "tool_result":
                    # Ollama models expect tool output as its own message.
                    out.append(
                        {
                            "role": "tool",
                            "content": str(block.get("content", ""))[:8000],
                        }
                    )

            if text_parts or tool_calls or images:
                entry: Dict[str, Any] = {"role": role, "content": "\n".join(p for p in text_parts if p)}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                if images:
                    entry["images"] = images
                out.append(entry)
        return out

    @staticmethod
    def _translate_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for tool in tools
        ]

    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> Completion:
        model = model or await self._resolve_model()
        prepared = self._prepare(messages, system)
        # Vision models generally aren't tool-callers; sending tools alongside
        # an image is the fastest way to get a confusing 400 back.
        if tools and any(m.get("images") for m in prepared):
            tools = None
        payload: Dict[str, Any] = {
            "keep_alive": self.keep_alive,
            "model": model,
            "messages": prepared,
            "stream": False,
            "options": self._options(temperature, max_tokens),
        }
        translated = self._translate_tools(tools)
        if translated:
            payload["tools"] = translated

        started = time.monotonic()
        try:
            response = await self._http().post("/api/chat", json=payload)
            if response.status_code == 404:
                raise ProviderError(
                    f"Ollama model '{model}' is not pulled. Run: ollama pull {model}", retryable=False
                )
            response.raise_for_status()
            data = response.json()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Ollama unreachable at {self.host}: {exc}") from exc

        return self._to_completion(data, model, int((time.monotonic() - started) * 1000),
                                   _tool_names(tools))

    # ------------------------------------------------------------------
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[Dict[str, Any]]:
        model = model or await self._resolve_model()
        payload: Dict[str, Any] = {
            "keep_alive": self.keep_alive,
            "model": model,
            "messages": self._prepare(messages, system),
            "stream": True,
            "options": self._options(temperature, max_tokens),
        }
        translated = self._translate_tools(tools)
        if translated:
            payload["tools"] = translated

        started = time.monotonic()
        collected = ""
        # Tool calls arrive on their own chunk, part-way through the stream,
        # and the final `done` chunk carries an empty message. Keeping only
        # the last chunk therefore threw every tool call away: the model asked
        # to run `system_status`, nothing ran it, and the turn ended with no
        # text either -- which reached the user as "it just doesn't answer".
        # They have to be gathered as they go.
        gathered_calls: List[Dict[str, Any]] = []
        final: Dict[str, Any] = {}
        try:
            async with self._http().stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = chunk.get("message") or {}
                    piece = message.get("content", "")
                    if piece:
                        collected += piece
                        yield {"type": "text", "text": piece}
                    calls = message.get("tool_calls")
                    if calls:
                        gathered_calls.extend(calls)
                    if chunk.get("done"):
                        final = chunk
        except Exception as exc:
            raise ProviderError(f"Ollama stream failed: {exc}") from exc

        if not final:
            final = {"message": {}}
        message = final.setdefault("message", {})
        if not message.get("content"):
            message["content"] = collected
        if gathered_calls and not message.get("tool_calls"):
            message["tool_calls"] = gathered_calls

        yield {
            "type": "done",
            "completion": self._to_completion(final, model,
                                              int((time.monotonic() - started) * 1000),
                                              _tool_names(tools)),
        }

    # ------------------------------------------------------------------
    def _to_completion(self, data: Dict[str, Any], model: str, latency_ms: int,
                       tool_names: Optional[Set[str]] = None) -> Completion:
        message = data.get("message") or {}
        text = (message.get("content") or "").strip()
        blocks: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []

        # Reasoning models can exhaust the token budget inside <think> and
        # return empty content. Falling back to the reasoning text beats
        # handing the caller a blank string with no explanation.
        if not text:
            thinking = (message.get("thinking") or "").strip()
            if thinking:
                logger.warning(
                    "%s produced only reasoning (%s chars) and no answer -- using the reasoning "
                    "text. Consider a non-thinking model, or raise JARVIS_MAX_TOKENS.",
                    model, len(thinking),
                )
                text = thinking

        if text:
            blocks.append({"type": "text", "text": text})

        # A local model that writes its tool call into the content instead of
        # the structured channel would otherwise have that JSON shown to the
        # user as the answer.
        # Only when tools were actually offered. With no tools on the call,
        # a JSON object is data or a hallucination, never a call worth
        # promoting -- and blanking the text would throw the answer away.
        if text and tool_names and not message.get("tool_calls"):
            inline = parse_inline_tool_call(text)
            known = inline and (tool_names is None or inline["name"] in tool_names)

            # An unknown name is still a call attempt when the JSON is the
            # entire message. Promoting it lets the dispatcher answer "no such
            # tool", which the model can then correct -- far better than
            # printing the attempt to the user and giving up. Quoted inside
            # prose it is part of an answer, so it is left alone.
            if inline and (known or inline["exclusive"]):
                logger.info(
                    "%s emitted an inline tool call for '%s'%s -- promoting it",
                    model, inline["name"], "" if known else " (not an available tool)")
                call_id = "ollama_inline_0"
                blocks = [{"type": "tool_use", "id": call_id,
                           "name": inline["name"], "input": inline["input"]}]
                tool_calls.append({"id": call_id, "name": inline["name"],
                                   "input": inline["input"]})
                text = ""

        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            call_id = call.get("id") or f"ollama_call_{index}"
            blocks.append({"type": "tool_use", "id": call_id, "name": function.get("name", ""), "input": arguments or {}})
            tool_calls.append({"id": call_id, "name": function.get("name", ""), "input": arguments or {}})

        return Completion(
            text=text,
            blocks=blocks,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=model,
            usage={
                "input_tokens": int(data.get("prompt_eval_count") or 0),
                "output_tokens": int(data.get("eval_count") or 0),
                # Ollama's own timings, in nanoseconds on the wire. Generation
                # speed taken from these is the model's, not the network's.
                "eval_ms": int((data.get("eval_duration") or 0) / 1_000_000),
                "prompt_ms": int((data.get("prompt_eval_duration") or 0) / 1_000_000),
                "load_ms": int((data.get("load_duration") or 0) / 1_000_000),
            },
            latency_ms=latency_ms,
            provider=self.name,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
