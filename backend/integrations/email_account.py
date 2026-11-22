"""The owner's email, over IMAP and SMTP.

JARVIS reaches a mailbox the way a mail app does: IMAP to read, SMTP to
send, signed in with an *app password* -- the separate password Gmail,
iCloud and Yahoo let you create for one app, which can be revoked on its
own without touching the account password. No Google Cloud project, no
OAuth app to register, nothing but the standard library.

What it promises:

  * **Reading never changes the mailbox.** Folders are opened read-only
    (EXAMINE) and messages fetched with BODY.PEEK, so nothing is marked as
    read, moved or deleted by JARVIS looking at it.
  * **The password is sealed.** It is stored encrypted with Windows DPAPI
    (see `core.secrets`), only ever sent to the provider's own servers, and
    never returned to the browser -- `describe()` has no secret in it.
  * **Sending is the owner's decision.** The `send_email` tool is HIGH risk:
    the confirmation dialog shows the exact message and nothing goes until
    the owner approves it. Unattended (Away) work can never send.
  * **What arrives is untrusted.** Mail is written by other people. Tool
    results pass through the prompt-injection screen like any web page.

Ported from OpenJarvis ``connectors/gmail_imap.py`` (Stanford Hazy Research,
Apache-2.0): IMAP with an app password instead of an OAuth app, TLS by
default, header decoding that survives raw 8-bit bytes. Reading with PEEK,
read-only folders, partial fetches, short message ids, Gmail search in All
Mail, sending and the error messages are JARVIS's.
"""
from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import json
import logging
import re
import smtplib
import socket
import ssl
import time
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core import secrets
from ..core.util import now_iso

logger = logging.getLogger("jarvis.email")

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "gmail": {
        "label": "Gmail",
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_security": "tls",
        "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl",
        "password_url": "https://myaccount.google.com/apppasswords",
        "web": "https://mail.google.com/mail/u/0/#inbox",
        "domains": ("gmail.com", "googlemail.com"),
        "app_password": True,
    },
    "icloud": {
        "label": "iCloud Mail",
        "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_security": "tls",
        "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_security": "starttls",
        "password_url": "https://account.apple.com/account/manage",
        "web": "https://www.icloud.com/mail",
        "domains": ("icloud.com", "me.com", "mac.com"),
        "app_password": True,
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_security": "tls",
        "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_security": "ssl",
        "password_url": "https://login.yahoo.com/account/security",
        "web": "https://mail.yahoo.com/",
        "domains": ("yahoo.com", "yahoo.co.uk", "ymail.com", "rocketmail.com"),
        "app_password": True,
    },
    "outlook": {
        "label": "Outlook / Hotmail",
        "imap_host": "outlook.office365.com", "imap_port": 993, "imap_security": "tls",
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
        "password_url": "https://account.live.com/proofs/AppPassword",
        "web": "https://outlook.live.com/mail/",
        "domains": ("outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com"),
        "app_password": True,
        # Microsoft has been moving Outlook.com to browser sign-in only for
        # other apps; an app password may simply be refused.
        "caveat": "Microsoft has been switching Outlook.com over to browser sign-in only for "
                  "other apps, so it may refuse an app password. If it does, Outlook can't be "
                  "reached this way.",
    },
    "other": {
        "label": "Other (IMAP)",
        "imap_host": "", "imap_port": 993, "imap_security": "tls",
        "smtp_host": "", "smtp_port": 465, "smtp_security": "ssl",
        "password_url": "", "web": "", "domains": (), "app_password": False,
    },
}

ACCOUNT_FILE = "email.json"
TIMEOUT_SECONDS = 30
SNIPPET_BYTES = 64 * 1024          # enough of each message for its first lines
READ_BYTES = 2 * 1024 * 1024       # enough for the text, not for every attachment
MAX_BODY_CHARS = 12000
MAX_LIST = 50
UNREAD_CACHE_SECONDS = 60
SECURITY = ("tls", "starttls", "ssl")

# ids are short enough for a small model to copy: "i.<uidvalidity>.<uid>"
# for the inbox, "a.<uidvalidity>.<uid>" for Gmail's All Mail.
_ID = re.compile(r"^([ia])\.(\d+)\.(\d+)$")
_ADDRESS = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")


class EmailError(Exception):
    """Something the owner should be told in plain words."""


# ---------------------------------------------------------------------------
# Parsing -- pure functions, tested without a server
# ---------------------------------------------------------------------------
def provider_for(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for key, preset in PROVIDERS.items():
        if domain in preset["domains"]:
            return key
    return "other"


def decode_text(raw: Any) -> str:
    """A header as plain text, whatever encoding or raw bytes it arrived in."""
    if not raw:
        return ""
    out: List[str] = []
    try:
        pieces = decode_header(raw if isinstance(raw, str) else str(raw))
    except Exception:
        return " ".join(str(raw).split())
    for part, charset in pieces:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(part)
    return " ".join("".join(out).split())


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    from ..services.web import _TextExtractor

    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.text()


def body_of(message: Message) -> Tuple[str, List[str]]:
    """(the readable text, attachment names). Plain text is preferred."""
    plain: List[str] = []
    html: List[str] = []
    attachments: List[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition") or "").lower()
        if filename or disposition.startswith("attachment"):
            attachments.append(decode_text(filename) or "attachment")
            continue
        kind = part.get_content_type()
        if kind == "text/plain":
            plain.append(_payload_text(part))
        elif kind == "text/html":
            html.append(_payload_text(part))
    text = "\n\n".join(p for p in plain if p.strip())
    if not text and html:
        text = "\n\n".join(html_to_text(h) for h in html)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, attachments


def snippet_of(text: str, length: int = 180) -> str:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    flat = " ".join(" ".join(lines).split())
    return flat if len(flat) <= length else flat[:length - 1].rstrip() + "…"


def _iso_date(message: Message) -> str:
    raw = message.get("Date")
    if not raw:
        return ""
    try:
        when = parsedate_to_datetime(str(raw))
    except Exception:
        return ""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.astimezone()
    return when.isoformat()


def _person(raw: Any) -> Dict[str, str]:
    name, address = parseaddr(decode_text(raw))
    return {"name": name or address, "address": address}


def _people(raw: Any) -> List[str]:
    return [f"{name} <{address}>" if name else address
            for name, address in getaddresses([decode_text(raw)]) if address]


_UID = re.compile(rb"\bUID (\d+)")
_FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
_NEW_MESSAGE = re.compile(rb"^\s*\d+ \(")


def parse_fetch(data: Any) -> List[Dict[str, Any]]:
    """Messages out of a UID FETCH response, however the server ordered it.

    imaplib hands back a list of (metadata, literal) tuples and bare byte
    strings. A server may put UID and FLAGS before the message literal or
    after it (Gmail does both), so both places are read.
    """
    records: List[Dict[str, Any]] = []

    def absorb(record: Dict[str, Any], chunk: bytes) -> None:
        uid = _UID.search(chunk)
        if uid and record["uid"] is None:
            record["uid"] = uid.group(1).decode()
        flags = _FLAGS.search(chunk)
        if flags and record["flags"] is None:
            record["flags"] = flags.group(1).decode(errors="replace").split()

    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            record = {"uid": None, "flags": None,
                      "raw": item[1] if isinstance(item[1], bytes) else b""}
            absorb(record, item[0] if isinstance(item[0], bytes) else b"")
            records.append(record)
        elif isinstance(item, bytes):
            if _NEW_MESSAGE.match(item):
                record = {"uid": None, "flags": None, "raw": b""}
                absorb(record, item)
                records.append(record)
            elif records:
                absorb(records[-1], item)
    for record in records:
        record["flags"] = record["flags"] or []
    return [r for r in records if r["uid"]]


def parse_list_line(line: Any) -> Optional[Tuple[List[str], str]]:
    """(flags, mailbox name) from one LIST response line."""
    if not isinstance(line, bytes):
        return None
    match = re.match(rb'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line.strip())
    if not match:
        return None
    name = match.group("name").decode("utf-8", errors="replace")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return match.group("flags").decode(errors="replace").split(), name


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def make_id(where: str, validity: str, uid: str) -> str:
    return f"{where}.{validity}.{uid}"


def split_id(message_id: str) -> Optional[Tuple[str, str, str]]:
    match = _ID.match((message_id or "").strip())
    return (match.group(1), match.group(2), match.group(3)) if match else None


def clean_addresses(raw: str) -> List[str]:
    """Recipients, checked. Raises EmailError on anything that isn't one."""
    if any(ch in (raw or "") for ch in "\r\n"):
        raise EmailError("A recipient can't contain a line break.")
    found = [address.strip() for _, address in getaddresses([raw or ""]) if address.strip()]
    bad = [a for a in found if not _ADDRESS.match(a)]
    if bad:
        raise EmailError(f"That doesn't look like an email address: {bad[0]}")
    return found


def explain_login(provider: str, server_text: str) -> str:
    label = PROVIDERS.get(provider, PROVIDERS["other"])["label"]
    text = server_text.strip()
    lowered = text.lower()
    if provider == "gmail" and ("authenticationfailed" in lowered or "invalid credentials" in lowered
                                or "application-specific" in lowered or "username and password" in lowered):
        return ("Gmail turned down that address and app password. Gmail only accepts an app "
                "password here — the 16 letters from myaccount.google.com/apppasswords, which "
                "needs 2-Step Verification switched on — not your normal Google password.")
    if "authenticationfailed" in lowered or "invalid credentials" in lowered or "535" in lowered:
        return (f"{label} turned down that address and password. Use an app password made "
                f"for JARVIS, not your normal account password.")
    return f"{label} refused the sign-in: {text or 'no reason given'}"


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def default_imap(host: str, port: int, security: str) -> imaplib.IMAP4:
    context = ssl.create_default_context()
    if security == "starttls":
        connection = imaplib.IMAP4(host, port, timeout=TIMEOUT_SECONDS)
        try:
            connection.starttls(ssl_context=context)
        except Exception:
            try:
                connection.shutdown()
            except Exception:
                pass
            raise
        return connection
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=TIMEOUT_SECONDS)


def default_smtp(host: str, port: int, security: str) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT_SECONDS)
    connection = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
    connection.starttls(context=context)
    return connection


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = b" ".join(v for v in value if isinstance(v, bytes))
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


class EmailAccount:
    """One mailbox. Blocking IMAP/SMTP runs in a worker thread."""

    def __init__(self, data_dir: Path, activity=None, audit=None,
                 imap_factory: Callable[..., Any] = default_imap,
                 smtp_factory: Callable[..., Any] = default_smtp) -> None:
        self.path = Path(data_dir) / ACCOUNT_FILE
        self.activity = activity
        self.audit = audit
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory
        self._config: Optional[Dict[str, Any]] = self._load()
        self._unread: Optional[Tuple[float, int]] = None
        self._last_listing: List[Dict[str, Any]] = []
        # Gmail allows 15 connections per account; JARVIS needs a couple.
        # Made on first use: on Python 3.9 a semaphore belongs to the loop
        # that was current when it was created.
        self._gate: Optional[asyncio.Semaphore] = None

    # ------------------------------------------------------------------
    def _load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("email account file unreadable; treating as not connected")
            return None
        return data if data.get("address") and data.get("password") else None

    @property
    def connected(self) -> bool:
        return self._config is not None

    @property
    def address(self) -> str:
        return (self._config or {}).get("address", "")

    def describe(self) -> Dict[str, Any]:
        """Everything the interface may show. Never the password."""
        if not self._config:
            return {"connected": False,
                    "providers": {key: {k: v for k, v in preset.items() if k != "domains"}
                                  for key, preset in PROVIDERS.items()}}
        config = self._config
        preset = PROVIDERS.get(config.get("provider", "other"), PROVIDERS["other"])
        return {
            "connected": True,
            "address": config["address"],
            "provider": config.get("provider", "other"),
            "provider_label": preset["label"],
            "imap_host": config["imap_host"],
            "smtp_host": config["smtp_host"],
            "web": preset.get("web", ""),
            "connected_at": config.get("connected_at", ""),
            "can_send": bool(config.get("smtp_ok")),
            "send_problem": config.get("smtp_error", ""),
            "password_storage": secrets.describe(config["password"]),
        }

    def _credentials(self) -> Tuple[Dict[str, Any], str]:
        if not self._config:
            raise EmailError("Email isn't connected. Connect it under Email in JARVIS's menu "
                             "with an app password from your email provider.")
        try:
            password = secrets.unseal(self._config["password"])
        except ValueError as exc:
            raise EmailError(f"The saved email password can't be opened here ({exc}). "
                             "Connect the account again.") from exc
        return self._config, password

    # ------------------------------------------------------------------
    async def _run(self, function: Callable[..., Any], *args: Any) -> Any:
        if self._gate is None:
            self._gate = asyncio.Semaphore(2)
        async with self._gate:
            return await asyncio.to_thread(function, *args)

    def _open(self, config: Dict[str, Any], password: str) -> Any:
        host, port = config["imap_host"], int(config["imap_port"])
        try:
            imap = self.imap_factory(host, port, config.get("imap_security", "tls"))
        except (socket.gaierror, socket.timeout, ssl.SSLError, OSError) as exc:
            raise EmailError(f"Couldn't reach {host}:{port} — {exc.__class__.__name__}: {exc}") from exc
        try:
            imap.login(config["address"], password)
        except imaplib.IMAP4.error as exc:
            self._close(imap)
            raise EmailError(explain_login(config.get("provider", "other"), _text(exc.args[0] if exc.args else exc))) from exc
        except Exception:
            self._close(imap)
            raise
        return imap

    @staticmethod
    def _close(imap: Any) -> None:
        if imap is None:
            return
        try:
            imap.logout()
        except Exception:
            try:
                imap.shutdown()
            except Exception:
                pass

    @staticmethod
    def _examine(imap: Any, mailbox: str) -> str:
        """Open a folder read-only. Returns its UIDVALIDITY."""
        status, data = imap.select(quote(mailbox), readonly=True)
        if status != "OK":
            raise EmailError(f"Couldn't open the {mailbox} folder: {_text(data)}")
        kind, values = imap.response("UIDVALIDITY")
        value = _text(values[-1] if values else b"").strip()
        if not value.isdigit():
            raise EmailError("The mail server didn't say which version of the folder this is "
                             "(UIDVALIDITY), so message ids can't be trusted.")
        return value

    @staticmethod
    def _all_mail(imap: Any) -> Optional[str]:
        """Gmail's All Mail folder -- '[Gmail]/All Mail', or '[Google Mail]/All Mail'
        on accounts made in the UK and Germany. Found by its \\All flag."""
        status, data = imap.list()
        if status != "OK":
            return None
        for line in data or []:
            parsed = parse_list_line(line)
            if parsed and "\\All" in parsed[0]:
                return parsed[1]
        return None

    def _summaries(self, imap: Any, where: str, validity: str, uids: List[str]) -> List[Dict[str, Any]]:
        if not uids:
            return []
        status, data = imap.uid("FETCH", ",".join(uids),
                                f"(UID FLAGS BODY.PEEK[]<0.{SNIPPET_BYTES}>)")
        if status != "OK":
            raise EmailError(f"The mail server wouldn't list those messages: {_text(data)}")
        by_uid = {record["uid"]: record for record in parse_fetch(data)}
        out = []
        for uid in uids:              # keep the order asked for: newest first
            record = by_uid.get(uid)
            if record is None:
                continue
            message = email_lib.message_from_bytes(record["raw"])
            text, attachments = body_of(message)
            sender = _person(message.get("From"))
            out.append({
                "id": make_id(where, validity, uid),
                "from": sender["name"],
                "from_address": sender["address"],
                "to": _people(message.get("To")),
                "subject": decode_text(message.get("Subject")) or "(no subject)",
                "date": _iso_date(message),
                "unread": "\\Seen" not in record["flags"],
                "starred": "\\Flagged" in record["flags"],
                "attachments": bool(attachments)
                or message.get_content_type() == "multipart/mixed",
                "snippet": snippet_of(text),
            })
        return out

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------
    async def connect(self, address: str, password: str, provider: str = "",
                      imap_host: str = "", imap_port: Optional[int] = None,
                      imap_security: str = "", smtp_host: str = "",
                      smtp_port: Optional[int] = None, smtp_security: str = "") -> Dict[str, Any]:
        """Sign in for real before keeping anything."""
        address = (address or "").strip()
        if not _ADDRESS.match(address):
            raise EmailError("Enter the full email address, e.g. name@gmail.com.")
        provider = provider if provider in PROVIDERS else provider_for(address)
        preset = PROVIDERS[provider]
        # App passwords are shown in groups ("abcd efgh ijkl mnop"); the
        # spaces are not part of them.
        password = "".join((password or "").split()) if preset["app_password"] \
            else (password or "").strip()
        if not password:
            raise EmailError("Paste the app password.")
        config = {
            "address": address,
            "provider": provider,
            "imap_host": (imap_host or preset["imap_host"]).strip(),
            "imap_port": int(imap_port or preset["imap_port"]),
            "imap_security": imap_security or preset["imap_security"],
            "smtp_host": (smtp_host or preset["smtp_host"]).strip(),
            "smtp_port": int(smtp_port or preset["smtp_port"]),
            "smtp_security": smtp_security or preset["smtp_security"],
        }
        if not config["imap_host"]:
            raise EmailError("Enter the IMAP server for this mailbox.")
        if config["imap_security"] not in SECURITY or config["smtp_security"] not in SECURITY:
            raise EmailError("Security must be tls, starttls or ssl.")

        def check() -> Tuple[bool, str]:
            imap = self._open(config, password)
            try:
                self._examine(imap, "INBOX")
            finally:
                self._close(imap)
            if not config["smtp_host"]:
                return False, "No outgoing (SMTP) server was given, so JARVIS can read but not send."
            try:
                smtp = self.smtp_factory(config["smtp_host"], config["smtp_port"],
                                         config["smtp_security"])
                try:
                    smtp.login(address, password)
                finally:
                    try:
                        smtp.quit()
                    except Exception:
                        pass
                return True, ""
            except smtplib.SMTPAuthenticationError as exc:
                return False, explain_login(provider, _text(exc.smtp_error))
            except Exception as exc:
                return False, f"Couldn't check sending: {exc.__class__.__name__}: {exc}"

        smtp_ok, smtp_error = await self._run(check)
        config.update(password=secrets.seal(password), smtp_ok=smtp_ok,
                      smtp_error=smtp_error, connected_at=now_iso())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self._config = config
        self._unread = None
        self._last_listing = []
        if self.audit:
            await self.audit.record("email.connected", subject=address,
                                    detail={"provider": provider, "can_send": smtp_ok})
        if self.activity:
            await self.activity.record(f"Email connected: {address}.", source="email")
        return self.describe()

    async def disconnect(self) -> bool:
        was = self.address
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._config = None
        self._unread = None
        self._last_listing = []
        if was:
            if self.audit:
                await self.audit.record("email.disconnected", subject=was)
            if self.activity:
                await self.activity.record(f"Email disconnected: {was}. The saved password "
                                           "was deleted.", source="email")
        return bool(was)

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    async def unread_count(self, fresh: bool = False) -> int:
        if not fresh and self._unread and time.monotonic() - self._unread[0] < UNREAD_CACHE_SECONDS:
            return self._unread[1]
        config, password = self._credentials()

        def count() -> int:
            imap = self._open(config, password)
            try:
                status, data = imap.status("INBOX", "(UNSEEN)")
                match = re.search(r"UNSEEN (\d+)", _text(data))
                if status != "OK" or not match:
                    raise EmailError("The mail server didn't say how many messages are unread.")
                return int(match.group(1))
            finally:
                self._close(imap)

        value = await self._run(count)
        self._unread = (time.monotonic(), value)
        return value

    async def inbox(self, unread_only: bool = False, limit: int = 15) -> Dict[str, Any]:
        config, password = self._credentials()
        limit = max(1, min(int(limit or 15), MAX_LIST))

        def fetch() -> Dict[str, Any]:
            imap = self._open(config, password)
            try:
                validity = self._examine(imap, "INBOX")
                status, data = imap.uid("SEARCH", None, "UNSEEN" if unread_only else "ALL")
                if status != "OK":
                    raise EmailError(f"The inbox search failed: {_text(data)}")
                uids = _text(data[0] if data else b"").split()
                matched = len(uids)
                newest = list(reversed(uids[-limit:]))
                return {"messages": self._summaries(imap, "i", validity, newest),
                        "matched": matched}
            finally:
                self._close(imap)

        result = await self._run(fetch)
        if unread_only:
            self._unread = (time.monotonic(), result["matched"])
        self._last_listing = result["messages"]
        return {**result, "unread_only": unread_only, "address": self.address}

    async def search(self, query: str, limit: int = 15) -> Dict[str, Any]:
        query = " ".join((query or "").split())
        if not query:
            raise EmailError("Say what to search for.")
        config, password = self._credentials()
        limit = max(1, min(int(limit or 15), MAX_LIST))

        def fetch() -> Dict[str, Any]:
            imap = self._open(config, password)
            try:
                gmail = "X-GM-EXT-1" in getattr(imap, "capabilities", ())
                folder, where = "INBOX", "i"
                if gmail:
                    found = self._all_mail(imap)
                    if found:
                        folder, where = found, "a"
                validity = self._examine(imap, folder)
                ascii_only = query.isascii()
                if gmail:
                    if ascii_only:
                        status, data = imap.uid("SEARCH", None, "X-GM-RAW", quote(query))
                    else:
                        imap.literal = query.encode("utf-8")
                        status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
                elif ascii_only:
                    term = quote(query)
                    status, data = imap.uid("SEARCH", None, "OR", "FROM", term,
                                            "OR", "SUBJECT", term, "TEXT", term)
                else:
                    imap.literal = query.encode("utf-8")
                    status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
                if status != "OK":
                    raise EmailError(f"The mail server refused that search: {_text(data)}")
                uids = _text(data[0] if data else b"").split()
                newest = list(reversed(uids[-limit:]))
                return {"messages": self._summaries(imap, where, validity, newest),
                        "matched": len(uids), "gmail_syntax": gmail}
            finally:
                self._close(imap)

        result = await self._run(fetch)
        self._last_listing = result["messages"]
        return {**result, "query": query}

    def resolve(self, reference: str) -> Optional[str]:
        """A message id from what a person (or a small model) would say:
        the id itself, its number in the last list (1 = first), or words
        from its sender or subject."""
        reference = (reference or "").strip().strip("#").strip()
        if split_id(reference):
            return reference
        listing = self._last_listing
        if reference.isdigit() and listing:
            position = int(reference)
            if 1 <= position <= len(listing):
                return listing[position - 1]["id"]
        words = [w for w in re.findall(r"\w+", reference.lower()) if len(w) > 1]
        best, best_score = None, 0
        for message in listing:
            haystack = " ".join([message["from"], message["from_address"],
                                 message["subject"]]).lower()
            score = sum(1 for w in words if w in haystack)
            if score > best_score:
                best, best_score = message["id"], score
        return best

    async def locate(self, reference: str) -> Optional[str]:
        """`resolve`, and failing that the newest email a search finds."""
        message_id = self.resolve(reference)
        if message_id is not None:
            return message_id
        words = " ".join(re.findall(r"[\w@.'-]+", reference or ""))
        if not words:
            return None
        found = await self.search(words, limit=1)
        return found["messages"][0]["id"] if found["messages"] else None

    async def sender_address(self, name: str) -> Optional[Dict[str, str]]:
        """The address of the person called `name`, from the newest email
        they sent. None when no email from them can be found."""
        name = " ".join((name or "").split())
        if not name:
            return None
        gmail = (self._config or {}).get("provider") == "gmail"
        found = await self.search(f"from:({name})" if gmail else name, limit=5)
        wanted = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
        for message in found["messages"]:
            haystack = f"{message['from']} {message['from_address']}".lower()
            if wanted and all(w in haystack for w in wanted) and message["from_address"]:
                return {"name": message["from"], "address": message["from_address"]}
        return None

    async def read(self, reference: str) -> Dict[str, Any]:
        config, password = self._credentials()
        message_id = await self.locate(reference)
        if message_id is None:
            raise EmailError(f"I couldn't find an email matching “{reference}”.")
        where, validity, uid = split_id(message_id)  # type: ignore[misc]

        def fetch() -> Dict[str, Any]:
            imap = self._open(config, password)
            try:
                folder = "INBOX"
                if where == "a":
                    folder = self._all_mail(imap) or "INBOX"
                current = self._examine(imap, folder)
                if current != validity:
                    raise EmailError("That mailbox changed since the message was listed. "
                                     "Refresh the list and open it again.")
                status, data = imap.uid("FETCH", uid, f"(UID FLAGS BODY.PEEK[]<0.{READ_BYTES}>)")
                records = parse_fetch(data) if status == "OK" else []
                if not records:
                    raise EmailError("That message is no longer there — it may have been "
                                     "moved or deleted.")
                return records[0]
            finally:
                self._close(imap)

        record = await self._run(fetch)
        message = email_lib.message_from_bytes(record["raw"])
        text, attachments = body_of(message)
        truncated = len(text) > MAX_BODY_CHARS
        sender = _person(message.get("From"))
        preset = PROVIDERS.get(config.get("provider", "other"), PROVIDERS["other"])
        return {
            "id": message_id,
            "from": sender["name"],
            "from_address": sender["address"],
            "reply_to": _person(message.get("Reply-To"))["address"] or sender["address"],
            "to": _people(message.get("To")),
            "cc": _people(message.get("Cc")),
            "subject": decode_text(message.get("Subject")) or "(no subject)",
            "date": _iso_date(message),
            "unread": "\\Seen" not in record["flags"],
            "body": text[:MAX_BODY_CHARS] + ("\n\n[… the rest of this email was cut]"
                                             if truncated else ""),
            "attachments": attachments,
            "message_id": decode_text(message.get("Message-ID")),
            "references": decode_text(message.get("References")),
            "web": preset.get("web", ""),
        }

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    async def send(self, to: str, subject: str, body: str, cc: str = "",
                   reply_to: str = "") -> Dict[str, Any]:
        config, password = self._credentials()
        if not config.get("smtp_host"):
            raise EmailError("No outgoing (SMTP) server is set for this mailbox, so it can't send.")
        recipients = clean_addresses(to)
        copies = clean_addresses(cc) if cc else []
        if not recipients:
            raise EmailError("Who should it go to? No email address was given.")
        subject = " ".join((subject or "").split())
        if not (body or "").strip():
            raise EmailError("The email has no text.")

        original: Optional[Dict[str, Any]] = None
        if reply_to:
            original = await self.read(reply_to)
            # Gmail only threads a reply whose subject matches the original's.
            subject = original["subject"]
            if not re.match(r"(?i)^re:", subject):
                subject = f"Re: {subject}"

        message = EmailMessage()
        message["From"] = config["address"]
        message["To"] = ", ".join(recipients)
        if copies:
            message["Cc"] = ", ".join(copies)
        message["Subject"] = subject or "(no subject)"
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=config["address"].rsplit("@", 1)[-1])
        if original and original.get("message_id"):
            message["In-Reply-To"] = original["message_id"]
            message["References"] = " ".join(
                filter(None, [original.get("references", ""), original["message_id"]]))
        message.set_content(body)

        def deliver() -> None:
            try:
                smtp = self.smtp_factory(config["smtp_host"], int(config["smtp_port"]),
                                         config.get("smtp_security", "ssl"))
            except (socket.gaierror, socket.timeout, ssl.SSLError, OSError) as exc:
                raise EmailError(f"Couldn't reach {config['smtp_host']} to send: {exc}") from exc
            try:
                smtp.login(config["address"], password)
                refused = smtp.send_message(message)
            except smtplib.SMTPAuthenticationError as exc:
                raise EmailError(explain_login(config.get("provider", "other"),
                                               _text(exc.smtp_error))) from exc
            except smtplib.SMTPRecipientsRefused as exc:
                raise EmailError("The mail server refused every recipient: "
                                 + ", ".join(exc.recipients)) from exc
            except smtplib.SMTPException as exc:
                raise EmailError(f"The mail server didn't accept the email: {exc}") from exc
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
            if refused:
                raise EmailError("Sent, except to: " + ", ".join(refused))

        await self._run(deliver)
        everyone = recipients + copies
        if self.audit:
            await self.audit.record("email.sent", subject=", ".join(everyone)[:200],
                                    detail={"subject": message["Subject"],
                                            "reply": bool(original)})
        if self.activity:
            await self.activity.record(
                f"Email sent from {config['address']} to {', '.join(everyone)}: "
                f"“{message['Subject']}”.", source="email")
        return {"sent": True, "from": config["address"], "to": recipients, "cc": copies,
                "subject": message["Subject"], "message_id": message["Message-ID"]}
