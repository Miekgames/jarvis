"""Sending files to your own devices over Tailscale.

Taildrop is part of Tailscale: `tailscale file cp <file> <device>:` pushes a
file straight to another machine on the same tailnet. No cloud service in
the middle, no account anywhere else, nothing uploaded to anyone -- the file
goes device to device over the same encrypted link JARVIS is already
reachable on.

That makes it the right way to get a wallpaper onto a phone. The alternative
-- emailing it, or putting it somewhere public and fetching the link -- sends
a private file through a third party for no reason.

Two details that matter in practice:

**Devices are addressed by tailnet IP, not name.** This phone's hostname is
literally "localhost", which would be a genuinely dangerous thing to pass to
a copy command. The IP is unambiguous and stable.

**The receiving device has to accept.** On iOS, Taildrop files land in the
Tailscale app and are saved from there; nothing is written to the phone
without the owner touching it. So this can send, and cannot silently plant
files on someone's device.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.taildrop")

SEND_TIMEOUT_S = 900.0
STATUS_TIMEOUT_S = 30.0

# Tailscale's own address ranges.
TAILNET_V4 = "100.64.0.0/10"
TAILNET_V6 = "fd7a:115c:a1e0::/48"


@dataclass
class Device:
    name: str
    os: str
    address: str
    online: bool
    is_self: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "os": self.os, "address": self.address,
                "online": self.online, "is_self": self.is_self}


class Taildrop:
    """Pushes files to the owner's other devices."""

    def __init__(self, settings, activity=None, audit=None):
        self.settings = settings
        self.activity = activity
        self.audit = audit

    # -- the CLI -----------------------------------------------------------
    @property
    def executable(self) -> Optional[Path]:
        found = shutil.which("tailscale")
        if found:
            return Path(found)
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Tailscale" / "tailscale.exe",
            Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
            Path("/usr/bin/tailscale"),
        ):
            if candidate.exists():
                return candidate
        return None

    def availability(self) -> Dict[str, Any]:
        if self.executable is None:
            return {"available": False,
                    "detail": "Tailscale is not installed on this machine."}
        return {"available": True, "detail": "Ready.",
                "tailscale": str(self.executable)}

    async def _run(self, *args: str, timeout: float = STATUS_TIMEOUT_S):
        from ..core.proc import exec_async

        process = await exec_async(
            str(self.executable), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return process.returncode, stdout.decode("utf-8", errors="replace")

    # -- devices -----------------------------------------------------------
    async def devices(self, online_only: bool = True) -> List[Device]:
        """The other machines on this tailnet."""
        if self.executable is None:
            return []
        try:
            code, output = await self._run("status", "--json")
        except Exception as exc:
            logger.debug("tailscale status failed: %s", exc)
            return []
        if code != 0:
            return []

        try:
            data = json.loads(output)
        except ValueError:
            return []

        found: List[Device] = []
        own = data.get("Self") or {}
        if own:
            found.append(Device(
                name=own.get("HostName") or "this machine",
                os=own.get("OS") or "", address=_first_ip(own),
                online=True, is_self=True))

        for peer in (data.get("Peer") or {}).values():
            if online_only and not peer.get("Online"):
                continue
            address = _first_ip(peer)
            if not address:
                continue
            found.append(Device(
                name=peer.get("HostName") or address,
                os=peer.get("OS") or "", address=address,
                online=bool(peer.get("Online"))))
        return found

    async def find_device(self, wanted: str) -> Optional[Device]:
        """Resolve a name, an address, or a loose description like 'phone'."""
        target = (wanted or "").strip().lower()
        if not target:
            return None

        devices = [d for d in await self.devices() if not d.is_self]
        for device in devices:
            if target in (device.address.lower(), device.name.lower()):
                return device

        # "phone", "my phone", "iphone" -- match on the operating system,
        # which is more reliable than a hostname the owner never set. This
        # phone reports its hostname as "localhost".
        if any(word in target for word in ("phone", "ios", "iphone", "mobile")):
            for device in devices:
                if (device.os or "").lower() in ("ios", "android"):
                    return device

        for device in devices:
            if target in device.name.lower():
                return device
        return None

    # -- sending -----------------------------------------------------------
    async def send(self, path: Path, device: str) -> Dict[str, Any]:
        """Push one file to one device. The device still has to accept it."""
        ready = self.availability()
        if not ready["available"]:
            return {"ok": False, "detail": ready["detail"]}

        path = Path(path)
        if not path.exists() or not path.is_file():
            return {"ok": False, "detail": f"{path} is not a file."}

        found = await self.find_device(device)
        if found is None:
            names = ", ".join(d.name for d in await self.devices()
                              if not d.is_self) or "none online"
            return {"ok": False,
                    "detail": f"No device matching {device!r}. Online now: {names}."}
        if not _is_tailnet(found.address):
            # Refuses to send anywhere that is not a tailnet address, so a
            # malformed name can never turn into a copy to somewhere else.
            return {"ok": False,
                    "detail": f"{found.address} is not a Tailscale address."}

        megabytes = path.stat().st_size / 1e6
        try:
            code, output = await self._run(
                "file", "cp", str(path), f"{found.address}:",
                timeout=SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"ok": False, "device": found.as_dict(),
                    "detail": f"Sending {path.name} timed out."}
        except Exception as exc:
            return {"ok": False, "device": found.as_dict(),
                    "detail": f"Could not run Tailscale: {str(exc)[:160]}"}

        if code != 0:
            return {"ok": False, "device": found.as_dict(),
                    "detail": (output or "").strip()[:300] or "The copy failed."}

        await self._record(f"sent {path.name} ({megabytes:.0f} MB) to "
                           f"{found.name} over Taildrop")
        return {
            "ok": True,
            "device": found.as_dict(),
            "file": path.name,
            "megabytes": round(megabytes, 1),
            "detail": (f"Sent {path.name} ({megabytes:.1f} MB) to {found.name}. "
                       "Open Tailscale on that device to save it."),
        }

    async def send_many(self, paths: List[Path], device: str) -> Dict[str, Any]:
        """Send several files, reporting each rather than stopping at the first
        failure -- a partial send is worth knowing the shape of."""
        results = []
        for path in paths:
            results.append(await self.send(path, device))
        sent = [r for r in results if r.get("ok")]
        return {
            "ok": bool(sent) and len(sent) == len(results),
            "sent": len(sent),
            "total": len(results),
            "results": results,
            "detail": f"{len(sent)} of {len(results)} sent.",
        }

    async def _record(self, message: str) -> None:
        if self.activity is not None:
            try:
                await self.activity.record(message, source="taildrop", level="info")
            except Exception:
                pass
        if self.audit is not None:
            try:
                await self.audit.record("taildrop.send", subject=message)
            except Exception:
                pass


# ---------------------------------------------------------------------------
def _first_ip(node: Dict[str, Any]) -> str:
    addresses = node.get("TailscaleIPs") or []
    for address in addresses:
        if ":" not in address:          # prefer IPv4, which is shorter to read
            return address
    return addresses[0] if addresses else ""


def _is_tailnet(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for network in (TAILNET_V4, TAILNET_V6):
        candidate = ipaddress.ip_network(network)
        if parsed.version == candidate.version and parsed in candidate:
            return True
    return False
