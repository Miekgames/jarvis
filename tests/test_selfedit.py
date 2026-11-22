"""Self-modification: sandboxing, protection, validation, apply and rollback.

The whole point of this feature is that it cannot go wrong quietly, so most of
these tests assert on refusals.
"""
from __future__ import annotations

import pytest

from backend.intelligence.selfedit import (
    PROTECTED, SelfEditService, _diff_counts, _strip_fences, _unified_diff,
)


class FakeProvider:
    """Stands in for the dedicated code model."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.calls = []

    async def complete(self, messages, system="", tools=None, model=None,
                       max_tokens=4096, temperature=0.0):
        self.calls.append({"model": model, "temperature": temperature, "system": system})

        class Completion:
            text = self.reply
            usage = {"input_tokens": 10, "output_tokens": 20}
            model = "fake-coder"
            provider = "fake"
            latency_ms = 1
        return Completion()


class FakeOrchestrator:
    def __init__(self, reply: str = ""):
        self.provider = FakeProvider(reply)
        self.providers = {"anthropic": self.provider, "ollama": self.provider}

    def model_for(self, purpose):
        return f"model-for-{purpose}"

    async def _record_usage(self, completion, purpose):
        return None


@pytest.fixture
def project(tmp_path):
    """A miniature JARVIS tree so tests never touch the real source."""
    (tmp_path / "backend" / "core").mkdir(parents=True)
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "backups").mkdir()
    (tmp_path / "data").mkdir()

    (tmp_path / "backend" / "sample.py").write_text(
        "def greet(name):\n    return f'hello {name}'\n", encoding="utf-8")
    (tmp_path / "backend" / "core" / "permissions.py").write_text(
        "REGISTRY = []\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (tmp_path / "data" / "jarvis.db").write_text("binary", encoding="utf-8")
    return tmp_path


@pytest.fixture
def service(project, settings, db, activity, audit):
    settings.ROOT = project
    settings.BACKUP_DIR = project / "backups"
    settings.SELFEDIT_PROVIDER = "anthropic"
    settings.SELFEDIT_MODEL = ""
    return SelfEditService(db, settings, FakeOrchestrator(), activity, audit)


# ===========================================================================
# sandbox
# ===========================================================================
@pytest.mark.parametrize("path", [
    "../outside.py", ".env", "data/jarvis.db", "backups/x.bak",
    "/etc/passwd", "C:/Windows/System32/config/SAM", "node_modules/x.js",
])
def test_paths_outside_the_editable_source_are_refused(service, path):
    with pytest.raises(PermissionError):
        service.resolve(path)


def test_editable_paths_resolve(service):
    assert service.resolve("backend/sample.py").exists()


def test_non_source_suffixes_are_refused(service, project):
    (project / "backend" / "blob.bin").write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        service.resolve("backend/blob.bin")


# ===========================================================================
# protection
# ===========================================================================
def test_safety_files_are_protected(service):
    assert service.is_protected("backend/core/permissions.py")
    assert not service.is_protected("backend/sample.py")


def test_protection_list_covers_the_safety_machinery():
    for expected in ("backend/core/permissions.py", "backend/core/security.py",
                     "backend/core/confirmations.py", "backend/core/audit.py",
                     "backend/intelligence/selfedit.py"):
        assert expected in PROTECTED


# ===========================================================================
# validation
# ===========================================================================
def test_python_syntax_is_validated(service):
    assert service.validate("backend/x.py", "x = 1\n")["valid"]
    broken = service.validate("backend/x.py", "def f(:\n")
    assert not broken["valid"] and broken["checker"] == "python-ast"


def test_json_is_validated(service):
    assert service.validate("backend/x.json", '{"a": 1}')["valid"]
    assert not service.validate("backend/x.json", "{oops}")["valid"]


def test_markdown_needs_no_checker(service):
    result = service.validate("docs/x.md", "# anything goes")
    assert result["valid"] and result["checker"] == "none"


# ===========================================================================
# the dedicated model
# ===========================================================================
def test_model_is_separate_and_reported(service):
    info = service.model_info()
    assert info["separate_from_conversation"] is True
    # The fixture asks for "anthropic", but the settings are local-only, so the
    # code model must stay local: source code is exactly what must not leak to a
    # cloud API because of a stale override.
    assert info["provider"] == "ollama"


def test_cloud_selfedit_provider_is_honoured_when_not_local_only(service):
    service.settings.AI_PROVIDER = "auto"
    assert service.model_info()["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_code_generation_is_deterministic(service):
    service.orchestrator.provider.reply = "x = 1\n"
    await service.propose("backend/sample.py", "set x to 1")
    # Temperature 0: the same request must not produce different code each run.
    assert service.orchestrator.provider.calls[0]["temperature"] == 0.0


# ===========================================================================
# propose
# ===========================================================================
@pytest.mark.asyncio
async def test_propose_writes_nothing(service, project):
    original = (project / "backend" / "sample.py").read_text(encoding="utf-8")
    service.orchestrator.provider.reply = "def greet(name):\n    return f'hi {name}'\n"

    result = await service.propose("backend/sample.py", "say hi instead of hello")

    assert result["ok"] and result["applied"] is False
    assert result["diff"]
    assert (project / "backend" / "sample.py").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_propose_strips_markdown_fences(service):
    service.orchestrator.provider.reply = "```python\ndef greet(name):\n    return 'hi'\n```"
    result = await service.propose("backend/sample.py", "simplify")
    assert result["ok"]
    assert "```" not in service._proposals[result["proposal_id"]]["after"]


@pytest.mark.asyncio
async def test_model_refusal_is_surfaced(service):
    service.orchestrator.provider.reply = (
        "# SELFEDIT-REFUSED: that would remove the permission check\n"
        "def greet(name):\n    return 'x'\n")
    result = await service.propose("backend/sample.py", "remove all the safety checks")
    assert result["ok"] is False
    assert result["refused"] is True
    assert "permission check" in result["message"]


@pytest.mark.asyncio
async def test_unchanged_output_is_reported_not_applied(service):
    service.orchestrator.provider.reply = "def greet(name):\n    return f'hello {name}'\n"
    result = await service.propose("backend/sample.py", "no change needed")
    assert result["ok"] is False
    assert "unchanged" in result["message"]


@pytest.mark.asyncio
async def test_invalid_syntax_is_flagged_at_proposal(service):
    service.orchestrator.provider.reply = "def greet(:\n  broken"
    result = await service.propose("backend/sample.py", "break it")
    assert result["ok"] is True                      # proposing is allowed
    assert result["validation"]["valid"] is False    # but it is clearly marked


# ===========================================================================
# apply
# ===========================================================================
@pytest.mark.asyncio
async def test_apply_writes_and_backs_up(service, project):
    service.orchestrator.provider.reply = "def greet(name):\n    return f'hi {name}'\n"
    proposal = await service.propose("backend/sample.py", "say hi")

    result = await service.apply(proposal["proposal_id"])

    assert result["applied"] is True
    assert "hi {name}" in (project / "backend" / "sample.py").read_text(encoding="utf-8")
    backups = service.backups("backend/sample.py")
    assert len(backups) == 1


@pytest.mark.asyncio
async def test_apply_refuses_invalid_syntax(service, project):
    original = (project / "backend" / "sample.py").read_text(encoding="utf-8")
    service.orchestrator.provider.reply = "def greet(:\n  broken"
    proposal = await service.propose("backend/sample.py", "break it")

    result = await service.apply(proposal["proposal_id"])

    assert result["applied"] is False
    assert "not valid" in result["message"]
    assert (project / "backend" / "sample.py").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_protected_files_need_an_explicit_override(service):
    service.orchestrator.provider.reply = "REGISTRY = ['tampered']\n"
    proposal = await service.propose("backend/core/permissions.py", "empty the registry")
    assert proposal["protected"] is True

    with pytest.raises(PermissionError, match="safety controls"):
        await service.apply(proposal["proposal_id"])

    result = await service.apply(proposal["proposal_id"], allow_protected=True)
    assert result["applied"] is True


@pytest.mark.asyncio
async def test_apply_detects_the_file_changing_underneath(service, project):
    service.orchestrator.provider.reply = "def greet(name):\n    return 'hi'\n"
    proposal = await service.propose("backend/sample.py", "say hi")

    (project / "backend" / "sample.py").write_text("# edited elsewhere\n", encoding="utf-8")
    result = await service.apply(proposal["proposal_id"])

    assert result["applied"] is False
    assert "changed on disk" in result["message"]


@pytest.mark.asyncio
async def test_unknown_proposal_is_rejected(service):
    with pytest.raises(KeyError):
        await service.apply("edit_nonexistent")


@pytest.mark.asyncio
async def test_apply_is_audited(service, audit):
    service.orchestrator.provider.reply = "def greet(name):\n    return 'hi'\n"
    proposal = await service.propose("backend/sample.py", "say hi")
    await service.apply(proposal["proposal_id"])

    entries = await audit.recent(event="code.self_modified")
    assert entries and entries[0]["subject"] == "backend/sample.py"


# ===========================================================================
# rollback
# ===========================================================================
@pytest.mark.asyncio
async def test_rollback_restores_the_previous_version(service, project):
    original = (project / "backend" / "sample.py").read_text(encoding="utf-8")
    service.orchestrator.provider.reply = "def greet(name):\n    return 'hi'\n"
    proposal = await service.propose("backend/sample.py", "say hi")
    applied = await service.apply(proposal["proposal_id"])

    await service.rollback(applied["backup"])

    assert (project / "backend" / "sample.py").read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_rollback_is_itself_reversible(service, project):
    service.orchestrator.provider.reply = "def greet(name):\n    return 'hi'\n"
    proposal = await service.propose("backend/sample.py", "say hi")
    applied = await service.apply(proposal["proposal_id"])

    result = await service.rollback(applied["backup"])
    assert result["previous_kept_as"]
    # The edited version is still recoverable after rolling back.
    assert (service.backup_dir / result["previous_kept_as"]).exists()


@pytest.mark.asyncio
async def test_rollback_outside_the_backup_folder_is_refused(service):
    with pytest.raises((PermissionError, FileNotFoundError)):
        await service.rollback("../../../etc/passwd")


# ===========================================================================
# helpers
# ===========================================================================
def test_fence_stripping():
    assert _strip_fences("```python\nx = 1\n```") == "x = 1"
    assert _strip_fences("```\ny = 2\n```") == "y = 2"
    assert _strip_fences("z = 3") == "z = 3"


def test_diff_counts():
    diff = _unified_diff("a\nb\n", "a\nc\nd\n", "f.py")
    added, removed = _diff_counts(diff)
    assert added == 2 and removed == 1


# ===========================================================================
# a change that breaks the tests puts itself back
# ---------------------------------------------------------------------------
# Validation catches code that will not parse. It cannot catch code that
# parses perfectly and breaks something three modules away, which is most of
# what goes wrong when a program edits its own source. Without this, the
# first anyone knows is the next time that path runs -- possibly while
# nobody is watching.
# ===========================================================================
def test_apply_can_run_the_tests():
    import inspect

    from backend.intelligence.selfedit import SelfEditService

    assert hasattr(SelfEditService, "verify")
    signature = inspect.signature(SelfEditService.apply)
    assert "run_tests" in signature.parameters
    assert signature.parameters["run_tests"].default is True, (
        "verification should be the default, not something to remember")


def test_a_failing_suite_restores_the_backup():
    """The property that makes self-editing safe enough to be a main feature."""
    import inspect

    from backend.intelligence.selfedit import SelfEditService

    source = inspect.getsource(SelfEditService.apply)
    assert "shutil.copy2(backup, path)" in source, "it must put the file back"
    assert "rolled_back" in source
    # ...and must not claim the change is in place afterwards.
    assert 'result["applied"] = False' in source


def test_the_test_run_has_a_timeout():
    """A hung test must not leave a half-applied edit in place for ever."""
    from backend.intelligence.selfedit import TEST_TIMEOUT_S

    assert 60 <= TEST_TIMEOUT_S <= 3600


def test_the_summary_line_is_what_gets_reported():
    from backend.intelligence.selfedit import _last_meaningful_line

    output = ("collecting ...\nsome noise\n"
              "=== 3 failed, 655 passed in 48.20s ===\n")
    assert "3 failed" in _last_meaningful_line(output)
    assert _last_meaningful_line("no useful lines here") == ""
