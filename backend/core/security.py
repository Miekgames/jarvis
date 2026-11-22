"""
Authentication and session management.

Two modes, both using the same session machinery so nothing downstream has to
care which is active:

  local     -- loopback clients are auto-issued an owner session. Correct
               default for a single-user machine; remote requests still 401.
  password  -- everyone authenticates with JARVIS_PASSWORD.

Passwords are stored as PBKDF2-HMAC-SHA256 (260k iterations) with a per-user
salt. Session tokens are random 32-byte values; only their SHA-256 hash is
persisted, so a stolen database cannot be replayed against a live server.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
from typing import Any, Dict, Optional

from .util import ahead, now_iso, parse_iso, utcnow

logger = logging.getLogger("jarvis.security")

_PBKDF2_ROUNDS = 260_000
_LOCAL_USERNAME = "owner"


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS)
    return {"hash": digest.hex(), "salt": salt}


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    if not stored_hash or not salt:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS)
    return hmac.compare_digest(candidate.hex(), stored_hash)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_loopback(ip: Optional[str]) -> bool:
    if not ip:
        return False
    host = ip.split("%")[0]
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
class AuthError(Exception):
    """Raised for any failed authentication attempt."""


class AuthService:
    def __init__(self, db, settings, audit=None):
        self.db = db
        self.settings = settings
        self.audit = audit

    # ------------------------------------------------------------------
    async def ensure_owner(self) -> Dict[str, Any]:
        """Create the owner account on first boot. In password mode the
        password comes from JARVIS_PASSWORD; changing it in .env re-hashes on
        the next start."""
        row = await self.db.fetch_one("SELECT * FROM users WHERE username = ?", (_LOCAL_USERNAME,))
        if row is None:
            creds = hash_password(self.settings.PASSWORD) if self.settings.PASSWORD else {"hash": "", "salt": ""}
            await self.db.execute(
                "INSERT INTO users (username, password_hash, salt, role, display_name, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    _LOCAL_USERNAME,
                    creds["hash"],
                    creds["salt"],
                    "owner",
                    self.settings.OWNER_NAME,
                    now_iso(),
                ),
            )
            row = await self.db.fetch_one("SELECT * FROM users WHERE username = ?", (_LOCAL_USERNAME,))
            logger.info("created owner account")
        elif self.settings.PASSWORD and not verify_password(
            self.settings.PASSWORD, row["password_hash"], row["salt"]
        ):
            creds = hash_password(self.settings.PASSWORD)
            await self.db.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (creds["hash"], creds["salt"], row["id"]),
            )
            logger.info("owner password updated from environment")
            row = await self.db.fetch_one("SELECT id FROM users WHERE id = ?", (row["id"],))
        return row or {}

    # ------------------------------------------------------------------
    async def login(self, password: str, ip: str = "", client: str = "") -> str:
        if self.settings.AUTH_MODE == "local":
            if not is_loopback(ip):
                await self._audit("auth.login", "rejected: non-loopback in local mode", ip, "denied")
                raise AuthError("Local auth mode only accepts connections from this machine.")
            return await self._issue(await self._owner_id(), ip, client)

        user = await self.db.fetch_one("SELECT * FROM users WHERE username = ?", (_LOCAL_USERNAME,))
        if not user or not user["password_hash"]:
            await self._audit("auth.login", "no password configured", ip, "denied")
            raise AuthError("No password is configured. Set JARVIS_PASSWORD in .env and restart.")
        if not verify_password(password or "", user["password_hash"], user["salt"]):
            await self._audit("auth.login", "bad password", ip, "denied")
            raise AuthError("Incorrect password.")
        await self.db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), user["id"]))
        await self._audit("auth.login", "success", ip, "ok")
        return await self._issue(user["id"], ip, client)

    async def _owner_id(self) -> int:
        row = await self.db.fetch_one("SELECT id FROM users WHERE username = ?", (_LOCAL_USERNAME,))
        if row is None:
            row = await self.ensure_owner()
        return int(row["id"])

    async def _issue(self, user_id: int, ip: str, client: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, client, ip)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                token_hash(token),
                user_id,
                now_iso(),
                ahead(self.settings.SESSION_HOURS * 3600),
                now_iso(),
                client[:200],
                ip,
            ),
        )
        return token

    # ------------------------------------------------------------------
    async def resolve(self, token: Optional[str], ip: str = "", client: str = "") -> Optional[Dict[str, Any]]:
        """Return the authenticated user, or None. In local mode a loopback
        request without a token is transparently given one."""
        if token:
            session = await self.db.fetch_one(
                "SELECT s.*, u.username, u.role, u.display_name FROM sessions s"
                " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (token_hash(token),),
            )
            if session:
                expires = parse_iso(session["expires_at"])
                if expires and expires > utcnow():
                    await self.db.execute(
                        "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                        (now_iso(), session["token_hash"]),
                    )
                    return {
                        "id": session["user_id"],
                        "username": session["username"],
                        "role": session["role"],
                        "display_name": session["display_name"],
                        "token": token,
                    }
                await self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))

        if self.settings.AUTH_MODE == "local" and is_loopback(ip):
            user_id = await self._owner_id()
            fresh = await self._issue(user_id, ip, client)
            return {
                "id": user_id,
                "username": _LOCAL_USERNAME,
                "role": "owner",
                "display_name": self.settings.OWNER_NAME,
                "token": fresh,
                "auto": True,
            }
        return None

    # ------------------------------------------------------------------
    async def logout(self, token: str, ip: str = "") -> None:
        await self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        await self._audit("auth.logout", "session ended", ip, "ok")

    async def logout_all(self, user_id: int, ip: str = "") -> int:
        count = await self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self._audit("auth.logout_all", f"{count} sessions revoked", ip, "ok")
        return count

    async def sessions(self, user_id: int):
        return await self.db.fetch_all(
            "SELECT token_hash, created_at, expires_at, last_seen, client, ip FROM sessions"
            " WHERE user_id = ? ORDER BY last_seen DESC",
            (user_id,),
        )

    async def purge_expired(self) -> int:
        return await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))

    async def _audit(self, event: str, detail: str, ip: str, result: str) -> None:
        if self.audit:
            await self.audit.record(event, subject=_LOCAL_USERNAME, detail={"note": detail}, ip=ip, result=result)
