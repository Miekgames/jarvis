"""Tool dispatch (gating, confirmation, error containment) and the sandboxed
file service."""
from __future__ import annotations

import asyncio

import pytest

from backend.ai.tools.registry import Tool, fail, ok
from backend.core.permissions import Risk
from backend.services.files import FileService, PathNotAllowed


# ===========================================================================
# tool dispatch
# ===========================================================================
def register(registry, name, handler, **kwargs):
    registry.register(Tool(name, "test tool", {"type": "object", "properties": {}}, handler, **kwargs))


@pytest.mark.asyncio
async def test_tool_runs_and_returns_structured_result(registry):
    async def handler(ctx, value: int = 1):
        return ok({"doubled": value * 2}, "done")

    register(registry, "double", handler)
    result = await registry.dispatch("double", {"value": 21})

    assert result.ok
    assert result.data == {"doubled": 42}


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_not_raised(registry):
    result = await registry.dispatch("nope", {})
    assert not result.ok
    assert "nope" in result.message
    # The message is fed back to the model, so it has to say what to do next.
    # A bare "unknown tool" leaves a small local model repeating the same
    # invented call until the round limit, and the user gets no answer.
    assert "answer" in result.message.lower()


@pytest.mark.asyncio
async def test_a_disabled_permission_stops_the_handler(registry, permissions):
    """A capability that is switched off never runs, whatever the model asks."""
    ran = {"yes": False}

    async def handler(ctx):
        ran["yes"] = True
        return ok()

    register(registry, "danger", handler, permission="files.delete", risk=Risk.LOW)
    await permissions.set("files.delete", False)
    result = await registry.dispatch("danger", {})

    assert not result.ok
    assert not ran["yes"]


@pytest.mark.asyncio
async def test_enabled_permission_allows_execution(registry, permissions):
    async def handler(ctx):
        return ok("ran")

    register(registry, "allowed", handler, permission="files.delete")
    await permissions.set("files.delete", True)

    result = await registry.dispatch("allowed", {})
    assert result.ok and result.data == "ran"


@pytest.mark.asyncio
async def test_switched_off_tools_are_not_offered_to_the_model(registry, permissions):
    """A model is not shown a tool it would only be refused."""
    async def handler(ctx):
        return ok()

    register(registry, "reader", handler, permission="files.read")
    register(registry, "deleter", handler, permission="files.delete")
    await permissions.set("files.delete", False)

    names = {schema["name"] for schema in registry.schemas()}
    assert "reader" in names and "deleter" not in names


@pytest.mark.asyncio
async def test_high_risk_tool_waits_for_confirmation(registry, permissions, confirmations):
    ran = {"yes": False}

    async def handler(ctx):
        ran["yes"] = True
        return ok("done")

    register(registry, "risky", handler, permission="files.read", risk=Risk.HIGH)

    call = asyncio.create_task(registry.dispatch("risky", {}))
    await asyncio.sleep(0.25)
    assert not ran["yes"], "the tool must not run before the user answers"

    pending = await confirmations.pending()
    await confirmations.resolve(pending[0]["id"], approved=True)

    result = await call
    assert result.ok and ran["yes"]


@pytest.mark.asyncio
async def test_rejected_confirmation_reports_honestly(registry, permissions, confirmations):
    ran = {"yes": False}

    async def handler(ctx):
        ran["yes"] = True
        return ok()

    register(registry, "risky", handler, permission="files.read", risk=Risk.CRITICAL)

    call = asyncio.create_task(registry.dispatch("risky", {}))
    await asyncio.sleep(0.25)
    pending = await confirmations.pending()
    await confirmations.resolve(pending[0]["id"], approved=False)

    result = await call
    assert not result.ok
    assert not ran["yes"]
    assert "did not approve" in result.message
    assert "not performed" in result.message


@pytest.mark.asyncio
async def test_handler_exception_becomes_an_error_result(registry):
    async def handler(ctx):
        raise ValueError("something broke")

    register(registry, "broken", handler)
    result = await registry.dispatch("broken", {})

    assert not result.ok
    assert "ValueError" in result.message
    assert "something broke" in result.message


@pytest.mark.asyncio
async def test_tool_timeout_is_contained(registry):
    async def handler(ctx):
        await asyncio.sleep(10)

    register(registry, "slow", handler, timeout=0.3)
    result = await registry.dispatch("slow", {})

    assert not result.ok
    assert "timed out" in result.message


@pytest.mark.asyncio
async def test_unexpected_arguments_are_dropped(registry):
    async def handler(ctx, expected: str = ""):
        return ok({"expected": expected})

    register(registry, "strict", handler)
    result = await registry.dispatch("strict", {"expected": "yes", "hallucinated": "no"})

    assert result.ok
    assert result.data == {"expected": "yes"}


@pytest.mark.asyncio
async def test_missing_integration_short_circuits(registry):
    class Registry:
        def is_connected(self, key):
            return False

    registry.context._services["integrations"] = Registry()
    registry.context.integrations = Registry()

    async def handler(ctx):
        return ok()

    register(registry, "needs_github", handler, requires_integration="github")
    result = await registry.dispatch("needs_github", {})

    assert not result.ok
    assert "github" in result.message
    assert "not connected" in result.message


# ===========================================================================
# file sandbox
# ===========================================================================
@pytest.fixture
def files(settings, tmp_path):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "notes.txt").write_text("hello jarvis", encoding="utf-8")
    return FileService(settings)


def test_paths_inside_the_root_resolve(files, tmp_path):
    assert files.resolve(str(tmp_path / "docs" / "notes.txt")).exists()


def test_paths_outside_the_root_are_refused(files):
    for path in ("C:\\Windows\\System32\\config\\SAM", "/etc/passwd"):
        with pytest.raises(PathNotAllowed):
            files.resolve(path)


def test_traversal_out_of_the_root_is_refused(files, tmp_path):
    with pytest.raises(PathNotAllowed):
        files.resolve(str(tmp_path / ".." / ".." / "secrets.txt"))


def test_empty_path_is_refused(files):
    with pytest.raises(PathNotAllowed):
        files.resolve("")


@pytest.mark.asyncio
async def test_read_and_write_round_trip(files, tmp_path):
    target = str(tmp_path / "docs" / "written.txt")
    await files.write(target, "written by a test")

    result = await files.read(target)
    assert result["text"] == "written by a test"


@pytest.mark.asyncio
async def test_overwrite_keeps_the_previous_version(files, tmp_path):
    from pathlib import Path

    target = str(tmp_path / "docs" / "notes.txt")
    result = await files.write(target, "replaced")

    assert result["previous_version"], "an overwrite should be recoverable"
    assert Path(result["previous_version"]).read_text(encoding="utf-8") == "hello jarvis"


@pytest.mark.asyncio
async def test_delete_moves_to_recoverable_trash(files, tmp_path):
    from pathlib import Path

    target = tmp_path / "docs" / "notes.txt"
    result = await files.delete(str(target))

    assert not target.exists()
    recovered = Path(result["recoverable_at"])
    assert recovered.exists()
    assert recovered.read_text(encoding="utf-8") == "hello jarvis"


@pytest.mark.asyncio
async def test_search_finds_by_name_and_content(files, tmp_path):
    (tmp_path / "docs" / "invoice_2026.txt").write_text("total due: 42", encoding="utf-8")

    by_name = await files.search("invoice")
    assert any("invoice_2026" in hit["name"] for hit in by_name)

    by_content = await files.search("total due", content=True)
    assert any("invoice_2026" in hit["name"] for hit in by_content)


@pytest.mark.asyncio
async def test_search_outside_the_root_is_refused(files):
    with pytest.raises(PathNotAllowed):
        await files.search("anything", root="C:\\Windows")


@pytest.mark.asyncio
async def test_duplicate_detection(files, tmp_path):
    (tmp_path / "a.bin").write_bytes(b"identical payload" * 100)
    (tmp_path / "b.bin").write_bytes(b"identical payload" * 100)
    (tmp_path / "c.bin").write_bytes(b"different payload!" * 100)

    groups = await files.find_duplicates()
    assert any(group["count"] == 2 for group in groups)


@pytest.mark.asyncio
async def test_unknown_tool_suggests_close_matches(registry):
    """A misspelled tool name should point at the real one, not just fail."""
    register(registry, "system_status", lambda ctx: None)
    result = await registry.dispatch("system_stats", {})
    assert not result.ok
    assert "system_status" in result.message


# ===========================================================================
# an empty setting is not an empty list
# ---------------------------------------------------------------------------
# A bare `JARVIS_FILE_ROOTS=` line in .env -- the way anyone writes "leave this
# one alone" -- left zero permitted folders, and every file tool broke. Asked
# over voice to write a file, JARVIS called write_file and got back
# "IndexError: list index out of range", which it reported to the user and
# could do nothing with: nothing in that message points at a blank line in a
# configuration file.
# ===========================================================================
def test_an_empty_env_var_falls_back_to_the_default(monkeypatch):
    from backend.config import _list

    monkeypatch.setenv("JARVIS_TEST_LIST", "")
    assert _list("JARVIS_TEST_LIST", "/home/someone") == ["/home/someone"]

    # Whitespace-only counts as empty too -- a trailing space after the '='
    # is invisible and should not mean something different.
    monkeypatch.setenv("JARVIS_TEST_LIST", "   ")
    assert _list("JARVIS_TEST_LIST", "/home/someone") == ["/home/someone"]


def test_an_absent_env_var_still_falls_back(monkeypatch):
    from backend.config import _list

    monkeypatch.delenv("JARVIS_TEST_LIST", raising=False)
    assert _list("JARVIS_TEST_LIST", "/home/someone") == ["/home/someone"]


def test_a_real_value_still_wins(monkeypatch):
    from backend.config import _list

    monkeypatch.setenv("JARVIS_TEST_LIST", "/one, /two ,/three")
    assert _list("JARVIS_TEST_LIST", "/default") == ["/one", "/two", "/three"]


def test_no_roots_gives_a_readable_error_not_an_indexerror(tmp_path):
    """The second line of defence, for a genuinely root-less configuration."""
    from backend.services.files import FileService, PathNotAllowed

    class _Settings:
        FILE_ROOTS: list = []
        DATA_DIR = tmp_path

    service = FileService(_Settings())
    with pytest.raises(PathNotAllowed) as caught:
        service.resolve("some-file.txt")

    message = str(caught.value)
    assert "JARVIS_FILE_ROOTS" in message, "it has to say which setting to change"
    assert "index" not in message.lower(), "an IndexError tells the user nothing"


def test_a_relative_path_lands_in_the_first_root(tmp_path):
    from backend.services.files import FileService

    class _Settings:
        FILE_ROOTS = [str(tmp_path)]
        DATA_DIR = tmp_path

    service = FileService(_Settings())
    assert service.resolve("JARVIS Capabilities.txt") == (
        tmp_path.resolve() / "JARVIS Capabilities.txt")
