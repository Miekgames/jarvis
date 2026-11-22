"""Keeping a secret on disk without keeping it in plain text.

Used for things the owner hands JARVIS to store -- a plug-in's access token,
an API key in a plug-in's environment. On Windows these are sealed with
DPAPI, the operating system's own per-user encryption: the sealed text is
useless to anyone who copies the file but is not logged in as this user on
this machine. Nothing to configure and no key file to lose.

Elsewhere there is no equivalent built in, so values are kept as-is with a
marker saying so, and the interface tells the owner to use a `${NAME}`
reference to their .env instead.

`${NAME}` references are the other half: a value written as `${GITHUB_TOKEN}`
is read from the environment (which includes .env) when it is used, and the
secret itself is never stored by JARVIS at all.
"""
from __future__ import annotations

import base64
import os
import re
import sys
from typing import Optional

PREFIX = "dpapi:"
PLAIN = "plain:"
_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _dpapi(data: bytes, protect: bool) -> Optional[bytes]:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    entropy = b"jarvis-local-secret"
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    extra = Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = (call(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x1,
               ctypes.byref(result)) if protect else
          call(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x1,
               ctypes.byref(result)))
    if not ok:
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def seal(value: str) -> str:
    """A value fit to write to disk. References are kept as references."""
    if not value or _REFERENCE.match(value.strip()):
        return value
    sealed = _dpapi(value.encode("utf-8"), protect=True)
    if sealed is None:
        return PLAIN + value
    return PREFIX + base64.b64encode(sealed).decode("ascii")


def unseal(stored: str) -> str:
    """The value to use. Raises ValueError if it cannot be opened here."""
    if not stored:
        return stored
    reference = _REFERENCE.match(stored.strip())
    if reference:
        return os.environ.get(reference.group(1), "")
    if stored.startswith(PREFIX):
        opened = _dpapi(base64.b64decode(stored[len(PREFIX):]), protect=False)
        if opened is None:
            raise ValueError("This secret was sealed by another user or machine.")
        return opened.decode("utf-8")
    if stored.startswith(PLAIN):
        return stored[len(PLAIN):]
    return stored


def is_reference(value: str) -> bool:
    return bool(value and _REFERENCE.match(value.strip()))


def describe(stored: str) -> str:
    """How a stored value is held, for display -- never the value itself."""
    if not stored:
        return ""
    if is_reference(stored):
        return f"from {stored.strip()}"
    if stored.startswith(PREFIX):
        return "encrypted"
    return "stored as typed"
