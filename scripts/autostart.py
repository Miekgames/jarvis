"""Start JARVIS when you log in to Windows.

    python scripts/autostart.py status      is it installed, and would it work?
    python scripts/autostart.py install     register it (builds the UI if needed)
    python scripts/autostart.py uninstall   remove it
    python scripts/autostart.py run         what the shortcut actually runs
    python scripts/autostart.py logs        the last of the autostart log

Design notes
------------
This installs a shortcut in your own Startup folder. Deliberately *not* a
scheduled task, a service, or a registry Run key:

  * It needs no administrator rights.
  * It appears in Task Manager -> Startup apps, so Windows itself offers you a
    switch to disable it, and you can see it is there.
  * Uninstalling is deleting one file, which this script does for you.

At logon it starts the backend only. The backend serves the built interface from
`frontend/dist` on the same origin, so there is no second dev-server process --
one Python process, not a Node toolchain.

It then *stays* in front of that process and restarts it if it dies. A thing
you reach from your phone has to survive a crash at three in the morning
without someone walking over to the desk, and a launcher that exits the moment
the child is up cannot do that. Restarts back off, and a process that keeps
dying immediately is left dead with a line in the log rather than hammered
forever -- a crash loop at one attempt a second is worse than an honest stop.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
LOG_DIR = ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "autostart.log"

SHORTCUT_NAME = "JARVIS.lnk"
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def startup_dir() -> Optional[Path]:
    """The current user's Startup folder, or None off Windows."""
    if not IS_WINDOWS:
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Optional[Path]:
    directory = startup_dir()
    return directory / SHORTCUT_NAME if directory else None


def pythonw() -> Path:
    """The windowed interpreter, so logon does not flash a console window.

    Falls back to the console interpreter when pythonw is absent (it ships with
    the standard Windows installer but not with every distribution).
    """
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def env_values() -> dict:
    """The .env file as a plain dict. No dependency on the backend package --
    this runs at logon, before anything is importable from a known cwd."""
    values: dict = {}
    env = ROOT / ".env"
    if not env.exists():
        return values
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def backend_port() -> int:
    """Read the port from .env if it has been changed, else the 8000 default."""
    try:
        return int(env_values().get("JARVIS_PORT", "8000"))
    except ValueError:
        return 8000


def remote_readiness() -> list:
    """Whether a phone on the same tailnet could actually reach this.

    Reported rather than fixed. Three separate things have to be true and each
    fails in a way that looks identical from the phone -- a page that never
    loads -- so naming which one is missing is most of the help there is.
    """
    values = env_values()
    host = values.get("JARVIS_HOST", "127.0.0.1")
    mode = values.get("JARVIS_AUTH_MODE", "local").lower()
    password = values.get("JARVIS_PASSWORD", "")
    checks = []

    if host in ("127.0.0.1", "localhost", "::1"):
        checks.append((False, f"listening on {host} -- only this machine can connect",
                       "JARVIS_HOST=0.0.0.0 in .env"))
    else:
        checks.append((True, f"listening on {host}", ""))

    if mode == "local":
        checks.append((False, "auth mode is 'local' -- it refuses anything that is "
                              "not this machine, and refuses to let it log in either",
                       "JARVIS_AUTH_MODE=password in .env"))
    elif not password:
        checks.append((False, "auth mode is 'password' but no password is set -- "
                              "nothing could log in",
                       "JARVIS_PASSWORD=<something only you know> in .env"))
    else:
        checks.append((True, "password login is configured", ""))

    if not DIST.exists():
        checks.append((False, "the interface has not been built, so there would be "
                              "an API and no screen",
                       "python scripts/autostart.py install --build"))
    else:
        checks.append((True, "the interface is built", ""))

    return checks


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------
def build_frontend() -> bool:
    print("  building the interface (this takes a minute)...")
    result = subprocess.run(["npm", "run", "build"], cwd=FRONTEND, shell=IS_WINDOWS)
    if result.returncode != 0:
        print("  ! the build failed -- fix that first, then run install again")
        return False
    return True


def create_shortcut(target: Path, arguments: str, destination: Path) -> bool:
    """Create a .lnk via the Windows scripting host.

    Done through PowerShell rather than pywin32 so this needs no extra package.
    """
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{dest}');"
        "$s.TargetPath = '{target}';"
        "$s.Arguments = '{args}';"
        "$s.WorkingDirectory = '{cwd}';"
        "$s.Description = 'Start JARVIS';"
        "$s.WindowStyle = 7;"
        "$s.Save()"
    ).format(
        dest=str(destination).replace("'", "''"),
        target=str(target).replace("'", "''"),
        args=arguments.replace("'", "''"),
        cwd=str(ROOT).replace("'", "''"),
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ! could not create the shortcut: {result.stderr.strip()[:300]}")
        return False
    return destination.exists()


def install(force_build: bool) -> int:
    if not IS_WINDOWS:
        print("This installer is Windows-only. On macOS use a LaunchAgent; on Linux "
              "a systemd --user unit. Both should run: "
              f"{sys.executable} {Path(__file__).name} run")
        return 1

    destination = shortcut_path()
    if destination is None:
        print("! could not locate your Startup folder (APPDATA is not set).")
        return 1

    if force_build or not DIST.exists():
        if not DIST.exists():
            print("  the interface has not been built yet.")
        if not build_frontend():
            return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher = pythonw()
    arguments = f'"{Path(__file__).resolve()}" run'

    if not create_shortcut(launcher, arguments, destination):
        return 1

    print(f"  installed: {destination}")
    print(f"  runs:      {launcher} {arguments}")
    print(f"  log:       {LOG_FILE}")
    print()
    print("  JARVIS will start the next time you log in, and open in your browser.")
    print("  To remove it:  python scripts/autostart.py uninstall")
    print("  Windows also lists it under Task Manager -> Startup apps.")
    return 0


def uninstall() -> int:
    destination = shortcut_path()
    if destination is None:
        print("Nothing to remove (not Windows).")
        return 0
    if destination.exists():
        destination.unlink()
        print(f"  removed {destination}")
    else:
        print("  not installed -- nothing to remove")
    return 0


def status() -> int:
    destination = shortcut_path()
    print("JARVIS autostart")
    print(f"  platform        {'Windows' if IS_WINDOWS else os.name + ' (installer is Windows-only)'}")

    if destination is None:
        print("  installed       no -- no Startup folder available")
    else:
        installed = destination.exists()
        print(f"  installed       {'yes' if installed else 'no'}")
        print(f"  shortcut        {destination}")

    print(f"  interface built {'yes' if DIST.exists() else 'no -- run install, it will build it'}")
    print(f"  launcher        {pythonw()}")
    print(f"  backend port    {backend_port()}")
    print(f"  responding now  {'yes' if already_running(backend_port()) else 'no'}")
    print(f"  log file        {LOG_FILE if LOG_FILE.exists() else str(LOG_FILE) + ' (not written yet)'}")

    print()
    print("Reachable from another device (your phone)?")
    blockers = []
    for ok, detail, remedy in remote_readiness():
        print(f"  {'ok  ' if ok else 'no  '}{detail}")
        if not ok and remedy:
            blockers.append(remedy)
    if blockers:
        print()
        print("  To change that:")
        for remedy in blockers:
            print(f"    - {remedy}")
        print("    then restart JARVIS.")
        print("  Being reachable is not the same as being safe to reach: put it on a")
        print("  private network (Tailscale) rather than forwarding a port.")
    return 0


# ---------------------------------------------------------------------------
# the boot sequence the shortcut runs
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {message}\n")


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


# How long a backend has to stay up before the next crash counts as a fresh
# problem rather than a continuing one. Below this, restarts back off.
HEALTHY_RUN_S = 120.0

# Restart backoff, and the point at which we stop pretending it will work.
RESTART_BASE_S = 5.0
RESTART_MAX_S = 120.0
MAX_CONSECUTIVE_CRASHES = 6


def spawn_backend(backend_log: Path):
    """Start the backend detached from any console."""
    creation_flags = 0
    if IS_WINDOWS:
        creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = backend_log.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "backend"], cwd=ROOT,
        stdout=handle, stderr=subprocess.STDOUT, creationflags=creation_flags)
    return process, handle


def wait_until_healthy(process, port: int, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if already_running(port):
            return True
        time.sleep(1.0)
    return False


def run(delay: float = 8.0, open_browser: bool = True, supervise: bool = True) -> int:
    """Start the backend and keep it up. Run by the Startup shortcut."""
    port = backend_port()
    url = f"http://127.0.0.1:{port}"
    backend_log = LOG_DIR / "backend.log"

    # Logging on: at logon the network stack, drives and antivirus are all still
    # settling. A short wait avoids a spurious failure on a cold boot.
    log(f"--- autostart: waiting {delay:.0f}s for the desktop to settle")
    time.sleep(delay)

    if already_running(port):
        log(f"backend already responding on {port} -- not starting a second one")
        if open_browser:
            webbrowser.open(url)
        return 0

    if not DIST.exists():
        log("frontend/dist is missing -- the backend will serve the API only. "
            "Run: python scripts/autostart.py install --build")

    for label, detail, remedy in remote_readiness():
        if not label:
            log(f"note: {detail}" + (f"  (set {remedy})" if remedy else ""))

    crashes = 0
    opened = False

    while True:
        log(f"starting the backend on {port}")
        started_at = time.time()
        process, handle = spawn_backend(backend_log)

        healthy = wait_until_healthy(process, port)
        if healthy:
            log(f"backend is up on {port} (pid {process.pid})")
            crashes = 0
            if open_browser and not opened:
                webbrowser.open(url)
                opened = True
                log(f"opened {url}")
        elif process.poll() is None:
            # Running but not answering. Leave it: a slow first boot that
            # pulls a model is not a crash, and killing it would turn a slow
            # start into no start at all.
            log("! the backend has not answered in 90s -- leaving it to carry on")
        # else: it exited during startup; the code below reports it.

        if not supervise:
            if process.poll() is not None:
                log(f"! the backend exited with code {process.returncode} "
                    f"-- see {backend_log}")
                handle.close()
                return 1
            handle.close()
            return 0

        # Block until it goes away.
        try:
            code = process.wait()
        except KeyboardInterrupt:
            log("stopping the backend (interrupted)")
            process.terminate()
            handle.close()
            return 0
        finally:
            handle.close()

        lifetime = time.time() - started_at
        if lifetime >= HEALTHY_RUN_S:
            # It ran for a while and then stopped. That is a restart, not a
            # crash loop -- most likely the user restarted it themselves, or
            # something killed it. Start again promptly and reset the count.
            crashes = 0
            log(f"backend exited with code {code} after {lifetime / 60:.0f}m -- restarting")
            time.sleep(RESTART_BASE_S)
            continue

        crashes += 1
        if crashes >= MAX_CONSECUTIVE_CRASHES:
            log(f"! the backend has died {crashes} times in a row within "
                f"{HEALTHY_RUN_S:.0f}s each. Giving up rather than looping. "
                f"The reason will be at the end of {backend_log}")
            return 1

        backoff = min(RESTART_BASE_S * (2 ** (crashes - 1)), RESTART_MAX_S)
        log(f"backend exited with code {code} after {lifetime:.0f}s "
            f"(crash {crashes}/{MAX_CONSECUTIVE_CRASHES}) -- retrying in {backoff:.0f}s")
        time.sleep(backoff)


def show_logs(lines: int = 40) -> int:
    if not LOG_FILE.exists():
        print(f"no log yet at {LOG_FILE}")
        return 0
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print("  " + line)
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Start JARVIS when you log in.")
    parser.add_argument("command",
                        choices=["install", "uninstall", "status", "run", "logs"])
    parser.add_argument("--build", action="store_true",
                        help="rebuild the interface during install")
    parser.add_argument("--delay", type=float, default=8.0,
                        help="seconds to wait at logon before starting (default 8)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser")
    parser.add_argument("--no-supervise", action="store_true",
                        help="start the backend and exit instead of restarting it "
                             "when it dies")
    parser.add_argument("--lines", type=int, default=40,
                        help="how much of the log to show (default 40)")
    args = parser.parse_args()

    if args.command == "install":
        return install(force_build=args.build)
    if args.command == "uninstall":
        return uninstall()
    if args.command == "status":
        return status()
    if args.command == "logs":
        return show_logs(args.lines)
    return run(delay=args.delay, open_browser=not args.no_open,
               supervise=not args.no_supervise)


if __name__ == "__main__":
    sys.exit(main())
