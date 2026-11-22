"""Authentication, sessions, permissions, confirmations and the audit trail."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.permissions import PermissionDenied, Risk
from backend.core.security import AuthError, hash_password, is_loopback, verify_password


# ===========================================================================
# passwords & sessions
# ===========================================================================
def test_password_hashing_round_trip():
    creds = hash_password("correct horse battery staple")
    assert creds["hash"] and creds["salt"]
    assert verify_password("correct horse battery staple", creds["hash"], creds["salt"])
    assert not verify_password("wrong", creds["hash"], creds["salt"])


def test_password_hashing_is_salted():
    a = hash_password("same")
    b = hash_password("same")
    assert a["salt"] != b["salt"]
    assert a["hash"] != b["hash"], "identical passwords must not produce identical hashes"


def test_loopback_detection():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("192.168.1.50")
    assert not is_loopback("8.8.8.8")
    assert not is_loopback(None)


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(auth):
    with pytest.raises(AuthError, match="Incorrect password"):
        await auth.login("not-the-password", ip="127.0.0.1")


@pytest.mark.asyncio
async def test_login_and_resolve_session(auth):
    token = await auth.login("test-password", ip="127.0.0.1")
    assert token

    user = await auth.resolve(token, ip="127.0.0.1")
    assert user is not None
    assert user["role"] == "owner"


@pytest.mark.asyncio
async def test_unknown_token_is_rejected(auth):
    assert await auth.resolve("not-a-real-token", ip="127.0.0.1") is None


@pytest.mark.asyncio
async def test_logout_invalidates_the_session(auth):
    token = await auth.login("test-password", ip="127.0.0.1")
    await auth.logout(token)
    assert await auth.resolve(token, ip="127.0.0.1") is None


@pytest.mark.asyncio
async def test_session_token_is_not_stored_in_plaintext(auth, db):
    token = await auth.login("test-password", ip="127.0.0.1")
    rows = await db.fetch_all("SELECT token_hash FROM sessions")
    assert rows
    assert all(row["token_hash"] != token for row in rows)


@pytest.mark.asyncio
async def test_expired_sessions_are_purged(auth, db):
    from backend.core.util import since

    token = await auth.login("test-password", ip="127.0.0.1")
    await db.execute("UPDATE sessions SET expires_at = ?", (since(3600),))

    assert await auth.resolve(token, ip="127.0.0.1") is None
    assert await db.fetch_value("SELECT COUNT(*) FROM sessions", default=0) == 0


# ===========================================================================
# permissions
# ===========================================================================

@pytest.mark.asyncio
async def test_safe_permissions_default_on(permissions):
    for key in ("files.read", "system.info", "web.search", "calendar.read"):
        assert permissions.allowed(key), f"{key} should be enabled by default"


def test_the_registry_still_knows_what_is_dangerous():
    """The definitions are what `risk_of()` reads, so they must not be
    thinned out just because the gate is open. Removing them would silently
    downgrade every critical action to medium."""
    from backend.core.permissions import BY_KEY, Risk

    assert BY_KEY["shell.execute"].risk == Risk.CRITICAL
    assert BY_KEY["files.delete"].risk == Risk.HIGH
    # The stored defaults are still least-privilege, which is what makes
    # flipping ALL_GRANTED back a real restoration.
    assert BY_KEY["files.delete"].default is False
    assert BY_KEY["shell.execute"].default is False


@pytest.mark.asyncio
async def test_revoking_everything_still_writes_through(permissions, db):
    """The one route back if granting everything turns out to be wrong."""
    await permissions.revoke_all()
    row = await db.fetch_one(
        "SELECT enabled FROM permissions WHERE key = 'shell.execute'")
    assert row["enabled"] == 0


@pytest.mark.asyncio
async def test_the_switches_gate_and_the_risky_ones_start_off(permissions):
    """In this edition every capability has a real switch.

    A fresh install can read and write files but cannot delete them or run
    shell commands until the owner turns those on.
    """
    from backend.core.permissions import ALL_GRANTED

    assert ALL_GRANTED is False
    assert permissions.allowed("files.read") and permissions.allowed("files.write")
    for key in ("files.delete", "shell.execute", "email.send", "system.power"):
        assert not permissions.allowed(key), key


@pytest.mark.asyncio
async def test_a_switch_takes_effect_at_once_and_is_stored(permissions, db):
    """Turning a capability on or off is immediate, and survives a restart."""
    await permissions.set("files.delete", True)
    assert permissions.allowed("files.delete")
    await permissions.set("files.delete", False)
    row = await db.fetch_one(
        "SELECT enabled FROM permissions WHERE key = 'files.delete'")
    assert row["enabled"] == 0
    assert not permissions.allowed("files.delete")


@pytest.mark.asyncio
async def test_risk_tiers_survive_so_confirmations_still_work(permissions):
    """Permissions and confirmations are separate layers. Granting
    everything must not quietly downgrade the second one."""
    assert permissions.risk_of("shell.execute") == "critical"
    assert permissions.risk_of("files.delete") == "high"
    assert permissions.risk_of("files.read") == "low"


@pytest.mark.asyncio
async def test_permission_changes_are_audited(permissions, audit):
    await permissions.set("files.delete", True)
    entries = await audit.recent(event="permission.changed")
    assert entries
    assert entries[0]["subject"] == "files.delete"


@pytest.mark.asyncio
async def test_revoke_all_disables_everything(permissions):
    await permissions.set("files.read", True)
    await permissions.revoke_all()
    assert not any(p["enabled"] for p in await permissions.list())


@pytest.mark.asyncio
async def test_unknown_permission_key_rejected(permissions):
    with pytest.raises(KeyError):
        await permissions.set("not.a.real.permission", True)


# ===========================================================================
# confirmations
# ===========================================================================
@pytest.mark.asyncio
async def test_low_risk_actions_skip_confirmation(confirmations):
    approved = await confirmations.request("open_app", "Open Discord?", risk=Risk.LOW, timeout=1)
    assert approved is True


@pytest.mark.asyncio
async def test_high_risk_action_blocks_until_approved(confirmations):
    request = asyncio.create_task(
        confirmations.request("delete_file", "Delete report.pdf?", risk=Risk.HIGH, timeout=5)
    )
    await asyncio.sleep(0.2)

    pending = await confirmations.pending()
    assert len(pending) == 1
    assert not request.done(), "the tool call must actually wait for the answer"

    await confirmations.resolve(pending[0]["id"], approved=True)
    assert await request is True


@pytest.mark.asyncio
async def test_rejection_returns_false(confirmations):
    request = asyncio.create_task(
        confirmations.request("shutdown", "Shut down the computer?", risk=Risk.CRITICAL, timeout=5)
    )
    await asyncio.sleep(0.2)
    pending = await confirmations.pending()
    await confirmations.resolve(pending[0]["id"], approved=False)

    assert await request is False


@pytest.mark.asyncio
async def test_confirmation_times_out_as_refusal(confirmations):
    approved = await confirmations.request(
        "send_email", "Send this email?", risk=Risk.HIGH, timeout=0.4
    )
    assert approved is False, "an unanswered confirmation must never be treated as approval"

    history = await confirmations.history()
    assert history[0]["status"] == "expired"


@pytest.mark.asyncio
async def test_stale_confirmations_expire_on_boot(confirmations, db):
    from backend.core.util import new_id, now_iso

    await db.execute(
        "INSERT INTO confirmations (id, ts, tool, summary, detail, risk, status)"
        " VALUES (?,?,?,?,?,?,?)",
        (new_id("cfm"), now_iso(), "delete_file", "left over", "{}", "high", "pending"),
    )
    count = await confirmations.expire_stale()
    assert count == 1
    assert await confirmations.pending() == []


@pytest.mark.asyncio
async def test_confirmation_decisions_are_audited(confirmations, audit):
    request = asyncio.create_task(
        confirmations.request("delete_file", "Delete it?", risk=Risk.HIGH, timeout=5)
    )
    await asyncio.sleep(0.2)
    pending = await confirmations.pending()
    await confirmations.resolve(pending[0]["id"], approved=True)
    await request

    entries = await audit.recent(event="confirmation.approved")
    assert entries and entries[0]["subject"] == "delete_file"


# ===========================================================================
# audit redaction
# ===========================================================================
@pytest.mark.asyncio
async def test_audit_never_records_secrets(audit):
    await audit.record(
        "integration.connected",
        subject="github",
        detail={"api_key": "sk-super-secret-value", "token": "ghp_abc123", "repo": "owner/site"},
    )
    entry = (await audit.recent())[0]
    assert entry["detail"]["api_key"] == "<redacted>"
    assert entry["detail"]["token"] == "<redacted>"
    assert entry["detail"]["repo"] == "owner/site"


def test_redaction_catches_inline_credentials():
    from backend.core.util import redact

    assert "sk-" not in redact("key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456")
    assert "<redacted>" in redact("Authorization: Bearer abcdef123456789")


# ===========================================================================
# websocket authentication
# ---------------------------------------------------------------------------
# The live event stream has its own auth path, separate from the HTTP one, and
# it is the half that is invisible when it breaks: the interface loads
# perfectly, every panel renders, and nothing ever updates.
#
# It read the token only from ?token=... . On the machine JARVIS runs on that
# is never noticed, because local mode accepts a loopback connection before
# the token is consulted at all. Open the same page from a phone, with a
# password, and the handshake carries no query parameter -- a browser cannot
# add one, since the session cookie is httpOnly and unreadable from script --
# so it closed 4401 and the interface sat at OFFLINE forever.
# ===========================================================================
class _StubWebSocket:
    """Just enough WebSocket for the endpoint's auth decision."""

    def __init__(self, cookies=None, query=None, host="100.64.0.20"):
        self.cookies = cookies or {}
        self.query_params = query or {}
        self.client = type("C", (), {"host": host})()
        self.closed_with = None
        self.accepted = False

    async def close(self, code=1000):
        self.closed_with = code


async def _resolve_like_the_endpoint(auth, websocket):
    """The token selection the websocket endpoint performs, verbatim."""
    from backend.api.deps import SESSION_COOKIE

    token = (websocket.cookies.get(SESSION_COOKIE)
             or websocket.query_params.get("token"))
    return await auth.resolve(
        token, ip=websocket.client.host, client="websocket")


@pytest.mark.asyncio
async def test_websocket_accepts_the_session_cookie(auth):
    """The phone case: a cookie, no query parameter, not loopback."""
    from backend.api.deps import SESSION_COOKIE

    token = await auth.login("test-password", ip="100.64.0.20")
    socket = _StubWebSocket(cookies={SESSION_COOKIE: token})

    user = await _resolve_like_the_endpoint(auth, socket)
    assert user is not None, (
        "a browser cannot put an httpOnly cookie in the query string, so "
        "refusing this means the live stream never connects from any device "
        "other than the one JARVIS runs on")
    assert user["token"] == token


@pytest.mark.asyncio
async def test_websocket_still_accepts_an_explicit_token(auth):
    """Non-browser clients have a token and no cookie jar."""
    token = await auth.login("test-password", ip="100.64.0.20")
    socket = _StubWebSocket(query={"token": token})
    assert await _resolve_like_the_endpoint(auth, socket) is not None


@pytest.mark.asyncio
async def test_websocket_refuses_a_connection_with_neither(auth):
    socket = _StubWebSocket()
    assert await _resolve_like_the_endpoint(auth, socket) is None


@pytest.mark.asyncio
async def test_websocket_refuses_a_stale_cookie(auth):
    from backend.api.deps import SESSION_COOKIE

    token = await auth.login("test-password", ip="100.64.0.20")
    await auth.logout(token)
    socket = _StubWebSocket(cookies={SESSION_COOKIE: token})
    assert await _resolve_like_the_endpoint(auth, socket) is None


@pytest.mark.asyncio
async def test_the_cookie_is_preferred_over_the_query_parameter(auth):
    """A token in a URL reaches server logs, proxy logs and history, so the
    cookie is the primary and the parameter only a fallback."""
    from backend.api.deps import SESSION_COOKIE

    good = await auth.login("test-password", ip="100.64.0.20")
    socket = _StubWebSocket(cookies={SESSION_COOKIE: good},
                            query={"token": "a-stale-token-from-an-old-url"})
    user = await _resolve_like_the_endpoint(auth, socket)
    assert user is not None and user["token"] == good


def test_the_endpoint_actually_reads_the_cookie():
    """Guards the line itself, so the behaviour above cannot drift away from
    the code the tests above only imitate."""
    import inspect

    from backend.api import core_routes

    source = inspect.getsource(core_routes.websocket_endpoint)
    assert "websocket.cookies.get(SESSION_COOKIE)" in source
