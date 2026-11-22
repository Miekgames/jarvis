"""How much conversation the model actually gets.

Two separate limits were cutting it short, and only one of them was visible
in this codebase.

The visible one: history was the last 48 messages, flat. In a conversation
that uses tools that is barely a dozen exchanges, because every tool call and
every result is a message.

The invisible one: Ollama does not use a model's full context unless asked.
qwen2.5 advertises 32768; left alone Ollama applied its own modest default and
silently dropped everything past it -- so sending more history would not have
helped at all. The history was arriving and being thrown away at the far end.
"""
from __future__ import annotations

import json

import pytest

from backend.ai.providers.ollama_provider import OllamaProvider


# ---------------------------------------------------------------------------
# the window itself
# ---------------------------------------------------------------------------
def test_the_context_window_is_sent_to_ollama():
    """Without `num_ctx` the runtime picks for itself, and picks small."""
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen2.5", num_ctx=16384)
    options = provider._options(0.7, 4096)
    assert options["num_ctx"] == 16384


def test_an_unset_window_is_left_to_the_runtime():
    """Zero means 'do not express an opinion', which must not become a
    literal num_ctx of 0."""
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen2.5", num_ctx=0)
    assert "num_ctx" not in provider._options(0.7, 4096)


def test_the_configured_default_leaves_room_for_a_conversation():
    from backend.config import Settings

    settings = Settings()
    # Tools and the system prompt measure ~800 tokens on this build, and the
    # reply is allowed MAX_TOKENS. Anything less than this and there is no
    # conversation left to speak of.
    spare = settings.OLLAMA_NUM_CTX - settings.MAX_TOKENS
    assert spare >= 8000, (
        f"only {spare} tokens for prompt + history; the model will lose the thread")


# ---------------------------------------------------------------------------
# trimming to fit
# ---------------------------------------------------------------------------
class _Settings:
    OLLAMA_NUM_CTX = 16384
    MAX_TOKENS = 4096
    MAX_HISTORY_TURNS = 120


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.limit_used = None

    async def fetch_all(self, _sql, params):
        self.limit_used = params[-1]
        return self.rows[-self.limit_used:][::-1]      # newest first, as the query does


def _orchestrator(rows):
    from backend.ai.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.db = _DB(rows)
    orchestrator.settings = _Settings()
    return orchestrator


def _message(role, text):
    return {"role": role, "blocks": json.dumps([{"type": "text", "text": text}])}


@pytest.mark.asyncio
async def test_a_short_conversation_is_sent_whole():
    rows = [_message("user" if i % 2 == 0 else "assistant", f"line {i}") for i in range(40)]
    history = await _orchestrator(rows).history("conv")
    assert len(history) == 40, "nothing should be dropped from a short conversation"
    assert history[0]["content"][0]["text"] == "line 0", "oldest first, in order"
    assert history[-1]["content"][0]["text"] == "line 39"


@pytest.mark.asyncio
async def test_far_more_than_the_old_limit_now_survives():
    """48 messages was the old ceiling. A real conversation exceeds it."""
    rows = [_message("user", f"line {i}") for i in range(200)]
    history = await _orchestrator(rows).history("conv")
    assert len(history) > 48, f"only {len(history)} messages survived"


@pytest.mark.asyncio
async def test_an_enormous_conversation_is_trimmed_rather_than_overflowing():
    """Sending more than the window holds is worse than trimming: the runtime
    drops the overflow from the front, where the system prompt lives."""
    big = "x" * 4000
    rows = [_message("user", big) for _ in range(200)]
    orchestrator = _orchestrator(rows)
    history = await orchestrator.history("conv")

    total = sum(len(json.dumps(m["content"])) for m in history)
    assert total <= orchestrator._history_budget(), "history must fit the budget"
    assert history, "something must survive"


@pytest.mark.asyncio
async def test_trimming_keeps_the_most_recent_exchanges():
    """What matters in a conversation is what was just said."""
    big = "y" * 4000
    rows = [_message("user", f"{i}:{big}") for i in range(200)]
    history = await _orchestrator(rows).history("conv")
    newest = history[-1]["content"][0]["text"]
    assert newest.startswith("199:"), f"kept the wrong end: {newest[:8]}"


@pytest.mark.asyncio
async def test_a_single_oversized_message_is_still_sent():
    """Better one over-long message than an empty conversation."""
    rows = [_message("user", "z" * 500_000)]
    history = await _orchestrator(rows).history("conv")
    assert len(history) == 1


@pytest.mark.asyncio
async def test_the_budget_follows_the_configured_window():
    orchestrator = _orchestrator([])
    small = orchestrator._history_budget()
    orchestrator.settings.OLLAMA_NUM_CTX = 32768
    assert orchestrator._history_budget() > small
