"""
Provider abstraction.

Everything above this layer speaks one message format (Anthropic-shaped, since
it is the most expressive of the three): a list of
`{"role": ..., "content": [block, ...]}` where a block is one of

    {"type": "text", "text": str}
    {"type": "tool_use", "id": str, "name": str, "input": dict}
    {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}

Each provider is responsible for translating to and from its own wire format.
"""
from __future__ import annotations

import abc
from typing import Any, AsyncIterator, Dict, List, Optional


class ProviderError(Exception):
    """Recoverable provider failure -- the orchestrator may fall back."""

    def __init__(self, message: str, *, retryable: bool = True, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message, retryable=True, status=429)
        self.retry_after = retry_after


class Completion:
    __slots__ = ("text", "blocks", "tool_calls", "stop_reason", "model", "usage", "latency_ms", "provider")

    def __init__(
        self,
        text: str = "",
        blocks: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        stop_reason: str = "end_turn",
        model: str = "",
        usage: Optional[Dict[str, int]] = None,
        latency_ms: int = 0,
        provider: str = "",
    ):
        self.text = text
        self.blocks = blocks or []
        self.tool_calls = tool_calls or []
        self.stop_reason = stop_reason
        self.model = model
        self.usage = usage or {"input_tokens": 0, "output_tokens": 0}
        self.latency_ms = latency_ms
        self.provider = provider

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "blocks": self.blocks,
            "tool_calls": self.tool_calls,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
        }


class Provider(abc.ABC):
    name: str = "provider"

    @abc.abstractmethod
    async def available(self) -> bool:
        """Cheap liveness probe used by the health service."""

    @abc.abstractmethod
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> Completion:
        """One non-streaming turn."""

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield `{"type": "text", "text": ...}` deltas, then exactly one
        `{"type": "done", "completion": Completion}`.

        The default implementation degrades gracefully to a single chunk so a
        provider without streaming support still works everywhere."""
        completion = await self.complete(messages, system, tools, model, max_tokens, temperature)
        if completion.text:
            yield {"type": "text", "text": completion.text}
        yield {"type": "done", "completion": completion}

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# helpers shared by providers
# ---------------------------------------------------------------------------

def blocks_to_text(blocks: List[Dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def normalise_content(content: Any) -> List[Dict[str, Any]]:
    """Accept a bare string or a block list; always return a block list."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]
