"""Streaming completions from Ollama.

Streaming is the path the whole interface uses, and tool calls were being
dropped on it: Ollama emits them on their own chunk part-way through the
stream, the reader kept only the final `done` chunk, and the final chunk's
message is empty. The model asked for `system_status`, nothing ran it, the
turn produced no text, and the user got silence.

The same question answered correctly through the non-streaming endpoint,
which is what made it look like the model's fault rather than the reader's.
These drive the real `stream()` against a fake transport so the shape of what
Ollama actually sends is pinned down.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from backend.ai.providers.ollama_provider import OllamaProvider


# ---------------------------------------------------------------------------
# A stand-in for httpx's streaming response.
# ---------------------------------------------------------------------------
class _FakeStream:
    def __init__(self, lines: List[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines: List[str]):
        self._lines = lines
        self.sent: Dict[str, Any] = {}

    def stream(self, method, url, json=None, **kwargs):
        self.sent = json or {}
        return _FakeStream(self._lines)


def _provider(lines: List[str]) -> OllamaProvider:
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen2.5")
    client = _FakeClient(lines)
    provider._http = lambda: client            # type: ignore[assignment]

    async def _resolve(*_args, **_kwargs):
        return "qwen2.5"

    provider._resolve_model = _resolve         # type: ignore[assignment]
    return provider


async def _drain(provider, tools=None):
    text = ""
    completion = None
    async for event in provider.stream([{"role": "user", "content": "hello"}], tools=tools):
        if event["type"] == "text":
            text += event["text"]
        elif event["type"] == "done":
            completion = event["completion"]
    return text, completion


TOOLS = [{"name": "system_status", "description": "CPU and memory",
          "input_schema": {"type": "object", "properties": {}}}]


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_plain_text_streams_through():
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Paris"}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": "."}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]
    text, completion = await _drain(_provider(lines))
    assert text == "Paris."
    assert completion.text == "Paris."
    assert not completion.wants_tools


@pytest.mark.asyncio
async def test_a_tool_call_on_its_own_chunk_is_not_lost():
    """The actual bug: the call arrives mid-stream, `done` carries nothing."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "system_status", "arguments": {}}}]}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]
    _text, completion = await _drain(_provider(lines), tools=TOOLS)

    assert completion.wants_tools, "a tool call sent mid-stream must survive to the completion"
    assert [call["name"] for call in completion.tool_calls] == ["system_status"]
    assert completion.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_tool_call_arguments_survive():
    lines = [
        json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "search_web",
                          "arguments": {"query": "weather in Leeds", "limit": 3}}}]},
            "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    tools = [{"name": "search_web", "description": "search",
              "input_schema": {"type": "object", "properties": {}}}]
    _text, completion = await _drain(_provider(lines), tools=tools)
    assert completion.tool_calls[0]["input"] == {"query": "weather in Leeds", "limit": 3}


@pytest.mark.asyncio
async def test_several_tool_calls_across_chunks_are_all_kept():
    lines = [
        json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "system_status", "arguments": {}}}]}, "done": False}),
        json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "list_processes", "arguments": {"limit": 5}}}]},
            "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    tools = TOOLS + [{"name": "list_processes", "description": "procs",
                      "input_schema": {"type": "object", "properties": {}}}]
    _text, completion = await _drain(_provider(lines), tools=tools)
    assert [call["name"] for call in completion.tool_calls] == ["system_status", "list_processes"]


@pytest.mark.asyncio
async def test_text_then_a_tool_call_keeps_both():
    lines = [
        json.dumps({"message": {"content": "Let me check. "}, "done": False}),
        json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "system_status", "arguments": {}}}]}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    text, completion = await _drain(_provider(lines), tools=TOOLS)
    assert text == "Let me check. "
    # The completion trims; the streamed deltas do not. Both are intended.
    assert completion.text.strip() == "Let me check."
    assert completion.wants_tools


@pytest.mark.asyncio
async def test_tool_calls_on_the_done_chunk_still_work():
    """Some versions put them on the terminal chunk. Both must be handled."""
    lines = [
        json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "system_status", "arguments": {}}}]}, "done": True}),
    ]
    _text, completion = await _drain(_provider(lines), tools=TOOLS)
    assert completion.wants_tools
    assert completion.tool_calls[0]["name"] == "system_status"


@pytest.mark.asyncio
async def test_a_stream_with_no_done_chunk_still_completes():
    """A truncated stream must not come back as None and blow up the caller."""
    lines = [json.dumps({"message": {"content": "half an ans"}, "done": False})]
    text, completion = await _drain(_provider(lines))
    assert completion is not None
    assert text == "half an ans"
    assert completion.text == "half an ans"


@pytest.mark.asyncio
async def test_malformed_lines_are_skipped_rather_than_fatal():
    lines = [
        "not json",
        "",
        json.dumps({"message": {"content": "fine"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]
    text, completion = await _drain(_provider(lines))
    assert text == "fine"
    assert completion.text == "fine"
