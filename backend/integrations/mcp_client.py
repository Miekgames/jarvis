"""Talking to MCP servers: the Model Context Protocol, JSON-RPC 2.0.

MCP is the common plug-in format for AI tools: a server offers tools
("search my notes", "create a GitHub issue", "control the lights") and any
assistant that speaks the protocol can use them. Two ways to reach one:

  * **stdio** -- JARVIS starts the server as a program and talks to it over
    its standard input and output, one JSON message per line;
  * **HTTP** -- the "streamable HTTP" transport: JSON-RPC posted to a URL,
    answered as JSON or as a short server-sent event stream.

Ported from OpenJarvis (Stanford Hazy Research, Apache-2.0):
``mcp/protocol.py``, ``mcp/client.py`` and ``mcp/transport.py`` -- the
handshake, the matching of responses to requests by id (skipping the
notifications and stray lines a server may print first), draining stderr
so a chatty server cannot block itself, and the session-id header. Theirs
uses threads; this is rewritten for asyncio, and starts servers with no
console window on Windows.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.mcp")

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "jarvis", "version": "1.0"}
REQUEST_TIMEOUT = 120.0
START_TIMEOUT = 30.0
# A line longer than this from a server is a fault, not a message.
LINE_LIMIT = 8 * 1024 * 1024


class MCPError(RuntimeError):
    def __init__(self, message: str, code: int = -1, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _request(method: str, params: Optional[Dict[str, Any]], request_id: Optional[int]) -> Dict[str, Any]:
    message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return message


def _result(message: Dict[str, Any]) -> Any:
    if "error" in message and message["error"]:
        error = message["error"]
        raise MCPError(str(error.get("message", "MCP error")), int(error.get("code", -1)),
                       error.get("data"))
    return message.get("result")


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------
class StdioTransport:
    def __init__(self, command: str, args: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = cwd
        self._process: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[Any, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._stderr: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self.stderr_tail: List[str] = []

    async def start(self) -> None:
        from ..core.proc import exec_async

        environment = dict(os.environ)
        environment.update({k: str(v) for k, v in self.env.items()})
        program = self.command
        # On Windows, "npx" and friends are .cmd files that the process API
        # will not run by bare name.
        if os.name == "nt" and not os.path.splitext(program)[1]:
            import shutil

            found = shutil.which(program)
            if found:
                program = found
        try:
            self._process = await exec_async(
                program, *self.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=environment, cwd=self.cwd,
                limit=LINE_LIMIT)
        except FileNotFoundError:
            raise MCPError(f"Could not find the program '{self.command}'. Is it installed?")
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain())

    async def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    logger.debug("[%s] non-JSON output: %s", self.command, text[:200])
                    continue
                # Only a response carrying the id of a request we are
                # waiting on settles anything; notifications and strays
                # are ignored rather than mistaken for answers.
                if isinstance(message, dict) and "id" in message and \
                        ("result" in message or "error" in message):
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
        except Exception:
            logger.debug("MCP stdout reader stopped", exc_info=True)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPError(
                        "The plug-in stopped." + self._why()))
            self._pending.clear()

    async def _drain(self) -> None:
        """Read stderr continuously. A server writing more than the pipe
        holds would otherwise block on that write and never answer."""
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail = (self.stderr_tail + [text])[-20:]
                    logger.debug("[%s stderr] %s", self.command, text)
        except Exception:
            pass

    def _why(self) -> str:
        return f" It said: {self.stderr_tail[-1][:200]}" if self.stderr_tail else ""

    async def _write(self, message: Dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise MCPError("The plug-in is not running." + self._why())
        async with self._write_lock:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def request(self, message: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        try:
            await self._write(message)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"The plug-in did not answer within {int(timeout)} seconds.")
        finally:
            self._pending.pop(message["id"], None)

    async def notify(self, message: Dict[str, Any]) -> None:
        await self._write(message)

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            except Exception:
                pass
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class HttpTransport:
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: Optional[str] = None
        self._client = None

    async def start(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=15.0))

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, message: Dict[str, Any]):
        assert self._client is not None
        response = await self._client.post(self.url, json=message, headers=self._headers())
        session = response.headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if response.status_code in (401, 403):
            raise MCPError("The plug-in refused the request: check its access token.")
        if response.status_code >= 400:
            raise MCPError(f"The plug-in answered HTTP {response.status_code}: "
                           f"{response.text[:200]}")
        return response

    async def request(self, message: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        response = await asyncio.wait_for(self._post(message), timeout)
        kind = response.headers.get("content-type", "")
        if "text/event-stream" in kind:
            for block in response.text.split("\n\n"):
                data = "".join(line[5:].strip() for line in block.splitlines()
                               if line.startswith("data:"))
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("id") == message["id"]:
                    return parsed
            raise MCPError("The plug-in's event stream held no answer.")
        try:
            parsed = response.json()
        except ValueError:
            raise MCPError("The plug-in did not answer with JSON.")
        if isinstance(parsed, list):        # a batch: find ours
            parsed = next((m for m in parsed if m.get("id") == message["id"]), {})
        return parsed

    async def notify(self, message: Dict[str, Any]) -> None:
        await self._post(message)

    async def close(self) -> None:
        if self._client is not None:
            if self.session_id:
                try:
                    await self._client.delete(self.url, headers=self._headers())
                except Exception:
                    pass
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
class MCPClient:
    def __init__(self, transport) -> None:
        self.transport = transport
        self._ids = itertools.count(1)
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.instructions = ""

    async def _call(self, method: str, params: Optional[Dict[str, Any]] = None,
                    timeout: float = REQUEST_TIMEOUT) -> Any:
        message = _request(method, params or {}, next(self._ids))
        return _result(await self.transport.request(message, timeout))

    async def connect(self) -> Dict[str, Any]:
        await self.transport.start()
        result = await self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=START_TIMEOUT) or {}
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        await self.transport.notify(_request("notifications/initialized", None, None))
        return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(20):                     # pages, with a sane ceiling
            result = await self._call("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any],
                        timeout: float = REQUEST_TIMEOUT) -> Dict[str, Any]:
        return await self._call("tools/call", {"name": name, "arguments": arguments or {}},
                                timeout=timeout) or {}

    async def close(self) -> None:
        await self.transport.close()


def content_text(result: Dict[str, Any]) -> str:
    """The text of a tool result. Images and other media are named, not inlined."""
    parts: List[str] = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or f"[resource {resource.get('uri', '')}]"))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind}: {item.get('mimeType', 'unknown type')}]")
        else:
            parts.append(f"[{kind} content]")
    structured = result.get("structuredContent")
    if not parts and structured is not None:
        parts.append(json.dumps(structured, default=str))
    return "\n".join(parts).strip()
