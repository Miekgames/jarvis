"""The wake-word matcher.

It lives in TypeScript because it runs in the browser, but it is pure and
dependency-free, so it can be compiled with the project's own TypeScript and
executed under node. That keeps one set of cases rather than a Python
reimplementation that could drift from what actually ships.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SOURCE = FRONTEND / "src" / "lib" / "wakeword.ts"
# .cjs because the frontend package is ESM; this harness uses require().
CASES = FRONTEND / "src" / "lib" / "wakeword.test.cjs"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None or not (FRONTEND / "node_modules").exists(),
    reason="needs node and an installed frontend (npm install --prefix frontend)")


@needs_node
def test_wake_word_matching(tmp_path):
    """Compile the real module and run every case against it."""
    compiled = tmp_path / "out"
    build = subprocess.run(
        ["npx", "tsc", str(SOURCE), "--outDir", str(compiled),
         "--module", "commonjs", "--target", "es2020", "--strict"],
        cwd=FRONTEND, capture_output=True, text=True, shell=(shutil.which("npx.cmd") is not None))
    assert build.returncode == 0, f"wakeword.ts did not compile:\n{build.stdout}{build.stderr}"

    result = subprocess.run(
        ["node", str(CASES), str(compiled / "wakeword.js")],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


@needs_node
def test_speaker_lease(tmp_path):
    """Which tab speaks.

    Compiled and run the same way as the matcher: the real module, against a
    fake browser with one shared localStorage and a clock that can be wound
    forward. Mute assistants and overlapping voices are both failures worth
    catching here rather than by opening tabs and listening.
    """
    source = FRONTEND / "src" / "lib" / "leader.ts"
    cases = FRONTEND / "src" / "lib" / "leader.test.cjs"
    compiled = tmp_path / "out"

    build = subprocess.run(
        ["npx", "tsc", str(source), "--outDir", str(compiled),
         "--module", "commonjs", "--target", "es2020", "--strict",
         "--lib", "es2020,dom"],
        cwd=FRONTEND, capture_output=True, text=True,
        shell=(shutil.which("npx.cmd") is not None))
    assert build.returncode == 0, f"leader.ts did not compile:\n{build.stdout}{build.stderr}"

    result = subprocess.run(
        ["node", str(cases), str(compiled / "leader.js")],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_adaptive_particle_quality(tmp_path):
    """Adaptive quality, including the recovery that did not previously exist.

    The field is the application's primary identity. Halving it on a single
    bad two-second window and never putting it back is not a small bug, so
    the hysteresis is pinned down here rather than eyeballed.
    """
    source = FRONTEND / "src" / "gl" / "quality.ts"
    cases = FRONTEND / "src" / "gl" / "quality.test.cjs"
    compiled = tmp_path / "out"

    build = subprocess.run(
        ["npx", "tsc", str(source), "--outDir", str(compiled),
         "--module", "commonjs", "--target", "es2020", "--strict"],
        cwd=FRONTEND, capture_output=True, text=True,
        shell=(shutil.which("npx.cmd") is not None))
    assert build.returncode == 0, f"quality.ts did not compile:\n{build.stdout}{build.stderr}"

    result = subprocess.run(
        ["node", str(cases), str(compiled / "quality.js")],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_sparkline_geometry(tmp_path):
    """The sidebar's trend lines.

    Missing readings and too-few readings are the cases that matter -- GPU
    and battery are absent on plenty of machines -- and both are far easier
    to be sure about as numbers than as pixels in a screenshot.
    """
    source = FRONTEND / "src" / "lib" / "sparkline.ts"
    cases = FRONTEND / "src" / "lib" / "sparkline.test.cjs"
    compiled = tmp_path / "out"

    build = subprocess.run(
        ["npx", "tsc", str(source), "--outDir", str(compiled),
         "--module", "commonjs", "--target", "es2020", "--strict",
         ],
        cwd=FRONTEND, capture_output=True, text=True,
        shell=(shutil.which("npx.cmd") is not None))
    assert build.returncode == 0, f"sparkline.ts did not compile:\n{build.stdout}{build.stderr}"

    result = subprocess.run(
        ["node", str(cases), str(compiled / "sparkline.js")],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_command_palette_ranking(tmp_path):
    """Ctrl+K ranking.

    What you typed should reach what you meant with as few keystrokes as
    possible, and the obvious thing should win -- "set" must find Settings
    before "Reset conversation". That is an ordering, which is easy to assert
    and painful to check by typing into a box.
    """
    source = FRONTEND / "src" / "lib" / "palette.ts"
    cases = FRONTEND / "src" / "lib" / "palette.test.cjs"
    compiled = tmp_path / "out"

    build = subprocess.run(
        ["npx", "tsc", str(source), "--outDir", str(compiled),
         "--module", "commonjs", "--target", "es2020", "--strict"],
        cwd=FRONTEND, capture_output=True, text=True,
        shell=(shutil.which("npx.cmd") is not None))
    assert build.returncode == 0, f"palette.ts did not compile:\n{build.stdout}{build.stderr}"

    result = subprocess.run(
        ["node", str(cases), str(compiled / "palette.js")],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
