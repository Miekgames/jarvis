"""Which tools a local model is offered.

Local models degrade sharply as the tool list grows, so they get a slice of
eight rather than all 115. Choosing that slice by keyword overlap against
tool names had a failure that looked exactly like the model being stupid:

    "list unofficial ps5 emulators"
      -> list_apps, list_automations, list_folder, list_processes,
         list_schedules, list_tasks, create_task, devices

Every slot went to tools whose name begins with "list_", on the strength of
the word "list". There was no web search in the set, so the model replied
"I don't have that on this computer. Let me check the web for you." and then
could not, because it had nothing to check it with.
"""
from __future__ import annotations

import pytest

from backend.ai.tools.registry import _SCAFFOLDING, _ALWAYS_OFFERED, _words


class _Tool:
    def __init__(self, name, description="", category="general"):
        self.name = name
        self.description = description
        self.category = category


def _registry(tools):
    """A registry with just enough surface for select_schemas."""
    from backend.ai.tools.registry import ToolRegistry

    registry = ToolRegistry.__new__(ToolRegistry)
    schemas = [{"name": t.name, "description": t.description,
                "input_schema": {"type": "object", "properties": {}}} for t in tools]
    registry.schemas = lambda include_denied=False: list(schemas)
    registry.all = lambda: list(tools)
    registry._compact_schema = lambda s: s
    return registry


CATALOGUE = [
    _Tool("web_search", "Search the web. Returns titles, URLs and snippets.", "web"),
    _Tool("read_webpage", "Fetch a web page and return its readable text.", "web"),
    _Tool("news_search", "Search recent news articles on a topic.", "web"),
    _Tool("list_apps", "List installed applications.", "system"),
    _Tool("list_folder", "List the contents of a folder.", "files"),
    _Tool("list_processes", "List running processes.", "system"),
    _Tool("list_tasks", "List background tasks.", "tasks"),
    _Tool("list_schedules", "List schedules.", "tasks"),
    _Tool("list_automations", "List automations.", "tasks"),
    _Tool("create_task", "Queue a background task.", "tasks"),
    _Tool("system_status", "CPU, memory and disk.", "system"),
    _Tool("what_am_i_doing", "What the user is currently focused on.", "awareness"),
    _Tool("what_should_i_do_next", "Suggest the next action.", "awareness"),
    _Tool("recall", "Recall something remembered earlier.", "memory"),
    _Tool("find_files", "Find files by name.", "files"),
]


def _names(query, limit=8):
    return [s["name"] for s in _registry(CATALOGUE).select_schemas(query, limit=limit)]


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------
def test_a_question_about_the_world_is_offered_web_search():
    assert "web_search" in _names("list unofficial ps5 emulators")


def test_the_word_list_does_not_claim_every_slot():
    chosen = _names("list unofficial ps5 emulators")
    listers = [n for n in chosen if n.startswith("list_")]
    assert len(listers) <= 2, f"'list' swamped the selection: {chosen}"


def test_the_word_what_does_not_claim_every_slot():
    chosen = _names("what are the best ps5 emulators")
    assert "web_search" in chosen
    assert "what_am_i_doing" not in chosen[:3], f"scaffolding ranked too high: {chosen}"


def test_looking_things_up_is_always_available():
    """A question about the outside world rarely contains the word "search",
    so the web tools are reserved rather than left to win a keyword contest."""
    for query in ("who directed Jaws", "how tall is Ben Nevis", "tell me about kestrels",
                  "what is my cpu usage", "list my running processes"):
        chosen = _names(query)
        for reserved in _ALWAYS_OFFERED:
            assert reserved in chosen, f"{reserved} missing for {query!r}: {chosen}"


# ---------------------------------------------------------------------------
# it must still pick the right tool for real requests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,expected", [
    ("what is my cpu usage", "system_status"),
    ("list my running processes", "list_processes"),
    ("show me my background tasks", "list_tasks"),
    ("what files are in my downloads folder", "list_folder"),
    ("find the file called invoice", "find_files"),
    ("what is the latest news about fusion", "news_search"),
])
def test_the_relevant_tool_survives(query, expected):
    chosen = _names(query)
    assert expected in chosen, f"{expected} missing for {query!r}: {chosen}"


def test_the_limit_is_respected():
    assert len(_names("list unofficial ps5 emulators", limit=8)) == 8
    assert len(_names("anything at all", limit=4)) == 4


def test_a_small_catalogue_is_returned_whole():
    registry = _registry(CATALOGUE[:5])
    assert len(registry.select_schemas("anything", limit=8)) == 5


# ---------------------------------------------------------------------------
# the scaffolding list itself
# ---------------------------------------------------------------------------
def test_scaffolding_covers_the_words_that_caused_this():
    for word in ("list", "what", "show", "find", "get", "tell", "which", "best"):
        assert word in _SCAFFOLDING, f"{word!r} is request phrasing, not a topic"


def test_scaffolding_does_not_swallow_real_topics():
    """Over-filtering would be the opposite failure: a query with nothing
    left to match on falls back to an arbitrary slice."""
    for word in ("cpu", "memory", "disk", "file", "folder", "task", "process",
                 "news", "web", "search", "email", "calendar", "emulator"):
        assert word not in _SCAFFOLDING, f"{word!r} is a real topic word"


def test_scaffolding_words_are_lowercase_and_tokenisable():
    for word in _SCAFFOLDING:
        assert word == word.lower()
        assert _words(word) == {word}, f"{word!r} would never match a token"
