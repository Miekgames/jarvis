"""Learning about the owner from what they say, without being told to.

Until now JARVIS only remembered something when the model decided to call
its `remember` tool -- which a small local model mostly does not, so "I'm
vegetarian" said in passing was gone by the next conversation. This listens
to each exchange afterwards and keeps the durable facts: preferences,
projects, people, routines, who the owner is.

The rules it keeps, because a memory that learns on its own has to be one
the owner can trust:

  * **Only what the owner said.** Facts are drawn from the owner's own
    message. JARVIS's reply is shown for context, but nothing it said --
    and nothing a web page or tool said -- becomes a fact about the owner.
  * **No secrets.** Anything that looks like a key, token or password is
    refused outright, not stored with a redaction.
  * **No duplicates.** A fact much like one already kept is skipped.
  * **Visible and removable.** Everything learned this way is stored with
    source "learned", shows as such in the Memory panel, and can be deleted
    like any other memory. It can be switched off in Settings.
  * **Cheap.** It only runs when the message sounds personal ("I", "my",
    "we", "remember") and is long enough to say something, and it runs
    after the reply has been sent, so it never delays an answer.

Ported from OpenJarvis ``memory/extractor.py`` (Stanford Hazy Research,
Apache-2.0): the prompt and the defensive "any failure means no facts"
design. The gating, the owner-only rule and the de-duplication are JARVIS's.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .memory import CATEGORIES, _tokens

logger = logging.getLogger("jarvis.learning")

MAX_FACTS = 5
MAX_FACT_CHARS = 200
MIN_MESSAGE_CHARS = 14
# Facts sharing this much of their wording with one already kept are the
# same fact again.
DUPLICATE_OVERLAP = 0.6

_PERSONAL = re.compile(
    r"\b(?:i|i'm|im|i've|i'd|i'll|me|my|mine|myself|we|we're|our|us|remember|"
    r"call me|prefer|favourite|favorite|always|never|usually|allergic|"
    r"birthday|wife|husband|partner|girlfriend|boyfriend|son|daughter|kids?|"
    r"mum|mom|dad|brother|sister|boss|job|work at|working on|live in)\b",
    re.IGNORECASE)

_SECRETISH = re.compile(
    r"(?i)(?:password|passcode|pin\b|api key|secret|token|ssn|social security|"
    r"card number|cvv|bank account|routing|\[REDACTED)")

_SYSTEM = (
    "You extract durable, long-term facts about the user from one exchange "
    "between the user and their assistant. A good fact is stable over time "
    "and useful in future conversations: preferences, identity, goals, "
    "ongoing projects, people in their life, routines, constraints.\n\n"
    "Rules:\n"
    "- Only facts the USER states about themselves or their life, in the "
    "USER message. Facts about other people's tasks, meetings, documents or "
    "events are not facts about the user.\n"
    "- Ignore one-off requests, questions, small talk, and temporary states "
    "(\"I'm tired\", \"I'm at the shops\").\n"
    "- Never include passwords, keys, codes, account or card numbers.\n"
    "- Write each fact in the third person about the user, under 200 "
    "characters (\"The user is vegetarian\").\n\n"
    "Reply with ONLY a JSON array of objects like "
    "[{\"fact\": \"...\", \"category\": \"preference\"}], where category is "
    "one of: " + ", ".join(CATEGORIES) + ". If there is nothing worth "
    "remembering, reply with []."
)


# Longer than this, a message is pasted material -- notes, an email, a
# document -- whose facts are about other people and other things.
MAX_MESSAGE_CHARS = 600

_QUESTION_START = re.compile(
    r"(?i)^(?:when|what|what's|whats|who|whom|whose|where|why|how|which|can|could|"
    r"would|should|will|shall|is|are|am|was|were|do|does|did|have|has|had|may|might)\b")


def statements(user_text: str) -> str:
    """The sentences of a message that state something rather than ask.

    "When is my boiler due a service?" mentions the owner's boiler and
    states nothing; learning from it means learning from the answer, which
    is the assistant talking.
    """
    sentences = re.split(r"(?<=[.!?])\s+|\n+", (user_text or "").strip())
    kept = [s for s in sentences
            if s.strip() and not s.strip().endswith("?")
            and not _QUESTION_START.match(s.strip())]
    return " ".join(kept).strip()


def worth_listening(user_text: str) -> bool:
    """Whether a message could plausibly contain a fact about the owner."""
    text = (user_text or "").strip()
    if text.startswith("/") or len(text) > MAX_MESSAGE_CHARS:
        return False            # a skill being run, or pasted material
    said = statements(text)
    if len(said) < MIN_MESSAGE_CHARS:
        return False
    return bool(_PERSONAL.search(said))


# Facts that describe the owner belong in every prompt; project details only
# when they come up. `core()` in the memory store keeps importance >= 4.
_CORE_CATEGORIES = {"preference", "person", "routine", "fact"}


def about_the_owner(fact: str, user_text: str) -> bool:
    """A fact is kept only if it is about the owner and grounded in their words."""
    if not re.search(r"(?i)\b(?:the user|user's|user is|user has|user)\b", fact):
        return False
    said = set(_tokens(user_text))
    meaningful = [t for t in _tokens(fact) if t not in {"user", "user's", "users"}]
    return bool(meaningful) and any(t in said or t[:5] in {s[:5] for s in said}
                                    for t in meaningful)


def parse_facts(reply: str) -> List[Dict[str, str]]:
    """Facts out of a model reply. Never raises; bad output means none."""
    text = (reply or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    facts: List[Dict[str, str]] = []
    for item in items[:MAX_FACTS * 2]:
        if isinstance(item, str):
            fact, category = item, "general"
        elif isinstance(item, dict):
            fact = str(item.get("fact") or item.get("text") or "")
            category = str(item.get("category") or "general").lower()
        else:
            continue
        fact = " ".join(fact.split())[:MAX_FACT_CHARS]
        if len(fact) < 8 or _SECRETISH.search(fact):
            continue
        facts.append({"fact": fact,
                      "category": category if category in CATEGORIES else "general"})
    return facts[:MAX_FACTS]


def overlap(a: str, b: str) -> float:
    left, right = set(_tokens(a)), set(_tokens(b))
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


class FactLearner:
    def __init__(self, memory, orchestrator, settings_store=None) -> None:
        self.memory = memory
        self.orchestrator = orchestrator
        self.settings_store = settings_store

    async def enabled(self) -> bool:
        if self.memory is None or not await self.memory.enabled():
            return False
        if self.settings_store is None:
            return True
        ai = await self.settings_store.get("ai", {})
        return bool(ai.get("learn_automatically", True))

    async def learn(self, user_text: str, reply_text: str = "") -> List[Dict[str, Any]]:
        """Keep any durable facts from one exchange. Never raises."""
        try:
            if not worth_listening(user_text) or not await self.enabled():
                return []
            from ..core.screening import screener

            cleaned, counts = screener.strip_credentials(user_text)
            if counts:
                return []          # a message carrying a secret teaches nothing
            said = statements(cleaned)

            # Only the owner's statements are sent -- not their questions and
            # not JARVIS's reply, which is where facts about other things
            # (the contents of a document, a web page) would come from.
            reply = await self.orchestrator.ask(
                f"USER message:\n{said[:2000]}",
                system=_SYSTEM, purpose="fast", max_tokens=300, temperature=0.0)

            kept: List[Dict[str, Any]] = []
            known = [row["value"] for row in await self.memory.list(limit=500)]
            for item in parse_facts(reply):
                if not about_the_owner(item["fact"], said):
                    continue
                if any(overlap(item["fact"], existing) >= DUPLICATE_OVERLAP
                       for existing in known):
                    continue
                stored = await self.memory.remember(
                    item["fact"], category=item["category"],
                    importance=4 if item["category"] in _CORE_CATEGORIES else 3,
                    source="learned")
                known.append(item["fact"])
                kept.append(stored)
            if kept:
                logger.info("learned %d fact(s) about the owner", len(kept))
            return kept
        except Exception:
            logger.debug("learning from an exchange failed", exc_info=True)
            return []
