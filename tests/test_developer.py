"""Code intelligence and the developer workspace."""
from __future__ import annotations

import pytest

from backend.intelligence.code import (
    CodeIntelligence, _extract_python, _extract_ts, _parse_trace, _static_checks,
)
from backend.intelligence.devtools import (
    DevTools, _diff_files, _diff_stats, _parse_build_errors, _parse_status, _parse_test_output,
)


class FakeOrchestrator:
    def __init__(self, reply: str = "explained"):
        self.reply = reply
        self.prompts = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, use_tools=False):
        self.prompts.append(prompt)
        return self.reply


@pytest.fixture
def repo(tmp_path, settings):
    """A small project inside the allowed roots."""
    settings.FILE_ROOTS = [str(tmp_path)]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        '"""App."""\n'
        "import os\n"
        "from pathlib import Path\n\n"
        "MAX_SIZE = 100\n\n\n"
        "class Engine:\n"
        '    """Runs things."""\n\n'
        "    def start(self, name):\n"
        '        """Start it."""\n'
        "        return name\n\n\n"
        "def helper(a, b=2):\n"
        "    return a + b\n",
        encoding="utf-8")
    (tmp_path / "src" / "ui.tsx").write_text(
        "import { useState } from 'react';\n"
        "export interface Props { id: string }\n"
        "export function Panel(props: Props) { return null; }\n"
        "export const HANDLER = 1;\n",
        encoding="utf-8")
    return tmp_path


@pytest.fixture
async def code(db, settings, activity, repo):
    return CodeIntelligence(db, settings, FakeOrchestrator(), activity)


@pytest.fixture
async def indexed(code, repo):
    workspace = await code.add_workspace(str(repo), "test-repo")
    await code.index(workspace["id"])
    return workspace


# ===========================================================================
# extraction
# ===========================================================================
def test_python_symbols_and_imports():
    source = open(__file__, encoding="utf-8").read()
    symbols, imports = _extract_python(source)
    names = {s["name"] for s in symbols}
    assert "FakeOrchestrator" in names
    assert any(s["kind"] == "class" for s in symbols)
    assert any(i.startswith("backend") for i in imports)


def test_python_extraction_records_line_numbers():
    symbols, _ = _extract_python("def a():\n    pass\n\n\ndef b():\n    pass\n")
    assert [(s["name"], s["line"]) for s in symbols] == [("a", 1), ("b", 5)]


def test_python_extraction_survives_broken_syntax():
    assert _extract_python("def broken(:\n") == ([], [])


def test_typescript_extraction():
    symbols, imports = _extract_ts(
        "import { x } from './a';\n"
        "export interface Thing { id: string }\n"
        "export function go() {}\n"
        "export const Comp = (props: P) => null;\n"
        "export const LIMIT = 5;\n")
    kinds = {s["name"]: s["kind"] for s in symbols}
    assert kinds["Thing"] == "interface"
    assert kinds["go"] == "function"
    assert kinds["Comp"] == "function"
    assert kinds["LIMIT"] == "constant"
    assert imports == ["./a"]


# ===========================================================================
# indexing and queries
# ===========================================================================
@pytest.mark.asyncio
async def test_workspace_must_be_inside_allowed_roots(code):
    with pytest.raises((PermissionError, NotADirectoryError)):
        await code.add_workspace("C:/Windows/System32")


@pytest.mark.asyncio
async def test_index_records_files_and_symbols(code, repo, indexed):
    workspace = await code.get_workspace(indexed["id"])
    assert workspace["file_count"] == 2
    assert workspace["symbol_count"] >= 6
    assert workspace["indexed_at"]


@pytest.mark.asyncio
async def test_find_definition_returns_exact_location(code, indexed):
    hits = await code.find_symbol("Engine", indexed["id"])
    assert hits
    assert hits[0]["kind"] == "class"
    assert hits[0]["path"] == "src/app.py"
    assert hits[0]["line"] == 8      # docstring, 2 imports, blank, constant, 2 blanks


@pytest.mark.asyncio
async def test_find_symbol_falls_back_to_partial_match(code, indexed):
    assert await code.find_symbol("Engin", indexed["id"])


@pytest.mark.asyncio
async def test_find_references_returns_lines(code, indexed):
    references = await code.find_references("Engine", indexed["id"])
    assert references
    assert all("path" in r and "line" in r for r in references)


@pytest.mark.asyncio
async def test_outline_lists_a_file(code, indexed):
    outline = await code.outline("src/app.py", indexed["id"])
    assert outline["language"] == "python"
    assert "os" in outline["imports"]
    assert {s["name"] for s in outline["symbols"]} >= {"Engine", "start", "helper", "MAX_SIZE"}


@pytest.mark.asyncio
async def test_outline_rejects_unknown_file(code, indexed):
    with pytest.raises(FileNotFoundError):
        await code.outline("src/nope.py", indexed["id"])


@pytest.mark.asyncio
async def test_overview_aggregates(code, indexed):
    overview = await code.overview(indexed["id"])
    languages = {l["language"] for l in overview["by_language"]}
    assert "python" in languages and "typescript" in languages
    assert overview["total_lines"] > 0


@pytest.mark.asyncio
async def test_resolve_workspace_by_name_and_error(code, indexed):
    assert (await code.resolve_workspace("test-repo"))["id"] == indexed["id"]
    with pytest.raises(LookupError):
        await code.resolve_workspace("no-such-project")


# ===========================================================================
# error explanation
# ===========================================================================
def test_trace_parsing_handles_several_formats():
    python = _parse_trace('File "src/app.py", line 12, in start')
    assert ("src/app.py", 12) in python

    node = _parse_trace("    at Panel (/home/x/src/ui.tsx:42:9)")
    assert any(line == 42 for _, line in node)

    tsc = _parse_trace("src/ui.tsx:7:3 - error TS2322: bad")
    assert any(line == 7 for _, line in tsc)


def test_trace_parsing_deduplicates():
    frames = _parse_trace('File "a.py", line 1\nFile "a.py", line 1')
    assert frames.count(("a.py", 1)) == 1


@pytest.mark.asyncio
async def test_explain_error_resolves_frames_to_real_source(code, indexed):
    result = await code.explain_error(
        'Traceback:\n  File "src/app.py", line 12, in start\nValueError: nope',
        indexed["id"])
    assert result["resolved_frames"] == 1
    assert "src/app.py" in result["frames"][0]["path"]
    # The excerpt must contain the real surrounding code, marked at the line.
    assert "return name" in result["frames"][0]["code"]


@pytest.mark.asyncio
async def test_explain_error_is_honest_when_frames_are_unknown(code, indexed):
    result = await code.explain_error('File "/elsewhere/other.py", line 3', indexed["id"])
    assert result["resolved_frames"] == 0


# ===========================================================================
# static checks
# ===========================================================================
def test_static_checks_flag_real_problems():
    findings = _static_checks(
        "try:\n    x()\nexcept:\n    pass\n"
        "# TODO: fix this\n"
        'password = "hunter2hunter2"\n', "a.py")
    issues = " ".join(f["issue"] for f in findings)
    assert "exception" in issues.lower()
    assert "marker" in issues.lower()
    assert "credential" in issues.lower()


def test_static_checks_redact_credentials():
    findings = _static_checks('api_key = "abcdefghijklmnop"\n', "a.py")
    assert all("abcdefgh" not in f["text"] for f in findings)


def test_static_checks_stay_quiet_on_clean_code():
    assert _static_checks("def f(x):\n    return x + 1\n", "a.py") == []


# ===========================================================================
# git parsing
# ===========================================================================
def test_status_parsing():
    changed, ahead, behind = _parse_status(
        "## main...origin/main [ahead 2, behind 1]\n"
        "M  staged.py\n"
        " M unstaged.py\n"
        "?? new.py\n")
    assert ahead == 2 and behind == 1
    by_path = {c["path"]: c for c in changed}
    assert by_path["staged.py"]["staged"] is True
    assert by_path["unstaged.py"]["staged"] is False
    assert by_path["new.py"]["untracked"] is True


def test_diff_stats_and_files():
    diff = ("--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n+extra\n"
            "--- a/y.ts\n+++ b/y.ts\n@@\n+added\n")
    added, removed = _diff_stats(diff)
    assert added == 3 and removed == 1
    assert _diff_files(diff) == ["x.py", "y.ts"]


# ===========================================================================
# test-output parsing
# ===========================================================================
def test_pytest_output_parsing():
    parsed = _parse_test_output(
        "FAILED tests/test_a.py::test_one - AssertionError: nope\n"
        "2 failed, 8 passed, 1 skipped in 3.2s\n")
    assert parsed["passed"] == 8
    assert parsed["skipped"] == 1
    assert any("test_one" in f["name"] for f in parsed["failures"])


def test_jest_output_parsing():
    parsed = _parse_test_output("Tests:       1 failed, 2 skipped, 5 passed, 8 total\n")
    assert parsed["passed"] == 5 and parsed["failed"] == 1 and parsed["skipped"] == 2


def test_clean_run_reports_no_failures():
    parsed = _parse_test_output("12 passed in 1.1s\n")
    assert parsed["passed"] == 12 and parsed["failed"] == 0 and parsed["failures"] == []


def test_build_error_parsing():
    errors = _parse_build_errors(
        "src/a.ts(12,5): error TS2322: Type 'string' is not assignable.\n"
        "src/b.rs:8:1: error: mismatched types\n")
    assert len(errors) == 2
    assert errors[0]["file"] == "src/a.ts" and errors[0]["line"] == 12


# ===========================================================================
# devtools
# ===========================================================================
@pytest.fixture
async def devtools(db, settings, activity, code, indexed):
    return DevTools(db, settings, FakeOrchestrator(), activity, code)


@pytest.mark.asyncio
async def test_non_repository_is_reported_not_faked(devtools):
    status = await devtools.status("test-repo")
    assert status["repository"] is False
    assert "not a git repository" in status["message"]


@pytest.mark.asyncio
async def test_runner_discovery_reads_package_json(devtools, repo):
    import json

    (repo / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8")
    discovered = devtools._discover(repo)
    assert "node" in discovered["kinds"]
    # No "test" script defined, so none is claimed.
    assert discovered["test"] is None
    assert discovered["build"] == ["npm", "run", "build"]


@pytest.mark.asyncio
async def test_runner_discovery_finds_pytest(devtools, repo):
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    discovered = devtools._discover(repo)
    assert "python" in discovered["kinds"]
    assert discovered["test"][:3] == ["python", "-m", "pytest"]


@pytest.mark.asyncio
async def test_missing_runner_is_reported(devtools, repo):
    result = await devtools.run_tests("test-repo")
    assert result["ok"] is False
    assert "can't tell how" in result["message"]


@pytest.mark.asyncio
async def test_timeline_records_events(devtools, db):
    await devtools._record("test", "Tests failed in demo", ok=False)
    events = await devtools.timeline("test-repo")
    assert events and events[0]["kind"] == "test"
    assert events[0]["ok"] is False


@pytest.mark.asyncio
async def test_ci_requires_the_github_integration(devtools):
    result = await devtools.ci_status("owner/repo")
    assert result["ok"] is False
    assert "GitHub integration" in result["message"]
