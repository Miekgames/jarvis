"""Running external commands.

Nothing in the backend may build a command line for a shell to parse. It used
to, on Windows, so that `.cmd` launchers like npm and npx could be found --
and that quietly broke every command whose arguments contained a shell
metacharacter.

The obvious casualty was git:

    git log -8 --pretty=format:%H|%h|%an|%aI|%s

cmd.exe reads those pipes as pipes, tries to run a program called `%h`, and
blocks until the timeout. Thirty seconds of nothing, three times over, for a
command git answers in a tenth of a second -- which is why the developer
panel's one summary call never came back at all.

The same pattern is how an argument becomes an instruction, and the app
launcher was interpolating part of a user's request into one.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def _python_files():
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


# ---------------------------------------------------------------------------
def test_nothing_runs_a_command_through_a_shell():
    """`shell=True`, or anything that evaluates to it, is banned outright."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", getattr(target, "id", ""))
            if name not in ("run", "Popen", "call", "check_output", "check_call"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                # A literal False is the only acceptable value. Anything
                # computed -- `os.name == "nt"` was the real one -- hides the
                # platform on which it turns itself on.
                value = keyword.value
                literal_false = (isinstance(value, ast.Constant)
                                 and value.value is False)
                if not literal_false:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} shell="
                        f"{ast.dump(value)[:60]}")
    assert not offenders, "commands must not be parsed by a shell:\n  " + "\n  ".join(offenders)


def test_no_command_line_is_built_by_string_formatting():
    """A command assembled from an f-string is one a shell has to re-parse."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in ("run", "Popen", "call", "check_output", "check_call"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            # An f-string, or a concatenation/%-format starting from a string
            # literal. `["git"] + list(args)` is also a BinOp and is exactly
            # the right way to do this, so the left operand decides.
            formatted = isinstance(first, ast.JoinedStr) or (
                isinstance(first, ast.BinOp)
                and isinstance(first.left, ast.Constant)
                and isinstance(first.left.value, str))
            if formatted:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, ("a command must be a list of arguments, never a formatted "
                           "string:\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# The behaviour the shell was breaking.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not (ROOT / ".git").exists() and os.name != "nt",
                    reason="needs a git binary")
def test_pipes_in_an_argument_survive_to_the_program():
    """The exact shape that hung: a format string full of `|`.

    Run without a shell it is one argument. Run through cmd.exe it is five
    pipelines, and the call never returns.
    """
    import shutil

    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")

    result = subprocess.run(
        [git, "log", "-1", "--pretty=format:%H|%h|%an|%aI|%s"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=20, shell=False)

    # Outside a repository (or in one with no commits yet) git exits 128 and
    # says so, promptly. Inside one it prints a single pipe-separated line.
    # Any of these is fine; hanging is not.
    assert result.returncode in (0, 128)
    if result.returncode == 0:
        assert result.stdout.count("|") >= 4, "the format string reached git intact"
    else:
        error = (result.stderr or "").lower()
        assert "not a git repository" in error or "does not have any commits" in error


def test_the_dev_runner_resolves_the_executable_itself():
    """Resolving with `shutil.which` is what replaced shell=True for finding
    `.cmd` launchers. If it goes, the shell comes back."""
    source = io.open(BACKEND / "intelligence" / "devtools.py", encoding="utf-8").read()
    assert "shutil.which(command[0])" in source
    assert "shell=False" in source


def test_a_missing_executable_is_reported_rather_than_guessed():
    source = io.open(BACKEND / "intelligence" / "devtools.py", encoding="utf-8").read()
    assert "is not installed or not on PATH" in source


def test_the_app_launcher_does_not_interpolate_into_a_command_line():
    """`start "" "{command}"` took part of the user's request and put it in a
    string for cmd.exe to parse."""
    source = io.open(BACKEND / "systemmon" / "apps.py", encoding="utf-8").read()
    # Check the code, not the prose: the comment explaining the old bug
    # legitimately quotes it.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            rendered = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str))
            assert "start " not in rendered, (
                f"apps.py:{node.lineno} builds a cmd.exe command line")
    assert "os.startfile" in source


def test_blocking_subprocess_calls_are_pushed_off_the_event_loop():
    """A `subprocess.run` inside a coroutine does not wait politely.

    It stops the whole backend for the duration: every other request, the
    websocket, the wake word's hop back onto the loop. The fix is a nested
    plain function handed to an executor, so a nested function is exactly
    what this walk declines to look inside.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            stack = list(node.body)
            while stack:
                item = stack.pop()
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue                      # runs in an executor, not here
                stack.extend(ast.iter_child_nodes(item))
                if (isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr in ("run", "check_output", "check_call", "call")
                        and isinstance(item.func.value, ast.Name)
                        and item.func.value.id == "subprocess"):
                    offenders.append(
                        "{}:{} in async {}()".format(
                            path.relative_to(ROOT), item.lineno, node.name))
    assert not offenders, (
        "blocking subprocess calls inside coroutines:\n  " + "\n  ".join(offenders))


def test_no_blocking_sleep_inside_a_coroutine():
    """`time.sleep` on the event loop freezes the application just as surely."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            stack = list(node.body)
            while stack:
                item = stack.pop()
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                stack.extend(ast.iter_child_nodes(item))
                if (isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "sleep"
                        and isinstance(item.func.value, ast.Name)
                        and item.func.value.id == "time"):
                    offenders.append(
                        "{}:{} in async {}()".format(
                            path.relative_to(ROOT), item.lineno, node.name))
    assert not offenders, (
        "time.sleep inside coroutines (use asyncio.sleep):\n  " + "\n  ".join(offenders))
