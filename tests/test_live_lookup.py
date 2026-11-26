"""Today's facts are looked up and read before answering.

Asked "who is projected to win the bears v lions game", the local model said
"The web search didn't provide a direct projection, but there is a video..."
-- it had six snippets, one of them a YouTube link, and read none of the
articles. Asked again, it said it had no real-time data and stopped.
"""
from __future__ import annotations

import pytest

from backend.ai.orchestrator import dodged_lookup
from backend.services.live_lookup import (
    as_context, excerpts, follow_up_question, key_terms, look_up, needs_live_lookup,
    search_query,
)


@pytest.mark.parametrize("question", [
    "who is projected to win the bears v lions game",
    "Who's favoured in the Lakers game tonight?",
    "who won the lakers game last night",
    "what's the score of the bears game",
    "bears v lions game, who wins?",
    "latest news on the AI act",
    "what's the stock price of nvidia",
])
def test_questions_that_need_todays_facts_are_looked_up(question):
    assert needs_live_lookup(question)


@pytest.mark.parametrize("question", [
    "what is a point spread", "explain how odds work", "who is the president of france",
    "write a poem about the lions", "what time is it", "remind me at 5 to call mom",
    "/research bears v lions", "",
])
def test_ordinary_questions_are_answered_without_a_look_up(question):
    assert not needs_live_lookup(question)


def test_the_question_becomes_a_search():
    # Question words pulled in a Ford v Ferrari car launch.
    assert search_query("who is projected to win the bears v lions game") == \
        "bears vs lions game prediction odds"
    assert search_query("who won the lakers game last night") == "lakers game last night score"
    assert key_terms("lakers game last night score") == ["lakers"]


def test_the_sentences_that_answer_it_are_kept_in_page_order():
    page = ("Welcome to our site. Subscribe now for more. "
            "section: | slug: bears-vs-lions | sport: betting | route: x |\n"
            "The Bears are a 2.5-point favorite over the Lions at BetMGM. "
            "The weather in Detroit will be cold. "
            "Our model predicts the Lions win 27-24 over the Bears.")
    kept = excerpts(page, "bears vs lions game prediction odds")
    assert kept == ["The Bears are a 2.5-point favorite over the Lions at BetMGM.",
                    "Our model predicts the Lions win 27-24 over the Bears."]


def test_vs_does_not_end_a_sentence():
    # Split at "vs." and the model read "Lions odds, Chicago is favored by 1.5"
    # as the Lions being favoured.
    page = "Bears vs. Lions odds, Chicago is favored by 1.5 points on the road. Nothing else."
    assert excerpts(page, "bears vs lions game prediction odds") == [
        "Bears vs. Lions odds, Chicago is favored by 1.5 points on the road."]


def test_sentences_about_something_else_are_dropped():
    page = "The Spurs lost to the Blazers last night 110-104. The Lakers beat the Jazz 118-109."
    assert excerpts(page, "lakers game last night score") == ["The Lakers beat the Jazz 118-109."]


class FakeWeb:
    """Two news stories, a video, a page that won't load, and one ordinary result."""

    def __init__(self):
        self.read = []
        self.queries = []

    async def news(self, query, max_results=8):
        self.queries.append(("news", query))
        return [
            {"title": "Bears vs Lions picks", "url": "https://old.example/picks", "source": "Old",
             "date": "2026-11-20T10:00:00+00:00", "snippet": "Bears Lions"},
            {"title": "Bears at Lions prediction", "url": "https://new.example/pred", "source": "New",
             "date": "2026-11-26T10:00:00+00:00", "snippet": "Bears Lions odds"},
            {"title": "Watch Bears vs Lions", "url": "https://www.youtube.com/watch?v=1",
             "source": "YouTube", "date": "2026-11-26T11:00:00+00:00", "snippet": "video"},
        ]

    async def search(self, query, max_results=6):
        self.queries.append(("web", query))
        return [{"title": "Bears Lions odds", "url": "https://broken.example/odds",
                 "snippet": "Bears Lions"}]

    async def fetch(self, url, max_chars=8000):
        self.read.append(url)
        if "broken" in url:
            return {"ok": False, "error": "403 Forbidden"}
        if "new.example" in url:
            return {"ok": True, "title": "Bears at Lions prediction",
                    "text": "The Lions are 3-point favourites over the Bears. Kickoff is at 12:30."}
        return {"ok": True, "title": "Bears vs Lions picks",
                "text": "Two of our three experts pick the Bears to beat the Lions."}


@pytest.mark.asyncio
async def test_a_look_up_reads_the_newest_articles_and_skips_videos():
    web = FakeWeb()
    found = await look_up(web, "who is projected to win the bears v lions game")

    assert ("news", "bears vs lions game prediction odds") in web.queries
    assert not any("youtube" in url for url in web.read), "nothing to read in a video"
    assert [page["source"] for page in found["pages"]] == ["New", "Old"], "newest first; the 403 is skipped"
    assert found["pages"][0]["excerpts"] == ["The Lions are 3-point favourites over the Bears."]

    context = as_context(found)
    assert "From New, 2026-11-26" in context
    assert "never move a number from one source to another" in context
    assert "check other websites" in context        # ...told not to


def test_a_short_follow_up_is_looked_up_with_the_question_before_it():
    asked = "who is projected to win the bears v lions game"
    assert follow_up_question("and what's the over/under?", asked) == \
        "who is projected to win the bears v lions game and what's the over/under?"
    assert follow_up_question("what about the spread", asked)
    assert follow_up_question("thanks, that's all", asked) is None
    assert follow_up_question("and the over/under?", "") is None, "only after a look-up"


@pytest.mark.parametrize("reply", [
    "I don't have real-time data access, so I can't provide a projection. Let me search the web for you.",
    "I cannot access the internet.",
    "For the most accurate prediction, you might want to check sports news websites.",
    "You can check the latest sportsbook odds for the exact over/under.",
    "As of my last update, the Lions were 8-2.",
])
def test_a_reply_that_puts_off_the_look_up_is_caught(reply):
    assert dodged_lookup(reply)


@pytest.mark.parametrize("reply", [
    "CBS Sports has the Bears as 1.5-point favourites.",
    "A point spread is a handicap used in sports betting.",
    "The president of France is Emmanuel Macron.",
])
def test_a_real_answer_is_left_alone(reply):
    assert not dodged_lookup(reply)


# ---------------------------------------------------------------------------
def _orchestrator(saved):
    from backend.ai.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)

    async def save(conversation_id, role, text, blocks, *rest):
        saved.append({"role": role, "content": blocks})

    async def history(conversation_id, limit=None):
        return saved[-(limit or len(saved)):]

    orchestrator._save_message = save
    orchestrator.history = history
    return orchestrator


@pytest.mark.asyncio
async def test_the_look_up_stays_in_the_conversation_for_the_next_question():
    saved = []
    orchestrator = _orchestrator(saved)
    messages = []
    await orchestrator._record_lookup("c1", "who wins bears v lions", "From CBS: Bears by 1.5.", messages)

    assert [m["role"] for m in messages] == ["assistant", "user"]
    call, result = messages[0]["content"][0], messages[1]["content"][0]
    assert call["type"] == "tool_use" and call["name"] == "live_lookup"
    assert result["tool_use_id"] == call["id"] and "Bears by 1.5" in result["content"]
    assert len(saved) == 2, "saved too, so the follow-up can see it"
    assert await orchestrator._last_lookup("c1") == "who wins bears v lions"


@pytest.mark.asyncio
async def test_no_earlier_look_up_means_no_follow_up():
    orchestrator = _orchestrator([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert await orchestrator._last_lookup("c1") == ""
