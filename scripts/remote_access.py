"""Make JARVIS reachable from your phone -- and say honestly what that costs.

    python scripts/remote_access.py status    what would work right now
    python scripts/remote_access.py on        open it up (asks for a password)
    python scripts/remote_access.py off       back to this machine only
    python scripts/remote_access.py firewall  restrict the port to the tailnet

Three things have to change together, and each one alone is useless or unsafe:

  1. **The bind address.** JARVIS listens on 127.0.0.1 by default, which no
     other device can reach. It has to listen on 0.0.0.0.

  2. **The auth mode.** `local` mode does not merely skip the password -- it
     actively *refuses* any request that did not come from this machine, and
     refuses to let it log in either. So opening the port without switching to
     `password` mode gets you a phone that connects and is then turned away.

  3. **A password.** Which you choose. This script will not invent one for
     you and will not accept a short one: it is the only thing standing
     between your assistant and whoever else is on the network.

What this does *not* do is forward a port on your router. Do not do that. A
machine that can read your files, run commands and hold your credentials
should not be answering the open internet, password or no password. Put it on
a private network instead -- Tailscale gives your devices their own addresses
that only they can reach, and nothing is exposed publicly.

Binding to 0.0.0.0 does mean every device on your *local* network could reach
the port too -- the coffee shop wifi, the hotel, the office. The `firewall`
command narrows that back down to Tailscale's address range, so the port
answers your own devices and nothing else. It needs administrator rights,
which is why it is a separate step you run deliberately.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
BACKUPS = ROOT / "backups"

IS_WINDOWS = os.name == "nt"

# Tailscale hands out addresses from the CGNAT range. Narrowing the firewall
# rule to this means the port answers devices on your tailnet and nothing on
# whatever wifi you happen to be joined to.
TAILNET_RANGE = "100.64.0.0/10"
# And the matching IPv6 range. Worth knowing about because a machine on a
# tailnet answers on both, and treating the v6 one as an ordinary network
# address makes the report claim the port is exposed somewhere it is not.
TAILNET_RANGE_V6 = "fd7a:115c:a1e0::/48"
FIREWALL_RULE = "JARVIS (tailnet only)"

# Short enough to type on a phone, long enough not to be guessed by anything
# that finds the port. Twelve is the floor, not a recommendation.
MIN_PASSWORD = 12

SETTINGS_FOR_REMOTE = {
    "JARVIS_HOST": "0.0.0.0",
    "JARVIS_AUTH_MODE": "password",
}
SETTINGS_FOR_LOCAL = {
    "JARVIS_HOST": "127.0.0.1",
    "JARVIS_AUTH_MODE": "local",
}


# ---------------------------------------------------------------------------
# .env, read and written without disturbing anything else in it
# ---------------------------------------------------------------------------
def read_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(changes: Dict[str, str]) -> Path:
    """Apply changes in place, preserving comments, order and everything else.

    Returns the path of the backup it took first. A .env holds API keys and
    tokens; rewriting it without a copy would be an unkind way to lose them.
    """
    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup = BACKUPS / f"env.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    if ENV.exists():
        shutil.copy2(ENV, backup)
        lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        backup.write_text("", encoding="utf-8")
        lines = []

    remaining = dict(changes)
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# added by scripts/remote_access.py")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    return backup


# ---------------------------------------------------------------------------
# what is true right now
# ---------------------------------------------------------------------------
def port() -> int:
    try:
        return int(read_env().get("JARVIS_PORT", "8000"))
    except ValueError:
        return 8000


def tailscale_cli() -> Optional[Path]:
    found = shutil.which("tailscale")
    if found:
        return Path(found)
    # It installs outside PATH more often than not.
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe",
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
        Path("/usr/bin/tailscale"),
    ):
        if candidate.exists():
            return candidate
    return None


def tailscale_address() -> Tuple[Optional[str], str]:
    """This machine's tailnet address, or why there isn't one."""
    cli = tailscale_cli()
    if cli is None:
        return None, "Tailscale is not installed."
    try:
        result = subprocess.run([str(cli), "ip", "-4"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Could not run Tailscale: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        hint = detail[0][:160] if detail else "no address yet"
        return None, f"Tailscale is installed but not connected ({hint})."
    address = result.stdout.strip().splitlines()
    if not address:
        return None, "Tailscale is installed but has no address yet -- log in to it."
    return address[0].strip(), "Connected."


def lan_addresses() -> List[str]:
    """Every address this machine answers on, tailnet ones separated out."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local:
                continue
            found.add(address)
    except OSError:
        pass
    return sorted(found)


def is_tailnet(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for network in (TAILNET_RANGE, TAILNET_RANGE_V6):
        candidate = ipaddress.ip_network(network)
        if parsed.version == candidate.version and parsed in candidate:
            return True
    return False


def firewall_rule_present() -> Optional[bool]:
    """True/False, or None if we cannot tell (not Windows, or no permission)."""
    if not IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"if (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
             # Single braces: this half is not an f-string, and doubling them
             # here produced a nested scriptblock that printed " 'no' " --
             # quotes and all -- which then matched neither branch.
             "-ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = result.stdout.strip().lower()
    if answer.startswith("yes"):
        return True
    if answer.startswith("no"):
        return False
    return None


def backend_responding(host: str = "127.0.0.1") -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port()}/api/health", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def status() -> int:
    env = read_env()
    host = env.get("JARVIS_HOST", "127.0.0.1")
    mode = env.get("JARVIS_AUTH_MODE", "local").lower()
    has_password = bool(env.get("JARVIS_PASSWORD", ""))
    dist = ROOT / "frontend" / "dist"

    print("Reaching JARVIS from another device")
    print()
    print("  Configuration")
    print(f"    bind address      {host}"
          f"{'   (this machine only)' if host in ('127.0.0.1', 'localhost', '::1') else ''}")
    print(f"    auth mode         {mode}"
          f"{'   (refuses anything not from this machine)' if mode == 'local' else ''}")
    print(f"    password set      {'yes' if has_password else 'no'}")
    print(f"    port              {port()}")
    print(f"    interface built   {'yes' if dist.exists() else 'no'}")
    print(f"    backend up        {'yes' if backend_responding() else 'no'}")

    print()
    print("  Private network")
    address, detail = tailscale_address()
    print(f"    tailscale         {detail}")
    if address:
        print(f"    this machine      {address}")

    others = [a for a in lan_addresses() if not is_tailnet(a)]
    if others:
        print(f"    also answers on   {', '.join(others)}   (ordinary network)")

    if IS_WINDOWS:
        rule = firewall_rule_present()
        print("    firewall rule     "
              + {True: "present",
                 False: "absent -- any device on your local network could reach "
                        "the port",
                 None: "could not check"}[rule])
        others, detail = competing_rules()
        if others is None:
            print(f"    wider rules       could not check ({detail})")
        elif others:
            print(f"    wider rules       {len(others)} rule(s) allow this port from "
                  "any address:")
            describe_competing(others)
        else:
            print("    wider rules       none -- nothing else opens this port")

    print()
    ready = (host not in ("127.0.0.1", "localhost", "::1")
             and mode == "password" and has_password and dist.exists())
    if ready and address:
        print(f"  Open this on your phone:  http://{address}:{port()}")
        print("  (Both devices need to be logged in to the same Tailscale account.)")
    elif ready:
        print("  JARVIS is configured for remote access, but there is no private")
        print("  network to reach it over yet. Install Tailscale and log in on both")
        print("  this machine and your phone, then run this again for the address.")
    else:
        print("  Not reachable from another device yet. To change that:")
        print("    python scripts/remote_access.py on")
    return 0


# ---------------------------------------------------------------------------
# on / off
# ---------------------------------------------------------------------------
def choose_password(given: Optional[str]) -> Optional[str]:
    """Get a password from the user. Never generates one.

    A generated password would have to be shown, which means printed to a
    terminal and scrolled back through -- and it would be *our* choice of the
    one secret protecting their machine. Theirs to pick.
    """
    if given is not None:
        candidate = given
    else:
        if not sys.stdin.isatty():
            print("! no password given, and nothing to prompt on.")
            print("  Re-run with:  python scripts/remote_access.py on --password '...'")
            return None
        print(f"Choose a password for reaching JARVIS remotely ({MIN_PASSWORD}+ characters).")
        print("It is stored in .env on this machine. Nothing is sent anywhere.")
        candidate = getpass.getpass("  password: ")
        again = getpass.getpass("  again:    ")
        if candidate != again:
            print("! they did not match.")
            return None

    candidate = candidate.strip()
    if len(candidate) < MIN_PASSWORD:
        print(f"! too short -- {MIN_PASSWORD} characters at minimum. This is the only "
              "thing between your assistant and the network it is on.")
        return None
    if candidate.lower() in {"password", "jarvis", "letmein", "changeme"} or candidate.isdigit():
        print("! pick something less guessable.")
        return None
    return candidate


def turn_on(password_arg: Optional[str]) -> int:
    password = choose_password(password_arg)
    if password is None:
        return 1

    changes = dict(SETTINGS_FOR_REMOTE)
    changes["JARVIS_PASSWORD"] = password
    backup = write_env(changes)

    print()
    print("  Done. JARVIS will now:")
    print("    - listen on all interfaces (0.0.0.0) rather than just this machine")
    print("    - require that password from every browser, including this one")
    print(f"  .env backed up to {backup}")
    print()
    print("  Next:")
    print("    1. Restart JARVIS so it picks this up.")
    if not (ROOT / "frontend" / "dist").exists():
        print("    2. Build the interface:  cd frontend && npm run build")
    address, detail = tailscale_address()
    if address:
        print(f"    2. On your phone, open:  http://{address}:{port()}")
    else:
        print(f"    2. Install Tailscale and log in here and on your phone ({detail})")
        print("       Then run this script's `status` for the address to open.")
    if IS_WINDOWS and firewall_rule_present() is not True:
        print("    3. Narrow the port to your own devices:")
        print("         python scripts/remote_access.py firewall")
        print("       Until then, any device on the same wifi could reach the port.")
    print()
    print("  Do not forward this port on your router.")
    return 0


def turn_off() -> int:
    changes = dict(SETTINGS_FOR_LOCAL)
    # The password stays in .env deliberately: removing it would mean typing a
    # new one to turn this back on, and it is harmless while nothing can
    # reach the port. `off --forget` removes it.
    backup = write_env(changes)
    print("  Back to this machine only: 127.0.0.1, local auth.")
    print(f"  .env backed up to {backup}")
    print("  Restart JARVIS for it to take effect.")
    print("  The password is still in .env, so turning it back on needs no new one.")
    print("  To remove it as well:  python scripts/remote_access.py off --forget")
    return 0


def forget_password() -> int:
    backup = write_env({**SETTINGS_FOR_LOCAL, "JARVIS_PASSWORD": ""})
    print("  Back to this machine only, and the password has been cleared.")
    print(f"  .env backed up to {backup}")
    return 0


# ---------------------------------------------------------------------------
# firewall
# ---------------------------------------------------------------------------
PROBE = Path(__file__).resolve().parent / "firewall_probe.ps1"


def competing_rules() -> Tuple[Optional[List[Tuple[str, str, str]]], str]:
    """Rules that would let any device reach the port regardless of ours.

    Returns (rules, detail). `rules` is None when the check could not run --
    which must not be reported as "none found", because the difference between
    "nothing else allows this port" and "we could not tell" is the difference
    between a true claim and a false one.

    Why this matters at all: Windows Firewall permits a packet if *any* enabled
    Allow rule matches it. Adding a rule scoped to the tailnet restricts
    nothing while a broader rule already covers the same port -- and on this
    kind of machine there usually is one, because the first time you run a
    server Windows pops up "allow python.exe on your networks?" and a yes
    leaves a rule allowing every port from every address.

    The analysis lives in firewall_probe.ps1, where it is readable, with the
    reasoning for each filter next to the filter.
    """
    if not IS_WINDOWS:
        return None, "not Windows"
    if not PROBE.exists():
        return None, f"{PROBE.name} is missing"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(PROBE), "-Port", str(port()),
             "-Exclude", FIREWALL_RULE, "-Program", sys.executable],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run the check ({exc})"
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return None, detail[0][:160] if detail else "the check failed"

    rules: List[Tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) == 3:
            rules.append((parts[0], parts[1], parts[2]))
    return rules, "checked"


def describe_competing(rules: List[Tuple[str, str, str]]) -> None:
    for kind, profile, name in rules:
        if kind == "program":
            print(f"    - {name}  [{profile}]")
            print("      allows this Python interpreter to accept connections on any")
            print("      port from any address. This is the one that matters.")
        else:
            print(f"    - {name}  [{profile}]")
            print("      is scoped to no program at all, so it covers this port too.")


def add_firewall_rule() -> int:
    if not IS_WINDOWS:
        print("This command is Windows-only. On macOS or Linux, restrict the port to "
              f"{TAILNET_RANGE} with pf or iptables.")
        return 1

    print(f"  Adding an inbound rule limiting port {port()} to {TAILNET_RANGE}.")
    print("  Windows will ask for administrator rights.")

    # Written to a file and elevated by path rather than passed as a nested
    # quoted string. The rule name contains spaces and parentheses, the range
    # contains a slash, and Start-Process re-parses its argument list -- every
    # attempt to inline this is one quoting layer away from running something
    # other than what it reads like.
    script = ROOT / "data" / "firewall-rule.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
        "-ErrorAction SilentlyContinue",
        f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
        "-Description 'JARVIS, reachable only from this tailnet.' "
        "-Direction Inbound -Action Allow -Protocol TCP "
        f"-LocalPort {port()} -RemoteAddress {TAILNET_RANGE} "
        "-Profile Any | Out-Null",
        "",
    ]), encoding="utf-8")

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process", "powershell", "-Verb", "RunAs", "-Wait",
             "-ArgumentList", "@('-NoProfile','-ExecutionPolicy','Bypass','-File',"
             f"'{str(script)}')"],
            timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ! could not run it: {exc}")
        return 1
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    if not firewall_rule_present():
        print("  ! the rule is not there. Either the prompt was declined, or it failed.")
        print("    You can add it by hand in Windows Defender Firewall: inbound, TCP,")
        print(f"    port {port()}, remote address {TAILNET_RANGE}.")
        return 1

    print(f"  Rule '{FIREWALL_RULE}' is in place.")

    others, detail = competing_rules()
    if others is None:
        print()
        print(f"  Could not check for rules that would override it ({detail}),")
        print("  so whether the port is actually limited now is unverified.")
        print("  Check Windows Defender Firewall -> Inbound Rules by hand for")
        print(f"  anything allowing port {port()} -- or python.exe on any port --")
        print("  from any address.")
        return 1

    if others:
        print()
        print("  It does not restrict anything on its own, though. Windows allows a")
        print("  connection if *any* rule permits it, and these already do:")
        describe_competing(others)
        print()
        print("  Disable or narrow those in Windows Defender Firewall -> Inbound")
        print("  Rules, or the port stays open on whatever network you join.")
        return 1

    print("  Nothing else allows this port from any address, so it now answers")
    print("  your tailnet devices and nothing else.")
    print("  To undo: delete that rule in Windows Defender Firewall.")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Make JARVIS reachable from another device, safely.")
    parser.add_argument("command", choices=["status", "on", "off", "firewall"])
    parser.add_argument("--password", help="the password, if you would rather not be "
                                           "prompted (it will be in your shell history)")
    parser.add_argument("--forget", action="store_true",
                        help="with 'off', also clear the stored password")
    args = parser.parse_args()

    if args.command == "status":
        return status()
    if args.command == "on":
        return turn_on(args.password)
    if args.command == "off":
        return forget_password() if args.forget else turn_off()
    return add_firewall_rule()


if __name__ == "__main__":
    sys.exit(main())
