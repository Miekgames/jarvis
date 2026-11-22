"""Local model servers that speak the OpenAI API, and choosing one."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.ai.engines import fit
from backend.ai.providers.openai_compat_provider import OpenAICompatProvider, api_base


def test_addresses_are_normalised_to_the_api_root():
    assert api_base("http://127.0.0.1:1234") == "http://127.0.0.1:1234/v1"
    assert api_base("http://127.0.0.1:8081/v1/") == "http://127.0.0.1:8081/v1"


def test_jarvis_conversation_becomes_openai_messages():
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "t1", "name": "weather", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "18C"}]},
    ]
    out = OpenAICompatProvider.prepare(messages, "Be brief.")
    assert out[0] == {"role": "system", "content": "Be brief."}
    assert out[2]["tool_calls"][0]["id"] == "t1"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "18C"}


def _provider(handler) -> OpenAICompatProvider:
    provider = OpenAICompatProvider("http://local.test", model="m")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


@pytest.mark.asyncio
async def test_a_tool_call_comes_back_in_jarvis_form():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c9", "function": {"name": "weather", "arguments": '{"city": "Oslo"}'}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9}})

    completion = await _provider(handler).complete(
        [{"role": "user", "content": "Oslo weather"}],
        tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    assert completion.tool_calls == [{"id": "c9", "name": "weather", "input": {"city": "Oslo"}}]
    assert completion.usage == {"input_tokens": 50, "output_tokens": 9}
    assert completion.provider == "local_server"


@pytest.mark.asyncio
async def test_streamed_tool_calls_are_put_back_together():
    chunks = [
        {"choices": [{"delta": {"content": "Let me "}}]},
        {"choices": [{"delta": {"content": "check."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
                                                "function": {"name": "wea", "arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                "function": {"name": "ther", "arguments": 'ty": "Rome"}'}}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body.encode())

    text, done = "", None
    async for event in _provider(handler).stream(
            [{"role": "user", "content": "Rome?"}],
            tools=[{"name": "weather", "input_schema": {"type": "object"}}]):
        if event["type"] == "text":
            text += event["text"]
        else:
            done = event["completion"]
    assert text == "Let me check."
    assert done.tool_calls == [{"id": "c1", "name": "weather", "input": {"city": "Rome"}}]
    assert done.usage["output_tokens"] == 7


@pytest.mark.asyncio
async def test_a_server_that_refuses_stream_options_is_asked_again_without():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("stream_options" in body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": "unknown field stream_options"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

    provider = _provider(handler)
    events = [e async for e in provider.stream([{"role": "user", "content": "hi"}])]
    assert seen == [True, False]
    assert events[0] == {"type": "text", "text": "ok"}


@pytest.mark.asyncio
async def test_a_small_models_inline_tool_call_is_rescued():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"name": "weather", "arguments": {"city": "Lima"}}'}}]})

    completion = await _provider(handler).complete(
        [{"role": "user", "content": "Lima?"}],
        tools=[{"name": "weather", "input_schema": {"type": "object"}}])
    assert completion.tool_calls and completion.tool_calls[0]["name"] == "weather"
    assert completion.text == ""


@pytest.mark.parametrize("size, vram, expected", [
    (4.7, 12.9, "fits"), (9.8, 12.0, "tight"), (11.5, 12.0, "spills"), (4.0, None, "unknown"),
])
def test_model_fit_against_gpu_memory(size, vram, expected):
    assert fit(size, vram) == expected


def test_a_chosen_local_server_leads_and_ollama_falls_back():
    from backend.ai.orchestrator import Orchestrator

    settings = SimpleNamespace(local_only=True)
    cached = {"ai": {"local_server": {"enabled": True, "base_url": "http://127.0.0.1:1234",
                                      "model": "qwen3-8b"}}}
    store = SimpleNamespace(get_cached=lambda key, default=None: cached.get(key, default))
    fake = SimpleNamespace(settings=settings, settings_store=store, providers={})
    fake._local_server = lambda: Orchestrator._local_server(fake)

    assert Orchestrator._provider_order(fake) == ["local_server", "ollama"]
    assert fake.providers["local_server"].base_url == "http://127.0.0.1:1234/v1"

    cached["ai"]["local_server"]["enabled"] = False
    assert Orchestrator._provider_order(fake) == ["ollama"]
    assert "local_server" not in fake.providers
