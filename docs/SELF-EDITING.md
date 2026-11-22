# Self-editing

JARVIS can read and rewrite its own source, using a **separate model** from the
one holding the conversation.

That separation is the point. The model writing code is chosen for coding, and
keeping it distinct means a conversational model cannot quietly rewrite the
application by drifting off-task mid-sentence.

**The permission is off by default.** Nothing below happens until you turn it on.

---

## Setup

Settings → Permissions → **Edit its own code** (critical tier).

Optionally pin the code model in `.env`:

```bash
# Blank = route to whatever your coding model is (Claude Sonnet, or the best
# local tool-capable model when running on Ollama).
JARVIS_SELFEDIT_PROVIDER=anthropic
JARVIS_SELFEDIT_MODEL=claude-sonnet-5
```

`GET /api/selfedit` reports which model is actually doing the writing.

---

## The pipeline

```
read → propose (dedicated model) → validate syntax → diff
     → confirm (shows the diff) → back up → write → restart notice
```

Proposal and application are **separate operations**. The model can propose
freely; applying is a distinct, confirmed, critical-risk action. Nothing is
ever written automatically.

```
"Read backend/ai/orchestrator.py and add a docstring to _provider_order"
  → propose_code_change   → diff, +6/-1, syntax valid, proposal id edit_a1b2
  → apply_code_change     → confirmation dialog showing the diff
                          → backup taken, written, "restart the backend"
```

---

## Guard rails

**Sandboxed.** Only `backend/`, `frontend/src/`, `tests/`, `docs/` and
`start.py` are editable, and only source suffixes. Verified refused: `.env`,
`data/jarvis.db`, `backups/`, `../` traversal, absolute system paths.

**Protected files.** Six files implement the controls that make this safe:

```
backend/core/permissions.py     backend/core/security.py
backend/core/confirmations.py   backend/core/audit.py
backend/api/deps.py             backend/intelligence/selfedit.py
```

Editing one requires `allow_protected=true`, and the confirmation prompt says
explicitly that approving it can weaken the protections gating every other
action.

**Invalid code is never written.** Python is parsed with `ast`; JSON with
`json`; TypeScript with the project's own `tsc` against a temporary sibling so
imports and tsconfig resolve. A failed check refuses the write and reports the
line.

**Deterministic.** Temperature 0 — the same request should not produce
different code each run.

**The model can refuse.** If a request would strip error handling, permission
checks or audit calls, it returns the file unchanged with a
`# SELFEDIT-REFUSED:` line, which is surfaced as the reason.

**Concurrency-safe.** If the file changed on disk since the proposal, applying
refuses and asks you to re-propose.

**Always reversible.** Every write is backed up first. A rollback is itself
backed up, so restoring an old version does not lose the new one. Backup names
are collision-proof — second-resolution timestamps alone were not enough, and a
rollback within the same second would have overwritten the backup it was
restoring from.

**Audited.** `code.self_modified` and `code.rolled_back` entries record the
file, the request, the model used and the backup name.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/selfedit` | Model in use, editable roots, protected list, pending proposals |
| GET | `/selfedit/files?pattern=` | Editable source files |
| GET | `/selfedit/read?path=` | One file's contents |
| POST | `/selfedit/propose` | `{path, request}` → diff + validation, writes nothing |
| POST | `/selfedit/apply` | `{proposal_id, allow_protected}` → writes, backs up |
| GET | `/selfedit/backups?path=` | Available backups |
| POST | `/selfedit/rollback?backup=` | Restore a file |

Tools: `source_files`, `read_source`, `propose_code_change`,
`apply_code_change`, `rollback_code_change`.

---

## Honest limits

- **A restart is still needed** for backend changes. JARVIS cannot hot-reload
  its own Python; the response says so rather than pretending the change is
  live. Frontend edits hot-reload via Vite.
- **Syntactically valid is not semantically correct.** The checks catch code
  that would not parse, not code that is wrong. Read the diff.
- **32 tests cover this**, most of them asserting refusals.
