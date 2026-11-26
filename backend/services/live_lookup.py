"""Looking up today's facts before answering: who's favoured, the score, the news.

A small local model asked "who is projected to win the Bears v Lions game?"
either says it has no real-time data and stops, or searches, gets six snippets
(a YouTube link among them), and says "the web search didn't provide a direct
projection". The answer was on the first article it didn't read.

So for questions that plainly need today's facts, JARVIS looks first:
- the question becomes a search ("Bears vs Lions game prediction odds"),
  because question words pull in junk ("who is projected to win" found a Ford
  v Ferrari car launch);
- dated news comes first, newest first, then ordinary results; videos and
  streaming sites are dropped, since there is nothing in them to read;
- several articles are read at once and the sentences that answer the
  question are kept, because some sites refuse to be read or build the page
  with script and come back empty.

The model then answers from what was read, naming where it came from.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Questions that need today's facts. Kept narrow on purpose: a general
# question ("what is a spread in football?") is answered from knowledge.
_LIVE = re.compile(
    r"\b(?:projected|predicted|expected|favou?red|tipped)\s+to\s+win\b"
    r"|\bwho(?:'s|\s+is|\s+are)\s+(?:favou?red|winning|leading|ahead|playing)\b"
    r"|\bwho\s+(?:won|will\s+win|wins|is\s+going\s+to\s+win|do\s+you\s+think\s+will\s+win)\b"
    r"|\b(?:odds|spread|point\s+spread|over/under|prediction|predictions|picks?)\s+(?:for|on)\b"
    r"|\b(?:what(?:'s|\s+is|\s+was)\s+the\s+score|final\s+score|score\s+of)\b"
    r"|\b(?:latest|breaking)\s+(?:news|update|updates)\b"
    r"|\bnews\s+(?:on|about)\b"
    r"|\bwhat\s+happened\s+(?:today|yesterday|last\s+night|this\s+week)\b"
    r"|\b(?:stock|share)\s+price\b|\bprice\s+of\s+(?:bitcoin|ethereum|gold|oil)\b",
    re.I,
)
# Something happening between two sides: "Bears v Lions", "Lakers vs. Celtics".
_MATCHUP = re.compile(r"\b[a-z0-9'.]+\s+(?:v|vs|versus|against|at)\.?\s+(?:the\s+)?[a-z0-9'.]+", re.I)
_GAME = re.compile(r"\b(?:game|match|fight|race|final|series|bowl|cup|tonight|today)\b", re.I)
_WINNER = re.compile(r"\b(?:win|winner|beat|favou?rite|favou?red|odds|predict\w*|projected|score)\b", re.I)

_ASKING = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:can|could|would)\s+you\s+(?:tell\s+me|find\s+out|look\s+up|check)\s+|"
    r"^(?:hey\s+)?(?:jarvis[,\s]+)?(?:tell\s+me|find\s+out|look\s+up|check)\s+", re.I)
_QUESTION_WORDS = re.compile(
    r"\b(?:who|what|which|when|where|how|what's|who's|how's|is|are|was|were|will|would|do|does|did|the|a|an|"
    r"going|to|be|it|of|in|on|for|please|jarvis|think|you|me|tell|currently|right|now)\b", re.I)
_WIN_PHRASE = re.compile(
    r"\b(?:projected|predicted|expected|favou?red|tipped|going)\s+to\s+win\b|\bwill\s+win\b|"
    r"\bwho(?:'s|\s+is)\s+favou?red\b|\bwho\s+wins\b", re.I)

# Nothing to read in these: video, streams, social posts.
_SKIP_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com",
               "x.com", "twitter.com", "reddit.com", "twitch.tv", "streameast", "vimeo.com")
_SKIP_WORDS = re.compile(r"\b(?:live\s*stream|watch\s+live|how\s+to\s+watch|tv\s+channel)\b", re.I)

PAGES_TO_TRY = 5
PAGES_TO_KEEP = 3
PAGE_TIMEOUT_S = 12.0
SENTENCES_PER_PAGE = 5


def needs_live_lookup(message: str) -> bool:
    """True when the question can only be answered with today's facts."""
    text = (message or "").strip()
    if not text or len(text) > 300 or text.startswith("/"):
        return False
    if _LIVE.search(text):
        return True
    # "bears v lions game, who wins?" -- a matchup plus a question about it.
    return bool(_MATCHUP.search(text) and _GAME.search(text) and _WINNER.search(text))


def search_query(message: str) -> str:
    """Turn the question into the words a search wants."""
    text = _ASKING.sub("", (message or "").strip())
    # "who won the Lakers game last night" -> "Lakers game last night score"
    if re.search(r"\bwho\s+won\b", text, re.I):
        text = re.sub(r"\bwho\s+won\b", " ", text, flags=re.I) + " score"
    winner = bool(_WIN_PHRASE.search(text)) or bool(re.search(r"\bwho\b.*\bwin", text, re.I))
    text = _WIN_PHRASE.sub(" ", text)
    text = re.sub(r"\b(v|versus)\b\.?", "vs", text, flags=re.I)
    text = re.sub(r"[?!.,]+", " ", text)
    words = [w for w in text.split() if not _QUESTION_WORDS.fullmatch(w)]
    words = [w for w in words if w.lower() not in ("win", "wins", "winning")] if winner else words
    query = " ".join(words).strip()
    if winner and not re.search(r"\b(?:prediction|odds|pick)\b", query, re.I):
        query += " prediction odds"
    return query or (message or "").strip()


def _readable(url: str, title: str = "") -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if not host or any(bad in host for bad in _SKIP_HOSTS):
        return False
    return not _SKIP_WORDS.search(title or "")


def _when(item: Dict[str, Any]) -> float:
    try:
        stamp = datetime.fromisoformat(str(item.get("date") or "").replace("Z", "+00:00"))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except ValueError:
        return 0.0


_STOP = set("the a an and or of to in on for at by with is are was were be will vs v game "
            "who what when which how this that it its from as".split())
# Words every sports or news page has: what the question is ABOUT is the rest.
_GENERIC = set("prediction predictions odds score scores result results won win latest news "
               "update updates match last night yesterday today tonight week over/under "
               "picks pick price".split())


def key_terms(query: str) -> List[str]:
    """What the question is about: 'lakers' in 'lakers game last night score'."""
    words = re.findall(r"[a-z0-9'/]+", (query or "").lower())
    return [w for w in words if w not in _STOP and w not in _GENERIC and len(w) > 1]


def _mentions(text: str, terms: List[str]) -> int:
    """How much of the subject a text names: whole words, more for the whole phrase."""
    lower = (text or "").lower()
    hits = sum(1 for term in terms if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lower))
    if len(terms) > 1 and " ".join(terms) in lower:
        hits += len(terms)                        # "ai act", not "ai" here and "act" there
    return hits
_ANSWERING = re.compile(
    r"\b(?:predict\w*|pick|picks|favou?r\w*|odds|spread|line|moneyline|projected|expect\w*|"
    r"win|wins|beat|beats|edge|final|score|lead\w*|record|-\d+(?:\.\d)?|\+\d+(?:\.\d)?)\b", re.I)


_ABBREVIATION = re.compile(
    r"\b(?:vs|v|no|st|jr|sr|mr|mrs|ms|dr|inc|co|lt|gen|sgt|mt|ft|u\.s|a\.m|p\.m|[A-Z])\.(?=\s)", re.I)


def excerpts(text: str, query: str, limit: int = SENTENCES_PER_PAGE) -> List[str]:
    """The sentences of a page that best answer the query, in page order."""
    terms = {w.lower() for w in re.findall(r"[a-z0-9']+", query.lower()) if w not in _STOP}
    about = key_terms(query)
    # "Bears vs. Lions odds, Chicago is favored by 1.5" is one sentence: split
    # at "vs." and the model reads "Lions odds, Chicago is favored by 1.5"
    # and says the Lions are favoured.
    protected = _ABBREVIATION.sub(lambda m: m.group(0)[:-1] + "․", text or "")
    sentences = [s.strip().replace("․", ".")
                 for s in re.split(r"(?<=[.!?])\s+|\n+", protected) if s.strip()]
    scored = []
    for index, sentence in enumerate(sentences):
        if not 25 <= len(sentence) <= 400 or sentence.count("|") >= 2 or "slug:" in sentence:
            continue                              # page furniture, not prose
        lower = sentence.lower()
        hits = sum(1 for term in terms if term in lower)
        # A sentence about something else ("the Spurs lost") answers nothing,
        # however many of the generic words it shares.
        if hits == 0 or (about and not _mentions(sentence, about)):
            continue
        score = hits * 2 + (3 if _ANSWERING.search(sentence) else 0) + (1 if re.search(r"\d", sentence) else 0)
        scored.append((score, index, sentence))
    best = sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]
    return [sentence for _, _, sentence in sorted(best, key=lambda item: item[1])]


async def look_up(web: Any, message: str) -> Dict[str, Any]:
    """Search for the question, read the best few articles, keep what answers it."""
    query = search_query(message)
    found: List[Dict[str, Any]] = []
    try:
        news = await web.news(query, 10)
        found += sorted(news, key=_when, reverse=True)
    except Exception:
        news = []
    try:
        found += await web.search(query, 8)
    except Exception:
        pass
    seen, results = set(), []
    for item in found:
        url = item.get("url") or ""
        if url in seen or not _readable(url, item.get("title", "")):
            continue
        seen.add(url)
        results.append({key: item.get(key, "") for key in ("title", "url", "source", "date", "snippet")
                        if item.get(key)})
    # Articles about the thing asked first (the Lakers, not the Spurs); among
    # equals the order stays newest news first.
    about = key_terms(query)
    if about:
        results.sort(key=lambda item: -_mentions(item.get("title", "") + " " + item.get("snippet", ""), about))

    async def read(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            page = await asyncio.wait_for(web.fetch(item["url"], 20000), PAGE_TIMEOUT_S)
        except Exception:
            return None
        lines = excerpts(page.get("text", ""), query) if page.get("ok") else []
        if not lines:
            return None
        return {"title": page.get("title") or item.get("title", ""), "url": item["url"],
                "source": item.get("source") or urlparse(item["url"]).hostname or "",
                "date": item.get("date", ""), "excerpts": lines}

    read_pages = await asyncio.gather(*(read(item) for item in results[:PAGES_TO_TRY]))
    pages = [page for page in read_pages if page][:PAGES_TO_KEEP]
    return {"query": query, "pages": pages, "results": results[:8]}


def as_context(found: Dict[str, Any], today: str = "") -> str:
    """What was found, written for the model to answer from."""
    lines = [f"Live web lookup, done just now{(' (' + today + ')') if today else ''} "
             f"for \"{found.get('query', '')}\":"]
    for page in found.get("pages") or []:
        dated = f", {str(page['date'])[:10]}" if page.get("date") else ""
        lines.append(f"- From {page['source']}{dated} ({page['title']}):")
        lines += [f"    {sentence}" for sentence in page["excerpts"]]
    if not found.get("pages"):
        for item in (found.get("results") or [])[:6]:
            dated = f", {str(item['date'])[:10]}" if item.get("date") else ""
            lines.append(f"- {item.get('source') or item.get('url', '')}{dated}: "
                         f"{item.get('title', '')}. {item.get('snippet', '')}")
    lines += [
        "",
        "Answer the question from this, in one to three sentences, answer first. Name "
        "the source of each fact. Use a number only together with the source it is "
        "written under: never move a number from one source to another, and never make "
        "one up. Call each side by the name its source uses. If the sources disagree, "
        "give each one's pick briefly. If what you report is from an earlier day than "
        "today, say which day (\"on Tuesday\"), not \"last night\". Don't mention "
        "searching, results, snippets or videos, and don't tell the user to check other "
        "websites. If none of it answers the question, say what you did find.",
    ]
    return "\n".join(lines)


# "and what's the over/under?" straight after a look-up is about the same game.
_FOLLOW_UP = re.compile(
    r"\b(?:odds|spread|over/?under|total|line|moneyline|score|scores|pick|picks|prediction|"
    r"predicted|favou?red|favou?rite|kick-?off|start|channel|injur\w*|record|winner|won|win|"
    r"latest|update|now|so\s+far)\b|^(?:and|what\s+about|how\s+about)\b",
    re.I,
)


def follow_up_question(message: str, previous_question: str) -> Optional[str]:
    """The question to look up for a short follow-up to an earlier look-up."""
    text = (message or "").strip()
    if not previous_question or not text or len(text) > 120 or text.startswith("/"):
        return None
    if not _FOLLOW_UP.search(text):
        return None
    return f"{previous_question.strip().rstrip('?.!')} {text}"
