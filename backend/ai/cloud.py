"""Bring your own AI: which cloud providers JARVIS can use, and their keys.

The owner chooses a provider in the setup screen (or Settings), pastes their
own API key, and picks a model from the list the provider itself returns --
so the choice is never limited to whatever model names were current when
this file was written.

Keys:
  * are sealed before they touch the disk (Windows DPAPI, the operating
    system's per-user encryption -- see core/secrets.py). Where no sealing
    exists, a key is refused rather than stored in plain text, and the owner
    is told to put it in .env instead;
  * are never sent back to the browser. The interface only ever learns
    whether a key is saved and its last four characters;
  * can also come from the environment (.env), under the provider's usual
    variable name -- OPENAI_API_KEY, ANTHROPIC_API_KEY and so on.

Anthropic is reached through its official SDK. Everyone else here speaks the
OpenAI chat-completions protocol, so one client serves them all.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import secrets
from .providers.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT
from .providers.anthropic_provider import AnthropicProvider
from .providers.base import ProviderError
from .providers.openai_compat_provider import OpenAICompatProvider, api_base

logger = logging.getLogger("jarvis.ai.cloud")

KEY_FILE = "provider_keys.json"

# id -> how to reach it. `base_url` is None for Anthropic (native SDK) and
# for "custom", where the owner supplies it.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)", "kind": "anthropic", "base_url": None,
        "env": "ANTHROPIC_API_KEY", "key_url": "https://console.anthropic.com/settings/keys",
        "default_model": ANTHROPIC_DEFAULT,
    },
    "openai": {
        "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY", "key_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey",
    },
    "groq": {
        "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY", "key_url": "https://console.groq.com/keys",
    },
    "mistral": {
        "label": "Mistral", "kind": "openai", "base_url": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY", "key_url": "https://console.mistral.ai/api-keys",
    },
    "xai": {
        "label": "xAI (Grok)", "kind": "openai", "base_url": "https://api.x.ai/v1",
        "env": "XAI_API_KEY", "key_url": "https://console.x.ai",
    },
    "deepseek": {
        "label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY", "key_url": "https://platform.deepseek.com/api_keys",
    },
    "openrouter": {
        "label": "OpenRouter (many models, one key)", "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY", "key_url": "https://openrouter.ai/keys",
    },
    "together": {
        "label": "Together AI", "kind": "openai", "base_url": "https://api.together.xyz/v1",
        "env": "TOGETHER_API_KEY", "key_url": "https://api.together.ai/settings/api-keys",
    },
    "custom": {
        "label": "Other (OpenAI-compatible URL)", "kind": "openai", "base_url": None,
        "env": "CUSTOM_AI_API_KEY", "key_url": "", "key_optional": True,
    },
}

# Listed models that cannot hold a conversation.
_NOT_CHAT = ("embed", "whisper", "tts", "dall-e", "moderation", "transcribe", "realtime",
             "image", "audio", "davinci", "babbage", "aqa", "imagen", "rerank", "guard",
             "search-preview")


def catalogue() -> List[Dict[str, Any]]:
    """The providers, as the interface shows them. No secrets."""
    return [{"id": key, "label": spec["label"], "key_url": spec.get("key_url", ""),
             "env": spec["env"], "needs_base_url": spec["base_url"] is None
             and spec["kind"] == "openai",
             "key_optional": bool(spec.get("key_optional")),
             "default_model": spec.get("default_model", "")}
            for key, spec in PROVIDERS.items()]


def _spec(provider: str) -> Dict[str, Any]:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider '{provider}'.")
    return PROVIDERS[provider]


def base_url_for(provider: str, base_url: str = "") -> str:
    spec = _spec(provider)
    if spec["kind"] == "anthropic":
        return ""
    url = spec["base_url"] or base_url
    if not url:
        raise ValueError("This provider needs the address of its API, for example "
                         "https://example.com/v1.")
    return api_base(url)


# ---------------------------------------------------------------------------
class KeyStore:
    """API keys, sealed on disk, never returned whole."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / KEY_FILE

    def _load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save(self, provider: str, key: str) -> None:
        _spec(provider)
        key = (key or "").strip()
        if not key:
            raise ValueError("The API key is empty.")
        sealed = secrets.seal(key)
        if sealed.startswith(secrets.PLAIN):
            # No sealing on this system: refuse rather than write it in clear.
            raise ValueError(
                "This computer has no built-in way to seal a key (Windows does), so JARVIS "
                f"won't store it in a file. Put it in your .env file as "
                f"{PROVIDERS[provider]['env']}=... and restart JARVIS instead.")
        data = self._load()
        data[provider] = sealed
        self._write(data)

    def remove(self, provider: str) -> bool:
        data = self._load()
        if provider in data:
            del data[provider]
            self._write(data)
            return True
        return False

    def get(self, provider: str) -> str:
        """The usable key: saved first, then the environment. '' if none."""
        stored = self._load().get(provider, "")
        if stored:
            try:
                return secrets.unseal(stored)
            except ValueError:
                logger.warning("saved key for %s can't be opened on this account", provider)
        spec = PROVIDERS.get(provider)
        return os.environ.get(spec["env"], "") if spec else ""

    def describe(self) -> Dict[str, Dict[str, Any]]:
        """Which providers have a key, and where from -- never the key."""
        saved = self._load()
        out: Dict[str, Dict[str, Any]] = {}
        for provider, spec in PROVIDERS.items():
            key = self.get(provider)
            source = "saved" if provider in saved and key else (
                "environment" if os.environ.get(spec["env"]) else None)
            out[provider] = {"has_key": bool(key), "source": source,
                             "hint": ("…" + key[-4:]) if len(key) >= 12 else ("…" if key else "")}
        return out


# ---------------------------------------------------------------------------
def build(provider: str, key: str, model: str, base_url: str = "", name: str = "cloud"):
    """A provider object for one call or for the orchestrator."""
    spec = _spec(provider)
    if spec["kind"] == "anthropic":
        if not key:
            raise ValueError("An Anthropic API key is needed.")
        return AnthropicProvider(key, model or ANTHROPIC_DEFAULT, name=name)
    if not key and not spec.get("key_optional"):
        raise ValueError(f"A {spec['label']} API key is needed.")
    return OpenAICompatProvider(base_url_for(provider, base_url), model=model, api_key=key,
                                name=name, label=spec["label"])


async def list_models(provider: str, key: str, base_url: str = "") -> List[str]:
    """The models this key can use, as the provider reports them."""
    spec = _spec(provider)
    if spec["kind"] == "anthropic":
        if not key:
            raise ValueError("Paste your Anthropic API key first.")
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key, max_retries=1)
        try:
            ids = [model.id async for model in client.models.list(limit=100)]
        except anthropic.AuthenticationError as exc:
            raise ValueError(f"Anthropic rejected the API key: {_api_message(exc)}") from exc
        except anthropic.APIError as exc:
            raise ValueError(f"Anthropic couldn't list models: {_api_message(exc)}") from exc
        finally:
            await client.close()
        # Newest first is how the API returns them; keep that order.
        return ids

    client = build(provider, key, "", base_url, name="setup")
    try:
        response = await client._http().get(f"{client.base_url}/models", timeout=15.0)
        if response.status_code >= 400:
            raise ValueError(str(client._failure(response.status_code, response.text)))
        data = response.json()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Couldn't reach {spec['label']}: {exc}") from exc
    finally:
        await client.close()

    entries = data.get("data") if isinstance(data, dict) else data
    models = []
    for entry in entries or []:
        model_id = (entry.get("id") if isinstance(entry, dict) else str(entry)) or ""
        if model_id.startswith("models/"):          # Gemini's naming
            model_id = model_id[len("models/"):]
        if model_id and not any(word in model_id.lower() for word in _NOT_CHAT):
            created = entry.get("created", 0) if isinstance(entry, dict) else 0
            models.append((created or 0, model_id))
    models.sort(key=lambda item: (-item[0], item[1]))
    return [model_id for _, model_id in models]


async def try_key(provider: str, key: str, model: str, base_url: str = "") -> Dict[str, Any]:
    """Send one tiny real message, so 'it works' means it worked."""
    if not model:
        return {"ok": False, "error": "Choose a model first."}
    try:
        client = build(provider, key, model, base_url, name="setup")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        completion = await client.complete(
            [{"role": "user", "content": "Reply with exactly one word: ready"}],
            system="You are being tested. Reply with one word.", model=model, max_tokens=512)
        return {"ok": True, "reply": completion.text[:200], "model": completion.model or model,
                "latency_ms": completion.latency_ms}
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _api_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(getattr(exc, "message", "") or exc)
