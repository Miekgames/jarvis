"""Countdowns: the right date or an honest question, three at most, real news.

The failure that matters is a confident countdown to the wrong day. Most of
these are ways that happens: a model's remembered date, the day an article was
written, a closing day, a guessed year, one site on its own.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backend.ai.tools import countdown_tools
from backend.ai.tools.registry import ToolContext, ToolRegistry
from backend.services import event_dates as ed
from backend.services.countdowns import LIMIT, CountdownError, Countdowns, select_news

TODAY = date(2026, 11, 22)


def result(url, snippet, title="Star Quest"):
    return {"url": f"https://{url}/x", "title": title, "snippet": snippet}


# ===========================================================================
# reading dates
# ===========================================================================
@pytest.mark.parametrize("text, expected", [
    ("out on December 18, 2026", date(2026, 12, 18)),
    ("out on 18 December 2026", date(2026, 12, 18)),
    ("on the 18th of December, 2026", date(2026, 12, 18)),
    ("Feb. 14, 2027", date(2027, 2, 14)),
    ("2027-06-05", date(2027, 6, 5)),
    ("arrives December 18th", date(2026, 12, 18)),
    ("every January 3", date(2027, 1, 3)),
])
def test_dates_are_read(text, expected):
    assert ed.find_dates(text, TODAY)[0].day == expected


@pytest.mark.parametrize("text", ["coming May 2027", "in 2027", "version 12.18.2", "at 18:30 on Sunday",
                                  "31 February 2027", "may 100 people"])
def test_what_is_not_a_date_is_not_read(text):
    assert ed.find_dates(text, TODAY) == []


@pytest.mark.parametrize("text", [
    "held July 14-30, 2028", "held July 14–30, 2028", "from 14th to 30th July 2028",
    "from Friday, July 14, to Sunday, July 30, 2028", "14 - 30 July 2028",
])
def test_something_that_lasts_starts_on_its_first_day(text):
    starts = [m.day for m in ed.find_dates(text, TODAY) if not m.ends_a_range]
    assert starts == [date(2028, 7, 14)]


@pytest.mark.parametrize("message, expected", [
    ("countdown to my trip on June 5", date(2027, 6, 5)),
    ("countdown to my trip on 5/6/2027", date(2027, 6, 5)),
    ("count down to the concert tomorrow", TODAY + timedelta(days=1)),
    ("a countdown for my exam in 3 weeks", TODAY + timedelta(weeks=3)),
    ("a countdown for my move in two months", date(2027, 1, 22)),
    ("countdown to the party on friday", date(2026, 11, 27)),
    ("countdown to the super bowl", None),
    ("countdown to GTA 6", None),
])
def test_the_owners_own_words(message, expected):
    assert ed.date_in_words(message, TODAY, day_first=True) == expected


@pytest.mark.parametrize("message, expected", [
    ("at 7pm", "19:00"), ("at 7:30 pm", "19:30"), ("at 12 am", "00:00"), ("starts 19:45", "19:45"),
    ("at noon", "12:00"), ("countdown to GTA 6", ""), ("on June 5", ""),
])
def test_times_of_day(message, expected):
    assert ed.time_in_words(message) == expected


def test_days_the_calendar_knows():
    assert ed.fixed_day("Christmas", TODAY) == date(2026, 12, 25)
    assert ed.fixed_day("New Year's Day", TODAY) == date(2027, 1, 1)
    assert ed.fixed_day("Thanksgiving", TODAY) == date(2026, 11, 26)
    assert ed.fixed_day("Easter 2027", TODAY) == date(2027, 3, 28)
    assert ed.fixed_day("Black Friday", TODAY) == date(2026, 11, 27)
    assert ed.fixed_day("Halloween", TODAY) == date(2027, 10, 31)
    assert ed.fixed_day("Super Bowl", TODAY) is None


@pytest.mark.parametrize("said, name", [
    ("countdown to avengers doomsday", "Avengers Doomsday"), ("Avengers: Doomsday Release", "Avengers: Doomsday"),
    ("GTA 7 release date", "GTA 7"), ("the lord of the rings", "The Lord of the Rings"),
    ("Press Release", "Press"), ("A countdown for the Super Bowl", "the Super Bowl"),
])
def test_names_are_tidied(said, name):
    assert ed.tidy(said) == name


@pytest.mark.parametrize("event, personal", [
    ("my birthday", True), ("Sam's wedding", True), ("our holiday", True), ("Graduation", True),
    ("Super Bowl 2027", False), ("The Game Awards", False), ("Schindler's List", False),
])
def test_only_the_owner_knows_some_dates(event, personal):
    assert ed.is_personal(event) is personal


# ===========================================================================
# weighing the evidence
# ===========================================================================
def test_sites_that_agree_settle_it():
    found = ed.weigh("Star Quest", [
        result("a.com", "Star Quest arrives in theaters on March 5, 2027."),
        result("b.com", "The release date for Star Quest is 5 March 2027."),
        result("c.com", "Star Quest opens March 5."),
    ], TODAY)
    assert (found.status, found.day, found.sources) == ("sure", date(2027, 3, 5), 2)


def test_one_site_repeating_itself_is_one_site():
    found = ed.weigh("Star Quest", [
        result("a.com", "Star Quest release date: March 5, 2027."),
        result("a.com", "Star Quest is released March 5, 2027."),
        result("www.a.com", "Star Quest is out March 5, 2027."),
    ], TODAY)
    assert found.status == "unsure" and found.sources == 1


def test_when_the_article_was_written_is_not_when_it_happens():
    found = ed.weigh("Star Quest", [
        result("a.com", "Nov 30, 2026 · Star Quest news and cast."),
        result("b.com", "Updated November 30, 2026 Star Quest rumours."),
        result("c.com", "2 days ago · Star Quest set photos"),
    ], TODAY)
    assert found.status == "none"


def test_a_guessed_year_is_never_agreement():
    """Two pages saying "July 31" about a film that came out last July."""
    found = ed.weigh("Star Quest", [
        result("a.com", "Star Quest release date: July 31."),
        result("b.com", "Star Quest arrives July 31."),
        result("c.com", "Star Quest opens on July 31!"),
    ], TODAY)
    assert found.status != "sure"


def test_a_date_without_a_year_takes_it_from_when_it_was_written():
    found = ed.weigh("Star Quest", [
        result("a.com", "Jul 20, 2026 · Star Quest arrives in theaters July 31."),
        result("b.com", "Star Quest was released on July 31, 2026."),
    ], TODAY)
    assert (found.status, found.day) == ("past", date(2026, 7, 31))


def test_presales_trailers_and_the_old_date_do_not_win():
    found = ed.weigh("Star Quest", [
        result("a.com", "Star Quest tickets go on sale December 1, 2026. It is released March 5, 2027."),
        result("b.com", "Star Quest trailer lands December 1, 2026. In theaters March 5, 2027."),
        result("c.com", "Star Quest, originally May 1, 2027, is now scheduled for March 5, 2027."),
    ], TODAY)
    assert (found.status, found.day) == ("sure", date(2027, 3, 5))


def test_sites_that_disagree_are_not_certain():
    found = ed.weigh("Star Quest", [
        result("a.com", "Star Quest release date: March 5, 2027."),
        result("b.com", "Star Quest is released April 9, 2027."),
        result("c.com", "Star Quest arrives on March 5, 2027."),
        result("d.com", "Star Quest opens on April 9, 2027."),
    ], TODAY)
    assert found.status == "unsure" and found.others


def test_results_about_something_else_have_no_say():
    results = [result("a.com", "Grand Auto 5 released March 5, 2027.", "Grand Auto 5"),
               result("b.com", "Grand Auto 5 arrives March 5, 2027.", "Grand Auto V")]
    assert ed.weigh("Grand Auto 6", results, TODAY).status == "none"


def test_the_year_in_the_name_is_the_year_meant():
    found = ed.weigh("Big Match 2028", [
        result("a.com", "Big Match 2027 takes place February 14, 2027. Big Match 2028 is on February 13, 2028.", "Big Match"),
        result("b.com", "The Big Match will be played February 13, 2028.", "Big Match 2028"),
    ], TODAY)
    assert (found.status, found.day) == ("sure", date(2028, 2, 13))


def test_the_next_one_is_not_the_one_that_has_been():
    results = [result("a.com", "Comet Night was held March 5, 2026.", "Comet Night"),
               result("b.com", "Comet Night took place March 5, 2026.", "Comet Night")]
    assert ed.weigh("Comet Night", results, TODAY).status == "past"
    assert ed.weigh("next Comet Night", results, TODAY).status == "none"


# ===========================================================================
# how long is left
# ===========================================================================
def test_time_left_counts_calendar_months():
    now = datetime(2026, 11, 22, 18, 30, 15)
    assert ed.left(now, datetime(2026, 12, 18)) == {
        "months": 0, "days": 25, "hours": 5, "minutes": 29, "seconds": 45}
    assert ed.left(now, datetime(2028, 5, 22, 18, 30, 15))["months"] == 18
    assert ed.left(datetime(2027, 1, 31), datetime(2027, 3, 1)) == {
        "months": 1, "days": 1, "hours": 0, "minutes": 0, "seconds": 0}
    assert ed.in_words(now, datetime(2027, 1, 23, 20, 30, 15)) == "2 months, 1 day and 2 hours"
    assert ed.in_words(now, now) == "it has already arrived"


# ===========================================================================
# the store
# ===========================================================================
class FakeWeb:
    def __init__(self, results=None, news=None, fails=False):
        self.results, self.articles, self.fails = results or [], news or [], fails
        self.searches, self.news_calls = [], 0

    async def search(self, query, n=6):
        self.searches.append(query)
        if self.fails:
            raise RuntimeError("no network")
        return self.results

    async def news(self, query, n=8):
        self.news_calls += 1
        if self.fails:
            raise RuntimeError("no network")
        return self.articles


def ahead(days):
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.mark.asyncio
async def test_three_at_a_time_and_the_fourth_says_why(db):
    service = Countdowns(db, FakeWeb())
    for n in range(LIMIT):
        await service.add(f"Event {'ABC'[n]}", ahead(10 + n))
    with pytest.raises(CountdownError) as exc:
        await service.add("Event D", ahead(50))
    assert "Event A" in str(exc.value) and "removed first" in str(exc.value)
    assert len(await service.list()) == 3
    await service.remove((await service.list())[0]["id"])
    await service.add("Event D", ahead(50))
    assert [i["title"] for i in await service.list()] == ["Event B", "Event C", "Event D"]


@pytest.mark.asyncio
async def test_the_limit_holds_when_four_arrive_at_once(db):
    import asyncio

    service = Countdowns(db, FakeWeb())
    done = await asyncio.gather(*(service.add(f"Event {c}", ahead(5)) for c in "ABCD"),
                                return_exceptions=True)
    assert sum(isinstance(r, CountdownError) for r in done) == 1
    assert len(await service.list()) == 3


@pytest.mark.asyncio
async def test_the_same_event_again_moves_it_rather_than_adding_another(db):
    service = Countdowns(db, FakeWeb())
    first = await service.add("the super bowl", ahead(10))
    again = await service.add("Super Bowl", ahead(20))
    assert again["id"] == first["id"] and again["updated"] and again["day"] == ahead(20)
    assert len(await service.list()) == 1


@pytest.mark.asyncio
async def test_date_and_time_can_be_set_by_hand(db):
    service = Countdowns(db, FakeWeb())
    item = await service.add("Concert", ahead(10))
    item = await service.update(item["id"], day=ahead(12), clock="19:30")
    assert (item["day"], item["clock"], item["origin"]) == (ahead(12), "19:30", "owner")
    item = await service.update(item["id"], clock="")
    assert item["clock"] == ""
    for bad in ({"clock": "25:00"}, {"clock": "7pm"}, {"day": "soon"}, {"day": "2027-02-31"},
                {"day": ahead(-3)}, {"title": "  "}):
        with pytest.raises(CountdownError):
            await service.update(item["id"], **bad)
    assert (await service.get(item["id"]))["day"] == ahead(12)


@pytest.mark.asyncio
async def test_changing_a_found_date_by_hand_drops_its_source(db):
    service = Countdowns(db, FakeWeb())
    item = await service.add("Film", ahead(10), origin="web",
                             found=ed.Found("sure", sources=4, source_name="a.com", source_url="https://a.com"))
    kept = await service.update(item["id"], clock="10:00")
    assert kept["sources"] == 4 and kept["origin"] == "web", "a time of day does not change the date"
    moved = await service.update(item["id"], day=ahead(11))
    assert (moved["origin"], moved["sources"], moved["source_name"]) == ("owner", 0, "")


@pytest.mark.asyncio
async def test_a_past_day_cannot_be_added(db):
    with pytest.raises(CountdownError, match="already passed"):
        await Countdowns(db, FakeWeb()).add("Film", ahead(-1))


@pytest.mark.asyncio
async def test_checking_again_moves_it_only_when_sites_agree(db):
    moved = date.today() + timedelta(days=90)
    say = moved.strftime("%B %d, %Y")
    web = FakeWeb([result("a.com", f"Film X release date: {say}", "Film X"),
                   result("b.com", f"Film X arrives {say}", "Film X")])
    service = Countdowns(db, web)
    item = await service.add("Film X", ahead(10))
    done = await service.recheck(item["id"])
    assert done["changed"] and done["item"]["day"] == moved.isoformat() and done["item"]["sources"] == 2
    web.results = [result("a.com", "Film X release date: January 1, 2031", "Film X")]
    done = await service.recheck(item["id"])
    assert not done["changed"] and done["item"]["day"] == moved.isoformat()


# ===========================================================================
# news
# ===========================================================================
NEWS = [
    {"title": "Star Quest cast revealed", "url": "https://a.com/1", "source": "A", "date": "2026-11-20T10:00:00+00:00", "snippet": ""},
    {"title": "Star Quest: the cast revealed", "url": "https://b.com/1", "source": "B", "date": "2026-11-19T10:00:00+00:00", "snippet": ""},
    {"title": "Stock markets fall", "url": "https://c.com/1", "source": "C", "date": "2026-11-22T10:00:00+00:00", "snippet": "a quest for yield"},
    {"title": "New Star Quest trailer", "url": "https://d.com/1", "source": "D", "date": "2026-11-21T10:00:00+00:00", "snippet": ""},
    {"title": "Star Quest scam", "url": "javascript:alert(1)", "source": "E", "date": "2026-11-22T10:00:00+00:00", "snippet": ""},
]


def test_news_is_about_the_topic_newest_first_once_each_and_safe_to_click():
    assert [a["url"] for a in select_news("Star Quest", NEWS)] == ["https://d.com/1", "https://a.com/1"]


@pytest.mark.asyncio
async def test_news_is_kept_briefly_and_refreshed_on_request(db):
    web = FakeWeb(news=NEWS)
    service = Countdowns(db, web)
    item = await service.add("Star Quest", ahead(10))
    first = await service.news(item["id"])
    await service.news(item["id"])
    assert web.news_calls == 1 and len(first["articles"]) == 2
    await service.news(item["id"], refresh=True)
    assert web.news_calls == 2
    web.fails = True
    stale = await service.news(item["id"], refresh=True)
    assert stale["error"] and stale["articles"] == first["articles"], "says so, and keeps what it had"


@pytest.mark.asyncio
async def test_there_is_no_news_about_your_own_birthday(db):
    web = FakeWeb(news=NEWS)
    service = Countdowns(db, web)
    item = await service.add("my birthday", ahead(10))
    assert item["topic"] == "" and (await service.news(item["id"]))["articles"] == []
    assert web.news_calls == 0


# ===========================================================================
# by asking
# ===========================================================================
async def _chat(db, *texts):
    """A conversation, oldest first: (role, text)."""
    now = datetime.now().isoformat()
    await db.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c1','t',?,?)", (now, now))
    for n, (role, text) in enumerate(texts):
        await db.execute(
            "INSERT INTO messages (id, conversation_id, role, text, blocks, created_at) VALUES (?,?,?,?,?,?)",
            (f"m{n}", "c1", role, text, "[]", f"2026-11-22T10:00:{n:02d}+00:00"))


def _tools(db, service):
    registry = ToolRegistry(ToolContext(db=db, countdowns=service))
    countdown_tools.register(registry)
    return registry


@pytest.mark.asyncio
async def test_a_date_the_model_made_up_is_ignored(db):
    """The owner gave no date; the model volunteered one. Sites say otherwise."""
    real = date.today() + timedelta(days=60)
    web = FakeWeb([result("a.com", f"Star Quest release date: {real.strftime('%B %d, %Y')}"),
                   result("b.com", f"Star Quest arrives {real.strftime('%B %d, %Y')}")])
    service = Countdowns(db, web)
    await _chat(db, ("user", "add a countdown to star quest"))
    done = await _tools(db, service).dispatch(
        "add_countdown", {"event": "star quest", "date": ahead(200)}, conversation_id="c1")
    assert done.ok and (await service.list())[0]["day"] == real.isoformat()
    assert (await service.list())[0]["title"] == "Star Quest"
    assert "2 websites agree" in done.message


@pytest.mark.asyncio
async def test_the_owners_date_and_time_win_without_looking_anything_up(db):
    web = FakeWeb()
    service = Countdowns(db, web)
    await _chat(db, ("user", f"countdown to the school play on {ahead(30)} at 7:30pm"))
    done = await _tools(db, service).dispatch("add_countdown", {"event": "the school play"}, conversation_id="c1")
    assert done.ok and web.searches == []
    item = (await service.list())[0]
    assert (item["day"], item["clock"], item["origin"]) == (ahead(30), "19:30", "owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("results, expect", [
    ([], "couldn't find a date"),
    ([result("a.com", "Star Quest release date: March 5, 2031")], "ask which date"),
    ([result("a.com", "Star Quest was released March 5, 2024"),
      result("b.com", "Star Quest release date March 5, 2024")], "already happened"),
])
async def test_when_it_cannot_be_settled_nothing_is_added(db, results, expect):
    service = Countdowns(db, FakeWeb(results))
    await _chat(db, ("user", "countdown to star quest"))
    done = await _tools(db, service).dispatch(
        "add_countdown", {"event": "Star Quest", "date": ahead(100)}, conversation_id="c1")
    assert not done.ok and expect in done.message and await service.list() == []


@pytest.mark.asyncio
async def test_choosing_one_of_the_dates_offered(db):
    service = Countdowns(db, FakeWeb())
    await _chat(db, ("user", "countdown to star quest"),
                ("assistant", "I couldn't confirm it. The best lead is 5 March 2031; others say 9 April 2031."),
                ("user", "use the first one"))
    tools = _tools(db, service)
    done = await tools.dispatch("add_countdown", {"event": "Star Quest", "date": "2031-03-05"}, conversation_id="c1")
    assert done.ok and (await service.list())[0]["day"] == "2031-03-05"


@pytest.mark.asyncio
async def test_a_personal_event_is_asked_about_not_searched_for(db):
    web = FakeWeb()
    service = Countdowns(db, web)
    await _chat(db, ("user", "countdown to my birthday"))
    done = await _tools(db, service).dispatch(
        "add_countdown", {"event": "my birthday", "date": ahead(40)}, conversation_id="c1")
    assert not done.ok and "Ask them for the date" in done.message and web.searches == []


@pytest.mark.asyncio
async def test_no_network_is_said_and_adds_nothing(db):
    service = Countdowns(db, FakeWeb(fails=True))
    await _chat(db, ("user", "countdown to star quest"))
    done = await _tools(db, service).dispatch("add_countdown", {"event": "Star Quest"}, conversation_id="c1")
    assert not done.ok and "couldn't reach the web" in done.message and await service.list() == []


@pytest.mark.asyncio
async def test_asking_how_long_adds_nothing(db):
    service = Countdowns(db, FakeWeb())
    await _chat(db, ("user", "how long until christmas?"))
    done = await _tools(db, service).dispatch("how_long_until", {"event": "christmas"}, conversation_id="c1")
    assert done.ok and "25 December" in done.message and await service.list() == []


@pytest.mark.asyncio
async def test_removing_by_name_number_or_not_at_all(db):
    service = Countdowns(db, FakeWeb())
    tools = _tools(db, service)
    await service.add("Star Quest", ahead(5))
    await service.add("Moon Match", ahead(6))
    await service.add("Moon Festival", ahead(7))
    assert not (await tools.dispatch("remove_countdown", {"event": "moon"})).ok, "two match: ask"
    assert (await tools.dispatch("remove_countdown", {"event": "the match"})).ok
    assert (await tools.dispatch("remove_countdown", {"event": "1"})).ok
    assert [i["title"] for i in await service.list()] == ["Moon Festival"]


# ===========================================================================
# reaching the tools at all, with a small model
# ===========================================================================
def _crowded():
    """The countdown tools among many others, as a local model meets them."""
    from backend.ai.tools.registry import Tool, ok

    registry = ToolRegistry(ToolContext())
    countdown_tools.register(registry)
    for n in range(60):
        registry.register(Tool(f"other_{'abcdefghij'[n % 10]}{'klmnop'[n // 10]}", "does another thing with days and events",
                               {"type": "object", "properties": {}}, lambda ctx: ok()))
    return registry


@pytest.mark.parametrize("query, tool", [
    ("add a countdown to the olympics", "add_countdown"),
    ("can you count down to gta 7", "add_countdown"),
    ("how many days until christmas", "how_long_until"),
    ("how much longer until the world cup", "how_long_until"),
    ("the olympics add_countdown remove_countdown list_countdowns", "add_countdown"),
])
def test_a_small_model_is_offered_the_right_tool(query, tool):
    assert tool in {s["name"] for s in _crowded().select_schemas(query, limit=8)}


def test_the_slash_skill_brings_its_tools_and_is_not_suggested(tmp_path):
    from types import SimpleNamespace

    from backend.ai.orchestrator import Orchestrator
    from backend.skills.library import SkillLibrary

    skills = SkillLibrary(tmp_path)
    fake = SimpleNamespace(registry=SimpleNamespace(context=SimpleNamespace(get=lambda name: skills)))
    assert "add_countdown" in Orchestrator._skill_tools(fake, "/countdown the olympics").split()
    assert Orchestrator._skill_tools(fake, "countdown to the olympics") == ""
    assert all(s.name != "countdown" for s, _ in skills.match("add a countdown to the olympics"))
