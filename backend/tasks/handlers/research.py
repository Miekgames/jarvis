"""
Multi-step web research as a background task.

Checkpoints after every source, so a crash at source 7 of 12 resumes at 7
rather than starting over. Every claim in the report carries the URL it came
from, and sources that failed to load are reported rather than quietly dropped.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ...core.util import truncate
from ..engine import TaskContext

logger = logging.getLogger("jarvis.tasks.research")

MAX_SOURCES = 8


async def research_handler(ctx: TaskContext) -> Dict[str, Any]:
    query = ctx.params.get("query") or ctx.params.get("topic") or ""
    depth = ctx.params.get("depth", "standard")
    if not query:
        raise ValueError("Research task needs a 'query' parameter.")

    orchestrator = ctx.services["orchestrator"]
    search = ctx.services["web"]

    state = ctx.load_checkpoint()
    findings: List[Dict[str, Any]] = state.get("findings", [])
    queries: List[str] = state.get("queries", [])
    visited: List[str] = state.get("visited", [])
    failed: List[Dict[str, str]] = state.get("failed", [])

    source_budget = {"quick": 4, "standard": MAX_SOURCES, "deep": 14}.get(depth, MAX_SOURCES)
    total_steps = source_budget + 3

    # ---- 1. plan -----------------------------------------------------
    if not queries:
        await ctx.step(1, total_steps, "Planning the search")
        plan = await orchestrator.ask(
            f"Break this research request into 3-5 specific web search queries. "
            f"Return one query per line, nothing else.\n\nRequest: {query}",
            purpose="fast",
        )
        queries = [line.strip("-• ").strip() for line in plan.splitlines() if line.strip()][:5]
        if not queries:
            queries = [query]
        await ctx.save_checkpoint({"queries": queries, "findings": findings, "visited": visited,
                                   "failed": failed})
        await ctx.log(f"Search plan: {'; '.join(queries)}")

    # ---- 2. gather ---------------------------------------------------
    step = 2
    for search_query in queries:
        ctx.checkpoint_now()
        if len(visited) >= source_budget:
            break

        await ctx.step(step, total_steps, f"Searching: {truncate(search_query, 60)}")
        results = await search.search(search_query, max_results=5)
        step += 1

        for result in results:
            ctx.checkpoint_now()
            if len(visited) >= source_budget:
                break
            url = result.get("url") or result.get("href") or ""
            if not url or url in visited:
                continue

            await ctx.progress(
                min(0.85, len(visited) / max(1, source_budget)),
                f"Reading {truncate(_domain(url), 40)}",
            )
            page = await search.fetch(url, max_chars=6000)
            visited.append(url)

            if not page.get("ok"):
                failed.append({"url": url, "error": truncate(page.get("error", "unreachable"), 120)})
                await ctx.log(f"Could not read {url}: {page.get('error')}", "warn")
            else:
                findings.append(
                    {
                        "url": url,
                        "title": page.get("title") or result.get("title", ""),
                        "excerpt": page.get("text", "")[:4000],
                    }
                )
                await ctx.log(f"Read {url} ({len(page.get('text', ''))} chars)")

            await ctx.save_checkpoint(
                {"queries": queries, "findings": findings, "visited": visited, "failed": failed}
            )

    if not findings:
        raise RuntimeError(
            "No sources could be read. The network may be down, or every result blocked automated access."
        )

    # ---- 3. synthesise ------------------------------------------------
    await ctx.step(total_steps - 1, total_steps, "Writing the report")
    corpus = "\n\n".join(
        f"SOURCE {i + 1}: {f['title']}\nURL: {f['url']}\n{f['excerpt'][:2500]}"
        for i, f in enumerate(findings)
    )
    report = await orchestrator.ask(
        "Write a research report answering the request below, using only the sources provided.\n"
        "Structure it as: a two-sentence summary, then the key findings as short paragraphs, "
        "then anything the sources disagree on or leave open.\n"
        "Cite sources inline as [1], [2] matching the SOURCE numbers. Do not invent facts, and say "
        "explicitly when the sources don't answer part of the request.\n\n"
        f"REQUEST: {query}\n\n{corpus}",
        purpose="reasoning",
        max_tokens=3000,
    )

    await ctx.step(total_steps, total_steps, "Done")
    return {
        "query": query,
        "report": report,
        "sources": [{"n": i + 1, "url": f["url"], "title": f["title"]} for i, f in enumerate(findings)],
        "unreachable": failed,
        "source_count": len(findings),
    }


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except Exception:
        return url


# ---------------------------------------------------------------------------
async def deep_research_handler(ctx: TaskContext):
    """Deep Research as a resumable background task.

    Gathering checkpoints after every source, so a crash at source 12 of 18
    resumes at 12 rather than re-reading everything."""
    service = ctx.services["research"]
    project_id = ctx.params.get("project_id")
    if not project_id:
        question = ctx.params.get("question") or ctx.params.get("query")
        if not question:
            raise ValueError("Deep research needs either a project_id or a question.")
        project = await service.create(question, ctx.params.get("depth", "deep"))
        project_id = project["id"]
        await ctx.save_checkpoint({"project_id": project_id})

    async def on_progress(step: str, fraction: float) -> None:
        ctx.checkpoint_now()
        await ctx.progress(min(0.8, fraction * 0.8), step)

    async def checkpoint(state):
        await ctx.save_checkpoint({"project_id": project_id, **state})

    await ctx.log(f"Gathering sources for project {project_id}")
    gathered = await service.gather(project_id, on_progress=on_progress, checkpoint=checkpoint)
    await ctx.log(f"{gathered['sources_added']} new source(s) read")

    await ctx.progress(0.85, "Checking for disagreements between sources")
    conflicts = await service.find_conflicts(project_id)
    if conflicts:
        await ctx.log(f"{len(conflicts)} disagreement(s) found between sources", "warn")

    await ctx.progress(0.92, "Writing the report")
    result = await service.synthesise(project_id)

    await ctx.progress(1.0, "Done")
    return {
        "project_id": project_id,
        "summary": result["summary"],
        "report": result["report"],
        "sources": result["source_count"],
        "conflicts": result["conflicts"],
        "outdated": result["outdated"],
        "unreachable": result["unreachable"],
        "citations": [c["text"] for c in result["citations"]],
    }


# ---------------------------------------------------------------------------
async def long_research_handler(ctx: TaskContext):
    """Fifteen to twenty minutes of research, then a 3-4 paragraph report,
    delivered into the conversation it was asked in and as a notification."""
    from ...core.events import Events, bus
    from ...core.util import now_iso
    from ...db.database import dumps
    from ...intelligence.long_research import LongResearch, as_message, paragraphs

    question = (ctx.params.get("question") or "").strip()
    if not question:
        raise ValueError("Research needs a question.")
    services = ctx.services
    worker = LongResearch(services["orchestrator"], services["web"], log=ctx.log)

    async def progress(fraction: float, step: str) -> None:
        ctx.checkpoint_now()
        await ctx.progress(fraction, step)

    await ctx.log(f"Researching for about {ctx.params.get('minutes', 17)} minutes: {question}")
    # The screen wears the deep-research colour for as long as this runs --
    # until the report, or until it is stopped.
    from ...skills.activity import skill_activity

    skills = services.get("skills")
    skill_activity.task_started(ctx.task_id, skills.get("deep-research") if skills else None)
    try:
        result = await worker.run(question, ctx.params.get("minutes", 17), progress)
    finally:
        skill_activity.task_done(ctx.task_id)
    await ctx.log(f"{result['searches']} searches, {result['pages_read']} pages read, "
                  f"{result['findings']} findings")
    message = as_message(result)
    first = (paragraphs(result["report"]) or [""])[0]

    # Kept with the other research, so it can be found again.
    research = services.get("research")
    if research is not None:
        try:
            project = await research.create(question, "deep")
            await research.db.execute(
                "UPDATE research_projects SET status = 'complete', summary = ?, report = ?,"
                " queries = ?, task_id = ?, updated_at = ? WHERE id = ?",
                (first[:600], message, dumps(result["queries"]), ctx.task_id, now_iso(),
                 project["id"]))
            result["project_id"] = project["id"]
        except Exception:
            logger.debug("could not save the research project", exc_info=True)

    # Into the conversation it was asked in.
    conversation_id = ctx.params.get("conversation_id")
    orchestrator = services.get("orchestrator")
    if conversation_id and orchestrator is not None:
        try:
            await orchestrator._save_message(conversation_id, "assistant", message,
                                             [{"type": "text", "text": message}])
            bus.publish(Events.MESSAGE, {"conversation_id": conversation_id, "role": "assistant",
                                         "text": message, "late": True}, source="research")
        except Exception:
            logger.debug("could not post the report to the conversation", exc_info=True)

    notifications = services.get("notifications")
    if notifications is not None:
        await notifications.create(title=f"Research finished: {question[:70]}",
                                   body=first[:400], priority="high", source="research",
                                   action={"panel": "research"})
    await ctx.progress(1.0, "Done")
    return {key: result.get(key) for key in ("report", "sources", "minutes", "searches",
                                             "pages_read", "findings", "stopped_early")} | {
        "project_id": result.get("project_id")}
