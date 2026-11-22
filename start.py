#!/usr/bin/env python
"""
Start JARVIS.

    python start.py            backend + frontend dev server, opens the browser
    python start.py --backend  backend only
    python start.py --build    build the frontend, then serve everything from
                               the backend on a single port
    python start.py --no-open  don't open a browser

The startup sequence itself (database -> AI core -> task engine -> scheduler ->
...) is reported by the backend and rendered by the UI; this script just brings
the two processes up and keeps them together.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
IS_WINDOWS = platform.system() == "Windows"

BACKEND_PORT = int(os.getenv("JARVIS_PORT", "8000"))
FRONTEND_PORT = int(os.getenv("JARVIS_FRONTEND_PORT", "3000"))

C_DIM, C_CYAN, C_GREEN, C_RED, C_YELLOW, C_RESET = (
    ("\033[38;5;244m", "\033[38;5;81m", "\033[38;5;84m", "\033[38;5;203m", "\033[38;5;214m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", "", "")
)

# The Windows console still defaults to cp1252, which cannot encode box-drawing
# characters or tick marks. Ask for UTF-8, then verify it actually took -- if
# not, fall back to ASCII rather than crashing on the first line of output.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass


def _unicode_ok() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "─│┌┐└┘✓✕→!".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


FANCY = _unicode_ok()
TICK = "✓" if FANCY else "+"
CROSS = "✕" if FANCY else "x"
ARROW = "→" if FANCY else ">"

processes: list = []


def say(message: str, colour: str = "") -> None:
    try:
        print(f"{colour}{message}{C_RESET}", flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(f"{colour}{safe}{C_RESET}", flush=True)


def banner() -> None:
    say("", "")
    if FANCY:
        say("   ┌───────────────────────────────────────────────┐", C_CYAN)
        say("   │   J A R V I S                                 │", C_CYAN)
        say("   │   personal AI command centre - local-first    │", C_DIM)
        say("   └───────────────────────────────────────────────┘", C_CYAN)
    else:
        say("   +-----------------------------------------------+", C_CYAN)
        say("   |   J A R V I S                                 |", C_CYAN)
        say("   |   personal AI command centre - local-first    |", C_DIM)
        say("   +-----------------------------------------------+", C_CYAN)
    say("", "")


# ---------------------------------------------------------------------------
def preflight(build_mode: bool) -> bool:
    ok = True

    if sys.version_info < (3, 9):
        say(f"  {CROSS}  Python 3.9+ required (found {platform.python_version()})", C_RED)
        ok = False
    else:
        say(f"  {TICK}  Python {platform.python_version()}", C_GREEN)

    missing = [m for m in ("fastapi", "uvicorn", "anthropic", "psutil", "httpx")
               if not _importable(m)]
    if missing:
        say(f"  {CROSS}  Missing Python packages: {', '.join(missing)}", C_RED)
        say("     Run: pip install -r requirements.txt", C_DIM)
        ok = False
    else:
        say(f"  {TICK}  Backend dependencies present", C_GREEN)

    if not (ROOT / ".env").exists():
        example = ROOT / ".env.example"
        if example.exists():
            shutil.copy(example, ROOT / ".env")
            say(f"  !  Created .env from .env.example - add your API key to it", C_YELLOW)
        else:
            say(f"  !  No .env file; JARVIS will run with local models only", C_YELLOW)
    else:
        say(f"  {TICK}  Configuration found (.env)", C_GREEN)

    if not _importable("dotenv"):
        say(f"  !  python-dotenv not installed; using the built-in .env parser", C_DIM)

    node = shutil.which("npm")
    if not node:
        say(f"  {CROSS}  npm not found - needed for the interface", C_RED)
        say("     Install Node.js 18+ from https://nodejs.org", C_DIM)
        ok = False
    elif not (FRONTEND / "node_modules").exists():
        say(f"  {ARROW}  Installing frontend dependencies (first run only)...", C_YELLOW)
        result = subprocess.run(["npm", "install", "--no-fund", "--no-audit"],
                                cwd=FRONTEND, shell=IS_WINDOWS)
        if result.returncode != 0:
            say(f"  {CROSS}  npm install failed", C_RED)
            ok = False
        else:
            say(f"  {TICK}  Frontend dependencies installed", C_GREEN)
    else:
        say(f"  {TICK}  Frontend dependencies present", C_GREEN)

    if build_mode and ok:
        say(f"  {ARROW}  Building the frontend...", C_YELLOW)
        result = subprocess.run(["npm", "run", "build"], cwd=FRONTEND, shell=IS_WINDOWS)
        if result.returncode != 0:
            say(f"  {CROSS}  Frontend build failed", C_RED)
            ok = False
        else:
            say(f"  {TICK}  Frontend built", C_GREEN)

    return ok


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


# ---------------------------------------------------------------------------
def start_backend() -> subprocess.Popen:
    say(f"  {ARROW}  Backend  http://127.0.0.1:{BACKEND_PORT}", C_DIM)
    process = subprocess.Popen([sys.executable, "-m", "backend"], cwd=ROOT)
    processes.append(process)
    return process


def start_frontend() -> subprocess.Popen:
    say(f"  {ARROW}  Interface http://localhost:{FRONTEND_PORT}", C_DIM)
    process = subprocess.Popen(["npm", "run", "dev"], cwd=FRONTEND, shell=IS_WINDOWS)
    processes.append(process)
    return process


def wait_for_backend(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{BACKEND_PORT}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if processes and processes[0].poll() is not None:
            return False
        time.sleep(0.6)
    return False


def shutdown(*_args) -> None:
    say("\n  Shutting down...", C_DIM)
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.time() + 12
    for process in processes:
        remaining = max(0.5, deadline - time.time())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    say("  JARVIS offline.\n", C_DIM)
    sys.exit(0)


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Start JARVIS")
    parser.add_argument("--backend", action="store_true", help="backend only")
    parser.add_argument("--build", action="store_true",
                        help="build the frontend and serve it from the backend")
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    banner()
    say("  Pre-flight", C_CYAN)
    if not preflight(build_mode=args.build):
        say("\n  Startup aborted - fix the items above and try again.\n", C_RED)
        return 1

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    say("\n  Starting", C_CYAN)
    start_backend()

    if not wait_for_backend():
        say(f"  {CROSS}  Backend failed to start - check the output above.", C_RED)
        shutdown()
        return 1
    say(f"  {TICK}  Backend online", C_GREEN)

    url = f"http://localhost:{FRONTEND_PORT}"
    if args.build:
        url = f"http://127.0.0.1:{BACKEND_PORT}"
        say(f"  {TICK}  Serving the built interface from the backend", C_GREEN)
    elif not args.backend:
        start_frontend()
        time.sleep(3.5)
        say(f"  {TICK}  Interface online", C_GREEN)
    else:
        url = f"http://127.0.0.1:{BACKEND_PORT}"

    say("", "")
    say(f"  JARVIS is online at  {url}", C_CYAN)
    say("  Press Ctrl+C to stop.", C_DIM)
    say("", "")

    if not args.no_open and not args.backend:
        import webbrowser

        webbrowser.open(url)

    try:
        while True:
            for process in processes:
                if process.poll() is not None:
                    say(f"  {CROSS}  A process exited (code {process.returncode}) - stopping.", C_RED)
                    shutdown()
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
