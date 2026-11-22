"""
Clipboard history and text snippets.

Two rules shape this module:

  * **Secrets are never stored.** Anything that looks like a password, API key,
    token, card number or private key is dropped before it reaches the database.
    A clipboard log is the single easiest place to leak a credential, so the
    filter runs first and the content never touches disk if it matches.
  * **Capture is off by default and explicit.** Nothing polls the clipboard
    unless capture is switched on, and the poller stops the moment it is
    switched off.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.clipboard")

MAX_CLIP_BYTES = 100_000
DEFAULT_KEEP = 200

# Patterns that mean "do not store this". Deliberately broad: a false positive
# costs one missed clipboard entry, a false negative writes a secret to disk.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS key id"),
    (re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "JWT"),
    (re.compile(r"(?i)\b(pass(word)?|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S{6,}"),
     "credential assignment"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "card-like number"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def looks_secret(text: str) -> Optional[str]:
    """Name the reason this text must not be stored, or None if it is safe."""
    if not text:
        return None
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(text):
            # A long run of digits is only card-like if it really is mostly
            # digits -- otherwise every git hash and log line trips it.
            if label == "card-like number":
                digits = sum(ch.isdigit() for ch in text)
                if digits < 13 or digits / max(1, len(text.strip())) < 0.6:
                    continue
            return label
    return None


def classify(text: str) -> str:
    """A coarse kind, used for filtering and for the UI icon."""
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    if re.match(r"^https?://\S+$", stripped):
        return "url"
    if re.match(r"^[\w.+-]+@[\w-]+\.[\w.]+$", stripped):
        return "email"
    if stripped[0] in "[{" and stripped[-1] in "]}":
        return "json"
    if re.search(r"^\s*(def |class |function |import |const |SELECT |<\w+)", stripped,
                 re.MULTILINE):
        return "code"
    if re.match(r"^[A-Za-z]:[\\/]|^/[\w./-]+$", stripped) and "\n" not in stripped:
        return "path"
    if "\n" in stripped:
        return "text"
    return "line"


def preview_of(text: str, width: int = 120) -> str:
    single = re.sub(r"\s+", " ", (text or "").strip())
    return single[:width] + ("..." if len(single) > width else "")


class ClipboardService:
    """History, snippets, and an optional capture loop."""

    def __init__(self, db, settings_store=None, activity=None):
        self.db = db
        self.settings_store = settings_store
        self.activity = activity
        self._task: Optional[asyncio.Task] = None
        self._last_seen = ""
        self.running = False
        self.last_error = ""
        self.skipped_secrets = 0

    # ==================================================================
    # capture
    # ==================================================================
    def _read_clipboard(self) -> Optional[str]:
        """Read the OS clipboard, or None if that is not possible here."""
        try:
            import tkinter
        except Exception:
            self.last_error = "tkinter is not available, so the clipboard cannot be read."
            return None
        try:
            root = tkinter.Tk()
            root.withdraw()
            try:
                return root.clipboard_get()
            finally:
                root.destroy()
        except Exception as exc:
            # An empty or non-text clipboard raises; that is not an error.
            message = str(exc)
            if "CLIPBOARD" not in message.upper():
                self.last_error = message[:200]
            return None

    async def capture_once(self) -> Dict[str, Any]:
        """Read the clipboard now and store it if it is new and not secret."""
        text = await asyncio.get_running_loop().run_in_executor(None, self._read_clipboard)
        if text is None:
            return {"stored": False, "reason": self.last_error or "Nothing readable."}
        return await self.add(text)

    async def add(self, text: str, source_app: str = "") -> Dict[str, Any]:
        text = text or ""
        if not text.strip():
            return {"stored": False, "reason": "Empty."}
        if text == self._last_seen:
            return {"stored": False, "reason": "Unchanged."}

        self._last_seen = text

        reason = looks_secret(text)
        if reason:
            # Counted, never stored, and never echoed back.
            self.skipped_secrets += 1
            if self.activity:
                await self.activity.record(
                    "clipboard entry skipped -- looked like a " + reason,
                    level="warn", source="clipboard")
            return {"stored": False, "reason": "Looked like a " + reason + " -- not stored.",
                    "secret": True}

        if len(text.encode("utf-8", "ignore")) > MAX_CLIP_BYTES:
            return {"stored": False,
                    "reason": "Too large ({} bytes).".format(len(text.encode('utf-8', 'ignore')))}

        clip_id = "clip_" + uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO clips (id, content, preview, kind, bytes, source_app, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clip_id, text, preview_of(text), classify(text),
             len(text.encode("utf-8", "ignore")), source_app, _now()))
        await self._trim()
        return {"stored": True, "id": clip_id, "kind": classify(text)}

    async def _trim(self) -> None:
        """Keep the history bounded. Pinned clips are never trimmed."""
        keep = DEFAULT_KEEP
        if self.settings_store:
            settings = await self.settings_store.get("clipboard", {})
            keep = int(settings.get("keep", DEFAULT_KEEP) or DEFAULT_KEEP)
        keep = max(10, min(keep, 2000))
        await self.db.execute(
            "DELETE FROM clips WHERE pinned = 0 AND id NOT IN ("
            "  SELECT id FROM clips WHERE pinned = 0 ORDER BY created_at DESC LIMIT ?)",
            (keep,))

    async def start(self) -> None:
        """Begin polling, but only if capture is switched on."""
        if self.running:
            return
        enabled = True
        if self.settings_store:
            settings = await self.settings_store.get("clipboard", {})
            enabled = bool(settings.get("capture", False))
        if not enabled:
            logger.info("clipboard capture is off")
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("clipboard capture started")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = 2.0
        while self.running:
            try:
                await self.capture_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("clipboard poll failed", exc_info=True)
            await asyncio.sleep(interval)

    # ==================================================================
    # history
    # ==================================================================
    async def history(self, query: str = "", kind: str = "",
                      limit: int = 50) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if query:
            clauses.append("content LIKE ?")
            params.append("%" + query + "%")
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        rows = await self.db.fetch_all(
            "SELECT id, preview, kind, bytes, pinned, source_app, created_at FROM clips "
            "{} ORDER BY pinned DESC, created_at DESC LIMIT ?".format(where), params)
        for row in rows:
            row["pinned"] = bool(row["pinned"])
        return rows

    async def get(self, clip_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT * FROM clips WHERE id = ?", (clip_id,))
        if not record:
            raise LookupError("No clipboard entry with id {}.".format(clip_id))
        record["pinned"] = bool(record["pinned"])
        return record

    async def pin(self, clip_id: str, pinned: bool = True) -> Dict[str, Any]:
        await self.get(clip_id)
        await self.db.execute("UPDATE clips SET pinned = ? WHERE id = ?",
                              (1 if pinned else 0, clip_id))
        return {"id": clip_id, "pinned": pinned}

    async def delete(self, clip_id: str) -> Dict[str, Any]:
        await self.get(clip_id)
        await self.db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        return {"deleted": True, "id": clip_id}

    async def clear(self, include_pinned: bool = False) -> Dict[str, Any]:
        if include_pinned:
            removed = await self.db.execute("DELETE FROM clips")
        else:
            removed = await self.db.execute("DELETE FROM clips WHERE pinned = 0")
        if self.activity:
            await self.activity.record(
                "clipboard history cleared ({} entries)".format(removed),
                level="warn", source="clipboard")
        return {"cleared": removed}

    async def stats(self) -> Dict[str, Any]:
        total = await self.db.fetch_value("SELECT COUNT(*) FROM clips", default=0)
        pinned = await self.db.fetch_value(
            "SELECT COUNT(*) FROM clips WHERE pinned = 1", default=0)
        rows = await self.db.fetch_all(
            "SELECT kind, COUNT(*) AS n FROM clips GROUP BY kind ORDER BY n DESC")
        return {
            "entries": total,
            "pinned": pinned,
            "by_kind": rows,
            "capturing": self.running,
            "secrets_skipped": self.skipped_secrets,
            "last_error": self.last_error,
        }

    # ==================================================================
    # snippets
    # ==================================================================
    async def add_snippet(self, name: str, body: str, trigger: str = "",
                          tags: Any = None) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A snippet needs a name.")
        if not (body or "").strip():
            raise ValueError("A snippet needs a body.")

        trigger = (trigger or "").strip()
        if trigger:
            clash = await self.db.fetch_one(
                "SELECT name FROM snippets WHERE trigger = ?", (trigger,))
            if clash:
                raise ValueError(
                    "The trigger '{}' is already used by '{}'.".format(trigger, clash["name"]))

        snippet_id = "snip_" + uuid.uuid4().hex[:10]
        tag_text = ",".join(t.strip().lower() for t in (
            tags if isinstance(tags, (list, tuple)) else str(tags or "").split(",")) if t.strip())
        await self.db.execute(
            "INSERT INTO snippets (id, name, trigger, body, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snippet_id, name, trigger, body, tag_text, _now()))
        return await self.get_snippet(snippet_id)

    async def get_snippet(self, snippet_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
        if not record:
            raise LookupError("No snippet with id {}.".format(snippet_id))
        record["tags"] = [t for t in (record["tags"] or "").split(",") if t]
        return record

    async def snippets(self, query: str = "") -> List[Dict[str, Any]]:
        if query:
            rows = await self.db.fetch_all(
                "SELECT * FROM snippets WHERE name LIKE ? OR trigger LIKE ? OR body LIKE ? "
                "ORDER BY uses DESC, name",
                ("%" + query + "%", "%" + query + "%", "%" + query + "%"))
        else:
            rows = await self.db.fetch_all("SELECT * FROM snippets ORDER BY uses DESC, name")
        for row in rows:
            row["tags"] = [t for t in (row["tags"] or "").split(",") if t]
        return rows

    async def expand(self, reference: str) -> Dict[str, Any]:
        """Resolve a trigger, id or name to its body and count the use."""
        reference = (reference or "").strip()
        record = await self.db.fetch_one(
            "SELECT * FROM snippets WHERE trigger = ? OR id = ? OR lower(name) = lower(?) "
            "LIMIT 1", (reference, reference, reference))
        if not record:
            record = await self.db.fetch_one(
                "SELECT * FROM snippets WHERE name LIKE ? ORDER BY uses DESC LIMIT 1",
                ("%" + reference + "%",))
        if not record:
            raise LookupError("No snippet matching '{}'.".format(reference))

        await self.db.execute(
            "UPDATE snippets SET uses = uses + 1, last_used = ? WHERE id = ?",
            (_now(), record["id"]))
        return {"id": record["id"], "name": record["name"], "body": record["body"]}

    async def delete_snippet(self, snippet_id: str) -> Dict[str, Any]:
        record = await self.get_snippet(snippet_id)
        await self.db.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
        return {"deleted": True, "name": record["name"]}
