"""Keeping the model resident.

This is the single biggest thing affecting how fast JARVIS feels, and it is
entirely a configuration detail rather than anything about the model.
Measured on this machine, same request, same model:

    resident   0.04s
    cold      12.24s   (11.98s of it reloading 4.4 GB of weights)

Ollama's default is a five-minute idle timeout, so any ordinary pause in a
conversation put the next reply on the wrong side of that.
"""
from __future__ import annotations

import pytest

from backend.ai.providers.ollama_provider import (DEFAULT_KEEP_ALIVE,
                                                  OllamaProvider, _keep_alive)


def test_a_numeric_string_becomes_an_integer():
    """Ollama parses a *string* keep_alive as a Go duration, so "-1" is
    rejected outright:

        HTTP 400 {"error":"time: missing unit in duration \"-1\""}

    Only the integer -1 means "hold indefinitely". The value arrives from
    configuration as text, so something has to make that distinction --
    and getting it wrong does not slow requests down, it fails all of them.
    """
    assert _keep_alive("-1") == -1
    assert isinstance(_keep_alive("-1"), int)
    assert _keep_alive("600") == 600
    assert _keep_alive("  600  ") == 600


def test_a_duration_string_is_left_alone():
    for value in ("30m", "24h", "90s"):
        assert _keep_alive(value) == value


def test_an_empty_setting_falls_back_to_indefinite():
    assert _keep_alive("") == -1
    assert _keep_alive("   ") == -1


def test_the_default_resolves_to_indefinite():
    assert _keep_alive(DEFAULT_KEEP_ALIVE) == -1


def test_the_provider_coerces_at_construction():
    provider = OllamaProvider("http://x", "m", keep_alive="-1")
    assert provider.keep_alive == -1

    provider = OllamaProvider("http://x", "m", keep_alive="45m")
    assert provider.keep_alive == "45m"

    # Unset means the default, not None or an empty string either of which
    # Ollama would reject.
    provider = OllamaProvider("http://x", "m")
    assert provider.keep_alive == -1


def test_keep_alive_is_sent_on_both_request_paths():
    """Streaming and non-streaming are separate payloads; a model kept warm
    on one and allowed to expire on the other is the worst of both."""
    import inspect

    for method in (OllamaProvider.complete, OllamaProvider.stream):
        source = inspect.getsource(method)
        assert '"keep_alive"' in source, method.__name__


def test_the_provider_can_warm_itself():
    assert hasattr(OllamaProvider, "warm")


def test_the_setting_is_configurable():
    from backend.config import settings

    assert hasattr(settings, "OLLAMA_KEEP_ALIVE")
    # Whatever it is set to must survive coercion into something Ollama takes.
    value = _keep_alive(settings.OLLAMA_KEEP_ALIVE)
    assert isinstance(value, (int, str))
