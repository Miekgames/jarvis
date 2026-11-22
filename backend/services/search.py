"""
Universal search: one query across everything JARVIS holds.

Each source is queried independently and contributes scored results. A source
that fails is reported as failed rather than silently contributing nothing --
"no results" and "that source is broken" are different answers, and conflating
them is how a search box quietly stops working.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("services.search")

SOURCE_ORDER = ["notes", "tasks", "goals", "files", "bookmarks", "clips", "snippets",
                "code", "memory", "activity", "feeds"]


def score(query: str, title: str, body: str = "") -> float:
    """Rank a hit. Exact title beats prefix beats substring beats body."""
    query = (query or "").strip().lower()
    title = (title or "").lower()
    body = (body or "").lower()
    if not query:
        return 0.0
    if title == query:
        return 1.0
    if title.startswith(query):
        return 0.85
    if query in title:
        return 0.7
    words = [w for w in re.split(r"\s+", query) if w]
    if words and all(w in title for w in words):
        return 0.6
    if query in body:
        return 0.4
    if words and all(w in body for w in words):
        return 0.3
    return 0.0


class SearchService:
    """Fan out to every registered source, merge, rank."""

    def __init__(self, services: Dict[str, Any], db=None):
        self.services = services
        self.db = db

    # ==================================================================
    async def search(self, query: str, sources: Optional[List[str]] = None,
                     limit: int = 40) -> Dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"query": "", "results": [], "counts": {}, "failed": {},
                    "message": "What should I search for?"}

        wanted = [s for s in (sources or SOURCE_ORDER) if s in SOURCE_ORDER]
        handlers: Dict[str, Callable] = {
            "notes": self._notes, "tasks": self._tasks, "goals": self._goals,
            "files": self._files, "bookmarks": self._bookmarks, "clips": self._clips,
            "snippets": self._snippets, "code": self._code, "memory": self._memory,
            "activity": self._activity, "feeds": self._feeds,
        }

        gathered = await asyncio.gather(
            *[self._run(name, handlers[name], query) for name in wanted],
            return_exceptions=False)

        results: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        failed: Dict[str, str] = {}
        unavailable: List[str] = []

        for name, outcome in zip(wanted, gathered):
            if outcome["status"] == "failed":
                failed[name] = outcome["error"]
                continue
            if outcome["status"] == "unavailable":
                unavailable.append(name)
                continue
            counts[name] = len(outcome["results"])
            results.extend(outcome["results"])

        results.sort(key=lambda item: -item["score"])
        return {
            "query": query,
            "results": results[:max(1, min(int(limit), 200))],
            "total": len(results),
            "counts": counts,
            "failed": failed,
            "unavailable": unavailable,
            "searched": [n for n in wanted if n in counts],
        }

    async def _run(self, name: str, handler: Callable, query: str) -> Dict[str, Any]:
        """Run one source, converting a failure into a reportable outcome."""
        try:
            results = await handler(query)
        except _Unavailable:
            return {"status": "unavailable", "results": []}
        except Exception as exc:
            logger.debug("search source %s failed", name, exc_info=True)
            return {"status": "failed", "error": "{}: {}".format(
                type(exc).__name__, str(exc)[:160]), "results": []}
        return {"status": "ok", "results": results}

    def _need(self, name: str):
        service = self.services.get(name)
        if service is None:
            raise _Unavailable(name)
        return service

    # ==================================================================
    # sources
    # ==================================================================
    async def _notes(self, query: str) -> List[Dict[str, Any]]:
        notes = self._need("notes")
        return [{
            "source": "notes", "id": note["id"], "title": note["title"],
            "detail": note.get("excerpt", "")[:180],
            "score": score(query, note["title"], note["body"]),
            "open": {"panel": "notes", "id": note["id"]},
        } for note in await notes.search(query, limit=15)]

    async def _tasks(self, query: str) -> List[Dict[str, Any]]:
        if not self.db:
            raise _Unavailable("db")
        like = "%" + query + "%"
        rows = await self.db.fetch_all(
            "SELECT id, name, status, type FROM tasks WHERE name LIKE ? "
            "ORDER BY created_at DESC LIMIT 15", (like,))
        return [{
            "source": "tasks", "id": row["id"], "title": row["name"],
            "detail": "{} - {}".format(row["type"], row["status"]),
            "score": score(query, row["name"]),
            "open": {"panel": "tasks", "id": row["id"]},
        } for row in rows]

    async def _goals(self, query: str) -> List[Dict[str, Any]]:
        if not self.db:
            raise _Unavailable("db")
        like = "%" + query + "%"
        rows = await self.db.fetch_all(
            "SELECT id, objective, status FROM goals WHERE objective LIKE ? LIMIT 10", (like,))
        return [{
            "source": "goals", "id": row["id"], "title": row["objective"],
            "detail": row["status"], "score": score(query, row["objective"]),
            "open": {"panel": "goals", "id": row["id"]},
        } for row in rows]

    async def _files(self, query: str) -> List[Dict[str, Any]]:
        files = self._need("files")
        finder = getattr(files, "search", None) or getattr(files, "find", None)
        if not finder:
            raise _Unavailable("files")
        found = await finder(query)
        entries = found.get("files", found) if isinstance(found, dict) else found
        results = []
        for entry in (entries or [])[:15]:
            path = entry.get("path") if isinstance(entry, dict) else str(entry)
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            results.append({
                "source": "files", "id": path, "title": name or path,
                "detail": path, "score": score(query, name or "", path),
                "open": {"panel": "files", "path": path},
            })
        return results

    async def _bookmarks(self, query: str) -> List[Dict[str, Any]]:
        reading = self._need("reading")
        return [{
            "source": "bookmarks", "id": item["id"],
            "title": item["title"] or item["url"],
            "detail": item["url"], "score": score(query, item["title"] or "", item["url"]),
            "open": {"panel": "reading", "id": item["id"]},
        } for item in await reading.bookmarks(query=query, limit=10)]

    async def _clips(self, query: str) -> List[Dict[str, Any]]:
        clipboard = self._need("clipboard")
        return [{
            "source": "clips", "id": clip["id"], "title": clip["preview"][:80],
            "detail": "{} - {}".format(clip["kind"], clip["created_at"][:16]),
            "score": score(query, clip["preview"]),
            "open": {"panel": "clipboard", "id": clip["id"]},
        } for clip in await clipboard.history(query=query, limit=10)]

    async def _snippets(self, query: str) -> List[Dict[str, Any]]:
        clipboard = self._need("clipboard")
        return [{
            "source": "snippets", "id": snip["id"], "title": snip["name"],
            "detail": snip["trigger"] or snip["body"][:80],
            "score": score(query, snip["name"], snip["body"]),
            "open": {"panel": "clipboard", "id": snip["id"]},
        } for snip in await clipboard.snippets(query)]

    async def _code(self, query: str) -> List[Dict[str, Any]]:
        code = self._need("code")
        workspaces = await code.workspaces() if hasattr(code, "workspaces") else []
        if not workspaces:
            return []
        hits = await code.find_symbol(query, workspaces[0]["id"])
        return [{
            "source": "code", "id": "{}:{}".format(hit["path"], hit["line"]),
            "title": hit["name"],
            "detail": "{} {}:{}".format(hit["kind"], hit["path"], hit["line"]),
            "score": score(query, hit["name"]),
            "open": {"panel": "developer", "path": hit["path"], "line": hit["line"]},
        } for hit in hits[:12]]

    async def _memory(self, query: str) -> List[Dict[str, Any]]:
        if not self.db:
            raise _Unavailable("db")
        like = "%" + query + "%"
        rows = await self.db.fetch_all(
            "SELECT id, content, kind FROM memories WHERE content LIKE ? "
            "ORDER BY updated_at DESC LIMIT 10", (like,))
        return [{
            "source": "memory", "id": row["id"], "title": row["content"][:90],
            "detail": row["kind"], "score": score(query, row["content"]),
            "open": {"panel": "memory", "id": row["id"]},
        } for row in rows]

    async def _activity(self, query: str) -> List[Dict[str, Any]]:
        if not self.db:
            raise _Unavailable("db")
        like = "%" + query + "%"
        rows = await self.db.fetch_all(
            "SELECT ts, level, source, message FROM activity WHERE message LIKE ? "
            "ORDER BY ts DESC LIMIT 10", (like,))
        return [{
            "source": "activity", "id": row["ts"], "title": row["message"][:100],
            "detail": "{} - {}".format(row["source"], row["ts"][:16]),
            "score": score(query, row["message"]) * 0.6,   # log lines rank below content
            "open": {"panel": "activity"},
        } for row in rows]

    async def _feeds(self, query: str) -> List[Dict[str, Any]]:
        reading = self._need("reading")
        items = await reading.feed_items(limit=100)
        needle = query.lower()
        matched = [i for i in items
                   if needle in (i["title"] or "").lower()
                   or needle in (i["summary"] or "").lower()][:10]
        return [{
            "source": "feeds", "id": item["id"], "title": item["title"],
            "detail": item["feed_title"] or "",
            "score": score(query, item["title"], item["summary"]),
            "open": {"panel": "reading", "id": item["id"]},
        } for item in matched]


class _Unavailable(Exception):
    """This source is not wired up in this build -- not an error."""
