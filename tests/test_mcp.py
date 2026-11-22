"""MCP plug-ins, against a real server process and a mocked HTTP one."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from backend.integrations.mcp_client import (HttpTransport, MCPClient, MCPError,
                                             StdioTransport, content_text)
from backend.integrations.mcp_manager import MCPManager, server_name, tool_name

SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


@pytest.fixture
def stdio_config():
    return {"command": sys.executable, "args": [SERVER]}


# ===========================================================================
# the client, over a real subprocess
# ===========================================================================
@pytest.mark.asyncio
async def test_handshake_list_and_call_through_a_noisy_server():
    client = MCPClient(StdioTransport(sys.executable, [SERVER]))
    try:
        info = await client.connect()
        assert info["serverInfo"]["name"] == "fake"
        tools = await client.list_tools()
        assert [t["name"] for t in tools] == ["echo", "add", "sneaky", "fails"]

        # The banner and the unprompted notification before each answer are
        # skipped; only the response with the request's id counts.
        result = await client.call_tool("echo", {"text": "hello"})
        assert content_text(result) == "hello"
        assert content_text(await client.call_tool("add", {"a": 2, "b": 3})) == "5"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_server_error_comes_back_as_an_error():
    client = MCPClient(StdioTransport(sys.executable, [SERVER]))
    try:
        await client.connect()
        with pytest.raises(MCPError) as raised:
            await client.call_tool("nope", {})
        assert "no tool nope" in str(raised.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_missing_program_says_so():
    client = MCPClient(StdioTransport("definitely-not-a-real-program-xyz", []))
    with pytest.raises(MCPError) as raised:
        await client.connect()
    assert "Could not find the program" in str(raised.value)


# ===========================================================================
# the client, over HTTP (mocked)
# ===========================================================================
@pytest.mark.asyncio
async def test_http_tracks_the_session_and_reads_event_streams():
    import httpx

    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body.get("method"), request.headers.get("mcp-session-id"),
                     request.headers.get("authorization")))
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "sess-1"}, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": "2025-03-26", "serverInfo": {"name": "remote"}}})
        if body["method"] == "tools/list":
            stream = ("event: message\ndata: " + json.dumps({
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"tools": [{"name": "lights", "description": "Turn lights on"}]}}) + "\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=stream.encode())
        return httpx.Response(404)

    transport = HttpTransport("https://mcp.example.com/mcp", headers={"Authorization": "Bearer t0k"})
    client = MCPClient(transport)
    await transport.start()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    transport.start = _noop
    await client.connect()
    tools = await client.list_tools()
    await client.close()

    assert tools[0]["name"] == "lights"
    assert seen[0] == ("initialize", None, "Bearer t0k")
    # Every request after the handshake carries the session id.
    assert all(session == "sess-1" for _, session, _ in seen[1:])


async def _noop():
    return None


# ===========================================================================
# the manager: servers become JARVIS tools
# ===========================================================================
@pytest.fixture
def manager(tmp_path, registry):
    return MCPManager(tmp_path, registry)


@pytest.mark.asyncio
async def test_a_servers_tools_become_jarvis_tools(manager, registry, stdio_config):
    status = await manager.add("My Tools", stdio_config)
    try:
        assert status["state"] == "connected", status["error"]
        names = {t["name"] for t in status["tools"]}
        assert names == {"mcp_my_tools_echo", "mcp_my_tools_add", "mcp_my_tools_fails"}
        assert registry.get("mcp_my_tools_echo") is not None

        result = await registry.dispatch("mcp_my_tools_echo", {"text": "hi there"},
                                         skip_confirmation=True)
        assert result.ok and result.to_model_string() == "hi there"

        failed = await registry.dispatch("mcp_my_tools_fails", {}, skip_confirmation=True)
        assert not failed.ok and "the thing broke" in failed.message
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_a_tool_whose_description_gives_orders_is_not_offered(manager, registry, stdio_config):
    status = await manager.add("tools", stdio_config)
    try:
        assert registry.get("mcp_tools_sneaky") is None
        assert status["skipped"][0]["tool"] == "sneaky"
        assert "override" in status["skipped"][0]["reason"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plugin_tools_ask_first_unless_trusted(manager, registry, stdio_config):
    from backend.core.permissions import Risk

    await manager.add("tools", stdio_config)
    try:
        assert registry.get("mcp_tools_echo").risk == Risk.MEDIUM
        await manager.update("tools", trusted=True)
        assert registry.get("mcp_tools_echo").risk == Risk.LOW
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_switching_off_and_removing_take_the_tools_away(manager, registry, stdio_config):
    await manager.add("tools", stdio_config)
    await manager.update("tools", enabled=False)
    assert registry.get("mcp_tools_echo") is None
    assert manager.describe("tools")["state"] == "off"

    await manager.update("tools", enabled=True)
    assert registry.get("mcp_tools_echo") is not None
    assert await manager.remove("tools")
    assert registry.get("mcp_tools_echo") is None
    assert manager.status() == []


@pytest.mark.asyncio
async def test_a_broken_server_does_not_stop_the_others(manager, registry, stdio_config):
    manager._write({
        "good": MCPManager.normalise(stdio_config),
        "broken": MCPManager.normalise({"command": "no-such-program-abc"}),
    })
    await manager.start()
    try:
        states = {s["name"]: s["state"] for s in manager.status()}
        assert states == {"broken": "error", "good": "connected"}
        assert "Could not find" in manager.describe("broken")["error"]
    finally:
        await manager.stop()


def test_secrets_are_sealed_on_disk_and_never_shown(manager):
    config = MCPManager.normalise({"command": "x", "env": {"API_KEY": "sk-secret-value-123456",
                                                             "OTHER": "${HOME_TOKEN}"},
                                   "token": "abc123"})
    manager._write({"svc": config})
    on_disk = manager.path.read_text(encoding="utf-8")
    assert "sk-secret-value-123456" not in on_disk and "abc123" not in on_disk
    assert "${HOME_TOKEN}" in on_disk, "a reference is kept as a reference"

    shown = json.dumps(manager.describe("svc"))
    assert "sk-secret" not in shown and "abc123" not in shown
    assert manager.describe("svc")["secrets"]["env OTHER"] == "from ${HOME_TOKEN}"


def test_claude_desktop_style_json_can_be_pasted():
    servers = MCPManager.parse_paste(json.dumps({"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/x"]},
        "remote": {"url": "https://example.com/mcp"},
    }}))
    assert set(servers) == {"filesystem", "remote"}
    assert MCPManager.normalise(servers["remote"])["transport"] == "http"
    assert MCPManager.normalise(servers["filesystem"])["transport"] == "stdio"


@pytest.mark.parametrize("bad, message", [
    ("not json", "not valid JSON"),
    ('{"command": "x"}', "Give the server a name"),
    ("{}", "No servers"),
])
def test_bad_pastes_say_why(bad, message):
    with pytest.raises(ValueError) as raised:
        MCPManager.parse_paste(bad)
    assert message in str(raised.value)


def test_names_are_made_safe():
    assert server_name("My GitHub!") == "my_github"
    assert tool_name("my_github", "create-issue") == "mcp_my_github_create_issue"


def test_plugin_tools_are_held_while_nobody_is_here(tmp_path):
    from backend.autonomy.guard import Guard, Verdict

    decision = Guard(tmp_path).check("mcp_tools_echo", {"text": "x"}, risk="low")
    assert decision.verdict is Verdict.HOLD
