"""
Notes: a small local knowledge base.

Markdown notes with [[wiki links]], tags, full-text search and backlinks.
Deliberately plain: notes are rows, and links are derived from the body every
time it is saved, so the graph can never drift out of step with what is written.

Nothing here calls a model unless you ask it to (summarise, extract tasks).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.notes")

# [[Target]] or [[Target|label]] -- the label is display only.
WIKI_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
MAX_BODY = 200_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def extract_links(body: str) -> List[str]:
    """Wiki-link targets in the order written, de-duplicated case-insensitively."""
    seen: Dict[str, str] = {}
    for match in WIKI_LINK.finditer(body or ""):
        target = match.group(1).strip()
        if target and target.lower() not in seen:
            seen[target.lower()] = target
    return list(seen.values())


def parse_tags(raw: Any) -> List[str]:
    """Tags from a list or a comma/space separated string, normalised lowercase."""
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = re.split(r"[,\s]+", str(raw or ""))
    out: List[str] = []
    for part in parts:
        tag = part.strip().lstrip("#").lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def extract_checkboxes(body: str) -> List[Dict[str, Any]]:
    """Markdown task-list items, so a note can become real tasks."""
    items: List[Dict[str, Any]] = []
    for line_number, line in enumerate((body or "").splitlines(), start=1):
        match = re.match(r"\s*[-*]\s*\[([ xX])\]\s+(.+)", line)
        if match:
            items.append({
                "line": line_number,
                "done": match.group(1).lower() == "x",
                "text": match.group(2).strip(),
            })
    return items


def _row(record: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    out["tags"] = [t for t in (out.get("tags") or "").split(",") if t]
    out["pinned"] = bool(out.get("pinned"))
    out["archived"] = bool(out.get("archived"))
    return out


def _excerpt(body: str, query: str, width: int = 160) -> str:
    """A window of the body around the first match, so a search result shows why
    it matched rather than just its opening line."""
    if not body:
        return ""
    position = body.lower().find(query.lower())
    if position < 0:
        return body[:width].strip()
    start = max(0, position - width // 3)
    end = min(len(body), position + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(body) else ""
    return prefix + body[start:end].strip() + suffix


class NotesService:
    """CRUD, search and the link graph."""

    def __init__(self, db, activity=None, orchestrator=None):
        self.db = db
        self.activity = activity
        self.orchestrator = orchestrator

    # ==================================================================
    # writing
    # ==================================================================
    async def create(self, title: str, body: str = "", tags: Any = None,
                     kind: str = "note") -> Dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("A note needs a title.")
        if len(body or "") > MAX_BODY:
            raise ValueError(
                "That note is too large ({} characters, limit {}).".format(len(body), MAX_BODY))

        note_id = "note_" + uuid.uuid4().hex[:12]
        stamp = _now()
        await self.db.execute(
            "INSERT INTO notes (id, title, body, tags, kind, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_id, title, body or "", ",".join(parse_tags(tags)), kind, stamp, stamp))
        await self._reindex_links(note_id, body or "")
        if self.activity:
            await self.activity.record("note created: " + title, level="info", source="notes")
        return await self.get(note_id)

    async def update(self, note_id: str, **fields: Any) -> Dict[str, Any]:
        existing = await self.get(note_id)

        sets: List[str] = []
        params: List[Any] = []
        for column in ("title", "body", "kind"):
            if column in fields and fields[column] is not None:
                value = fields[column]
                if column == "title" and not str(value).strip():
                    raise ValueError("A note needs a title.")
                if column == "body" and len(str(value)) > MAX_BODY:
                    raise ValueError(
                        "That note is too large (limit {} characters).".format(MAX_BODY))
                sets.append(column + " = ?")
                params.append(str(value))
        if "tags" in fields and fields["tags"] is not None:
            sets.append("tags = ?")
            params.append(",".join(parse_tags(fields["tags"])))
        for flag in ("pinned", "archived"):
            if flag in fields and fields[flag] is not None:
                sets.append(flag + " = ?")
                params.append(1 if fields[flag] else 0)

        if not sets:
            return existing

        sets.append("updated_at = ?")
        params.extend([_now(), note_id])
        await self.db.execute(
            "UPDATE notes SET {} WHERE id = ?".format(", ".join(sets)), params)

        if "body" in fields and fields["body"] is not None:
            await self._reindex_links(note_id, str(fields["body"]))
        return await self.get(note_id)

    async def delete(self, note_id: str) -> Dict[str, Any]:
        note = await self.get(note_id)
        await self.db.execute("DELETE FROM note_links WHERE note_id = ?", (note_id,))
        await self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if self.activity:
            await self.activity.record(
                "note deleted: " + note["title"], level="warn", source="notes")
        return {"deleted": True, "title": note["title"]}

    async def append(self, note_id: str, text: str) -> Dict[str, Any]:
        """Add a line to an existing note without rewriting it."""
        note = await self.get(note_id)
        body = (note["body"] + "\n" + text).strip() if note["body"] else text
        return await self.update(note_id, body=body)

    # ==================================================================
    # reading
    # ==================================================================
    async def get(self, note_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT * FROM notes WHERE id = ?", (note_id,))
        if not record:
            raise LookupError("No note with id {}.".format(note_id))
        return _row(record)

    async def by_title(self, title: str) -> Optional[Dict[str, Any]]:
        record = await self.db.fetch_one(
            "SELECT * FROM notes WHERE lower(title) = lower(?) LIMIT 1", (title.strip(),))
        return _row(record) if record else None

    async def resolve(self, reference: str) -> Dict[str, Any]:
        """Find a note by id, then exact title, then partial title."""
        reference = (reference or "").strip()
        if not reference:
            raise LookupError("Which note?")
        record = await self.db.fetch_one("SELECT * FROM notes WHERE id = ?", (reference,))
        if record:
            return _row(record)
        exact = await self.by_title(reference)
        if exact:
            return exact
        record = await self.db.fetch_one(
            "SELECT * FROM notes WHERE title LIKE ? ORDER BY updated_at DESC LIMIT 1",
            ("%" + reference + "%",))
        if not record:
            raise LookupError("No note matching '{}'.".format(reference))
        return _row(record)

    async def list(self, kind: str = "", tag: str = "", archived: bool = False,
                   limit: int = 100) -> List[Dict[str, Any]]:
        clauses = ["archived = ?"]
        params: List[Any] = [1 if archived else 0]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if tag:
            clauses.append("(',' || tags || ',') LIKE ?")
            params.append("%," + tag.strip().lower() + ",%")
        params.append(max(1, min(int(limit), 500)))
        rows = await self.db.fetch_all(
            "SELECT * FROM notes WHERE {} ORDER BY pinned DESC, updated_at DESC "
            "LIMIT ?".format(" AND ".join(clauses)), params)
        return [_row(r) for r in rows]

    async def search(self, query: str, limit: int = 40) -> List[Dict[str, Any]]:
        """Title and body search, titles ranked first, with a matching excerpt."""
        query = (query or "").strip()
        if not query:
            return []
        like = "%" + query + "%"
        rows = await self.db.fetch_all(
            "SELECT *, (CASE WHEN title LIKE ? THEN 0 ELSE 1 END) AS rank FROM notes "
            "WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? "
            "ORDER BY rank, updated_at DESC LIMIT ?",
            (like, like, like, like, max(1, min(int(limit), 200))))

        results = []
        for record in rows:
            note = _row(record)
            note.pop("rank", None)
            note["excerpt"] = _excerpt(note["body"], query)
            results.append(note)
        return results

    async def tags(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT tags FROM notes WHERE archived = 0")
        counts: Dict[str, int] = {}
        for record in rows:
            for tag in (record["tags"] or "").split(","):
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
        return [{"tag": t, "count": c}
                for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    # ==================================================================
    # the link graph
    # ==================================================================
    async def _reindex_links(self, note_id: str, body: str) -> None:
        """Rebuild this note's outgoing links from its body.

        Targets are stored even when nothing matches yet: an unresolved link is
        a real thing in a knowledge base, not an error.
        """
        await self.db.execute("DELETE FROM note_links WHERE note_id = ?", (note_id,))
        for target in extract_links(body):
            match = await self.by_title(target)
            await self.db.execute(
                "INSERT OR REPLACE INTO note_links (note_id, target, resolved_id) "
                "VALUES (?, ?, ?)",
                (note_id, target, match["id"] if match else None))

    async def links(self, note_id: str) -> Dict[str, Any]:
        """Outgoing links and backlinks, each marked resolved or not."""
        note = await self.get(note_id)

        outgoing = []
        for record in await self.db.fetch_all(
                "SELECT target, resolved_id FROM note_links WHERE note_id = ?", (note_id,)):
            # Resolve late as well -- the target may have been created since.
            resolved = record["resolved_id"]
            if not resolved:
                match = await self.by_title(record["target"])
                resolved = match["id"] if match else None
            outgoing.append({"target": record["target"], "id": resolved,
                             "exists": bool(resolved)})

        backlinks = await self.db.fetch_all(
            "SELECT n.id, n.title FROM note_links l JOIN notes n ON n.id = l.note_id "
            "WHERE l.resolved_id = ? OR lower(l.target) = lower(?)",
            (note_id, note["title"]))

        return {
            "note": {"id": note["id"], "title": note["title"]},
            "outgoing": outgoing,
            "backlinks": [{"id": b["id"], "title": b["title"]} for b in backlinks],
            "orphan": not outgoing and not backlinks,
        }

    async def graph(self) -> Dict[str, Any]:
        """The whole link graph, for visualising or spotting orphans."""
        notes = await self.db.fetch_all(
            "SELECT id, title FROM notes WHERE archived = 0 ORDER BY title")
        edges = await self.db.fetch_all("SELECT note_id, target, resolved_id FROM note_links")
        by_title = {n["title"].lower(): n["id"] for n in notes}

        linked = set()
        links = []
        for edge in edges:
            target_id = edge["resolved_id"] or by_title.get(edge["target"].lower())
            if target_id:
                links.append({"from": edge["note_id"], "to": target_id})
                linked.add(edge["note_id"])
                linked.add(target_id)

        return {
            "nodes": [{"id": n["id"], "title": n["title"]} for n in notes],
            "links": links,
            "orphans": [n["title"] for n in notes if n["id"] not in linked],
        }

    # ==================================================================
    # the daily journal
    # ==================================================================
    async def journal(self, day: str = "") -> Dict[str, Any]:
        """Today's journal note, created on first use."""
        day = day or _today()
        title = "Journal " + day
        existing = await self.by_title(title)
        if existing:
            return existing
        return await self.create(title, body="# " + day + "\n\n",
                                 tags=["journal"], kind="journal")

    async def journal_append(self, text: str, day: str = "") -> Dict[str, Any]:
        note = await self.journal(day)
        stamp = datetime.now().strftime("%H:%M")
        return await self.append(note["id"], "- {} {}".format(stamp, text))

    # ==================================================================
    # model-assisted, and clearly labelled as such
    # ==================================================================
    async def summarise(self, note_id: str) -> Dict[str, Any]:
        note = await self.get(note_id)
        if not note["body"].strip():
            return {"ok": False,
                    "message": "That note is empty -- there is nothing to summarise."}
        if not self.orchestrator:
            return {"ok": False, "message": "No model is available to summarise."}

        summary = await self.orchestrator.ask(
            "Summarise this note in at most four sentences. Do not add facts.\n\n"
            "# {}\n\n{}".format(note["title"], note["body"][:8000]),
            purpose="fast")
        return {"ok": True, "summary": (summary or "").strip(), "title": note["title"]}

    async def extract_tasks(self, note_id: str) -> Dict[str, Any]:
        """Checkbox items in the note.

        Deterministic -- no model involved, so it cannot invent a task that is
        not actually written down.
        """
        note = await self.get(note_id)
        items = extract_checkboxes(note["body"])
        return {
            "note": note["title"],
            "open": [i["text"] for i in items if not i["done"]],
            "done": [i["text"] for i in items if i["done"]],
            "total": len(items),
        }

    async def stats(self) -> Dict[str, Any]:
        total = await self.db.fetch_value("SELECT COUNT(*) FROM notes", default=0)
        archived = await self.db.fetch_value(
            "SELECT COUNT(*) FROM notes WHERE archived = 1", default=0)
        words = await self.db.fetch_value(
            "SELECT COALESCE(SUM(LENGTH(body) - LENGTH(REPLACE(body, ' ', '')) + 1), 0) "
            "FROM notes WHERE body <> ''", default=0)
        links = await self.db.fetch_value("SELECT COUNT(*) FROM note_links", default=0)
        return {
            "notes": total,
            "active": total - archived,
            "archived": archived,
            "approx_words": int(words or 0),
            "links": links,
            "tags": len(await self.tags()),
        }
