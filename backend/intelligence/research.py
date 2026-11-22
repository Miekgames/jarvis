"""
Deep Research mode.

A research *project*, not a one-shot answer: sources are tracked individually,
conflicts between them are surfaced rather than smoothed over, citations are
generated from what was actually read, potentially outdated material is flagged,
and the whole thing is resumable days later.

The distinction that matters: this module records what each source *said*, so
the report can attribute claims and show disagreement. A single blended summary
would lose exactly the information that makes research trustworthy.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.events import bus
from ..core.util import iso, new_id, now_iso, parse_iso, truncate, utcnow
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.research")

DEPTHS = {
    "quick": {"queries": 2, "sources": 4, "chars": 4000},
    "standard": {"queries": 4, "sources": 9, "chars": 6000},
    "deep": {"queries": 7, "sources": 18, "chars": 8000},
}

# Anything older than this is worth flagging in a fast-moving topic.
STALE_AFTER_DAYS = 540

_DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2})\b", re.I),
    re.compile(r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2})\b", re.I),
]

_CONFLICT_SYSTEM = """You compare what sources say and find genuine disagreements.

Return ONLY a JSON array (empty if the sources agree):
[
  {
    "claim": the point at issue, stated neutrally,
    "positions": [ {"source": <source number>, "says": short quote or paraphrase} ],
    "severity": "minor" | "material",
    "note": one sentence on which is more likely and why (recency, specificity, authority)
  }
]

Only report real contradictions about facts. Different emphasis, different scope, or one
source simply covering more ground is NOT a conflict.
"""


class ResearchService:
    def __init__(self, db, orchestrator, web, activity, tasks=None, notifications=None):
        self.db = db
        self.orchestrator = orchestrator
        self.web = web
        self.activity = activity
        self.tasks = tasks
        self.notifications = notifications

    # ==================================================================
    # projects
    # ==================================================================
    async def create(self, question: str, depth: str = "standard",
                     goal_id: Optional[str] = None) -> Dict[str, Any]:
        depth = depth if depth in DEPTHS else "standard"
        project_id = new_id("res")
        await self.db.execute(
            "INSERT INTO research_projects (id, question, depth, status, created_at, updated_at,"
            " goal_id) VALUES (?,?,?,'active',?,?,?)",
            (project_id, question, depth, now_iso(), now_iso(), goal_id),
        )
        await self.activity.record(f"Research project opened: {truncate(question, 70)}",
                                   source="research")
        bus.publish("research.started", {"id": project_id, "question": truncate(question, 120),
                                         "depth": depth}, source="research")
        return await self.get(project_id)

    async def get(self, project_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT * FROM research_projects WHERE id = ?", (project_id,))
        if not row:
            return {}
        record = _decode(row)
        record["sources"] = await self.sources(project_id)
        return record

    async def list(self, limit: int = 25, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM research_projects"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        rows = await self.db.fetch_all(sql, (limit,))
        out = []
        for row in rows:
            record = _decode(row)
            record["source_count"] = int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM research_sources WHERE project_id = ?",
                (row["id"],), default=0))
            out.append(record)
        return out

    async def sources(self, project_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM research_sources WHERE project_id = ? ORDER BY n", (project_id,))
        return [{**dict(r), "stale": bool(r["stale"]), "reachable": bool(r["reachable"])}
                for r in rows]

    # ==================================================================
    # gathering
    # ==================================================================
    async def plan_queries(self, question: str, depth: str) -> List[str]:
        budget = DEPTHS[depth]["queries"]
        raw = await self.orchestrator.ask(
            f"Break this research question into {budget} specific web search queries that together "
            f"cover it from different angles. One per line, no numbering, no commentary.\n\n"
            f"Question: {question}",
            purpose="fast", max_tokens=400,
        )
        queries = [
            re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip().strip('"')
            for line in (raw or "").splitlines() if line.strip()
        ]
        queries = [q for q in queries if 3 < len(q) < 200][:budget]
        return queries or [question]

    async def gather(self, project_id: str, on_progress=None,
                     checkpoint=None) -> Dict[str, Any]:
        """Search and read, recording every source. Safe to call repeatedly --
        already-visited URLs are skipped, which is what makes a research project
        resumable after a crash or a pause."""
        project = await self.get(project_id)
        if not project:
            raise KeyError(project_id)

        depth = DEPTHS[project["depth"]]
        queries = project.get("queries") or await self.plan_queries(project["question"],
                                                                    project["depth"])
        await self.db.execute(
            "UPDATE research_projects SET queries = ?, updated_at = ? WHERE id = ?",
            (dumps(queries), now_iso(), project_id),
        )

        existing = await self.sources(project_id)
        visited = {s["url"] for s in existing}
        next_n = max([s["n"] for s in existing], default=0) + 1
        added = 0

        for query in queries:
            if len(visited) >= depth["sources"]:
                break
            if on_progress:
                await on_progress(f"Searching: {truncate(query, 50)}",
                                  len(visited) / max(1, depth["sources"]))
            try:
                results = await self.web.search(query, max_results=5)
            except Exception as exc:
                logger.warning("search failed for '%s': %s", query, exc)
                continue

            for result in results:
                if len(visited) >= depth["sources"]:
                    break
                url = result.get("url") or ""
                if not url or url in visited:
                    continue
                visited.add(url)

                if on_progress:
                    await on_progress(f"Reading {_domain(url)}",
                                      len(visited) / max(1, depth["sources"]))

                page = await self.web.fetch(url, max_chars=depth["chars"])
                published, stale, reason = _assess_freshness(page.get("text", ""))

                await self.db.execute(
                    "INSERT INTO research_sources (id, project_id, n, url, title, domain, excerpt,"
                    " published, retrieved_at, stale, stale_reason, reachable, error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("src"), project_id, next_n, url,
                     truncate(page.get("title") or result.get("title", ""), 200),
                     _domain(url), truncate(page.get("text", ""), depth["chars"]),
                     published, now_iso(), 1 if stale else 0, reason,
                     1 if page.get("ok") else 0,
                     truncate(page.get("error", ""), 200)),
                )
                next_n += 1
                added += 1
                if checkpoint:
                    await checkpoint({"visited": list(visited), "queries": queries})

        await self.db.execute(
            "UPDATE research_projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))
        return {"sources_added": added, "total_sources": len(visited), "queries": queries}

    # ==================================================================
    # analysis
    # ==================================================================
    async def find_conflicts(self, project_id: str) -> List[Dict[str, Any]]:
        """Ask specifically for disagreements, separately from the summary.

        Rolled into one 'write me a report' prompt, contradictions get smoothed
        into consensus prose -- which is precisely the information a researcher
        needs to see."""
        sources = [s for s in await self.sources(project_id) if s["reachable"] and s["excerpt"]]
        if len(sources) < 2:
            return []

        project = await self.get(project_id)
        corpus = "\n\n".join(
            f"SOURCE {s['n']} ({s['domain']}):\n{truncate(s['excerpt'], 2200)}" for s in sources[:12]
        )
        raw = await self.orchestrator.ask(
            f"Question under research: {project['question']}\n\n{corpus}",
            system=_CONFLICT_SYSTEM, purpose="reasoning", max_tokens=1500,
        )
        conflicts = _parse_array(raw)
        await self.db.execute(
            "UPDATE research_projects SET conflicts = ?, updated_at = ? WHERE id = ?",
            (dumps(conflicts), now_iso(), project_id),
        )
        if conflicts:
            logger.info("research %s: %s conflict(s) found", project_id, len(conflicts))
        return conflicts

    async def synthesise(self, project_id: str) -> Dict[str, Any]:
        """Executive summary and full report, both cited."""
        project = await self.get(project_id)
        sources = [s for s in project["sources"] if s["reachable"] and s["excerpt"]]
        unreachable = [s for s in project["sources"] if not s["reachable"]]
        stale = [s for s in sources if s["stale"]]

        if not sources:
            raise RuntimeError(
                "No sources could be read. The network may be down, or every result blocked "
                "automated access."
            )

        conflicts = project.get("conflicts") or await self.find_conflicts(project_id)
        corpus = "\n\n".join(
            f"SOURCE {s['n']} — {s['title'] or s['domain']} ({s['url']})"
            + (f" [published {s['published']}]" if s["published"] else "")
            + (" [POSSIBLY OUTDATED]" if s["stale"] else "")
            + f"\n{truncate(s['excerpt'], 2500)}"
            for s in sources[:14]
        )
        conflict_note = ""
        if conflicts:
            conflict_note = "\n\nKNOWN DISAGREEMENTS between sources:\n" + "\n".join(
                f"- {c.get('claim')}: " + "; ".join(
                    f"source {p.get('source')} says {p.get('says')}"
                    for p in (c.get("positions") or [])
                ) for c in conflicts[:6]
            )

        summary = await self.orchestrator.ask(
            "Write a two-to-three sentence executive summary answering the question, using only "
            "these sources. Cite as [1], [2]. If the sources disagree on the central point, say so "
            "in the summary rather than picking a side silently.\n\n"
            f"QUESTION: {project['question']}\n\n{corpus}{conflict_note}",
            purpose="reasoning", max_tokens=500,
        )

        report = await self.orchestrator.ask(
            "Write a detailed research report using only these sources.\n"
            "Structure: what the evidence shows; where sources disagree and which is more "
            "credible; what remains unanswered; what to treat with caution (outdated or "
            "single-sourced claims).\n"
            "Cite inline as [1], [2] matching the SOURCE numbers. Never state something no "
            "source supports. Say explicitly when the sources do not answer part of the question."
            f"\n\nQUESTION: {project['question']}\n\n{corpus}{conflict_note}",
            purpose="reasoning", max_tokens=3000,
        )

        await self.db.execute(
            "UPDATE research_projects SET summary = ?, report = ?, status = 'complete',"
            " updated_at = ? WHERE id = ?",
            (summary, report, now_iso(), project_id),
        )
        bus.publish("research.completed", {"id": project_id, "sources": len(sources),
                                           "conflicts": len(conflicts)}, source="research")

        return {
            "project_id": project_id,
            "question": project["question"],
            "summary": summary,
            "report": report,
            "citations": self.citations(sources),
            "conflicts": conflicts,
            "source_count": len(sources),
            "unreachable": [{"url": s["url"], "error": s["error"]} for s in unreachable],
            "outdated": [{"n": s["n"], "url": s["url"], "why": s["stale_reason"]} for s in stale],
        }

    # ==================================================================
    @staticmethod
    def citations(sources: List[Dict[str, Any]], style: str = "plain") -> List[Dict[str, Any]]:
        """Citations built from what was actually retrieved -- never invented."""
        out = []
        for source in sources:
            retrieved = parse_iso(source["retrieved_at"])
            accessed = retrieved.astimezone().strftime("%d %B %Y") if retrieved else "unknown"
            title = source["title"] or source["domain"]
            if style == "markdown":
                text = f"[{source['n']}] [{title}]({source['url']}) — {source['domain']}, accessed {accessed}"
            else:
                text = f"[{source['n']}] {title}. {source['domain']}. {source['url']} (accessed {accessed})"
            out.append({
                "n": source["n"], "text": text, "url": source["url"],
                "domain": source["domain"], "accessed": accessed,
                "published": source["published"], "outdated": source["stale"],
            })
        return out

    async def continue_project(self, project_id: str) -> Dict[str, Any]:
        """Resume a saved project: gather more, re-check conflicts, rewrite."""
        project = await self.get(project_id)
        if not project:
            raise KeyError(project_id)
        await self.db.execute(
            "UPDATE research_projects SET status = 'active', updated_at = ? WHERE id = ?",
            (now_iso(), project_id),
        )
        gathered = await self.gather(project_id)
        result = await self.synthesise(project_id)
        return {**result, "new_sources": gathered["sources_added"]}

    async def delete(self, project_id: str) -> bool:
        await self.db.execute("DELETE FROM research_sources WHERE project_id = ?", (project_id,))
        return await self.db.execute(
            "DELETE FROM research_projects WHERE id = ?", (project_id,)) > 0

    async def stats(self) -> Dict[str, Any]:
        return {
            "projects": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM research_projects", default=0)),
            "active": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM research_projects WHERE status = 'active'", default=0)),
            "sources": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM research_sources", default=0)),
        }


# ---------------------------------------------------------------------------
def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url[:60]
    except Exception:
        return url[:60]


def _assess_freshness(text: str) -> Tuple[Optional[str], bool, str]:
    """Look for a publication date in the page and judge whether it's old.

    Heuristic and honest about it: a date found in body text may not be the
    publication date, so a flag means 'worth checking', not 'wrong'."""
    if not text:
        return None, False, ""

    head = text[:2500]
    found: Optional[datetime] = None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(head)
        if not match:
            continue
        raw = match.group(0)
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                found = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if found:
            break

    if found is None:
        return None, False, ""

    age_days = (datetime.now() - found).days
    if age_days > STALE_AFTER_DAYS:
        return (found.strftime("%Y-%m-%d"), True,
                f"page appears to date from {found.strftime('%B %Y')}, about "
                f"{age_days // 365} year(s) old")
    return found.strftime("%Y-%m-%d"), False, ""


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["conflicts"] = loads(item.get("conflicts"), [])
    item["queries"] = loads(item.get("queries"), [])
    return item


def _parse_array(raw: str) -> List[Dict[str, Any]]:
    import json

    text = re.sub(r"<think>.*?</think>", " ", raw or "", flags=re.S | re.I)
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [c for c in parsed if isinstance(c, dict) and c.get("claim")] \
        if isinstance(parsed, list) else []
