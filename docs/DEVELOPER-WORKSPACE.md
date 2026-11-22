# Developer workspace

JARVIS can read a codebase the way you'd want a colleague to: it indexes the
files, knows where a symbol is defined and who calls it, resolves a stack trace
to the actual lines that threw, and drives the project's own git, test and build
commands. It does not guess. When it cannot answer something it says so.

This is the layer behind the **Developer** panel and the `find_in_code`,
`ask_codebase`, `explain_error`, `git_*`, `run_tests` and `run_build` tools.

---

## Workspaces

A workspace is a folder JARVIS is allowed to read. It is not "anywhere on disk":
`CodeIntelligence._authorise` resolves the path and rejects anything outside
`FILE_ROOTS` (from `.env`) or the JARVIS project root itself. A path that escapes
those roots raises `PermissionError` before a single file is opened.

```
add my project at C:\Users\me\code\thing
index it
```

Indexing walks the tree, skipping the usual noise (`node_modules`, `.git`,
`__pycache__`, `venv`, `dist`, `build`) and any file over 1.5 MB, up to a
ceiling of 6000 files, then records one row per file and one per symbol.

Symbol extraction is language-specific and honest about its limits:

| Language | Method | Extracts |
| --- | --- | --- |
| Python | `ast` parse | classes, functions, methods, module constants, imports |
| TypeScript / JavaScript | regex | interfaces, types, functions, arrow components, exported constants, imports |
| Everything else | line count only | no symbols claimed |

A Python file with a syntax error yields no symbols rather than a partial guess —
`_extract_python` returns `([], [])` and the file is still counted.

Indexing this project produces **166 files and 2142 symbols** across 42,396 lines.

---

## What you can ask

**Where is this defined?**

```
where is GoalPlanner defined?
```

Returns the exact file and line. Exact name matches rank first; if none match it
falls back to partial matches rather than returning nothing.

**Who uses it?**

```
what calls checkpoint_now?
```

A textual reference scan across indexed files, returning path, line and the
source line itself, so you can see the call rather than trust a count.

**What's in this file?**

```
outline backend/tasks/engine.py
```

Imports and symbols in declaration order. An unknown path raises
`FileNotFoundError` — it will not invent an outline.

**Explain this error.**

```
explain this error: <paste a traceback>
```

`_parse_trace` handles Python (`File "x.py", line 12`), Node
(`at fn (/path/x.ts:42:9)`) and tsc (`x.ts:7:3 - error TS2322`) frames, then
resolves each to a real indexed file and reads the surrounding lines. The result
reports `resolved_frames`; when it's `0`, the explanation says the frames point
outside the workspace instead of pretending to have read them.

**Review a file.**

Combines deterministic static checks with a model pass. The static checks flag
bare `except:`, TODO/FIXME/HACK markers, and hard-coded credentials — and
**redact the credential value** in the finding, so the audit trail never carries
the secret it just found.

---

## Git, tests and builds

Git operations shell out to real `git` and parse porcelain output.
`_parse_status` reads staged/unstaged/untracked state and ahead/behind counts;
`_diff_stats` and `_diff_files` summarise a diff.

If the folder is not a repository, JARVIS says exactly that:

> JARVIS is not a git repository.

Test and build runners are **discovered, not assumed**. `_discover` looks for
`pytest.ini`/`pyproject.toml`/`tests/` for Python, and reads `package.json` to
see which scripts actually exist. If `package.json` defines a `build` script but
no `test` script, JARVIS offers build and reports that it can't tell how to run
the tests — it does not fabricate an `npm test`.

Output parsing covers pytest and jest summaries. Each count is matched
independently; a combined all-optional pattern would match the empty string and
silently report zero passing tests.

Every run is recorded in the development timeline with its real outcome.

---

## Permissions

Three separate permissions gate this layer, and the model is not given the tools
for a permission that is off:

| Permission | Default | Covers |
| --- | --- | --- |
| `dev.workspace` — *Read your code* | **on** | indexing, symbols, references, outlines, git status and diff |
| `dev.execute` — *Run builds and tests* | **off** | running the project's own scripts, which is arbitrary code execution |
| `dev.git_write` — *Write to git* | **off** | staging and committing |

`dev.execute` and `dev.git_write` are both HIGH risk and off by default. The
panel shows the requirement in place of the control rather than showing a button
that fails when pressed.

`ci_status` additionally needs the GitHub integration configured; without it the
call returns `ok: false` and names what's missing.

Self-editing (`code.self_modify`) is a separate, stricter thing —
see [`SELF-EDITING.md`](SELF-EDITING.md).

---

## API

All routes sit under `/api`:

```
GET    /dev                          panel state
GET    /dev/workspaces               list
POST   /dev/workspaces               add (path is authorised first)
POST   /dev/workspaces/{id}/index    re-index
DELETE /dev/workspaces/{id}          forget
GET    /dev/overview                 languages, files, lines

GET    /dev/git/status               branch, changes, ahead/behind
GET    /dev/git/diff                 unified diff
GET    /dev/git/log                  recent commits
POST   /dev/git/review               review the working diff
POST   /dev/git/commit-message       draft a message from the diff
POST   /dev/git/commit               stage + commit          (dev.git_write)

POST   /dev/tests                    run discovered tests     (dev.execute)
POST   /dev/build                    run discovered build     (dev.execute)
POST   /dev/debug-plan               plan from a failure
GET    /dev/ci                       GitHub checks
GET    /dev/timeline                 recorded development events

GET    /code/find                    symbol definitions and references
GET    /code/outline                 one file's structure
POST   /code/ask                     question about the codebase
POST   /code/explain-error           traceback → real source
POST   /code/review                  review one file
```

---

## Storage

Schema v4 adds four tables:

- `workspaces` — authorised roots, last index time, file and symbol counts
- `code_files` — path, language, line count, import list
- `code_symbols` — name, kind, path, line, signature
- `dev_events` — the development timeline

Re-indexing replaces a workspace's rows rather than accumulating stale ones.

---

## What it deliberately does not do

- It does not modify your code. Reading and editing are different permissions,
  and editing lives in the self-editing layer with its own guard rails.
- It does not resolve symbols semantically. Reference search is textual, so a
  name used in a comment or string will appear. The line is shown so you can
  judge it.
- It does not claim CI results, coverage, or performance numbers it did not
  measure.
- It does not run anything on a folder outside the authorised roots.
