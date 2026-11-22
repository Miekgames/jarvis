"""
Repository audits: TODOs, complexity, licences, environment variables, changelogs.

Everything here is measured from the files themselves. Complexity is counted
from real branch points, licences are matched against known text, and a missing
environment variable is one that appears in code but not in .env -- no model is
asked to guess any of it.
"""
from __future__ import annotations

import logging
import os
import re
import asyncio
import subprocess

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.proc import run as _run, popen as _popen

logger = logging.getLogger("intelligence.codeaudit")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", ".cache", "coverage", ".pytest_cache"}

MARKERS = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|OPTIMI[SZ]E|REFACTOR|NOTE)\b[:\s]*(.*)",
                     re.IGNORECASE)

# Branch keywords by language. Complexity is 1 + the number of these found,
# which is cyclomatic complexity in its usual practical form.
BRANCH_TOKENS = {
    ".py": [r"\bif\b", r"\belif\b", r"\bfor\b", r"\bwhile\b", r"\bexcept\b",
            r"\band\b", r"\bor\b", r"\bassert\b"],
    ".js": [r"\bif\b", r"\bfor\b", r"\bwhile\b", r"\bcatch\b", r"\bcase\b",
            r"&&", r"\|\|", r"\?"],
}
BRANCH_TOKENS[".ts"] = BRANCH_TOKENS[".js"]
BRANCH_TOKENS[".tsx"] = BRANCH_TOKENS[".js"]
BRANCH_TOKENS[".jsx"] = BRANCH_TOKENS[".js"]

LICENCE_SIGNATURES = [
    ("MIT", "permission is hereby granted, free of charge"),
    ("Apache-2.0", "licensed under the apache license, version 2.0"),
    ("BSD-3-Clause", "neither the name of the copyright holder nor the names"),
    ("BSD-2-Clause", "redistribution and use in source and binary forms"),
    ("GPL-3.0", "gnu general public license version 3"),
    ("GPL-2.0", "gnu general public license, version 2"),
    ("LGPL", "gnu lesser general public license"),
    ("MPL-2.0", "mozilla public license version 2.0"),
    ("Unlicense", "this is free and unencumbered software released into the public domain"),
    ("ISC", "permission to use, copy, modify, and/or distribute this software"),
]

ENV_PATTERNS = [
    re.compile(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Z0-9_]+)[\"']"),
    re.compile(r"os\.environ\[\s*[\"']([A-Z0-9_]+)[\"']\s*\]"),
    re.compile(r"process\.env\.([A-Z0-9_]+)"),
    re.compile(r"import\.meta\.env\.([A-Z0-9_]+)"),
]


def _walk(root: Path, suffixes: Optional[set] = None, limit: int = 20000):
    count = 0
    for base, dirs, names in os.walk(root, onerror=lambda _: None):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in names:
            path = Path(base) / name
            if suffixes and path.suffix.lower() not in suffixes:
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= limit:
                return


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def complexity_of(source: str, suffix: str) -> int:
    """Cyclomatic complexity: 1 plus each branch point."""
    tokens = BRANCH_TOKENS.get(suffix)
    if not tokens:
        return 0
    # Strip strings and comments so the words inside them are not counted.
    cleaned = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', " ", source)
    cleaned = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', " ", cleaned)
    cleaned = re.sub(r"#.*$|//.*$", " ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*[\s\S]*?\*/", " ", cleaned)
    return 1 + sum(len(re.findall(token, cleaned)) for token in tokens)


def detect_licence(text: str) -> str:
    lowered = (text or "").lower()
    for name, signature in LICENCE_SIGNATURES:
        if signature in lowered:
            return name
    return "unknown"


class CodeAudit:
    """Audits over an authorised workspace."""

    def __init__(self, code, activity=None, orchestrator=None):
        self.code = code
        self.activity = activity
        self.orchestrator = orchestrator

    async def _root(self, workspace: str) -> Tuple[Path, Dict[str, Any]]:
        record = await self.code.resolve_workspace(workspace)
        return Path(record["path"]), record

    # ==================================================================
    async def todos(self, workspace: str, limit: int = 200) -> Dict[str, Any]:
        """Every TODO/FIXME/HACK marker, with file and line."""
        root, record = await self._root(workspace)
        found: List[Dict[str, Any]] = []
        by_kind: Counter = Counter()

        for path in _walk(root):
            text = _read(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                match = MARKERS.search(line)
                if not match:
                    continue
                # Only count a marker inside a comment; "TODO" in prose or a
                # string literal is not a code marker.
                if not re.search(r"(#|//|/\*|\*|<!--)", line[:match.start() + 4]):
                    continue
                kind = match.group(1).upper()
                by_kind[kind] += 1
                if len(found) < limit:
                    found.append({
                        "kind": kind,
                        "text": (match.group(2) or "").strip()[:160],
                        "path": str(path.relative_to(root)).replace("\\", "/"),
                        "line": number,
                    })

        return {
            "workspace": record["name"],
            "markers": found,
            "total": sum(by_kind.values()),
            "by_kind": [{"kind": k, "count": c} for k, c in by_kind.most_common()],
            "truncated": sum(by_kind.values()) > len(found),
        }

    async def metrics(self, workspace: str, top: int = 25) -> Dict[str, Any]:
        """Size and complexity per file, and the ones worth looking at."""
        root, record = await self._root(workspace)
        files: List[Dict[str, Any]] = []
        total_lines = 0

        for path in _walk(root, suffixes=set(BRANCH_TOKENS)):
            text = _read(path)
            if text is None:
                continue
            lines = text.splitlines()
            code_lines = [l for l in lines
                          if l.strip() and not l.strip().startswith(("#", "//", "*"))]
            complexity = complexity_of(text, path.suffix.lower())
            total_lines += len(lines)
            files.append({
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "lines": len(lines),
                "code_lines": len(code_lines),
                "complexity": complexity,
                "per_line": round(complexity / max(1, len(code_lines)), 3),
            })

        files.sort(key=lambda f: -f["complexity"])
        average = round(sum(f["complexity"] for f in files) / len(files), 1) if files else 0

        # Flag files that are both long and dense, rather than merely long.
        hotspots = [f for f in files
                    if f["complexity"] > max(40, average * 2) and f["code_lines"] > 120]

        return {
            "workspace": record["name"],
            "files": len(files),
            "total_lines": total_lines,
            "average_complexity": average,
            "most_complex": files[:top],
            "hotspots": hotspots[:15],
            "method": "cyclomatic complexity: 1 + branch points, strings and comments "
                      "excluded",
        }

    async def licences(self, workspace: str) -> Dict[str, Any]:
        """The project's own licence, and its declared dependencies."""
        root, record = await self._root(workspace)

        own = "none found"
        for name in ("LICENSE", "LICENCE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            candidate = root / name
            if candidate.exists():
                own = detect_licence(_read(candidate) or "")
                break

        dependencies: List[Dict[str, Any]] = []
        package_json = root / "package.json"
        if package_json.exists():
            import json
            try:
                data = json.loads(_read(package_json) or "{}")
                for section in ("dependencies", "devDependencies"):
                    for name, version in (data.get(section) or {}).items():
                        licence = "unknown"
                        meta = root / "node_modules" / name / "package.json"
                        if meta.exists():
                            try:
                                licence = (json.loads(_read(meta) or "{}").get("license")
                                           or "unknown")
                            except ValueError:
                                licence = "unknown"
                        dependencies.append({"name": name, "version": version,
                                             "licence": licence, "source": "npm",
                                             "dev": section == "devDependencies"})
            except ValueError:
                pass

        requirements = root / "requirements.txt"
        if requirements.exists():
            for line in (_read(requirements) or "").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    name = re.split(r"[=<>!~\[]", line)[0].strip()
                    if name:
                        dependencies.append({"name": name, "version": line[len(name):].strip(),
                                             "licence": "not checked", "source": "pypi",
                                             "dev": False})

        counts = Counter(d["licence"] for d in dependencies)
        # Copyleft licences matter if you ever ship this; flag rather than judge.
        copyleft = [d for d in dependencies
                    if any(tag in str(d["licence"]).upper() for tag in ("GPL", "AGPL"))]

        return {
            "workspace": record["name"],
            "project_licence": own,
            "dependencies": len(dependencies),
            "by_licence": [{"licence": k, "count": v} for k, v in counts.most_common()],
            "copyleft": copyleft,
            "unchecked": len([d for d in dependencies if d["licence"] == "not checked"]),
            "note": "Python dependency licences are not read from disk here; only the "
                    "names are listed." if requirements.exists() else "",
        }

    async def env_check(self, workspace: str) -> Dict[str, Any]:
        """Environment variables the code reads, against what .env defines."""
        root, record = await self._root(workspace)

        used: Dict[str, List[str]] = {}
        for path in _walk(root, suffixes={".py", ".js", ".ts", ".tsx", ".jsx"}):
            text = _read(path)
            if text is None:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            for pattern in ENV_PATTERNS:
                for name in pattern.findall(text):
                    used.setdefault(name, [])
                    if relative not in used[name]:
                        used[name].append(relative)

        defined = set()
        env_file = None
        for name in (".env", ".env.local", ".env.example"):
            candidate = root / name
            if candidate.exists():
                env_file = env_file or name
                for line in (_read(candidate) or "").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        defined.add(line.split("=", 1)[0].strip())

        missing = sorted(set(used) - defined - set(os.environ))
        unused = sorted(defined - set(used))

        return {
            "workspace": record["name"],
            "env_file": env_file or "none found",
            "used": len(used),
            "defined": len(defined),
            "missing": [{"name": n, "used_in": used[n][:4]} for n in missing],
            "unused": unused,
            "ok": not missing,
            "note": "'missing' means read by the code but absent from .env and from "
                    "the current environment.",
        }

    # ==================================================================
    # git-derived
    # ==================================================================
    async def _git(self, root: Path, *args: str) -> Optional[str]:
        """Run a read-only git command off the event loop.

        It used to run inline in an async method. git is normally quick, but
        the timeout here is 25 seconds -- and a blocking call in a coroutine
        does not wait politely, it stops the whole backend: every other
        request, the websocket, the wake word's event hop. A slow repository
        would have frozen the application.
        """
        def _execute() -> Optional[str]:
            try:
                completed = _run(
                    ["git"] + list(args), cwd=str(root), capture_output=True,
                    text=True, timeout=25, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                return None
            if completed.returncode != 0:
                return None
            return completed.stdout

        return await asyncio.get_event_loop().run_in_executor(None, _execute)

    async def stale_branches(self, workspace: str, days: int = 60) -> Dict[str, Any]:
        root, record = await self._root(workspace)
        output = await self._git(root, "for-each-ref", "--format=%(refname:short)|%(committerdate:iso8601)|%(authorname)",
                           "refs/heads")
        if output is None:
            return {"ok": False, "workspace": record["name"],
                    "message": "{} is not a git repository.".format(record["name"])}

        branches = []
        now = datetime.now()
        for line in output.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            name, date_text, author = parts[0], parts[1], parts[2]
            try:
                when = datetime.fromisoformat(date_text.strip().replace(" ", "T", 1)[:19])
            except ValueError:
                continue
            age = (now - when).days
            branches.append({"branch": name, "author": author, "age_days": age,
                             "last_commit": when.isoformat(timespec="seconds")})

        branches.sort(key=lambda b: -b["age_days"])
        stale = [b for b in branches if b["age_days"] >= days]
        return {"ok": True, "workspace": record["name"], "branches": len(branches),
                "stale": stale, "threshold_days": days, "all": branches[:40]}

    async def changelog(self, workspace: str, since: str = "",
                        limit: int = 100) -> Dict[str, Any]:
        """Group commits by conventional-commit type, falling back to 'other'."""
        root, record = await self._root(workspace)
        args = ["log", "--pretty=format:%h|%s|%an|%ad", "--date=short",
                "-n", str(max(1, min(int(limit), 500)))]
        if since:
            args.insert(1, "{}..HEAD".format(since))

        output = await self._git(root, *args)
        if output is None:
            return {"ok": False, "workspace": record["name"],
                    "message": "{} is not a git repository, or that range does not "
                               "exist.".format(record["name"])}

        groups: Dict[str, List[Dict[str, str]]] = {}
        for line in output.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            sha, subject, author, date = parts
            match = re.match(r"^(\w+)(\([^)]*\))?!?:\s*(.+)", subject)
            kind = match.group(1).lower() if match else "other"
            text = match.group(3) if match else subject
            groups.setdefault(kind, []).append(
                {"sha": sha, "text": text, "author": author, "date": date})

        order = ["feat", "fix", "perf", "refactor", "docs", "test", "build", "ci",
                 "chore", "style", "other"]
        ordered = {k: groups[k] for k in order if k in groups}
        for key in groups:
            ordered.setdefault(key, groups[key])

        total = sum(len(v) for v in groups.values())
        return {"ok": True, "workspace": record["name"], "commits": total,
                "groups": ordered, "conventional": total - len(groups.get("other", [])),
                "since": since or "the beginning of history"}

    async def contributors(self, workspace: str) -> Dict[str, Any]:
        root, record = await self._root(workspace)
        output = await self._git(root, "shortlog", "-sne", "--all")
        if output is None:
            return {"ok": False, "workspace": record["name"],
                    "message": "{} is not a git repository.".format(record["name"])}

        people = []
        for line in output.strip().splitlines():
            match = re.match(r"\s*(\d+)\s+(.+?)\s*<(.+?)>", line)
            if match:
                people.append({"commits": int(match.group(1)), "name": match.group(2),
                               "email": match.group(3)})
        total = sum(p["commits"] for p in people)
        for person in people:
            person["share"] = round(person["commits"] / total, 3) if total else 0.0
        return {"ok": True, "workspace": record["name"], "contributors": people[:40],
                "total_commits": total}

    async def coverage(self, workspace: str) -> Dict[str, Any]:
        """Read an existing coverage report. Never invents a number."""
        root, record = await self._root(workspace)

        for name in ("coverage.xml", ".coverage.xml"):
            candidate = root / name
            if candidate.exists():
                text = _read(candidate) or ""
                match = re.search(r'line-rate="([0-9.]+)"', text)
                if match:
                    return {"ok": True, "workspace": record["name"],
                            "line_coverage": round(float(match.group(1)) * 100, 1),
                            "source": name}

        summary = root / "coverage" / "coverage-summary.json"
        if summary.exists():
            import json
            try:
                data = json.loads(_read(summary) or "{}")
                total = data.get("total", {})
                return {"ok": True, "workspace": record["name"],
                        "line_coverage": total.get("lines", {}).get("pct"),
                        "branch_coverage": total.get("branches", {}).get("pct"),
                        "source": "coverage/coverage-summary.json"}
            except ValueError:
                pass

        return {"ok": False, "workspace": record["name"],
                "message": "No coverage report found. Run your tests with coverage "
                           "enabled first -- I will not estimate a number."}
