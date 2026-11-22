"""Skills: written know-how JARVIS can pick up when a request calls for it.

A skill is a folder holding a SKILL.md -- YAML frontmatter with a name and a
description, then plain markdown instructions. That is the agentskills.io
open format, so skills written for other assistants work here, and the
other way round.

How they are used:

  * The model sees a short catalogue of the skills that match the request
    (name and one line each) and can call `use_skill` to read the full
    instructions before doing the work.
  * The owner can invoke one directly: a message starting "/skill-name"
    runs with that skill's instructions in front of the model. For a small
    local model this is the reliable path.

Three kinds, by where they came from -- the trust tiers from OpenJarvis:

  bundled   shipped with JARVIS, read-only (can be switched off)
  mine      written in JARVIS by the owner
  imported  pasted in or fetched from a URL

Imported skills are screened for prompt injection on the way in. A skill is
instructions the model will follow, so one that tries to override JARVIS's
rules or send data somewhere is saved switched off, with the reason, and
only runs if the owner turns it on knowing that.

Ported from OpenJarvis (Stanford Hazy Research, Apache-2.0):
``skills/parser.py`` (frontmatter validation rules), ``skills/loader.py``
(SKILL.md parsing), ``skills/security.py`` (trust tiers). Storage, the
catalogue, invocation and the screening on import are JARVIS's.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("jarvis.skills")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_CHARS = 20000
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-))*[a-z0-9]$|^[a-z0-9]$")

BUNDLED_DIR = Path(__file__).parent / "bundled"
STATE_FILE = "skills.json"

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "my",
         "me", "i", "it", "this", "that", "is", "are", "be", "can", "you", "please",
         "jarvis", "what", "how", "do", "from", "into", "about", "using", "use"}


class SkillError(ValueError):
    """A skill that cannot be read, named or saved as given."""


@dataclass
class Skill:
    name: str
    description: str
    body: str
    tier: str                         # bundled | mine | imported
    path: Path
    enabled: bool = True
    version: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)
    origin: str = ""                  # where an imported skill came from
    flagged: List[str] = field(default_factory=list)

    def as_dict(self, body: bool = False) -> Dict[str, Any]:
        out = {
            "name": self.name, "description": self.description, "tier": self.tier,
            "enabled": self.enabled, "version": self.version, "author": self.author,
            "tags": self.tags, "origin": self.origin, "flagged": self.flagged,
            "editable": self.tier != "bundled", "lines": len(self.body.splitlines()),
        }
        if body:
            out["body"] = self.body
            out["text"] = render(self)
        return out


# ---------------------------------------------------------------------------
# reading and writing SKILL.md
# ---------------------------------------------------------------------------
def parse(text: str, fallback_name: str = "") -> Tuple[Dict[str, Any], str]:
    """Frontmatter and body of a SKILL.md. Raises SkillError if unusable."""
    raw = (text or "").replace("\r\n", "\n").lstrip("﻿")
    front: Dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        rest = raw[3:].lstrip(" \t")
        if rest.startswith("\n"):
            rest = rest[1:]
        end = rest.find("\n---")
        if end != -1:
            try:
                loaded = yaml.safe_load(rest[:end]) or {}
            except yaml.YAMLError as exc:
                raise SkillError(f"The frontmatter is not valid YAML: {exc}")
            if not isinstance(loaded, dict):
                raise SkillError("The frontmatter must be a set of key: value lines.")
            front = loaded
            body = rest[end + 4:]
            body = body[1:] if body.startswith("\n") else body
    front.setdefault("name", fallback_name)
    validate(front)
    if not body.strip():
        raise SkillError("The skill has no instructions after the frontmatter.")
    if len(body) > MAX_BODY_CHARS:
        raise SkillError(f"The instructions are longer than {MAX_BODY_CHARS} characters.")
    return front, body.strip() + "\n"


def validate(front: Dict[str, Any]) -> None:
    """The agentskills.io rules, as OpenJarvis's parser enforces them."""
    name, description = front.get("name"), front.get("description")
    if not isinstance(name, str) or not name:
        raise SkillError("A skill needs a name.")
    if not isinstance(description, str) or not description.strip():
        raise SkillError("A skill needs a description saying when to use it.")
    if len(name) > MAX_NAME_LENGTH:
        raise SkillError(f"The name must be at most {MAX_NAME_LENGTH} characters.")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise SkillError(f"The description must be at most {MAX_DESCRIPTION_LENGTH} characters.")
    if not NAME.match(name):
        raise SkillError(
            f"'{name}' is not a valid skill name: use lowercase letters, numbers "
            "and single hyphens, like 'meeting-notes'.")


def slug(text: str) -> str:
    """A valid skill name made from any title."""
    words = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    words = re.sub(r"-{2,}", "-", words)
    return words[:MAX_NAME_LENGTH].strip("-") or "skill"


def render(skill: Skill) -> str:
    front: Dict[str, Any] = {"name": skill.name, "description": skill.description}
    if skill.version:
        front["version"] = skill.version
    if skill.author:
        front["author"] = skill.author
    if skill.tags:
        front["tags"] = skill.tags
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{skill.body.strip()}\n"


def _stem(word: str) -> str:
    """Crude, on purpose: "organise", "organiser" and "organizer" meet at
    "organ"; "emails" and "email" at "email". Enough to match a request to
    a description without a dictionary."""
    return word[:5] if len(word) > 5 else word.rstrip("s")


# Words in skill names that turn up in all sorts of requests.
_GENERIC = {"note", "notes", "file", "files", "doc", "docs", "data", "code", "web",
            "topic", "email"}


def _terms(text: str) -> set:
    return {_stem(w) for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOP}


# ---------------------------------------------------------------------------
class SkillLibrary:
    def __init__(self, root: Path, bundled: Path = BUNDLED_DIR, web=None) -> None:
        self.root = Path(root)
        self.bundled = Path(bundled)
        self.web = web
        self.root.mkdir(parents=True, exist_ok=True)

    # -- state --------------------------------------------------------------
    def _state(self) -> Dict[str, Any]:
        try:
            return json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        temporary = self.root / (STATE_FILE + ".tmp")
        temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
        temporary.replace(self.root / STATE_FILE)

    # -- reading ------------------------------------------------------------
    def _load(self, folder: Path, tier: str, state: Dict[str, Any]) -> Optional[Skill]:
        path = folder / "SKILL.md"
        try:
            front, body = parse(path.read_text(encoding="utf-8"), folder.name)
        except (OSError, SkillError) as exc:
            logger.warning("skipping skill %s: %s", folder.name, exc)
            return None
        info = (state.get("skills") or {}).get(front["name"], {})
        if tier != "bundled":
            tier = info.get("tier", tier)
        disabled = set(state.get("disabled") or [])
        tags = front.get("tags") or []
        return Skill(
            name=front["name"], description=str(front["description"]).strip(),
            body=body, tier=tier, path=folder,
            enabled=front["name"] not in disabled,
            version=str(front.get("version") or ""), author=str(front.get("author") or ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            origin=info.get("origin", ""), flagged=list(info.get("flagged") or []))

    def all(self) -> List[Skill]:
        state = self._state()
        found: Dict[str, Skill] = {}
        for tier, base in (("bundled", self.bundled), ("mine", self.root)):
            if not base.exists():
                continue
            for folder in sorted(p for p in base.iterdir() if p.is_dir()):
                skill = self._load(folder, tier, state)
                # One of the owner's own with a bundled name replaces it.
                if skill is not None:
                    found[skill.name] = skill
        return sorted(found.values(), key=lambda s: (s.tier != "mine", s.name))

    def get(self, name: str) -> Optional[Skill]:
        return next((s for s in self.all() if s.name == (name or "").strip().lstrip("/")), None)

    def enabled(self) -> List[Skill]:
        return [s for s in self.all() if s.enabled]

    # -- finding the right one ---------------------------------------------
    def match(self, query: str, limit: int = 5) -> List[Tuple[Skill, float]]:
        """Enabled skills whose name or description fits a request."""
        wanted = _terms(query)
        if not wanted:
            return []
        scored = []
        for skill in self.enabled():
            name_terms = _terms(skill.name.replace("-", " "))
            terms = name_terms | _terms(skill.description) | {t.lower() for t in skill.tags}
            hits = wanted & terms
            named = wanted & name_terms
            # One shared word in a description is coincidence ("weather" in
            # the daily digest's); a word of the name, or two of the
            # description, is a match. But some name words are everyday
            # words: "what's in my notes about the boiler" is not a request
            # for meeting-notes, so those need a second word to agree.
            if not named and len(hits) < 2:
                continue
            if named and named <= _GENERIC and len(hits) < 2:
                continue
            score = len(hits) + 2 * len(named)
            scored.append((skill, float(score)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def catalogue(self, query: str) -> str:
        """The few skills worth mentioning to the model for this request."""
        matches = self.match(query, limit=4)
        if not matches:
            return ""
        lines = [f"- {skill.name}: {skill.description[:160]}" for skill, _ in matches]
        return ("Skills that may fit this request -- call use_skill with the name "
                "to read its instructions before doing the work:\n" + "\n".join(lines))

    # -- writing ------------------------------------------------------------
    def create(self, name: str, description: str, body: str, tier: str = "mine",
               origin: str = "", replace: bool = False) -> Skill:
        name = (name or "").strip()
        front = {"name": name, "description": (description or "").strip()}
        validate(front)
        if not (body or "").strip():
            raise SkillError("Write the instructions the skill should give.")
        if len(body) > MAX_BODY_CHARS:
            raise SkillError(f"The instructions are longer than {MAX_BODY_CHARS} characters.")
        existing = self.get(name)
        if existing and existing.tier != "bundled" and not replace:
            raise SkillError(f"There is already a skill called '{name}'.")

        from ..core.screening import screener

        found = screener.scan(f"{description}\n{body}")
        flagged = sorted({f.description for f in found})

        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        skill = Skill(name=name, description=front["description"], body=body.strip() + "\n",
                      tier=tier, path=folder, origin=origin, flagged=flagged)
        (folder / "SKILL.md").write_text(render(skill), encoding="utf-8")

        state = self._state()
        state.setdefault("skills", {})[name] = {"tier": tier, "origin": origin,
                                                "flagged": flagged}
        disabled = set(state.get("disabled") or [])
        # Instructions that try to give JARVIS orders do not run unless the
        # owner chooses to switch them on.
        if flagged:
            disabled.add(name)
        state["disabled"] = sorted(disabled)
        self._save_state(state)
        skill.enabled = name not in disabled
        return skill

    def update(self, name: str, text: str) -> Skill:
        current = self.get(name)
        if current is None:
            raise SkillError(f"No skill called '{name}'.")
        if current.tier == "bundled":
            raise SkillError("Bundled skills cannot be edited. Make a copy of it instead.")
        front, body = parse(text, name)
        if front["name"] != name:
            raise SkillError("Renaming a skill is not supported: create a new one instead.")
        return self.create(name, front["description"], body, tier=current.tier,
                           origin=current.origin, replace=True)

    def import_text(self, text: str, origin: str = "pasted") -> Skill:
        front, body = parse(text)
        if self.get(front["name"]) and self.get(front["name"]).tier != "bundled":
            raise SkillError(f"There is already a skill called '{front['name']}'.")
        return self.create(front["name"], front["description"], body,
                           tier="imported", origin=origin)

    async def import_url(self, url: str) -> Skill:
        if self.web is None:
            raise SkillError("Fetching from the web is not available.")
        raw = _raw_url(url)
        result = await self.web.fetch_raw(raw)
        if not result.get("ok"):
            raise SkillError(f"Could not fetch {raw}: {result.get('error')}")
        return self.import_text(result.get("text", ""), origin=raw)

    def set_enabled(self, name: str, enabled: bool) -> Skill:
        skill = self.get(name)
        if skill is None:
            raise SkillError(f"No skill called '{name}'.")
        state = self._state()
        disabled = set(state.get("disabled") or [])
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        state["disabled"] = sorted(disabled)
        self._save_state(state)
        skill.enabled = enabled
        return skill

    def delete(self, name: str) -> bool:
        skill = self.get(name)
        if skill is None:
            return False
        if skill.tier == "bundled":
            raise SkillError("Bundled skills cannot be deleted -- switch it off instead.")
        shutil.rmtree(skill.path, ignore_errors=True)
        state = self._state()
        (state.get("skills") or {}).pop(name, None)
        state["disabled"] = sorted(set(state.get("disabled") or []) - {name})
        self._save_state(state)
        return True


def _raw_url(url: str) -> str:
    """A GitHub page address turned into the address of the file itself."""
    url = (url or "").strip()
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if match:
        return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/{match.group(3)}"
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$", url)
    if match:
        return (f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/"
                f"{match.group(3)}/{match.group(4)}/SKILL.md")
    return url
