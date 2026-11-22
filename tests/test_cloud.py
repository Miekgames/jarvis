"""Bring-your-own-key AI: the provider requests, the key store, setup presets.

The Anthropic tests run the real SDK against a fake network layer and read
back exactly what would have gone over the wire -- the request shape is what
breaks first when models change, and it is checked here without a real key.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.ai import cloud
from backend.ai.providers import anthropic_provider as ap
from backend.ai.providers.openai_compat_provider import OpenAICompatProvider
from backend.core import secrets


# ---------------------------------------------------------------------------
# Anthropic: what is sent
# ---------------------------------------------------------------------------
def _history():
    return [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "old-sig"},
            {"type": "text", "text": "earlier answer"}]},
        {"role": "user", "content": "what's in my notes?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "current-sig"},
            {"type": "tool_use", "id": "tu_1", "name": "search_documents", "input": {"question": "notes"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "a note"}]},
    ]


def test_current_models_get_no_temperature_and_adaptive_thinking():
    kwargs, betas = ap.request_for("claude-opus-5-5", _history(), "system", None, 4096)
    assert "temperature" not in kwargs
    assert kwargs["thinking"] == {"type": "adaptive",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert kwargs["max_tokens"] >= 16000, "thinking needs room inside max_tokens"
    assert ap.BETA_BINDING in betas and ap.BETA_FALLBACK in betas
    assert kwargs["extra_body"] == {"fallbacks": "default"}


def test_thinking_from_earlier_turns_is_removed_and_this_turns_kept():
    messages = ap.prepare_messages(_history())
    earlier, current = messages[1]["content"], messages[3]["content"]
    assert all(block["type"] != "thinking" for block in earlier)
    assert current[0] == {"type": "thinking", "thinking": "", "signature": "current-sig"}


def test_older_models_get_a_plain_request():
    kwargs, betas = ap.request_for("claude-haiku-4-5", _history(), "", None, 4096)
    assert betas == [] and "thinking" not in kwargs and "extra_body" not in kwargs
    assert "temperature" not in kwargs and kwargs["max_tokens"] == 4096


def test_fallback_only_for_the_models_that_take_it():
    assert ap.uses_fallback("claude-opus-5-5") and ap.uses_fallback("claude-sonnet-5-5")
    assert not ap.uses_fallback("claude-haiku-5-5") and not ap.uses_fallback("claude-opus-4-8")


def _anthropic_with(reply, seen):
    import anthropic

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "headers": dict(request.headers),
                     "body": json.loads(request.content)})
        return httpx.Response(200, json=reply)

    client = anthropic.AsyncAnthropic(
        api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return ap.AnthropicProvider("test-key", "claude-opus-5-5", name="cloud", client=client)


@pytest.mark.asyncio
async def test_the_wire_request_and_the_reply_round_trip():
    seen = []
    reply = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "tu_2", "name": "get_weather", "input": {"place": "Leeds"}},
        ],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    provider = _anthropic_with(reply, seen)
    completion = await provider.complete(_history(), system="be brief",
                                         tools=[{"name": "get_weather", "description": "w",
                                                 "input_schema": {"type": "object"}}],
                                         temperature=0.7)

    sent = seen[0]
    body = sent["body"]
    assert "temperature" not in body
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"]["type"] == "adaptive"
    assert body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert body["fallbacks"] == "default"
    beta_header = sent["headers"].get("anthropic-beta", "")
    assert ap.BETA_BINDING in beta_header and ap.BETA_FALLBACK in beta_header
    assert "old-sig" not in json.dumps(body["messages"])
    assert "current-sig" in json.dumps(body["messages"])

    # The thinking block comes back verbatim, for the next loop iteration.
    assert completion.blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert completion.tool_calls == [{"id": "tu_2", "name": "get_weather", "input": {"place": "Leeds"}}]
    assert completion.text == "Looking."
    assert completion.provider == "anthropic"


@pytest.mark.asyncio
async def test_a_refusal_is_said_plainly_and_runs_no_tools():
    seen = []
    reply = {
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "tool_use", "id": "tu_3", "name": "x", "input": {}}],
        "stop_reason": "refusal", "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    completion = await _anthropic_with(reply, seen).complete(
        [{"role": "user", "content": "hi"}])
    assert completion.tool_calls == []
    assert "declined" in completion.text


# ---------------------------------------------------------------------------
# OpenAI-compatible services: adapting to what each one accepts
# ---------------------------------------------------------------------------
def test_official_openai_starts_with_its_own_parameter_names():
    provider = OpenAICompatProvider("https://api.openai.com/v1", model="m", api_key="k")
    payload = provider._payload([{"role": "user", "content": "hi"}], "", None, "m", 100, 0.7, False)
    assert payload["max_completion_tokens"] == 100 and "max_tokens" not in payload
    assert "temperature" not in payload


def test_other_services_get_the_classic_names_and_adapt_on_a_400():
    provider = OpenAICompatProvider("https://api.groq.com/openai/v1", model="m", api_key="k")
    payload = provider._payload([{"role": "user", "content": "hi"}], "", None, "m", 100, 0.7, False)
    assert payload["max_tokens"] == 100 and payload["temperature"] == 0.7

    assert provider._adapt('{"error": {"message": "Unsupported parameter: \'max_tokens\'. '
                           'Use \'max_completion_tokens\' instead."}}')
    assert provider._adapt('{"error": {"message": "temperature: only the default (1) is supported"}}')
    payload = provider._payload([{"role": "user", "content": "hi"}], "", None, "m", 100, 0.7, False)
    assert payload["max_completion_tokens"] == 100 and "temperature" not in payload
    assert not provider._adapt('{"error": {"message": "model not found"}}')


def test_errors_read_as_sentences_not_json():
    provider = OpenAICompatProvider("https://api.openai.com/v1", label="OpenAI")
    error = provider._failure(401, '{"error": {"message": "Incorrect API key provided."}}')
    assert str(error) == "OpenAI rejected the API key (401): Incorrect API key provided."
    assert error.retryable is False


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
@pytest.fixture
def sealing(monkeypatch):
    """A stand-in for DPAPI: reversible, and visibly not the plain key."""
    monkeypatch.setattr(secrets, "seal", lambda v: secrets.PREFIX + v[::-1])
    monkeypatch.setattr(secrets, "unseal", lambda v: v[len(secrets.PREFIX):][::-1]
                        if v.startswith(secrets.PREFIX) else v)


def test_a_saved_key_is_sealed_and_never_described(tmp_path, sealing, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = cloud.KeyStore(tmp_path)
    store.save("openai", "  sk-test-1234567890abcd  ")
    on_disk = (tmp_path / cloud.KEY_FILE).read_text(encoding="utf-8")
    assert "sk-test-1234567890abcd" not in on_disk
    assert store.get("openai") == "sk-test-1234567890abcd"
    described = json.dumps(store.describe())
    assert "sk-test-1234567890abcd" not in described
    assert store.describe()["openai"] == {"has_key": True, "source": "saved", "hint": "…abcd"}
    assert store.remove("openai") and store.get("openai") == ""


def test_the_environment_is_a_source_too(tmp_path, sealing, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_env_000000")
    store = cloud.KeyStore(tmp_path)
    assert store.get("groq") == "gsk_from_env_000000"
    assert store.describe()["groq"]["source"] == "environment"


def test_without_sealing_a_key_is_refused_not_written(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "seal", lambda v: secrets.PLAIN + v)
    store = cloud.KeyStore(tmp_path)
    with pytest.raises(ValueError, match=".env"):
        store.save("openai", "sk-test-abc")
    assert not (tmp_path / cloud.KEY_FILE).exists()


def test_unknown_providers_and_missing_addresses_are_refused():
    with pytest.raises(ValueError):
        cloud.build("nope", "k", "m")
    with pytest.raises(ValueError, match="address"):
        cloud.build("custom", "", "m")
    with pytest.raises(ValueError, match="key"):
        cloud.build("openai", "", "m")


# ---------------------------------------------------------------------------
# Model lists
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_model_lists_drop_non_chat_models_and_gemini_prefixes(monkeypatch):
    listing = {"data": [
        {"id": "models/gemini-x-flash", "created": 3},
        {"id": "text-embedding-3", "created": 9},
        {"id": "chat-model-a", "created": 5},
        {"id": "whisper-1", "created": 1},
    ]}

    real_build = cloud.build

    def build(*args, **kwargs):
        provider = real_build(*args, **kwargs)
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=listing)))
        return provider

    monkeypatch.setattr(cloud, "build", build)
    models = await cloud.list_models("openai", "sk-test")
    assert models == ["chat-model-a", "gemini-x-flash"]


# ---------------------------------------------------------------------------
# Setup's permission presets
# ---------------------------------------------------------------------------
def test_presets_are_ordered_by_reach():
    from backend.api.setup_routes import _preset
    from backend.core.permissions import BY_KEY

    careful, recommended, everything = (_preset(n) for n in ("careful", "recommended", "everything"))
    assert all(everything.values())
    assert recommended == {k: p.default for k, p in BY_KEY.items()}
    assert not recommended["shell.execute"] and not recommended["files.delete"]
    assert sum(careful.values()) < sum(recommended.values())
    assert not careful["files.write"]
