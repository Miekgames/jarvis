"""Anthropic provider (Claude), through the official Anthropic SDK.

Current Claude models differ from older ones in ways that turn a request
written for the old ones into a 400:

  * **No sampling parameters.** `temperature`, `top_p` and `top_k` are
    rejected, so none is ever sent -- the model's own default is used for
    every Claude model, old or new.
  * **Thinking is on.** On the current models thinking is adaptive and cannot
    be switched off. Its blocks come back in the reply and must be passed back
    unchanged while a tool loop is running.
  * **Thinking is tied to the conversation that produced it.** A thinking
    block's signature records the system prompt, the tools and every message
    before it. JARVIS rebuilds its system prompt each turn (the time, the
    owner's memories), so a block from an earlier turn would fail that check
    with a 400. Blocks from earlier turns are therefore removed (removing all
    of them is allowed), the current turn's are kept, and as a safety net the
    API is asked to drop -- not reject -- any block that still does not match.
  * **Refusals.** A reply can stop with `stop_reason == "refusal"`. For the
    models that support it, the server-side fallback is switched on, so a
    declined request is answered by another Claude model instead of failing.

Everything else -- tools, streaming -- is the ordinary Messages API.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .base import Completion, Provider, ProviderError, RateLimited, normalise_content

logger = logging.getLogger("jarvis.ai.anthropic")

DEFAULT_MODEL = "claude-opus-5-5"

# Rough USD per million tokens, used only for the local cost estimate shown in
# the observability panel. Longest prefix wins; unknown models use Opus 5.5.
PRICING: Dict[str, Dict[str, float]] = {
    "claude-fable-5-2": {"in": 8.00, "out": 40.00},
    "claude-fable-5": {"in": 10.00, "out": 50.00},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00},
    "claude-opus-5": {"in": 5.00, "out": 25.00},
    "claude-opus-4": {"in": 5.00, "out": 25.00},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00},
    "claude-sonnet-5": {"in": 2.00, "out": 10.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}
_DEFAULT_PRICE = PRICING["claude-opus-5-5"]

# Models that take adaptive thinking (and the block-binding safety net).
# Older models -- Haiku 4.5, Sonnet 4.5 and before -- get a plain request.
_ADAPTIVE = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5",
             "claude-haiku-5-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
             "claude-sonnet-4-6")
# Models whose refusals the server can hand to another model.
_FALLBACK = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
             "claude-sonnet-5-5")
# Thinking counts against max_tokens; below this a long answer is cut short.
_THINKING_FLOOR = 16000

BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_BINDING = "thinking-binding-controls-2026-08-01"


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price = _DEFAULT_PRICE
    best = ""
    for prefix, table in PRICING.items():
        if model.startswith(prefix) and len(prefix) > len(best):
            price, best = table, prefix
    return (tokens_in / 1_000_000) * price["in"] + (tokens_out / 1_000_000) * price["out"]


def is_adaptive(model: str) -> bool:
    return model.startswith(_ADAPTIVE)


def uses_fallback(model: str) -> bool:
    # Whole ids only: "claude-opus-5" must not match a model it isn't.
    return model in _FALLBACK


def _thinking_kinds() -> Tuple[str, str]:
    return ("thinking", "redacted_thinking")


def prepare_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The history as the API should see it.

    Thinking blocks are kept only for the current turn -- everything after the
    last message the owner typed. Earlier turns lose theirs, because the
    system prompt they were bound to has since been rebuilt.
    """
    prepared: List[Dict[str, Any]] = []
    for message in messages:
        content = normalise_content(message.get("content"))
        if content:
            prepared.append({"role": message.get("role", "user"), "content": content})

    current_turn = 0
    for index, message in enumerate(prepared):
        if message["role"] == "user" and any(
                block.get("type") in ("text", "image") for block in message["content"]
                if isinstance(block, dict)):
            current_turn = index

    out: List[Dict[str, Any]] = []
    for index, message in enumerate(prepared):
        content = message["content"]
        if index < current_turn and message["role"] == "assistant":
            content = [block for block in content
                       if not (isinstance(block, dict) and block.get("type") in _thinking_kinds())]
            if not content:
                continue
        out.append({"role": message["role"], "content": content})
    return out


def request_for(model: str, messages: List[Dict[str, Any]], system: str,
                tools: Optional[List[Dict[str, Any]]], max_tokens: int) -> Tuple[Dict[str, Any], List[str]]:
    """The keyword arguments for one Messages API call, and the betas it needs."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": prepare_messages(messages),
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    betas: List[str] = []
    if is_adaptive(model):
        kwargs["max_tokens"] = max(int(max_tokens or 0), _THINKING_FLOOR)
        # Drop, rather than reject, a replayed block whose conversation no
        # longer matches -- a degraded answer instead of a failed one.
        kwargs["thinking"] = {"type": "adaptive",
                              "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        betas.append(BETA_BINDING)
    if uses_fallback(model):
        betas.append(BETA_FALLBACK)
        kwargs["extra_body"] = {"fallbacks": "default"}
    return kwargs, betas


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, default_model: str = DEFAULT_MODEL, name: str = "anthropic",
                 client: Any = None):
        self.api_key = api_key
        self.default_model = default_model or DEFAULT_MODEL
        self.name = name
        self._client = client
        self._sdk_error = None
        if client is None and api_key:
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
            except ImportError as exc:  # pragma: no cover - dependency guard
                self._sdk_error = f"anthropic SDK not installed: {exc}"
                logger.warning(self._sdk_error)

    # ------------------------------------------------------------------
    async def available(self) -> bool:
        return self._client is not None

    def _require(self):
        if self._client is None:
            raise ProviderError(
                self._sdk_error or "No Anthropic API key is set", retryable=False
            )
        return self._client

    def _translate_error(self, exc: Exception) -> ProviderError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        # The API's own sentence ("invalid x-api-key"), not the SDK's wrapper.
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("message"):
            exc = Exception(str(error["message"]))
        if name in ("RateLimitError",) or status == 429:
            retry_after = 5.0
            headers = getattr(getattr(exc, "response", None), "headers", None) or {}
            try:
                retry_after = float(headers.get("retry-after", 5.0))
            except (TypeError, ValueError):
                pass
            return RateLimited(f"Anthropic rate limit: {exc}", retry_after=retry_after)
        if name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
            return ProviderError(f"Anthropic rejected the API key: {exc}", retryable=False, status=status)
        if name == "NotFoundError" or status == 404:
            return ProviderError(f"Anthropic does not know that model: {exc}", retryable=False,
                                 status=status)
        if name in ("BadRequestError", "UnprocessableEntityError") or status == 400:
            return ProviderError(f"Anthropic rejected the request: {exc}", retryable=False, status=status)
        return ProviderError(f"Anthropic error: {exc}", retryable=True, status=status)

    def _messages_api(self, betas: List[str]):
        client = self._require()
        return client.beta.messages if betas else client.messages

    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,        # accepted for the interface; never sent
    ) -> Completion:
        model = model or self.default_model
        kwargs, betas = request_for(model, messages, system, tools, max_tokens)
        if betas:
            kwargs["betas"] = betas
        started = time.monotonic()
        try:
            response = await self._messages_api(betas).create(**kwargs)
        except Exception as exc:
            raise self._translate_error(exc) from exc
        return self._to_completion(response, model, int((time.monotonic() - started) * 1000))

    # ------------------------------------------------------------------
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,        # accepted for the interface; never sent
    ) -> AsyncIterator[Dict[str, Any]]:
        model = model or self.default_model
        kwargs, betas = request_for(model, messages, system, tools, max_tokens)
        if betas:
            kwargs["betas"] = betas
        started = time.monotonic()
        try:
            async with self._messages_api(betas).stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield {"type": "text", "text": text}
                final = await stream.get_final_message()
        except Exception as exc:
            raise self._translate_error(exc) from exc

        yield {
            "type": "done",
            "completion": self._to_completion(final, model, int((time.monotonic() - started) * 1000)),
        }

    # ------------------------------------------------------------------
    def _to_completion(self, response: Any, model: str, latency_ms: int) -> Completion:
        blocks: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []

        for block in getattr(response, "content", None) or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                blocks.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
            elif kind == "tool_use":
                payload = {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                blocks.append(payload)
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
            elif kind == "thinking":
                # Kept verbatim: the API needs it back while this turn's tool
                # loop runs. Its text is usually empty (display "omitted").
                blocks.append({"type": "thinking", "thinking": getattr(block, "thinking", "") or "",
                               "signature": getattr(block, "signature", "") or ""})
            elif kind == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "data": getattr(block, "data", "")})
            # Anything else -- a fallback marker, a server-side block -- is
            # information about the call, not part of the answer.

        stop_reason = getattr(response, "stop_reason", "end_turn") or "end_turn"
        text = "".join(text_parts).strip()
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            note = ("Claude declined to answer this"
                    + (f" ({category})" if category else "") + ".")
            text = (text + "\n\n" + note).strip() if text else note
            blocks.append({"type": "text", "text": note})

        usage = getattr(response, "usage", None)
        return Completion(
            text=text,
            blocks=blocks,
            tool_calls=tool_calls if stop_reason != "refusal" else [],
            stop_reason=stop_reason,
            model=getattr(response, "model", None) or model,
            usage={
                "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
            latency_ms=latency_ms,
            provider="anthropic",
        )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
