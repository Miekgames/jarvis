"""
Repo-wide code understanding.

Indexes an authorised project so JARVIS can answer questions across the whole
codebase rather than one file at a time: where a symbol is defined, what uses
it, what a stack trace actually points at, and where the obvious problems are.

The index is deliberately structural, not semantic. Python is parsed with
`ast`; TypeScript and JavaScript with targeted regexes. That gives exact,
verifiable answers -- a line number is either right or it isn't -- and it works
offline with no embedding model. The AI is used on top of the index, for
explanation, never for locating things.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.events import bus
from ..core.util import new_id, now_iso, truncate
from ..db.database import dumps, loads

logger = logging.getLogger("jarvis.code")

LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".php": "php", ".sh": "shell",
    ".css": "css", ".html": "html", ".sql": "sql", ".json": "json", ".md": "markdown",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env", "dist", "build",
    ".next", ".nuxt", "target", "vendor", ".pytest_cache", ".mypy_cache", "coverage",
    ".idea", ".vscode", "site-packages", ".tox", "bin", "obj",
}

MAX_FILE_BYTES = 1_500_000
MAX_FILES = 6000

# -- TypeScript / JavaScript symbol patterns --------------------------------
_TS_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*(\([^)]*\))"),
     "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface"),
    (re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*="), "type"),
    (re.compile(r"^\s*(?:export\s+)?enum\s+(\w+)"), "enum"),
    (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>"),
     "function"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*="), "constant"),
]
_TS_IMPORT = re.compile(r"""^\s*import\s+.*?from\s+['"]([^'"]+)['"]""")

# Common stack-trace shapes: Python, Node/TS, and generic file:line.
_TRACE_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),                  # python
    re.compile(r"at .*?\(([^:)]+):(\d+):\d+\)"),                # node
    re.compile(r"^\s*([\w./\\-]+\.\w+):(\d+)(?::\d+)?", re.M),  # tsc / generic
]


class CodeIntelligence:
    def __init__(self, db, settings, orchestrator, activity, files=None):
        self.db = db
        self.settings = settings
        self.orchestrator = orchestrator
        self.activity = activity
        self.files = files

    # ==================================================================
    # workspaces
    # ==================================================================
    def _authorise(self, path: str) -> Path:
        """A workspace must sit inside the folders you already allow JARVIS to
        read. Registering one does not widen the sandbox."""
        candidate = Path(os.path.expandvars(path)).expanduser().resolve()
        if not candidate.is_dir():
            raise NotADirectoryError(f"'{path}' is not a folder.")

        roots = [Path(r).expanduser().resolve() for r in self.settings.FILE_ROOTS]
        roots.append(Path(self.settings.ROOT).resolve())
        for root in roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise PermissionError(
            f"'{candidate}' is outside the folders I'm allowed to read "
            f"({', '.join(str(r) for r in roots[:2])}…). Add it to JARVIS_FILE_ROOTS first."
        )

    async def add_workspace(self, path: str, name: str = "") -> Dict[str, Any]:
        resolved = self._authorise(path)
        existing = await self.db.fetch_one(
            "SELECT * FROM workspaces WHERE path = ?", (str(resolved),))
        if existing:
            return _decode_workspace(existing)

        workspace_id = new_id("ws")
        await self.db.execute(
            "INSERT INTO workspaces (id, name, path, kind, vcs, added_at) VALUES (?,?,?,?,?,?)",
            (workspace_id, name or resolved.name, str(resolved), _detect_kind(resolved),
             "git" if (resolved / ".git").exists() else "", now_iso()),
        )
        await self.activity.record(f"Workspace registered: {resolved.name}", source="code")
        return await self.get_workspace(workspace_id)

    async def get_workspace(self, workspace_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        return _decode_workspace(row) if row else {}

    async def workspaces(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM workspaces ORDER BY added_at DESC")
        return [_decode_workspace(r) for r in rows]

    async def resolve_workspace(self, reference: str = "") -> Dict[str, Any]:
        """Accept an id, a name, or nothing (meaning the only/most recent one)."""
        listing = await self.workspaces()
        if not listing:
            raise LookupError(
                "No workspace is registered. Add one first, e.g. the JARVIS project itself."
            )
        if not reference:
            return listing[0]
        for workspace in listing:
            if reference in (workspace["id"], workspace["name"]) or \
                    reference.lower() in workspace["name"].lower():
                return workspace
        raise LookupError(f"No workspace matching '{reference}'. "
                          f"Known: {', '.join(w['name'] for w in listing)}.")

    async def remove_workspace(self, workspace_id: str) -> bool:
        await self.db.execute("DELETE FROM code_symbols WHERE workspace_id = ?", (workspace_id,))
        await self.db.execute("DELETE FROM code_files WHERE workspace_id = ?", (workspace_id,))
        return await self.db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,)) > 0

    # ==================================================================
    # indexing
    # ==================================================================
    async def index(self, workspace_id: str, on_progress=None) -> Dict[str, Any]:
        workspace = await self.get_workspace(workspace_id)
        if not workspace:
            raise LookupError(f"No workspace '{workspace_id}'.")
        root = Path(workspace["path"])

        await self.db.execute("DELETE FROM code_symbols WHERE workspace_id = ?", (workspace_id,))
        await self.db.execute("DELETE FROM code_files WHERE workspace_id = ?", (workspace_id,))

        def _walk() -> List[Path]:
            found: List[Path] = []
            for current, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    path = Path(current) / name
                    if path.suffix.lower() in LANGUAGES and len(found) < MAX_FILES:
                        found.append(path)
            return found

        paths = await asyncio.get_event_loop().run_in_executor(None, _walk)

        file_rows: List[tuple] = []
        symbol_rows: List[tuple] = []

        for index, path in enumerate(paths):
            if on_progress and index % 40 == 0:
                await on_progress(f"Indexing {path.name}", index / max(1, len(paths)))
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            relative = path.relative_to(root).as_posix()
            language = LANGUAGES.get(path.suffix.lower(), "")
            symbols, imports = _extract(text, language, relative)

            file_rows.append((
                workspace_id, relative, language, text.count("\n") + 1,
                len(text), dumps(imports),
                _mtime(path),
            ))
            for symbol in symbols:
                symbol_rows.append((
                    workspace_id, symbol["name"], symbol["kind"], relative, symbol["line"],
                    symbol["signature"], symbol["parent"], symbol["docstring"], language,
                ))

        if file_rows:
            await self.db.execute_many(
                "INSERT INTO code_files (workspace_id, path, language, lines, size, imports,"
                " modified) VALUES (?,?,?,?,?,?,?)", file_rows)
        if symbol_rows:
            await self.db.execute_many(
                "INSERT INTO code_symbols (workspace_id, name, kind, path, line, signature,"
                " parent, docstring, language) VALUES (?,?,?,?,?,?,?,?,?)", symbol_rows)

        await self.db.execute(
            "UPDATE workspaces SET indexed_at = ?, file_count = ?, symbol_count = ? WHERE id = ?",
            (now_iso(), len(file_rows), len(symbol_rows), workspace_id),
        )
        await self.activity.record(
            f"Indexed {workspace['name']}: {len(file_rows)} files, {len(symbol_rows)} symbols.",
            source="code",
        )
        bus.publish("code.indexed", {"workspace": workspace["name"], "files": len(file_rows),
                                     "symbols": len(symbol_rows)}, source="code")
        return {"workspace": workspace["name"], "files": len(file_rows),
                "symbols": len(symbol_rows)}

    # ==================================================================
    # querying
    # ==================================================================
    async def find_symbol(self, name: str, workspace_id: str,
                          limit: int = 20) -> List[Dict[str, Any]]:
        exact = await self.db.fetch_all(
            "SELECT * FROM code_symbols WHERE workspace_id = ? AND name = ? LIMIT ?",
            (workspace_id, name, limit),
        )
        if exact:
            return [dict(r) for r in exact]
        return [dict(r) for r in await self.db.fetch_all(
            "SELECT * FROM code_symbols WHERE workspace_id = ? AND name LIKE ?"
            " ORDER BY LENGTH(name) LIMIT ?",
            (workspace_id, f"%{name}%", limit),
        )]

    async def find_references(self, name: str, workspace_id: str,
                              limit: int = 60) -> List[Dict[str, Any]]:
        """Textual references. Honest about being textual -- it will include
        comments and strings, which is usually what you want when tracing."""
        workspace = await self.get_workspace(workspace_id)
        root = Path(workspace["path"])
        files = await self.db.fetch_all(
            "SELECT path FROM code_files WHERE workspace_id = ?", (workspace_id,))
        pattern = re.compile(rf"\b{re.escape(name)}\b")

        def _scan() -> List[Dict[str, Any]]:
            hits: List[Dict[str, Any]] = []
            for row in files:
                if len(hits) >= limit:
                    break
                path = root / row["path"]
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if name not in text:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        hits.append({"path": row["path"], "line": number,
                                     "text": truncate(line.strip(), 160)})
                        if len(hits) >= limit:
                            break
            return hits

        return await asyncio.get_event_loop().run_in_executor(None, _scan)

    async def outline(self, path: str, workspace_id: str) -> Dict[str, Any]:
        symbols = await self.db.fetch_all(
            "SELECT * FROM code_symbols WHERE workspace_id = ? AND path = ? ORDER BY line",
            (workspace_id, path),
        )
        record = await self.db.fetch_one(
            "SELECT * FROM code_files WHERE workspace_id = ? AND path = ?", (workspace_id, path))
        if not record:
            raise FileNotFoundError(f"'{path}' is not in the index. Re-index if it's new.")
        return {
            "path": path, "language": record["language"], "lines": record["lines"],
            "imports": loads(record["imports"], []),
            "symbols": [dict(s) for s in symbols],
        }

    async def overview(self, workspace_id: str) -> Dict[str, Any]:
        workspace = await self.get_workspace(workspace_id)
        by_language = await self.db.fetch_all(
            "SELECT language, COUNT(*) AS files, SUM(lines) AS lines FROM code_files"
            " WHERE workspace_id = ? GROUP BY language ORDER BY lines DESC", (workspace_id,))
        by_kind = await self.db.fetch_all(
            "SELECT kind, COUNT(*) AS n FROM code_symbols WHERE workspace_id = ?"
            " GROUP BY kind ORDER BY n DESC", (workspace_id,))
        largest = await self.db.fetch_all(
            "SELECT path, lines FROM code_files WHERE workspace_id = ?"
            " ORDER BY lines DESC LIMIT 8", (workspace_id,))
        return {
            "workspace": workspace,
            "by_language": [dict(r) for r in by_language],
            "by_symbol_kind": [dict(r) for r in by_kind],
            "largest_files": [dict(r) for r in largest],
            "total_lines": sum(int(r["lines"] or 0) for r in by_language),
        }

    # ==================================================================
    # analysis
    # ==================================================================
    async def explain_error(self, error_text: str, workspace_id: str) -> Dict[str, Any]:
        """Pull the real source at every frame in a stack trace, then explain.

        Explaining a trace without the code it refers to is guesswork; this
        resolves the frames first so the explanation is grounded."""
        workspace = await self.get_workspace(workspace_id)
        root = Path(workspace["path"])
        frames = _parse_trace(error_text)

        excerpts: List[Dict[str, Any]] = []
        for file_hint, line in frames[:6]:
            path = _match_path(root, file_hint)
            if path is None:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            start = max(0, line - 6)
            end = min(len(lines), line + 5)
            excerpts.append({
                "path": path.relative_to(root).as_posix() if _inside(path, root) else str(path),
                "line": line,
                "code": "\n".join(
                    f"{n + 1:>5}{'>' if n + 1 == line else ' '} {lines[n]}"
                    for n in range(start, end)
                ),
            })

        context = "\n\n".join(f"--- {e['path']} around line {e['line']} ---\n{e['code']}"
                              for e in excerpts)
        explanation = await self.orchestrator.ask(
            "Explain this error and how to fix it. Use the source shown; do not invent code that "
            "isn't there. Say plainly which line is at fault, what the cause is, and the concrete "
            "fix. If the source doesn't explain it, say what else you'd need to see.\n\n"
            f"ERROR:\n{truncate(error_text, 4000)}\n\n"
            + (f"SOURCE AT THE FRAMES:\n{context}" if context
               else "None of the referenced files are in the indexed workspace."),
            purpose="coding", max_tokens=1400,
        )
        return {"frames": excerpts, "resolved_frames": len(excerpts),
                "total_frames": len(frames), "explanation": explanation}

    async def review_file(self, path: str, workspace_id: str,
                          focus: str = "bugs") -> Dict[str, Any]:
        """Review one file for likely bugs, or for improvements, or suggest tests."""
        workspace = await self.get_workspace(workspace_id)
        full = Path(workspace["path"]) / path
        if not full.exists():
            raise FileNotFoundError(f"'{path}' does not exist in {workspace['name']}.")
        text = full.read_text(encoding="utf-8", errors="replace")
        if len(text) > 120_000:
            raise ValueError(f"'{path}' is too large to review in one pass.")

        instructions = {
            "bugs": ("Find likely bugs. For each: the line, what goes wrong, and a concrete "
                     "input or state that triggers it. Report only defects you can point at in "
                     "this code -- no speculation, no style notes. If you find none, say so."),
            "improve": ("Suggest specific improvements: duplication that could be shared, "
                        "unclear naming, missing error handling, needless complexity. Quote the "
                        "line. Skip anything that is merely stylistic preference."),
            "tests": ("Propose tests for this file. For each: what it verifies and why it is "
                      "worth testing. Prioritise the paths most likely to break. Write the test "
                      "code in the project's existing style."),
        }.get(focus, "Review this file and report what matters.")

        review = await self.orchestrator.ask(
            f"{instructions}\n\nFILE: {path}\n\n{text}", purpose="coding", max_tokens=2500)

        static = _static_checks(text, path)
        return {"path": path, "focus": focus, "review": review,
                "static_findings": static, "lines": text.count("\n") + 1}

    async def ask_about_codebase(self, question: str, workspace_id: str) -> Dict[str, Any]:
        """Answer a question using the index to choose what to read.

        The index picks the files; the model reads them. That keeps answers
        anchored to real code rather than to a guess about the structure."""
        terms = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question)][:8]
        workspace = await self.get_workspace(workspace_id)
        root = Path(workspace["path"])

        scored: Dict[str, int] = {}
        for term in terms:
            for symbol in await self.find_symbol(term, workspace_id, limit=8):
                scored[symbol["path"]] = scored.get(symbol["path"], 0) + 3
            rows = await self.db.fetch_all(
                "SELECT path FROM code_files WHERE workspace_id = ? AND path LIKE ? LIMIT 5",
                (workspace_id, f"%{term.lower()}%"))
            for row in rows:
                scored[row["path"]] = scored.get(row["path"], 0) + 2

        chosen = sorted(scored, key=lambda p: scored[p], reverse=True)[:5]
        if not chosen:
            chosen = [r["path"] for r in await self.db.fetch_all(
                "SELECT path FROM code_files WHERE workspace_id = ? ORDER BY lines DESC LIMIT 3",
                (workspace_id,))]

        excerpts = []
        for relative in chosen:
            try:
                text = (root / relative).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpts.append(f"--- {relative} ---\n{truncate(text, 14000)}")

        answer = await self.orchestrator.ask(
            "Answer this question about the codebase using only the files shown. Cite file paths "
            "and line numbers where you can. If the answer isn't in these files, say which other "
            "files you'd need.\n\n"
            f"QUESTION: {question}\n\n" + "\n\n".join(excerpts),
            purpose="coding", max_tokens=2000,
        )
        return {"question": question, "files_consulted": chosen, "answer": answer}

    # ==================================================================
    async def stats(self) -> Dict[str, Any]:
        return {
            "workspaces": await self.workspaces(),
            "indexed_files": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM code_files", default=0)),
            "indexed_symbols": int(await self.db.fetch_value(
                "SELECT COUNT(*) FROM code_symbols", default=0)),
        }


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _extract(text: str, language: str, path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    if language == "python":
        return _extract_python(text)
    if language in ("typescript", "javascript"):
        return _extract_ts(text)
    return [], []


def _extract_python(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []

    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    def visit(node: ast.AST, parent: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                try:
                    args = ast.unparse(child.args)
                except Exception:
                    args = ""
                symbols.append({
                    "name": child.name,
                    "kind": "method" if parent else "function",
                    "line": child.lineno,
                    "signature": truncate(f"{child.name}({args})", 300),
                    "parent": parent,
                    "docstring": truncate(ast.get_docstring(child) or "", 400),
                })
                visit(child, child.name)
            elif isinstance(child, ast.ClassDef):
                symbols.append({
                    "name": child.name, "kind": "class", "line": child.lineno,
                    "signature": f"class {child.name}", "parent": parent,
                    "docstring": truncate(ast.get_docstring(child) or "", 400),
                })
                visit(child, child.name)
            elif isinstance(child, ast.Assign) and not parent:
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append({
                            "name": target.id, "kind": "constant", "line": child.lineno,
                            "signature": target.id, "parent": "", "docstring": "",
                        })

    visit(tree)
    return symbols, sorted(set(imports))


def _extract_ts(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []

    for number, line in enumerate(text.splitlines(), start=1):
        match = _TS_IMPORT.match(line)
        if match:
            imports.append(match.group(1))
            continue
        for pattern, kind in _TS_PATTERNS:
            found = pattern.match(line)
            if found:
                symbols.append({
                    "name": found.group(1), "kind": kind, "line": number,
                    "signature": truncate(line.strip(), 300), "parent": "", "docstring": "",
                })
                break
    return symbols, sorted(set(imports))


# ---------------------------------------------------------------------------
def _static_checks(text: str, path: str) -> List[Dict[str, Any]]:
    """Cheap checks that need no model. Deliberately few and high-signal."""
    findings: List[Dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if re.search(r"\bexcept\s*:\s*$", stripped) or re.search(r"\bexcept\s+Exception\s*:\s*pass",
                                                                 stripped):
            findings.append({"line": number, "issue": "Bare or silenced exception handler",
                             "text": truncate(stripped, 120)})
        if re.search(r"#\s*(TODO|FIXME|XXX|HACK)\b", stripped, re.I):
            findings.append({"line": number, "issue": "Unresolved marker",
                             "text": truncate(stripped, 120)})
        if re.search(r"""(?i)\b(password|secret|api_key|token)\s*=\s*['"][^'"]{8,}""", stripped):
            findings.append({"line": number, "issue": "Possible hardcoded credential",
                             "text": "<redacted>"})
        if len(line) > 200:
            findings.append({"line": number, "issue": f"Very long line ({len(line)} chars)",
                             "text": truncate(stripped, 80)})
    return findings[:40]


def _parse_trace(text: str) -> List[Tuple[str, int]]:
    frames: List[Tuple[str, int]] = []
    for pattern in _TRACE_PATTERNS:
        for match in pattern.finditer(text or ""):
            try:
                frames.append((match.group(1), int(match.group(2))))
            except (ValueError, IndexError):
                continue
    seen = set()
    unique = []
    for frame in frames:
        if frame not in seen:
            seen.add(frame)
            unique.append(frame)
    return unique


def _match_path(root: Path, hint: str) -> Optional[Path]:
    """Stack traces carry absolute paths, relative paths or bare filenames."""
    candidate = Path(hint)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    direct = root / hint
    if direct.exists():
        return direct
    name = candidate.name
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if name in filenames:
            return Path(current) / name
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _detect_kind(root: Path) -> str:
    markers = [
        ("package.json", "node"), ("pyproject.toml", "python"), ("requirements.txt", "python"),
        ("Cargo.toml", "rust"), ("go.mod", "go"), ("pom.xml", "java"), ("Gemfile", "ruby"),
    ]
    kinds = [kind for marker, kind in markers if (root / marker).exists()]
    return "+".join(sorted(set(kinds))) or "unknown"


def _mtime(path: Path) -> Optional[str]:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def _decode_workspace(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["settings"] = loads(item.get("settings"), {})
    return item
