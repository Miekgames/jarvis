"""Update 3: goal milestones, agent boundaries, research analysis and the
context resolver."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from backend.core.util import ahead, iso, now_iso, since, utcnow
from backend.intelligence.agents import AGENTS, AgentOrchestrator, DEFAULT_AGENT
from backend.intelligence.goals2 import GoalPlanner, _spread_deadlines
from backend.intelligence.memory2 import MemoryManager
from backend.intelligence.research import ResearchService, _assess_freshness, _domain
from backend.intelligence.resolver import ContextResolver


# ===========================================================================
# fixtures
# ===========================================================================
class FakeOrchestrator:
    """Returns canned model output so tests assert on our logic, not the model."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, use_tools=False):
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        for marker, reply in self.replies.items():
            if marker in (system or "") or marker in prompt:
                return reply
        return self.replies.get("_default", "")


@pytest.fixture
async def research_service(db, activity, web_stub):
    return ResearchService(db, FakeOrchestrator(), web_stub, activity)


@pytest.fixture
def web_stub():
    class Web:
        def __init__(self):
            self.pages = {}
            self.results = []

        async def search(self, query, max_results=6, region="wt-wt"):
            return self.results[:max_results]

        async def fetch(self, url, max_chars=8000):
            return self.pages.get(url, {"ok": False, "url": url, "error": "not found"})

    return Web()


@pytest.fixture
async def resolver(db, engine):
    return ContextResolver(db, awareness=None, tasks=engine)


# ===========================================================================
# goal milestones
# ===========================================================================
def test_deadlines_spread_by_effort_and_end_on_time():
    deadline = iso(utcnow() + timedelta(days=10))
    milestones = [{"effort_hours": 1}, {"effort_hours": 3}, {"effort_hours": 1}]

    dates = _spread_deadlines(milestones, deadline)
    assert len(dates) == 3
    assert all(d is not None for d in dates)

    from backend.core.util import parse_iso
    parsed = [parse_iso(d) for d in dates]
    assert parsed[0] < parsed[1] < parsed[2]
    # The last milestone lands on the deadline, not past it.
    assert abs((parsed[2] - parse_iso(deadline)).total_seconds()) < 60
    # The heavy middle milestone gets the largest share of time.
    assert (parsed[1] - parsed[0]) > (parsed[2] - parsed[1])


def test_no_deadline_means_no_invented_dates():
    assert _spread_deadlines([{"effort_hours": 2}, {"effort_hours": 2}], None) == [None, None]


def test_deadline_already_passed_does_not_produce_negative_schedule():
    dates = _spread_deadlines([{"effort_hours": 1}], iso(utcnow() - timedelta(days=1)))
    assert all(d is not None for d in dates)


@pytest.mark.asyncio
async def test_assess_flags_blocking_questions(db, activity, engine, settings_store):
    orchestrator = FakeOrchestrator({"_default": """
    {"size": "large", "estimated_hours": 20, "milestone_count": 5, "confidence": 0.4,
     "needs_clarification": ["Which platform?", "What is the budget?"],
     "reasoning": "The objective names no platform.", "risks": ["scope creep"]}
    """})
    planner = GoalPlanner(db, orchestrator, engine, activity, goals=None)

    assessment = await planner.assess("Build me an app")
    assert assessment["size"] == "large"
    assert assessment["needs_approval"] is True
    assert len(assessment["questions"]) == 2
    assert assessment["confidence"] == 0.4


@pytest.mark.asyncio
async def test_goal_with_questions_waits_instead_of_guessing(db, activity, engine, settings_store):
    orchestrator = FakeOrchestrator({"_default": """
    {"size": "medium", "estimated_hours": 4, "milestone_count": 2, "confidence": 0.5,
     "needs_clarification": ["Which repository?"], "reasoning": "ambiguous", "risks": []}
    """})
    planner = GoalPlanner(db, orchestrator, engine, activity, goals=None)

    record = await planner.create("Fix the build", auto_start=True)
    assert record["status"] == "needs_input"
    assert record["awaiting_answers"] == ["Which repository?"]

    # Nothing should have been queued while it was waiting.
    assert await engine.list() == []


@pytest.mark.asyncio
async def test_goal_creates_milestones_and_tasks(db, activity, engine, settings_store):
    async def handler(ctx):
        return "done"

    engine.register("agent", handler)
    engine.register("research", handler)

    orchestrator = FakeOrchestrator({
        "You estimate how much work": """
        {"size": "medium", "estimated_hours": 6, "milestone_count": 2, "confidence": 0.8,
         "needs_clarification": [], "reasoning": "clear", "risks": []}""",
        "You break an objective into milestones": """
        [
          {"title": "Requirements gathered", "detail": "Know what is needed",
           "depends_on": [], "effort_hours": 2,
           "tasks": [{"title": "Research requirements", "task_type": "research",
                      "agent": "researcher", "params": {"query": "requirements"}}]},
          {"title": "Draft written", "detail": "First version exists",
           "depends_on": [0], "effort_hours": 4,
           "tasks": [{"title": "Write draft", "task_type": "agent", "agent": "general"}]}
        ]""",
    })
    planner = GoalPlanner(db, orchestrator, engine, activity, goals=None)

    goal = await planner.create("Prepare the project brief", deadline=iso(utcnow() + timedelta(days=5)))
    milestones = await planner.milestones(goal["id"])

    assert len(milestones) == 2
    assert milestones[0]["title"] == "Requirements gathered"
    assert milestones[1]["depends_on"] == [0]
    assert all(m["due_at"] for m in milestones), "a deadline should date every milestone"

    await planner.start(goal["id"])
    tasks = await engine.list()
    assert len(tasks) == 2

    # The second milestone's task must wait for the first.
    second = next(t for t in tasks if t["name"] == "Write draft")
    assert second["status"] == "waiting"


@pytest.mark.asyncio
async def test_goal_progress_tracks_milestone_completion(db, activity, engine, settings_store):
    async def handler(ctx):
        return "ok"

    engine.register("agent", handler)
    orchestrator = FakeOrchestrator({
        "You estimate": '{"size":"small","estimated_hours":1,"milestone_count":1,'
                        '"confidence":0.9,"needs_clarification":[],"reasoning":"","risks":[]}',
        "You break an objective": '[{"title":"Only step","detail":"do it","depends_on":[],'
                                  '"effort_hours":1,"tasks":[{"title":"Do it",'
                                  '"task_type":"agent","agent":"general"}]}]',
        "_default": "Report text.",
    })
    planner = GoalPlanner(db, orchestrator, engine, activity, goals=None)

    goal = await planner.create("Small thing")
    await planner.start(goal["id"])
    await engine.start()

    for _ in range(60):
        state = await planner.progress(goal["id"])
        if state["percent"] == 100:
            break
        await asyncio.sleep(0.1)

    state = await planner.progress(goal["id"])
    assert state["percent"] == 100
    assert state["completed_milestones"] == 1


# ===========================================================================
# agent boundaries
# ===========================================================================
def test_every_agent_declares_boundaries():
    for key, spec in AGENTS.items():
        assert spec.label and spec.description
        assert spec.time_limit_s > 0
        assert isinstance(spec.denied_permissions, list)
        assert spec.colour.startswith("#")


def test_researcher_cannot_modify_anything():
    researcher = AGENTS["researcher"]
    for permission in ("files.write", "files.delete", "email.send", "shell.execute"):
        assert permission in researcher.denied_permissions


def test_no_agent_may_run_shell_or_touch_security():
    for key, spec in AGENTS.items():
        assert "shell.execute" in spec.denied_permissions or key == "coder", \
            f"{key} should not have shell access"
        assert "smarthome.security" in spec.denied_permissions


@pytest.mark.asyncio
async def test_agent_selection_is_explainable(db, registry, permissions, activity):
    orchestrator = AgentOrchestrator(db, FakeOrchestrator(), registry, permissions, activity)

    coding = orchestrator.select("there's a bug in this function, the stack trace says KeyError")
    assert coding["agent"] == "coder"
    assert coding["why"]
    assert coding["confidence"] > 0.4

    research = orchestrator.select("research the latest on local LLM inference and compare")
    assert research["agent"] == "researcher"


@pytest.mark.asyncio
async def test_unmatched_objective_falls_back_honestly(db, registry, permissions, activity):
    orchestrator = AgentOrchestrator(db, FakeOrchestrator(), registry, permissions, activity)
    selection = orchestrator.select("hmm")
    assert selection["agent"] == DEFAULT_AGENT
    assert selection["confidence"] < 0.5
    assert "general" in selection["why"].lower()


@pytest.mark.asyncio
async def test_agent_tool_set_respects_its_boundary(db, registry, permissions, activity):
    from backend.ai.tools.registry import Tool, ok

    async def handler(ctx):
        return ok()

    registry.register(Tool("delete_thing", "d", {"type": "object", "properties": {}}, handler,
                           permission="files.delete", category="files"))
    registry.register(Tool("read_thing", "r", {"type": "object", "properties": {}}, handler,
                           permission="files.read", category="files"))
    await permissions.set("files.delete", True)

    orchestrator = AgentOrchestrator(db, FakeOrchestrator(), registry, permissions, activity)
    names = {t["name"] for t in orchestrator.tools_for(AGENTS["researcher"])}

    assert "read_thing" in names
    assert "delete_thing" not in names, "the boundary must hold even when the user allows it"


@pytest.mark.asyncio
async def test_agent_messages_persist_and_are_consumed_once(db, registry, permissions, activity):
    orchestrator = AgentOrchestrator(db, FakeOrchestrator(), registry, permissions, activity)

    await orchestrator.send("run_a", "found three candidate sources", to_run="run_b")
    inbox = await orchestrator.inbox("run_b")
    assert len(inbox) == 1
    assert "three candidate" in inbox[0]["body"]
    assert await orchestrator.inbox("run_b") == []


@pytest.mark.asyncio
async def test_stranded_agent_runs_are_cleared_on_boot(db, registry, permissions, activity):
    orchestrator = AgentOrchestrator(db, FakeOrchestrator(), registry, permissions, activity)
    await db.execute(
        "INSERT INTO agent_runs (id, agent, objective, status, created_at) VALUES (?,?,?,?,?)",
        ("run_x", "researcher", "something", "running", now_iso()),
    )
    assert await orchestrator.recover() == 1
    assert (await orchestrator.get("run_x"))["status"] == "cancelled"


# ===========================================================================
# research
# ===========================================================================
def test_domain_extraction():
    assert _domain("https://www.example.com/a/b?c=1") == "example.com"
    assert _domain("not a url") == "not a url"


def test_freshness_flags_old_pages_only():
    old, stale, reason = _assess_freshness("Published 2019-03-14. Some article text.")
    assert stale is True and "2019" in reason

    recent, stale_recent, _ = _assess_freshness(
        f"Published {utcnow().strftime('%Y-%m-%d')}. Fresh text.")
    assert stale_recent is False

    none_found, stale_none, _ = _assess_freshness("No date anywhere in this text.")
    assert none_found is None and stale_none is False


@pytest.mark.asyncio
async def test_research_tracks_each_source(research_service, web_stub):
    web_stub.results = [
        {"url": "https://a.com/1", "title": "A"},
        {"url": "https://b.com/2", "title": "B"},
    ]
    web_stub.pages = {
        "https://a.com/1": {"ok": True, "url": "https://a.com/1", "title": "A",
                            "text": "Published 2024-01-01. Alpha says X."},
        "https://b.com/2": {"ok": False, "url": "https://b.com/2", "error": "403 blocked"},
    }

    project = await research_service.create("Does X hold?", depth="quick")
    research_service.orchestrator.replies["_default"] = "query one\nquery two"
    await research_service.gather(project["id"])

    sources = await research_service.sources(project["id"])
    assert len(sources) == 2
    reachable = [s for s in sources if s["reachable"]]
    unreachable = [s for s in sources if not s["reachable"]]
    assert len(reachable) == 1
    assert unreachable[0]["error"] == "403 blocked", "failures are recorded, not dropped"


@pytest.mark.asyncio
async def test_citations_are_built_from_retrieved_sources(research_service, web_stub):
    web_stub.results = [{"url": "https://a.com/1", "title": "A"}]
    web_stub.pages = {"https://a.com/1": {"ok": True, "url": "https://a.com/1",
                                          "title": "Alpha Report", "text": "content"}}
    project = await research_service.create("Q", depth="quick")
    research_service.orchestrator.replies["_default"] = "one query"
    await research_service.gather(project["id"])

    citations = research_service.citations(await research_service.sources(project["id"]))
    assert len(citations) == 1
    assert "Alpha Report" in citations[0]["text"]
    assert "a.com" in citations[0]["text"]
    assert "accessed" in citations[0]["text"]


@pytest.mark.asyncio
async def test_synthesis_refuses_when_nothing_could_be_read(research_service, web_stub):
    web_stub.results = [{"url": "https://x.com/1", "title": "X"}]
    web_stub.pages = {"https://x.com/1": {"ok": False, "url": "https://x.com/1",
                                          "error": "timeout"}}
    project = await research_service.create("Q", depth="quick")
    research_service.orchestrator.replies["_default"] = "one query"
    await research_service.gather(project["id"])

    with pytest.raises(RuntimeError, match="No sources could be read"):
        await research_service.synthesise(project["id"])


@pytest.mark.asyncio
async def test_gather_is_resumable_and_skips_seen_sources(research_service, web_stub):
    web_stub.results = [{"url": "https://a.com/1", "title": "A"}]
    web_stub.pages = {"https://a.com/1": {"ok": True, "url": "https://a.com/1",
                                          "title": "A", "text": "text"}}
    project = await research_service.create("Q", depth="quick")
    research_service.orchestrator.replies["_default"] = "one query"

    first = await research_service.gather(project["id"])
    second = await research_service.gather(project["id"])

    assert first["sources_added"] == 1
    assert second["sources_added"] == 0, "a resumed project must not re-read what it has"


# ===========================================================================
# context resolver
# ===========================================================================
def test_vague_commands_are_classified():
    resolver = ContextResolver(None)
    assert resolver.classify("JARVIS, take care of this") == "current"
    assert resolver.classify("finish what I was doing yesterday") == "historical"
    assert resolver.classify("continue that project") == "interrupted"
    assert resolver.classify("find out why this isn't working") == "problem"
    assert resolver.classify("handle the boring stuff") == "chores"
    assert resolver.classify("prepare everything for tomorrow") == "preparation"


def test_specific_commands_are_not_classified_as_vague():
    resolver = ContextResolver(None)
    assert resolver.classify("open spotify") is None
    assert resolver.classify("what is my cpu usage") is None


def test_destructive_detection():
    resolver = ContextResolver(None)
    assert resolver.is_destructive("delete that")
    assert resolver.is_destructive("shut down the computer")
    assert not resolver.is_destructive("show me that")


@pytest.mark.asyncio
async def test_resolver_offers_candidates_from_real_state(resolver, db, engine):
    async def handler(ctx):
        await asyncio.sleep(5)

    engine.register("slow", handler)
    await engine.start()
    await engine.create("Compile the report", "slow")
    await asyncio.sleep(0.6)

    result = await resolver.resolve("take care of this")
    assert result["vague"] is True
    assert result["candidates"], "should surface the running task"
    assert any("Compile the report" in c["label"] for c in result["candidates"])


@pytest.mark.asyncio
async def test_resolver_never_guesses_on_destructive_commands(resolver, db, engine):
    async def handler(ctx):
        await asyncio.sleep(5)

    engine.register("slow", handler)
    await engine.start()
    await engine.create("Only candidate", "slow")
    await asyncio.sleep(0.6)

    result = await resolver.resolve("delete this")
    assert result["destructive"] is True
    assert result["resolved"] is None, "a destructive command must never auto-resolve"
    assert "irreversible" in result["question"]


@pytest.mark.asyncio
async def test_resolver_is_honest_when_it_has_nothing(resolver):
    result = await resolver.resolve("take care of this")
    assert result["vague"] is True
    assert result["candidates"] == []
    assert result["resolved"] is None
    assert "What should I take care of" in result["question"]


@pytest.mark.asyncio
async def test_context_block_tells_the_model_not_to_guess(resolver, db, engine):
    async def handler(ctx):
        await asyncio.sleep(5)

    engine.register("slow", handler)
    await engine.start()
    for name in ("Task one", "Task two"):
        await engine.create(name, "slow")
    await asyncio.sleep(0.7)

    block = await resolver.context_block("delete that")
    assert "Do NOT guess" in block
    assert "Task one" in block or "Task two" in block


# ===========================================================================
# memory 2.0
# ===========================================================================
@pytest.fixture
async def memory_manager(db, settings_store, activity):
    from backend.ai.memory import MemoryStore

    return MemoryManager(db, MemoryStore(db, settings_store), None, activity)


@pytest.mark.asyncio
async def test_short_term_memory_expires(memory_manager, db):
    record = await memory_manager.remember("temporary note", scope="short_term")
    assert record["scope"] == "short_term"
    assert record["expires_at"]

    await db.execute("UPDATE memory SET expires_at = ? WHERE id = ?",
                     (since(3600), record["id"]))
    assert await memory_manager.expire() == 1
    assert await memory_manager.get(record["id"]) == {}


@pytest.mark.asyncio
async def test_long_term_memory_does_not_expire(memory_manager, db):
    record = await memory_manager.remember("permanent fact", scope="long_term")
    assert record["expires_at"] is None
    await memory_manager.expire()
    assert await memory_manager.get(record["id"])


@pytest.mark.asyncio
async def test_project_memory_is_scoped(memory_manager):
    await memory_manager.remember("project detail", scope="project", project_id="prj_1")
    await memory_manager.remember("global detail", scope="long_term")

    in_project = await memory_manager.recall("detail", project_id="prj_1")
    elsewhere = await memory_manager.recall("detail", project_id="prj_2")

    assert any(m["value"] == "project detail" for m in in_project)
    assert not any(m["value"] == "project detail" for m in elsewhere)
    assert any(m["value"] == "global detail" for m in elsewhere)


@pytest.mark.asyncio
async def test_provenance_is_recorded_and_explainable(memory_manager):
    record = await memory_manager.remember(
        "he prefers dark mode", scope="long_term", source="conversation",
        provenance={"source": "conversation", "conversation_id": "conv_1"},
    )
    explanation = await memory_manager.explain(record["id"])
    assert "conversation" in explanation["origin"]
    assert explanation["provenance"]["conversation_id"] == "conv_1"


@pytest.mark.asyncio
async def test_recall_increments_usage(memory_manager):
    record = await memory_manager.remember("kettle is broken", scope="long_term")
    await memory_manager.recall("kettle")
    assert (await memory_manager.get(record["id"]))["use_count"] >= 1


@pytest.mark.asyncio
async def test_cleanup_reports_before_deleting(memory_manager, db):
    await memory_manager.remember("duplicate", scope="long_term")
    await memory_manager.remember("duplicate", scope="long_term")

    report = await memory_manager.cleanup_report()
    assert report["duplicate_groups"]
    assert report["total_removable"] >= 1
    # Nothing removed by asking.
    assert int(await db.fetch_value("SELECT COUNT(*) FROM memory", default=0)) == 2

    result = await memory_manager.cleanup(duplicates=True)
    assert result["duplicates"] == 1


@pytest.mark.asyncio
async def test_cleanup_never_removes_pinned(memory_manager, db):
    record = await memory_manager.remember("important", scope="short_term")
    await db.execute("UPDATE memory SET pinned = 1, expires_at = ? WHERE id = ?",
                     (since(3600), record["id"]))
    await memory_manager.cleanup(expired=True)
    assert await memory_manager.get(record["id"])


@pytest.mark.asyncio
async def test_promote_makes_a_memory_permanent(memory_manager):
    record = await memory_manager.remember("keep this", scope="short_term")
    promoted = await memory_manager.promote(record["id"])
    assert promoted["scope"] == "long_term"
    assert promoted["expires_at"] is None
