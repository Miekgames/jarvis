"""Any local model server that speaks the OpenAI chat API.

LM Studio, llama.cpp's server, vLLM, Jan, LocalAI, KoboldCpp, text-generation
-webui and Ollama itself all answer `POST /v1/chat/completions` in the same
shape, so one provider reaches all of them. Ollama keeps its own provider
(it reports token timings this API does not); this is for everything else.

The conversation arrives in JARVIS's own format -- text, tool_use and
tool_result blocks -- and is translated: tool calls become `tool_calls`
with JSON-string arguments and an id, results become `role: tool` messages
carrying that id, images become data URLs.

Ported idea: OpenJarvis ``engine/_openai_compat.py`` and its engine
discovery (Stanford Hazy Research, Apache-2.0) -- one OpenAI-compatible
client for the whole family of local servers. Written against JARVIS's
provider interface, with its inline-tool-call rescue for small models.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from .base import Completion, Provider, ProviderError, RateLimited, normalise_content
from .ollama_provider import _tool_names, parse_inline_tool_call

logger = logging.getLogger("jarvis.ai.openai_compat")


def api_base(url: str) -> str:
    """A server address as the API root: no trailing slash, ending in /v1."""
    base = (url or "").strip().rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


class OpenAICompatProvider(Provider):
    def __init__(self, base_url: str, model: str = "", api_key: str = "",
                 name: str = "local_server", label: str = "") -> None:
        self.name = name
        self.base_url = api_base(base_url)
        self.model = model
        self.api_key = api_key
        self.label = label or "Local server"
        self._client = None
        self._usage_option = True        # dropped if a server rejects it
        # OpenAI's newer models want max_completion_tokens and only their
        # default temperature; most other OpenAI-compatible services want
        # the classic names. Start from what the service is known to take
        # and adapt when a 400 names the parameter.
        official_openai = "api.openai.com" in self.base_url
        self._token_param = "max_completion_tokens" if official_openai else "max_tokens"
        self._send_temperature = not official_openai

    def _http(self):
        if self._client is None:
            import httpx

            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0),
                                             headers=headers)
        return self._client

    async def available(self) -> bool:
        try:
            response = await self._http().get(f"{self.base_url}/models", timeout=2.5)
            return response.status_code == 200
        except Exception:
            return False

    async def models(self) -> List[str]:
        try:
            response = await self._http().get(f"{self.base_url}/models", timeout=4.0)
            response.raise_for_status()
            return [m.get("id", "") for m in response.json().get("data") or [] if m.get("id")]
        except Exception:
            return []

    async def resolve_for(self, purpose: str = "conversation") -> str:
        if self.model:
            return self.model
        found = await self.models()
        if not found:
            raise ProviderError(f"{self.label} at {self.base_url} has no model loaded.",
                                retryable=False)
        return found[0]

    # -- translation -----------------------------------------------------------
    @staticmethod
    def prepare(messages: List[Dict[str, Any]], system: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for message in messages:
            role = message.get("role", "user")
            texts: List[str] = []
            images: List[str] = []
            calls: List[Dict[str, Any]] = []
            results: List[Dict[str, Any]] = []
            for block in normalise_content(message.get("content")):
                kind = block.get("type")
                if kind == "text":
                    texts.append(block.get("text", ""))
                elif kind == "image":
                    source = block.get("source") or {}
                    if isinstance(source, dict) and source.get("data"):
                        images.append(f"data:{source.get('media_type', 'image/png')};base64,"
                                      f"{source['data']}")
                elif kind == "tool_use":
                    calls.append({"id": block.get("id") or f"call_{len(calls)}",
                                  "type": "function",
                                  "function": {"name": block.get("name", ""),
                                               "arguments": json.dumps(block.get("input") or {})}})
                elif kind == "tool_result":
                    results.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                                    "content": str(block.get("content", ""))[:12000]})
            out.extend(results)
            if texts or calls or images:
                text = "\n".join(t for t in texts if t)
                entry: Dict[str, Any] = {"role": role}
                if images:
                    entry["content"] = ([{"type": "text", "text": text}] if text else []) + [
                        {"type": "image_url", "image_url": {"url": url}} for url in images]
                else:
                    entry["content"] = text
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
        return out

    @staticmethod
    def translate_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        return [{"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }} for tool in tools]

    def _completion(self, message: Dict[str, Any], usage: Dict[str, Any], model: str,
                    latency_ms: int, tool_names) -> Completion:
        text = (message.get("content") or "").strip()
        blocks: List[Dict[str, Any]] = []
        calls: List[Dict[str, Any]] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    arguments = {"_raw": arguments}
            call_id = call.get("id") or f"call_{index}"
            calls.append({"id": call_id, "name": function.get("name", ""), "input": arguments})
        # A small model writing its call into the text: rescue it, as the
        # Ollama provider does.
        if text and tool_names and not calls:
            inline = parse_inline_tool_call(text)
            if inline and (inline["name"] in tool_names or inline["exclusive"]):
                calls.append({"id": "inline_0", "name": inline["name"], "input": inline["input"]})
                text = ""
        if text:
            blocks.append({"type": "text", "text": text})
        for call in calls:
            blocks.append({"type": "tool_use", **call})
        return Completion(
            text=text, blocks=blocks, tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn", model=model,
            usage={"input_tokens": int(usage.get("prompt_tokens") or 0),
                   "output_tokens": int(usage.get("completion_tokens") or 0)},
            latency_ms=latency_ms, provider=self.name)

    # -- calls -------------------------------------------------------------------
    def _payload(self, messages, system, tools, model, max_tokens, temperature, stream):
        payload: Dict[str, Any] = {
            "model": model, "messages": self.prepare(messages, system),
            self._token_param: max_tokens, "stream": stream,
        }
        if self._send_temperature:
            payload["temperature"] = temperature
        translated = self.translate_tools(tools)
        if translated:
            payload["tools"] = translated
        if stream and self._usage_option:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def complete(self, messages, system="", tools=None, model=None,
                       max_tokens=4096, temperature=0.7) -> Completion:
        model = model or await self.resolve_for()
        started = time.monotonic()
        try:
            for attempt in range(4):
                response = await self._http().post(
                    f"{self.base_url}/chat/completions",
                    json=self._payload(messages, system, tools, model, max_tokens, temperature,
                                       False))
                if response.status_code == 400 and attempt < 3 and self._adapt(response.text):
                    continue
                if response.status_code >= 400:
                    raise self._failure(response.status_code, response.text)
                break
            data = response.json()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"{self.label} unreachable at {self.base_url}: {exc}") from exc
        choice = (data.get("choices") or [{}])[0]
        return self._completion(choice.get("message") or {}, data.get("usage") or {}, model,
                                int((time.monotonic() - started) * 1000), _tool_names(tools))

    async def stream(self, messages, system="", tools=None, model=None,
                     max_tokens=4096, temperature=0.7) -> AsyncIterator[Dict[str, Any]]:
        model = model or await self.resolve_for()
        started = time.monotonic()
        text = ""
        calls: Dict[int, Dict[str, Any]] = {}
        usage: Dict[str, Any] = {}
        for attempt in range(4):
            payload = self._payload(messages, system, tools, model, max_tokens, temperature, True)
            try:
                async with self._http().stream("POST", f"{self.base_url}/chat/completions",
                                               json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        if response.status_code == 400 and attempt < 3 and self._adapt(body):
                            continue
                        raise self._failure(response.status_code, body)
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                text += delta["content"]
                                yield {"type": "text", "text": delta["content"]}
                            for part in delta.get("tool_calls") or []:
                                slot = calls.setdefault(int(part.get("index", 0)),
                                                        {"id": "", "name": "", "arguments": ""})
                                slot["id"] = part.get("id") or slot["id"]
                                function = part.get("function") or {}
                                slot["name"] += function.get("name") or ""
                                slot["arguments"] += function.get("arguments") or ""
                break
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(f"{self.label} stream failed: {exc}") from exc

        message = {"content": text, "tool_calls": [
            {"id": slot["id"] or f"call_{index}",
             "function": {"name": slot["name"], "arguments": slot["arguments"]}}
            for index, slot in sorted(calls.items())]}
        yield {"type": "done", "completion": self._completion(
            message, usage, model, int((time.monotonic() - started) * 1000), _tool_names(tools))}

    def _adapt(self, body: str) -> bool:
        """Change what is sent after a 400 that names the parameter at fault.

        Returns whether anything changed -- if not, the 400 is the answer.
        """
        text = (body or "").lower()
        if "max_completion_tokens" in text and self._token_param == "max_tokens":
            self._token_param = "max_completion_tokens"
            return True
        if "max_completion_tokens" in text and self._token_param == "max_completion_tokens" \
                and "max_tokens" in text.replace("max_completion_tokens", ""):
            self._token_param = "max_tokens"
            return True
        if "temperature" in text and self._send_temperature:
            self._send_temperature = False
            return True
        if "stream_options" in text and self._usage_option:
            self._usage_option = False
            return True
        return False

    def _failure(self, status: int, body: str) -> ProviderError:
        """A readable error from a service's reply, without its JSON wrapping."""
        message = (body or "").strip()
        try:
            parsed = json.loads(message)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
            elif isinstance(error, str):
                message = error
            elif isinstance(parsed, dict) and parsed.get("message"):
                message = str(parsed["message"])
        except ValueError:
            pass
        if status in (401, 403):
            return ProviderError(f"{self.label} rejected the API key ({status}): {message[:300]}",
                                 retryable=False, status=status)
        if status == 404:
            return ProviderError(f"{self.label} does not know that model or address ({status}): "
                                 f"{message[:300]}", retryable=False, status=status)
        if status == 429:
            return RateLimited(f"{self.label} rate limit or quota reached: {message[:300]}")
        return ProviderError(f"{self.label} answered {status}: {message[:300]}",
                             retryable=status >= 500, status=status)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
