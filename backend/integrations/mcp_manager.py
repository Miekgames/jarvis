"""MCP plug-ins: the servers JARVIS is connected to, and their tools.

Each server the owner adds is kept in `mcp.json` in the data folder, in the
same shape Claude Desktop and most MCP documentation use, so a snippet from
a server's README can be pasted straight in. Its tools become JARVIS tools
named `mcp_<server>_<tool>`, offered to the model like any other.

What keeps this safe enough to switch on:

  * **Asked before each use.** Plug-in tools need a confirmation every time
    unless the owner marks that server trusted -- a plug-in can do anything
    its author wrote it to.
  * **Never unattended.** The While away runner holds every plug-in call for
    the owner rather than running it (see `autonomy.guard`).
  * **Screened both ways.** Results go through the same injection screening
    as every other tool result. Tool *descriptions* are screened too: they
    go into the model's instructions, so a tool whose description tries to
    give orders is not offered at all.
  * **Secrets sealed.** Tokens and environment values are encrypted with
    Windows DPAPI, or written as `${NAME}` references to .env so JARVIS
    never stores them (see `core.secrets`). They are never sent back to the
    browser.

Ported idea: OpenJarvis ``mcp/loader.py`` -- per-server isolation (one
broken server never stops the rest), include/exclude tool filters, holding
on to live clients for as long as their tools are registered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import secrets
from .mcp_client import HttpTransport, MCPClient, MCPError, StdioTransport, content_text

logger = logging.getLogger("jarvis.mcp")

CONFIG_FILE = "mcp.json"
PREFIX = "mcp_"
_SAFE = re.compile(r"[^a-z0-9_]+")


def server_name(raw: str) -> str:
    name = _SAFE.sub("_", (raw or "").strip().lower()).strip("_")
    return name[:24] or "server"


def tool_name(server: str, tool: str) -> str:
    return (PREFIX + server + "_" + _SAFE.sub("_", tool.lower()).strip("_"))[:64]


class _Server:
    def __init__(self, name: str, config: Dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.client: Optional[MCPClient] = None
        self.state = "off"            # off | connecting | connected | error
        self.error = ""
        self.tools: List[Dict[str, Any]] = []
        self.skipped: List[Dict[str, str]] = []
        self.registered: List[str] = []


class MCPManager:
    def __init__(self, root: Path, registry, activity=None) -> None:
        self.root = Path(root)
        self.registry = registry
        self.activity = activity
        self.servers: Dict[str, _Server] = {}
        self._lock = asyncio.Lock()

    # -- configuration ------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.root / CONFIG_FILE

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return dict(data.get("servers") or {})
        except (OSError, ValueError):
            return {}

    def _write(self, servers: Dict[str, Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"servers": servers}, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        """One server's settings, from any of the usual ways of writing them."""
        if not isinstance(raw, dict):
            raise ValueError("A server's settings must be an object.")
        url = str(raw.get("url") or raw.get("serverUrl") or "").strip()
        command = str(raw.get("command") or "").strip()
        if not url and not command:
            raise ValueError("A server needs either a command to run or a url.")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("The url must start with http:// or https://")
        args = raw.get("args") or []
        if not isinstance(args, list):
            raise ValueError("args must be a list.")
        env = raw.get("env") or {}
        headers = dict(raw.get("headers") or {})
        token = str(raw.get("token") or "").strip()
        if token:
            headers["Authorization"] = token if token.lower().startswith("bearer ") \
                else f"Bearer {token}"
        return {
            "transport": "http" if url else "stdio",
            "command": command, "args": [str(a) for a in args],
            "cwd": str(raw.get("cwd") or ""),
            "url": url,
            # Sealed before they are ever written down.
            "env": {str(k): secrets.seal(str(v)) for k, v in dict(env).items()},
            "headers": {str(k): secrets.seal(str(v)) for k, v in headers.items()},
            "enabled": bool(raw.get("enabled", True)),
            "trusted": bool(raw.get("trusted", False)),
            "include": [str(t) for t in raw.get("include_tools") or raw.get("include") or []],
            "exclude": [str(t) for t in raw.get("exclude_tools") or raw.get("exclude") or []],
        }

    @staticmethod
    def parse_paste(text: str) -> Dict[str, Dict[str, Any]]:
        """Servers from pasted JSON: Claude Desktop's {"mcpServers": {...}},
        {"servers": {...}}, or a bare {"name": {...}} map."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"That is not valid JSON: {exc}")
        if not isinstance(data, dict):
            raise ValueError("Paste a JSON object.")
        for key in ("mcpServers", "servers", "mcp_servers"):
            if isinstance(data.get(key), dict):
                data = data[key]
                break
        if "command" in data or "url" in data:
            raise ValueError('Give the server a name: {"my-server": {"command": ...}}')
        if not data:
            raise ValueError("No servers found in that.")
        return data

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Connect every enabled server. One failing never stops the rest."""
        await asyncio.gather(*(self.connect(name) for name, cfg in self._read().items()
                               if cfg.get("enabled", True)), return_exceptions=True)

    async def connect(self, name: str) -> Dict[str, Any]:
        async with self._lock:
            config = self._read().get(name)
            if config is None:
                raise KeyError(name)
            await self._disconnect(name)
            server = _Server(name, config)
            self.servers[name] = server
            server.state = "connecting"
        try:
            transport = self._transport(config)
            client = MCPClient(transport)
            await asyncio.wait_for(client.connect(), 45)
            tools = await client.list_tools()
            server.client = client
            self._register(server, tools)
            server.state = "connected"
            logger.info("MCP server %s connected: %d tool(s)", name, len(server.registered))
            await self._note(f"Plug-in '{name}' connected with {len(server.registered)} tool(s).")
        except Exception as exc:
            server.state = "error"
            server.error = _plain(exc)
            logger.warning("MCP server %s failed: %s", name, server.error)
            try:
                if server.client is None:
                    await transport.close()          # type: ignore[name-defined]
            except Exception:
                pass
            await self._note(f"Plug-in '{name}' could not connect: {server.error}", "warn")
        return self.describe(name)

    def _transport(self, config: Dict[str, Any]):
        if config.get("transport") == "http":
            headers = {k: secrets.unseal(v) for k, v in (config.get("headers") or {}).items()}
            return HttpTransport(config["url"], headers=headers)
        env = {k: secrets.unseal(v) for k, v in (config.get("env") or {}).items()}
        return StdioTransport(config["command"], config.get("args") or [], env=env,
                              cwd=config.get("cwd") or None)

    def _register(self, server: _Server, tools: List[Dict[str, Any]]) -> None:
        from ..ai.tools.registry import Tool, fail, ok
        from ..core.permissions import Risk
        from ..core.screening import screener

        include = set(server.config.get("include") or [])
        exclude = set(server.config.get("exclude") or [])
        trusted = bool(server.config.get("trusted"))
        for spec in tools:
            original = str(spec.get("name") or "")
            if not original or (include and original not in include) or original in exclude:
                continue
            description = " ".join(str(spec.get("description") or original).split())
            # The description goes into the model's instructions.
            found = screener.scan(description)
            if found:
                server.skipped.append({"tool": original,
                                       "reason": "its description " + found[0].description})
                continue
            name = tool_name(server.name, original)
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
            if not isinstance(schema, dict) or schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}

            def handler_for(remote: str):
                async def handler(ctx, **arguments):
                    client = server.client
                    if client is None or server.state != "connected":
                        return fail(f"The '{server.name}' plug-in is not connected.")
                    try:
                        result = await client.call_tool(remote, arguments)
                    except MCPError as exc:
                        return fail(f"The '{server.name}' plug-in failed: {exc}")
                    text = content_text(result) or "(no output)"
                    if result.get("isError"):
                        return fail(text)
                    return ok(text, plugin=server.name, tool=remote)
                return handler

            tool = Tool(
                name, f"[{server.name} plug-in] {description}"[:600], schema,
                handler_for(original), permission="",
                risk=Risk.LOW if trusted else Risk.MEDIUM,
                confirm_summary=(lambda a, s=server.name, t=original:
                                 f"Let the {s} plug-in run '{t}'"
                                 + (f" with {json.dumps(a)[:160]}" if a else "") + "?"),
                category=f"plugin mcp {server.name}", timeout=180.0)
            self.registry.register(tool)
            server.registered.append(name)
            server.tools.append({"name": name, "tool": original, "description": description[:300]})

    async def _disconnect(self, name: str) -> None:
        server = self.servers.pop(name, None)
        if server is None:
            return
        for registered in server.registered:
            self.registry.unregister(registered)
        if server.client is not None:
            try:
                await server.client.close()
            except Exception:
                logger.debug("closing MCP server %s failed", name, exc_info=True)

    async def stop(self) -> None:
        for name in list(self.servers):
            await self._disconnect(name)

    # -- changes from the interface -------------------------------------------
    async def add(self, name: str, raw: Dict[str, Any], connect: bool = True) -> Dict[str, Any]:
        name = server_name(name)
        servers = self._read()
        servers[name] = self.normalise(raw)
        self._write(servers)
        if connect and servers[name]["enabled"]:
            return await self.connect(name)
        return self.describe(name)

    async def import_text(self, text: str) -> List[Dict[str, Any]]:
        added = []
        for name, raw in self.parse_paste(text).items():
            added.append(await self.add(name, raw))
        return added

    async def update(self, name: str, enabled: Optional[bool] = None,
                     trusted: Optional[bool] = None) -> Dict[str, Any]:
        servers = self._read()
        if name not in servers:
            raise KeyError(name)
        if enabled is not None:
            servers[name]["enabled"] = enabled
        if trusted is not None:
            servers[name]["trusted"] = trusted
        self._write(servers)
        if servers[name]["enabled"]:
            return await self.connect(name)
        await self._disconnect(name)
        return self.describe(name)

    async def remove(self, name: str) -> bool:
        servers = self._read()
        if name not in servers:
            return False
        await self._disconnect(name)
        servers.pop(name)
        self._write(servers)
        return True

    # -- what the interface shows --------------------------------------------
    def describe(self, name: str) -> Dict[str, Any]:
        config = self._read().get(name) or {}
        server = self.servers.get(name)
        return {
            "name": name,
            "transport": config.get("transport", ""),
            "command": " ".join([config.get("command", "")] + list(config.get("args") or [])).strip(),
            "url": config.get("url", ""),
            "enabled": bool(config.get("enabled", True)),
            "trusted": bool(config.get("trusted", False)),
            "state": server.state if server else ("off" if not config.get("enabled", True)
                                                  else "not started"),
            "error": server.error if server else "",
            "server": (server.client.server_info if server and server.client else {}),
            "tools": server.tools if server else [],
            "skipped": server.skipped if server else [],
            # How each secret is held -- never its value.
            "secrets": {**{f"env {k}": secrets.describe(v)
                           for k, v in (config.get("env") or {}).items()},
                        **{f"header {k}": secrets.describe(v)
                           for k, v in (config.get("headers") or {}).items()}},
        }

    def status(self) -> List[Dict[str, Any]]:
        return [self.describe(name) for name in sorted(self._read())]

    async def _note(self, message: str, level: str = "info") -> None:
        if self.activity is None:
            return
        try:
            await self.activity.record(message, source="plugins", level=level)
        except Exception:
            pass


def _plain(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError):
        return "it did not answer in time"
    return text[:300]
