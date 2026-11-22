"""Learning facts about the owner from what they say.

The risk in a memory that learns on its own is learning the wrong things:
secrets, things the assistant said, one-off requests, the same fact twelve
times. Those are what is tested.
"""
from __future__ import annotations

import json

import pytest

from backend.ai.learning import FactLearner, overlap, parse_facts, worth_listening


@pytest.mark.parametrize("text", [
    "I'm vegetarian, so keep that in mind for recipes",
    "My sister Emma is visiting next month",
    "We're building a habit tracker app for my team",
    "Remember that I prefer short answers",
    "I work at a design studio in Leeds",
])
def test_personal_messages_are_listened_to(text):
    assert worth_listening(text)


@pytest.mark.parametrize("text", [
    "what's the weather",
    "open spotify",
    "How tall is Mount Everest?",
    "ok",
    "Send the report to the team by five",
])
def test_ordinary_requests_are_not(text):
    assert not worth_listening(text)


def test_facts_come_out_of_a_wrapped_reply():
    reply = 'Sure:\n[{"fact": "The user is vegetarian", "category": "preference"},' \
            ' {"fact": "The user has a sister called Emma", "category": "person"}]'
    facts = parse_facts(reply)
    assert [f["fact"] for f in facts] == ["The user is vegetarian",
                                         "The user has a sister called Emma"]
    assert [f["category"] for f in facts] == ["preference", "person"]


def test_an_unknown_category_becomes_general_and_strings_are_accepted():
    facts = parse_facts('["The user lives in Leeds", {"fact": "Likes jazz", "category": "vibes"}]')
    assert facts[0] == {"fact": "The user lives in Leeds", "category": "general"}
    assert facts[1]["category"] == "general"


@pytest.mark.parametrize("fact", [
    "The user's password is hunter22",
    "The user's bank account ends 4421",
    "The user's API key is [REDACTED api_key]",
    "The user's PIN is 0000",
])
def test_secrets_are_never_facts(fact):
    assert parse_facts(json.dumps([fact])) == []


def test_garbage_means_no_facts():
    for reply in ("", "I couldn't find anything", "[not json", '{"fact": "x"}'):
        assert parse_facts(reply) == []


def test_the_same_fact_reworded_is_the_same_fact():
    assert overlap("The user is vegetarian", "User is a vegetarian") >= 0.6
    assert overlap("The user is vegetarian", "The user lives in Leeds") < 0.6


# ---------------------------------------------------------------------------
class _Memory:
    def __init__(self, existing=()):
        self.rows = [{"value": value} for value in existing]
        self.stored = []
        self.importance = {}

    async def enabled(self):
        return True

    async def list(self, limit=500):
        return self.rows

    async def remember(self, value, category="general", importance=3, source="jarvis", **_):
        row = {"value": value, "category": category, "source": source}
        self.importance[value] = importance
        self.stored.append(row)
        self.rows.append(row)
        return row


class _Model:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def ask(self, prompt, system="", purpose="fast", max_tokens=None, temperature=None):
        self.prompts.append({"prompt": prompt, "system": system, "temperature": temperature})
        return self.reply


@pytest.mark.asyncio
async def test_new_facts_are_kept_as_learned_and_duplicates_skipped():
    memory = _Memory(existing=["The user is vegetarian"])
    model = _Model('[{"fact": "User is a vegetarian", "category": "preference"},'
                   ' {"fact": "The user is training for a half marathon", "category": "routine"}]')
    kept = await FactLearner(memory, model).learn(
        "I'm vegetarian and I'm training for a half marathon in May")

    assert [row["value"] for row in kept] == ["The user is training for a half marathon"]
    assert kept[0]["source"] == "learned" and kept[0]["category"] == "routine"
    assert model.prompts[0]["temperature"] == 0.0
    assert "Only facts the USER states" in model.prompts[0]["system"]


@pytest.mark.parametrize("text", [
    # A question mentions the owner's boiler and states nothing about them;
    # learning from it means learning from the assistant's answer.
    "when is my boiler next due a service, and who did the last one?",
    "search my documents: when and where is the party?",
    # A skill being run, and pasted material, are about other things.
    "/meeting-notes Sam said the launch moves to Friday. Priya tests by Wednesday.",
    "My notes from today: " + "the vendor said the price is fixed. " * 30,
])
def test_questions_skill_runs_and_pasted_material_teach_nothing(text):
    assert not worth_listening(text)


def test_only_statements_go_to_the_extractor():
    from backend.ai.learning import statements

    said = statements("Quick thing: I'm vegetarian. Any snack ideas? What about protein?")
    assert said == "Quick thing: I'm vegetarian."


@pytest.mark.parametrize("fact, said, kept", [
    ("The user is vegetarian", "I'm vegetarian", True),
    ("The user has a sister called Emma", "My sister Emma is visiting", True),
    ("The party is on 12 December", "The party is on 12 December", False),
    ("Priya will finish testing by Wednesday", "Priya will finish testing", False),
    ("The user is a pilot", "I'm vegetarian", False),
])
def test_a_fact_must_be_about_the_owner_and_in_their_words(fact, said, kept):
    from backend.ai.learning import about_the_owner

    assert about_the_owner(fact, said) is kept


@pytest.mark.asyncio
async def test_facts_about_the_owner_are_core_and_project_details_are_not():
    memory = _Memory()
    model = _Model('[{"fact": "The user is vegetarian", "category": "preference"},'
                   ' {"fact": "The user is building a habit tracker", "category": "project"}]')
    kept = await FactLearner(memory, model).learn(
        "I'm vegetarian and I'm building a habit tracker for my team")
    by_fact = {row["value"]: row for row in memory.stored}
    assert len(kept) == 2
    assert memory.importance["The user is vegetarian"] == 4, "always in the prompt"
    assert memory.importance["The user is building a habit tracker"] == 3


@pytest.mark.asyncio
async def test_the_assistants_reply_is_never_sent_to_the_extractor():
    memory = _Memory()
    model = _Model("[]")
    await FactLearner(memory, model).learn(
        "I'm vegetarian, remember that", reply_text="Your party is on 12 December")
    assert "12 December" not in model.prompts[0]["prompt"]


@pytest.mark.asyncio
async def test_a_message_carrying_a_secret_teaches_nothing():
    memory = _Memory()
    model = _Model('[{"fact": "The user uses OpenAI", "category": "fact"}]')
    kept = await FactLearner(memory, model).learn(
        "my key is sk-proj-" + "a" * 30 + " remember it for me")
    assert kept == [] and model.prompts == [], "not even sent to the model"


@pytest.mark.asyncio
async def test_switched_off_means_nothing_is_learned():
    class _Settings:
        async def get(self, key, default=None):
            return {"learn_automatically": False} if key == "ai" else {}

    memory = _Memory()
    model = _Model('[{"fact": "The user is vegetarian", "category": "preference"}]')
    kept = await FactLearner(memory, model, _Settings()).learn("I'm vegetarian, remember that")
    assert kept == [] and model.prompts == []


@pytest.mark.asyncio
async def test_a_failing_model_is_no_facts_not_an_error():
    class _Broken:
        async def ask(self, *args, **kwargs):
            raise RuntimeError("ollama went away")

    assert await FactLearner(_Memory(), _Broken()).learn("I'm vegetarian, remember that") == []
