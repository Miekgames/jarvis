"""v6 -- retire outbound calling and phone messaging.

The telephony and phone-push features were removed from the app, which leaves
rows behind on any existing install: a granted `calls.make` permission, a
`notifications.send` permission, and a `twilio` integration row that may still
hold credentials. A permission row that no longer maps to a capability is worse
than untidy -- it reads as "JARVIS may place calls" on the security screen for a
capability that no longer exists -- so the migration clears them rather than
leaving them to rot.

Never edit a migration that has shipped -- add a new one.
"""
from __future__ import annotations

from typing import List

V6: List[str] = [
    # Permissions for capabilities that no longer exist.
    "DELETE FROM permissions WHERE key IN ('calls.make', 'notifications.send')",

    # The integration row, including any stored credentials.
    "DELETE FROM integrations WHERE key = 'twilio'",

    # Audit entries naming the removed tools stay: the log is a record of what
    # happened and rewriting history would defeat its purpose.
]
