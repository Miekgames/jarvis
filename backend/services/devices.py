"""
Connected JARVIS — the device mesh.

One machine runs the JARVIS backend; every other device (phone, laptop, tablet)
pairs with it and talks to the same brain. That keeps state genuinely shared:
one task list, one memory, one conversation history, visible from anywhere.

Pairing uses a short-lived code shown on the host and typed on the new device;
the device then holds a token whose hash alone is stored, exactly like a browser
session. Each device carries its own permission list, so a phone can be allowed
to ask questions and read status without being allowed to control the computer.

Handoff is a queue: "send this to my PC" writes an entry the target device
claims the next time it polls or over its WebSocket.
"""
from __future__ import annotations

import logging
import platform
import secrets
import socket
from typing import Any, Dict, List, Optional

from ..core.events import bus
from ..core.security import token_hash
from ..core.util import ahead, new_id, now_iso, parse_iso, since, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.devices")

PAIRING_TTL_SECONDS = 300
PRESENCE_TIMEOUT_SECONDS = 120

# What a paired device may do. Deliberately narrower than the host's own
# permissions -- a lost phone should not be able to shut down the desktop.
DEVICE_PERMISSIONS = {
    "ask": "Talk to JARVIS and read its replies",
    "tasks.read": "See background tasks and their progress",
    "tasks.create": "Start background tasks",
    "status": "See system status and notifications",
    "control": "Control this computer (apps, volume, power)",
    "handoff": "Send commands to other devices",
}

DEFAULT_DEVICE_PERMISSIONS = ["ask", "tasks.read", "status", "handoff"]


class DeviceService:
    def __init__(self, db, settings, activity, audit=None):
        self.db = db
        self.settings = settings
        self.activity = activity
        self.audit = audit
        self._pairing: Dict[str, Dict[str, Any]] = {}

    # ==================================================================
    async def bootstrap(self) -> Dict[str, Any]:
        """Register the host machine as device zero."""
        existing = await self.db.fetch_one("SELECT * FROM devices WHERE is_self = 1")
        if existing:
            await self.heartbeat(existing["id"])
            return _decode(existing)

        device_id = new_id("dev")
        await self.db.execute(
            "INSERT INTO devices (id, name, kind, platform, address, permissions, last_seen,"
            " paired_at, is_self) VALUES (?,?,?,?,?,?,?,?,1)",
            (device_id, socket.gethostname(), "desktop",
             f"{platform.system()} {platform.release()}", _local_address(),
             dumps(list(DEVICE_PERMISSIONS)), now_iso(), now_iso()),
        )
        logger.info("registered this machine as device %s", device_id)
        return await self.get(device_id)

    # ==================================================================
    # pairing
    # ==================================================================
    async def begin_pairing(self, name: str = "New device", kind: str = "mobile",
                            permissions: Optional[List[str]] = None) -> Dict[str, Any]:
        """Produce a short code to type on the other device."""
        code = f"{secrets.randbelow(1000000):06d}"
        granted = [p for p in (permissions or DEFAULT_DEVICE_PERMISSIONS) if p in DEVICE_PERMISSIONS]
        self._pairing[code] = {
            "name": name,
            "kind": kind,
            "permissions": granted,
            "expires": ahead(PAIRING_TTL_SECONDS),
        }
        await self.activity.record(f"Pairing code issued for '{name}'.", source="devices")
        return {
            "code": code,
            "expires_in_seconds": PAIRING_TTL_SECONDS,
            "permissions": granted,
            "connect_to": f"http://{_local_address()}:{self.settings.PORT}",
        }

    async def complete_pairing(self, code: str, device_name: str = "",
                               address: str = "") -> Dict[str, Any]:
        """Redeem a pairing code. Returns the device token exactly once."""
        pending = self._pairing.get(code)
        if pending is None:
            raise PermissionError("That pairing code is not valid.")
        expires = parse_iso(pending["expires"])
        if expires and expires < utcnow():
            self._pairing.pop(code, None)
            raise PermissionError("That pairing code has expired. Generate a new one.")

        self._pairing.pop(code, None)
        token = secrets.token_urlsafe(32)
        device_id = new_id("dev")

        await self.db.execute(
            "INSERT INTO devices (id, name, kind, platform, address, token_hash, permissions,"
            " last_seen, paired_at, is_self) VALUES (?,?,?,?,?,?,?,?,?,0)",
            (device_id, device_name or pending["name"], pending["kind"], "",
             address, token_hash(token), dumps(pending["permissions"]),
             now_iso(), now_iso()),
        )

        if self.audit:
            await self.audit.record(
                "device.paired", subject=device_id,
                detail={"name": device_name or pending["name"],
                        "permissions": pending["permissions"]},
            )
        await self.activity.record(
            f"Device paired: {device_name or pending['name']}", source="devices")
        bus.publish("device.paired", {"id": device_id, "name": device_name or pending["name"]},
                    source="devices")

        return {"device_id": device_id, "token": token,
                "permissions": pending["permissions"],
                "note": "This token is shown once. Store it on the device."}

    # ==================================================================
    async def authenticate(self, token: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM devices WHERE token_hash = ?", (token_hash(token),))
        if not row:
            return None
        await self.heartbeat(row["id"])
        return _decode(row)

    def may(self, device: Dict[str, Any], permission: str) -> bool:
        return permission in (device.get("permissions") or [])

    # ==================================================================
    async def heartbeat(self, device_id: str) -> None:
        await self.db.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ?", (now_iso(), device_id))

    async def present(self) -> List[Dict[str, Any]]:
        """Devices that have checked in recently."""
        rows = await self.db.fetch_all(
            "SELECT * FROM devices WHERE last_seen > ? ORDER BY is_self DESC, name",
            (since(PRESENCE_TIMEOUT_SECONDS),),
        )
        return [_decode(r) for r in rows]

    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM devices ORDER BY is_self DESC, paired_at DESC")
        devices = []
        for row in rows:
            device = _decode(row)
            last = parse_iso(row["last_seen"])
            device["online"] = bool(
                last and (utcnow() - last).total_seconds() < PRESENCE_TIMEOUT_SECONDS)
            devices.append(device)
        return devices

    async def get(self, device_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM devices WHERE id = ?", (device_id,))
        return _decode(row) if row else {}

    async def revoke(self, device_id: str) -> Dict[str, Any]:
        device = await self.get(device_id)
        if not device:
            raise KeyError(device_id)
        if device.get("is_self"):
            raise PermissionError("This is the host machine; it can't be unpaired from itself.")

        await self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await self.db.execute(
            "DELETE FROM handoffs WHERE to_device = ? OR from_device = ?", (device_id, device_id))
        if self.audit:
            await self.audit.record("device.revoked", subject=device_id,
                                    detail={"name": device["name"]})
        await self.activity.record(f"Device unpaired: {device['name']}", source="devices")
        return {"revoked": device_id, "name": device["name"]}

    async def set_permissions(self, device_id: str, permissions: List[str]) -> Dict[str, Any]:
        granted = [p for p in permissions if p in DEVICE_PERMISSIONS]
        await self.db.execute(
            "UPDATE devices SET permissions = ? WHERE id = ?", (dumps(granted), device_id))
        if self.audit:
            await self.audit.record("device.permissions_changed", subject=device_id,
                                    detail={"permissions": granted})
        return await self.get(device_id)

    # ==================================================================
    # handoff
    # ==================================================================
    async def handoff(self, to_device: str, payload: Dict[str, Any],
                      from_device: str = "") -> Dict[str, Any]:
        """Queue something for another device: a command to run, a conversation
        to continue, a link to open."""
        target = await self.get(to_device)
        if not target:
            raise KeyError(f"No device '{to_device}'.")

        handoff_id = new_id("hnd")
        await self.db.execute(
            "INSERT INTO handoffs (id, ts, from_device, to_device, payload, status)"
            " VALUES (?,?,?,?,?, 'pending')",
            (handoff_id, now_iso(), from_device, to_device, dumps(payload)),
        )
        bus.publish(
            "device.handoff",
            {"id": handoff_id, "to": to_device, "to_name": target["name"], "payload": payload},
            source="devices",
        )
        await self.activity.record(
            f"Handed off to {target['name']}: {truncate(payload.get('text') or payload.get('kind', ''), 60)}",
            source="devices",
        )
        return {"handoff_id": handoff_id, "to": target["name"], "status": "pending"}

    async def claim(self, device_id: str) -> List[Dict[str, Any]]:
        """Called by a device to collect anything waiting for it."""
        rows = await self.db.fetch_all(
            "SELECT * FROM handoffs WHERE to_device = ? AND status = 'pending' ORDER BY ts",
            (device_id,),
        )
        for row in rows:
            await self.db.execute(
                "UPDATE handoffs SET status = 'claimed', claimed_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
        await self.heartbeat(device_id)
        return [{"id": r["id"], "from": r["from_device"], "payload": loads(r["payload"], {}),
                 "ts": r["ts"]} for r in rows]

    async def pending_handoffs(self, limit: int = 25) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM handoffs ORDER BY ts DESC LIMIT ?", (limit,))
        return [{**dict(r), "payload": loads(r["payload"], {})} for r in rows]

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        devices = await self.list()
        return {
            "devices": len(devices),
            "online": sum(1 for d in devices if d.get("online")),
            "pending_pairings": len(self._pairing),
            "pending_handoffs": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM handoffs WHERE status = 'pending'", default=0)),
            "host_address": f"{_local_address()}:{self.settings.PORT}",
            "available_permissions": DEVICE_PERMISSIONS,
        }


# ---------------------------------------------------------------------------
def _local_address() -> str:
    """Best-effort LAN address. No traffic is actually sent by the connect()."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "127.0.0.1"


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["permissions"] = loads(item.get("permissions"), [])
    item["is_self"] = bool(item.get("is_self"))
    item.pop("token_hash", None)      # never leaves the service
    return item
