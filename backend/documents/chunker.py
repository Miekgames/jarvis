"""Cutting documents into passages small enough to search and quote.

A passage is what search returns and what the model reads, so it has to be
a coherent piece of the document -- a section, a few paragraphs -- not an
arbitrary window that starts mid-sentence. Splitting goes from the largest
natural boundary down to the smallest that will fit:

    section heading  ->  paragraph  ->  sentence  ->  hard cut

Each passage is capped by word count *and* character count (text without
spaces, such as URLs or tables, defeats a word cap alone), and carries a
short overlap from the end of the one before, so a fact split across a
boundary is still findable from either side.

Ported from OpenJarvis ``connectors/chunker.py`` (Stanford Hazy Research,
Apache-2.0): the boundary order, the double cap and the overlap. Trimmed to
what documents need -- their email and message rules are left out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

MAX_WORDS = 220
MAX_CHARS = 1600
OVERLAP_WORDS = 30

_SECTION = re.compile(r"(?m)^(?:#{1,4}\s+.+|[A-Z][A-Z0-9 ,'&-]{3,60}:?)\s*$")
_SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"“(])')


@dataclass
class Passage:
    text: str
    heading: str
    position: int


def _fits(text: str) -> bool:
    return len(text.split()) <= MAX_WORDS and len(text) <= MAX_CHARS


def _hard_cut(text: str) -> List[str]:
    words, pieces, current = text.split(), [], []
    for word in words:
        while len(word) > MAX_CHARS:            # one enormous "word"
            pieces.append(word[:MAX_CHARS])
            word = word[MAX_CHARS:]
        candidate = current + [word]
        if len(candidate) > MAX_WORDS or len(" ".join(candidate)) > MAX_CHARS:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current = candidate
    if current:
        pieces.append(" ".join(current))
    return [p for p in pieces if p.strip()]


def _pack(segments: List[str], joiner: str) -> List[str]:
    """Greedily join small segments up to the cap; split big ones further."""
    out: List[str] = []
    current = ""
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        if not _fits(segment):
            if current:
                out.append(current)
                current = ""
            sentences = _SENTENCE.split(segment)
            if len(sentences) > 1:
                out.extend(_pack(sentences, " "))
            else:
                out.extend(_hard_cut(segment))
            continue
        candidate = f"{current}{joiner}{segment}" if current else segment
        if _fits(candidate):
            current = candidate
        else:
            out.append(current)
            current = segment
    if current:
        out.append(current)
    return out


def _sections(text: str) -> List[tuple]:
    """(heading, body) pairs, split at heading-like lines."""
    marks = list(_SECTION.finditer(text))
    if not marks:
        return [("", text)]
    sections = []
    if marks[0].start() > 0:
        sections.append(("", text[:marks[0].start()]))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        heading = mark.group(0).strip().lstrip("#").strip().rstrip(":")
        sections.append((heading, text[mark.end():end]))
    return sections


def chunk(text: str) -> List[Passage]:
    text = (text or "").replace("\r\n", "\n").replace("\x00", "")
    passages: List[Passage] = []
    for heading, body in _sections(text):
        # Overlap runs within a section, never across a heading: the end of
        # "Introduction" says nothing useful about "Budget".
        previous_tail = ""
        paragraphs = re.split(r"\n\s*\n", body)
        for piece in _pack(paragraphs, "\n\n"):
            room = MAX_WORDS + OVERLAP_WORDS - len(piece.split())
            tail = " ".join(previous_tail.split()[-max(0, room):]) if room > 0 else ""
            content = f"{tail} {piece}".strip() if tail else piece
            passages.append(Passage(content, heading[:120], len(passages)))
            previous_tail = " ".join(piece.split()[-OVERLAP_WORDS:])
    return [p for p in passages if len(p.text.split()) >= 3]
