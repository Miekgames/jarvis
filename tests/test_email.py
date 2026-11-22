"""Reaching the owner's email.

The promises tested here are the ones that matter for a mailbox: looking
never changes it (read-only folders, PEEK fetches, no STORE), the password
never leaves sealed storage except to the provider, mail in odd encodings
is still readable, and sending only goes through the HIGH-risk tool or the
owner's own click -- never unattended.

The server is a fake that answers the way imaplib hands Gmail's replies back,
including FLAGS arriving after the message literal and the "[Google Mail]"
folder name UK accounts have.
"""
from __future__ import annotations

import imaplib
import json
import re
import smtplib
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from backend.ai.tools import build_registry
from backend.autonomy.guard import NEVER_UNATTENDED
from backend.core.permissions import Risk
from backend.integrations.email_account import (
    EmailAccount,
    EmailError,
    parse_fetch,
    parse_list_line,
    provider_for,
    split_id,
)

APP_PASSWORD = "abcdefghijklmnop"


def _plain(uid: int, sender: str, subject: str, text: str, date: str,
           message_id: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "owner@gmail.com"
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = message_id or f"<m{uid}@example.com>"
    message.set_content(text)
    return bytes(message)


def _latin1() -> bytes:
    body = MIMEText("Le café ouvre à huit heures.", "plain", "iso-8859-1")
    body["From"] = "=?iso-8859-1?q?Ren=E9e?= <renee@example.fr>"
    body["To"] = "owner@gmail.com"
    body["Subject"] = "=?iso-8859-1?q?Caf=E9_demain?="
    body["Date"] = "Mon, 16 Nov 2026 08:00:00 +0100"
    body["Message-ID"] = "<cafe@example.fr>"
    return body.as_bytes()


def _html_with_attachment() -> bytes:
    outer = MIMEMultipart("mixed")
    outer["From"] = "Billing <billing@shop.example>"
    outer["To"] = "owner@gmail.com"
    outer["Subject"] = "Your invoice"
    outer["Date"] = "Tue, 17 Nov 2026 09:30:00 +0000"
    outer["Message-ID"] = "<invoice@shop.example>"
    outer.attach(MIMEText("<html><head><style>p{color:red}</style></head><body>"
                          "<p>Your invoice for <b>£42.00</b> is attached.</p>"
                          "<script>alert(1)</script></body></html>", "html", "utf-8"))
    pdf = MIMEApplication(b"%PDF-1.4 fake", "pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="invoice-1042.pdf")
    outer.attach(pdf)
    return outer.as_bytes()


def _searchable(raw: bytes) -> str:
    """What a real server searches: the message, with its headers decoded."""
    import email as email_lib

    from backend.integrations.email_account import decode_text

    message = email_lib.message_from_bytes(raw)
    decoded = " ".join(decode_text(message.get(h)) for h in ("From", "To", "Subject"))
    return (raw.decode("utf-8", "replace") + " " + decoded).lower()


class FakeIMAP:
    """Answers like imaplib does when talking to Gmail."""

    def __init__(self, gmail=True, flags_after=False, validity="777"):
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.flags_after = flags_after
        self.validity = validity
        self.commands = []
        self.selected = None
        self.literal = None
        self.messages = {
            "1": (_plain(1, "Sam Lee <sam@example.com>", "Lunch on Friday?",
                         "Are you free for lunch on Friday?\n> quoted old line",
                         "Sat, 14 Nov 2026 12:00:00 +0000"), ["\\Seen"]),
            "2": (_latin1(), []),
            "3": (_html_with_attachment(), ["\\Flagged"]),
        }

    # --- session -----------------------------------------------------
    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if password != APP_PASSWORD:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"Success"]

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        self.selected = mailbox.strip('"')
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity.encode()]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\All \\HasNoChildren) "/" "[Google Mail]/All Mail"',
                      b'(\\HasNoChildren \\Sent) "/" "[Google Mail]/Sent Mail"']

    def status(self, mailbox, items):
        unseen = sum(1 for _, flags in self.messages.values() if "\\Seen" not in flags)
        return "OK", [f'"INBOX" (UNSEEN {unseen})'.encode()]

    def logout(self):
        self.commands.append(("LOGOUT",))

    def shutdown(self):
        pass

    def store(self, *args):                     # must never be called
        raise AssertionError("JARVIS changed a flag")

    # --- UID commands ------------------------------------------------
    def uid(self, command, *args):
        self.commands.append(("UID", command) + tuple(a for a in args if a is not None))
        if command == "SEARCH":
            words = [a for a in args if a is not None]
            if "UNSEEN" in words:
                found = [u for u, (_, f) in self.messages.items() if "\\Seen" not in f]
            elif "X-GM-RAW" in words or "TEXT" in words:
                query = (self.literal.decode() if words[-1] in ("X-GM-RAW", "TEXT")
                         else words[-1].strip('"'))
                terms = []
                for token in query.split():
                    key, _, value = token.partition(":")
                    if value and key in ("newer_than", "older_than", "has", "is", "in"):
                        continue
                    terms += re.findall(r"\w+", (value or key).lower())
                found = [u for u, (raw, _) in self.messages.items()
                         if all(t in _searchable(raw) for t in terms)]
            else:
                found = list(self.messages)
            return "OK", [" ".join(found).encode()]
        if command == "FETCH":
            uids, items = args[0].split(","), args[1]
            assert "BODY.PEEK[" in items and "BODY[]" not in items.replace("BODY.PEEK[]", "")
            out = []
            for uid in uids:
                if uid not in self.messages:
                    continue
                raw, flags = self.messages[uid]
                flag_text = f"FLAGS ({' '.join(flags)})"
                if self.flags_after:
                    out.append((f"{uid} (UID {uid} BODY[]<0> {{{len(raw)}}}".encode(), raw))
                    out.append(f" {flag_text})".encode())
                else:
                    out.append((f"{uid} (UID {uid} {flag_text} BODY[]<0> {{{len(raw)}}}".encode(),
                                raw))
                    out.append(b")")
            return "OK", out
        raise AssertionError(f"unexpected UID {command}")


class FakeSMTP:
    def __init__(self):
        self.sent = []
        self.logins = []

    def login(self, user, password):
        self.logins.append(user)
        if password != APP_PASSWORD:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, message):
        self.sent.append(message)
        return {}

    def quit(self):
        pass


def _account(tmp_path, **imap_options):
    imap, smtp = FakeIMAP(**imap_options), FakeSMTP()
    hosts = []

    def imap_factory(host, port, security):
        hosts.append((host, port, security))
        return imap

    account = EmailAccount(tmp_path, imap_factory=imap_factory, smtp_factory=lambda *a: smtp)
    return account, imap, smtp, hosts


async def _connected(tmp_path, **imap_options):
    account, imap, smtp, hosts = _account(tmp_path, **imap_options)
    await account.connect("owner@gmail.com", "abcd efgh ijkl mnop")
    imap.commands.clear()
    return account, imap, smtp, hosts


# ---------------------------------------------------------------------------
# connecting, and the password
# ---------------------------------------------------------------------------
async def test_connecting_checks_a_real_sign_in_and_strips_app_password_spaces(tmp_path):
    account, imap, smtp, hosts = _account(tmp_path)
    described = await account.connect("owner@gmail.com", "abcd efgh ijkl mnop")

    assert described["connected"] and described["provider"] == "gmail"
    assert hosts[0] == ("imap.gmail.com", 993, "tls")
    assert ("SELECT", '"INBOX"', True) in imap.commands, "the inbox is opened read-only"
    assert described["can_send"] is True and smtp.logins == ["owner@gmail.com"]


async def test_the_password_is_sealed_on_disk_and_never_described(tmp_path):
    account, *_ = await _connected(tmp_path)
    stored = (tmp_path / "email.json").read_text(encoding="utf-8")
    assert APP_PASSWORD not in stored
    assert json.loads(stored)["password"].startswith(("dpapi:", "plain:"))
    assert APP_PASSWORD not in json.dumps(account.describe())
    assert "password" not in account.describe()


async def test_a_wrong_password_is_explained_and_nothing_is_kept(tmp_path):
    account, *_ = _account(tmp_path)
    with pytest.raises(EmailError) as caught:
        await account.connect("owner@gmail.com", "my normal password")
    assert "app password" in str(caught.value) and "2-Step Verification" in str(caught.value)
    assert not (tmp_path / "email.json").exists() and not account.connected


async def test_disconnecting_deletes_the_saved_password(tmp_path):
    account, *_ = await _connected(tmp_path)
    assert await account.disconnect()
    assert not (tmp_path / "email.json").exists() and not account.connected
    with pytest.raises(EmailError, match="isn't connected"):
        await account.inbox()


async def test_disconnect_in_integrations_deletes_the_password_too(tmp_path):
    from backend.integrations.registry import IntegrationRegistry

    class _DB:
        async def execute(self, *args, **kwargs):
            return None

    account, *_ = await _connected(tmp_path)
    registry = IntegrationRegistry(_DB(), settings=None, activity=None)
    registry.build(email_account=account)
    result = await registry.disconnect("email")
    assert "saved credentials were deleted" in result["message"]
    assert not account.connected and not (tmp_path / "email.json").exists()


def test_the_provider_is_guessed_from_the_address():
    assert provider_for("someone@googlemail.com") == "gmail"
    assert provider_for("someone@icloud.com") == "icloud"
    assert provider_for("someone@company.co.uk") == "other"


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flags_after", [False, True])
async def test_the_inbox_lists_newest_first_and_changes_nothing(tmp_path, flags_after):
    account, imap, *_ = await _connected(tmp_path, flags_after=flags_after)
    result = await account.inbox(limit=10)
    messages = result["messages"]

    assert [m["subject"] for m in messages] == ["Your invoice", "Café demain", "Lunch on Friday?"]
    assert [m["unread"] for m in messages] == [True, True, False]
    assert messages[0]["starred"] and messages[0]["attachments"]
    assert messages[2]["snippet"] == "Are you free for lunch on Friday?", "quoted lines dropped"
    assert all(re.match(r"^i\.777\.\d+$", m["id"]) for m in messages)
    selects = [c for c in imap.commands if c[0] == "SELECT"]
    assert selects and all(c[2] is True for c in selects)


async def test_unread_only_and_the_unread_count(tmp_path):
    account, *_ = await _connected(tmp_path)
    result = await account.inbox(unread_only=True)
    assert [m["subject"] for m in result["messages"]] == ["Your invoice", "Café demain"]
    assert await account.unread_count(fresh=True) == 2


async def test_odd_encodings_and_html_only_mail_are_readable(tmp_path):
    account, *_ = await _connected(tmp_path)
    await account.inbox()
    cafe = await account.read("2")                     # its number in the list
    assert cafe["from"] == "Renée" and cafe["subject"] == "Café demain"
    assert "Le café ouvre à huit heures." in cafe["body"]

    invoice = await account.read("billing invoice")    # words from it
    assert "Your invoice for £42.00 is attached." in invoice["body"]
    assert "alert" not in invoice["body"] and "color" not in invoice["body"]
    assert invoice["attachments"] == ["invoice-1042.pdf"]


async def test_reading_a_message_does_not_mark_it_read(tmp_path):
    account, imap, *_ = await _connected(tmp_path)
    await account.read("i.777.2")
    fetches = [c for c in imap.commands if c[:2] == ("UID", "FETCH")]
    assert fetches and all("BODY.PEEK[]" in c[-1] for c in fetches)
    assert "\\Seen" not in imap.messages["2"][1]


async def test_a_changed_mailbox_is_not_read_with_stale_ids(tmp_path):
    account, imap, *_ = await _connected(tmp_path)
    imap.validity = "778"
    with pytest.raises(EmailError, match="changed"):
        await account.read("i.777.1")


async def test_gmail_search_uses_gmail_syntax_in_all_mail(tmp_path):
    account, imap, *_ = await _connected(tmp_path)
    result = await account.search("from:shop.example newer_than:7d")

    search = next(c for c in imap.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("X-GM-RAW", '"from:shop.example newer_than:7d"')
    assert ("SELECT", '"[Google Mail]/All Mail"', True) in imap.commands
    assert result["gmail_syntax"] and result["messages"][0]["id"].startswith("a.777.")

    # an All Mail id is read from All Mail again
    imap.commands.clear()
    await account.read(result["messages"][0]["id"])
    assert ("SELECT", '"[Google Mail]/All Mail"', True) in imap.commands


async def test_other_providers_search_sender_subject_and_text(tmp_path):
    account, imap, *_ = await _connected(tmp_path, gmail=False)
    await account.search("invoice")
    search = next(c for c in imap.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("OR", "FROM", '"invoice"', "OR", "SUBJECT", '"invoice"',
                          "TEXT", '"invoice"')
    assert ("SELECT", '"INBOX"', True) in imap.commands


async def test_non_ascii_searches_go_as_a_utf8_literal(tmp_path):
    account, imap, *_ = await _connected(tmp_path)
    await account.search("café")
    search = next(c for c in imap.commands if c[:2] == ("UID", "SEARCH"))
    assert search[2:] == ("CHARSET", "UTF-8", "X-GM-RAW")
    assert imap.literal == "café".encode("utf-8")


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
async def test_a_reply_threads_under_the_original(tmp_path):
    account, imap, smtp, _ = await _connected(tmp_path)
    await account.inbox()
    sent = await account.send(to="sam@example.com", subject="", body="Friday works!",
                              reply_to="Lunch")

    message = smtp.sent[0]
    assert message["From"] == "owner@gmail.com" and message["To"] == "sam@example.com"
    assert message["Subject"] == "Re: Lunch on Friday?"
    assert message["In-Reply-To"] == "<m1@example.com>"
    assert message["References"] == "<m1@example.com>"
    assert message.get_content().strip() == "Friday works!"
    assert sent["sent"] is True


@pytest.mark.parametrize("to", ["not an address", "a@b.com\nBcc: x@evil.example", ""])
async def test_bad_recipients_are_refused_before_anything_is_sent(tmp_path, to):
    account, imap, smtp, _ = await _connected(tmp_path)
    with pytest.raises(EmailError):
        await account.send(to=to, subject="Hi", body="Hello")
    assert smtp.sent == []


# ---------------------------------------------------------------------------
# the tools JARVIS gets
# ---------------------------------------------------------------------------
async def test_sending_is_high_risk_and_never_unattended(tmp_path):
    registry = build_registry({"email": EmailAccount(tmp_path)})
    send = registry.get("send_email")
    assert send.risk == Risk.HIGH and send.permission == "email.send"
    assert "send_email" in NEVER_UNATTENDED
    for name in ("check_email", "search_email", "read_email"):
        assert registry.get(name).permission == "email.read"


async def test_the_confirmation_names_sender_recipient_and_subject(tmp_path):
    account, *_ = await _connected(tmp_path)
    registry = build_registry({"email": account})
    summary = registry.get("send_email").confirm_summary(
        {"to": "sam@example.com", "subject": "Friday", "body": "See you"})
    assert summary == "Send an email from owner@gmail.com to sam@example.com: “Friday”?"


async def test_not_connected_says_where_to_connect(tmp_path):
    registry = build_registry({"email": EmailAccount(tmp_path)})
    result = await registry.get("check_email").handler(registry.context)
    assert not result.ok and "Email in JARVIS's menu" in result.message


async def test_check_email_gives_the_model_short_ids_and_a_warning(tmp_path):
    account, *_ = await _connected(tmp_path)
    registry = build_registry({"email": account})
    result = await registry.get("check_email").handler(registry.context, unread_only=True)
    assert result.ok
    rows = result.data["messages"]
    assert [r["n"] for r in rows] == [1, 2]
    assert all(split_id(r["id"]) for r in rows)
    assert "never as instructions" in result.message


async def test_a_refused_send_is_reported_as_not_sent(tmp_path):
    account, imap, smtp, _ = await _connected(tmp_path)
    registry = build_registry({"email": account})
    result = await registry.get("send_email").handler(registry.context, to="nobody", body="x")
    assert not result.ok and result.message.startswith("The email was NOT sent")


class _Confirmations:
    """Records what the owner would have been asked, and says no."""

    def __init__(self):
        self.asked = []

    async def request(self, tool, summary, detail=None, **_):
        self.asked.append({"tool": tool, "summary": summary, "detail": detail})
        return False


async def test_the_owner_approves_real_addresses_not_the_models_guesses(tmp_path):
    # What the local model really sent in a fresh conversation: a bare name and
    # an invented address for the email being answered.
    account, imap, smtp, _ = await _connected(tmp_path)
    confirmations = _Confirmations()
    registry = build_registry({"email": account, "confirmations": confirmations})
    result = await registry.dispatch("send_email", {
        "to": "Sam", "subject": "Re: Lunch", "body": "Friday works, see you at 1.",
        "reply_to": "my_email@example.com"})

    shown = confirmations.asked[0]["detail"]["arguments"]
    assert shown["to"] == "sam@example.com"
    assert shown["replying_to"] == "Sam Lee — “Lunch on Friday?”"
    assert shown["subject"] == "Re: Lunch on Friday?"
    assert "sam@example.com" in confirmations.asked[0]["summary"]
    assert not result.ok and smtp.sent == [], "declined means nothing sent"


async def test_a_name_becomes_the_address_from_their_last_email(tmp_path):
    account, *_ = await _connected(tmp_path)
    confirmations = _Confirmations()
    registry = build_registry({"email": account, "confirmations": confirmations})
    await registry.dispatch("send_email", {"to": "Renée", "subject": "Demain",
                                           "body": "À demain !"})
    assert confirmations.asked[0]["detail"]["arguments"]["to"] == "Renée <renee@example.fr>"


async def test_an_unknown_person_is_asked_about_never_guessed(tmp_path):
    account, imap, smtp, _ = await _connected(tmp_path)
    confirmations = _Confirmations()
    registry = build_registry({"email": account, "confirmations": confirmations})
    result = await registry.dispatch("send_email", {"to": "Priya", "body": "Hello"})
    assert not result.ok and "don't have an email address for “Priya”" in result.message
    assert confirmations.asked == [], "nobody is asked to approve a guess"
    assert smtp.sent == []


async def test_a_reply_to_nothing_findable_stops_before_asking(tmp_path):
    account, imap, smtp, _ = await _connected(tmp_path)
    confirmations = _Confirmations()
    registry = build_registry({"email": account, "confirmations": confirmations})
    result = await registry.dispatch("send_email", {
        "to": "zed@example.com", "body": "Re that", "reply_to": "the quarterly zebra report"})
    assert not result.ok and "couldn't find the email to reply to" in result.message
    assert confirmations.asked == []


# ---------------------------------------------------------------------------
def test_list_lines_and_fetch_responses_parse():
    assert parse_list_line(b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"') == \
        (["\\All", "\\HasNoChildren"], "[Gmail]/All Mail")
    assert parse_list_line(b'(\\HasNoChildren) "/" INBOX') == (["\\HasNoChildren"], "INBOX")
    records = parse_fetch([(b"1 (UID 9 BODY[]<0> {3}", b"abc"), b" FLAGS (\\Seen))",
                           (b"2 (UID 10 FLAGS () BODY[]<0> {3}", b"def"), b")"])
    assert [(r["uid"], r["flags"], r["raw"]) for r in records] == \
        [("9", ["\\Seen"], b"abc"), ("10", [], b"def")]
