"""Resolving the Ollama endpoint.

OLLAMA_HOST is Ollama's own *server bind* variable and is very commonly set to
"0.0.0.0" by anyone who has exposed their Ollama to the network. That is a
valid address to listen on and a useless one to connect to, so reading it
straight into a client URL makes every model call fail with a confusing
"missing protocol" error.
"""
from __future__ import annotations

import pytest

from backend.config import _ollama_url


@pytest.mark.parametrize("raw, expected", [
    # The case that actually broke this machine.
    ("0.0.0.0", "http://127.0.0.1:11434"),
    ("0.0.0.0:11434", "http://127.0.0.1:11434"),
    ("0.0.0.0:9999", "http://127.0.0.1:9999"),
    # IPv6 wildcards contain colons, so a naive split mangles them.
    ("::", "http://127.0.0.1:11434"),
    ("[::]", "http://127.0.0.1:11434"),
    ("*", "http://127.0.0.1:11434"),
    # Nothing configured at all.
    ("", "http://127.0.0.1:11434"),
    ("   ", "http://127.0.0.1:11434"),
    # A missing scheme is supplied.
    ("localhost:11434", "http://localhost:11434"),
    ("ollama.lan", "http://ollama.lan:11434"),
    # Already-correct values pass through untouched.
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://box:11434", "http://box:11434"),
    # A remote host keeps its scheme and gains the default port.
    ("https://remote.example.com", "https://remote.example.com:11434"),
])
def test_ollama_url_normalisation(raw, expected):
    assert _ollama_url(raw) == expected


def test_a_path_is_preserved():
    assert _ollama_url("http://proxy.local:8080/ollama") == "http://proxy.local:8080/ollama"


def test_result_is_always_usable_by_an_http_client():
    """Whatever goes in, what comes out has a scheme and a host."""
    for raw in ("0.0.0.0", "", "::", "weird", "http://x"):
        url = _ollama_url(raw)
        assert url.startswith(("http://", "https://"))
        assert "://" in url and len(url.split("://")[1]) > 0
