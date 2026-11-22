# The AI Operating Environment (Update 3)

Update 1 built the command centre. Update 2 made it ambient. This makes it
something you give **objectives** to rather than commands.

Nothing was replaced: goals still run through the task engine, agents still
inherit checkpointing and cancellation, the particle field is the same renderer
with more layers. Schema migration 3 adds tables; it does not rewrite any.

---

## Goals with milestones

`backend/intelligence/goals2.py`

> "I need to get ready for this project by Friday."

1. **Assess before acting.** Size, estimated hours, confidence, risks, and any
   genuinely blocking questions — computed before a single task is created.
   A `large` estimate is surfaced for approval rather than quietly spawning
   twenty tasks.
2. **Ask only what blocks.** An objective with a clear deliverable and a date
   gets on with it. One that names no platform, repo or scope stops at
   `needs_input` and asks. Nothing is queued while it waits.
3. **Milestones, not steps.** Outcome-shaped checkpoints ("Requirements
   gathered"), each holding 1–3 tasks, with dependencies between them.
4. **Dates are arithmetic.** Milestone deadlines are spread across the time
   available *by effort share*, ending exactly on the deadline. The model
   never invents a date.
5. **Reminders are real.** Each milestone schedules a notification 12 hours
   before it's due.
6. **Adapt when things change.** `adapt` re-plans only the *remaining*
   milestones; completed work is never rewritten.
7. **A completion report** built from what the tasks actually returned,
   including what didn't get done and whether it overran the estimate.

```bash
curl -X POST localhost:8000/api/planner/goals \
  -d '{"objective":"Ship the landing page","deadline":"2026-08-21T17:00:00Z","start_now":true}'
```

---

## Agent orchestrator

`backend/intelligence/agents.py`

Eight specialists, each a first-class runtime object you can watch, message,
bound and stop individually.

| Agent | Tools | Limit | Cannot |
| --- | --- | --- | --- |
| Researcher | 17 | 30m | write, delete, send, call, shell |
| Coding assistant | 25 | 30m | delete, power, send, call |
| Planner | 19 | 15m | delete, power, send, shell |
| File assistant | 21 | 15m | power, send, call, smart home |
| System assistant | 22 | 10m | delete, send, call, shell |
| Communication assistant | 8 | 15m | delete, power, smart home |
| Meeting assistant | 22 | 15m | delete, power, call, shell |
| General assistant | 96 | 20m | power, shell, locks |

**Boundaries are enforced, not advertised.** The tool set is filtered *before*
the agent runs — a researcher physically cannot delete a file even if you have
`files.delete` enabled globally. Verified by test.

**Selection is explainable.** No model call, no opacity:

```
"there is a bug in this function, KeyError in the stack trace"
  -> coder       conf 0.95  Matched coding assistant cues: bug, error, stack trace.
```

**Time limits are real.** An agent over budget is stopped and reports partial
results. **Stopping one agent stops one agent** — everything else continues.
Agents message each other through the database, so notes survive a restart.

---

## Deep Research

`backend/intelligence/research.py`

A research *project*, not a one-shot answer.

- **Every source tracked individually** — URL, domain, excerpt, when retrieved,
  whether it was reachable. Failures are recorded, not dropped.
- **Conflicts asked for separately.** Rolled into one "write me a report"
  prompt, disagreements get smoothed into consensus prose — which is exactly
  the information a researcher needs. A dedicated pass finds them.
- **Citations built from what was actually read**, never invented.
- **Staleness flagged** by finding publication dates in the page. A flag means
  "worth checking", not "wrong", and it says so.
- **Resumable.** `gather` skips URLs already visited, so a project continues
  days later without re-reading anything.
- **Two outputs**: a 2–3 sentence executive summary and a detailed report,
  both cited.

Depths: `quick` (4 sources) · `standard` (9) · `deep` (18, background task).

---

## Memory 2.0

`backend/intelligence/memory2.py`

- **Scopes** — `short_term` expires in 12 hours; `long_term` persists;
  `project` and `conversation` only surface in their context.
- **Provenance** — where each memory came from, so "why do you think that?"
  has an answer.
- **Usage tracking** — what actually gets recalled drives cleanup.
- **Natural-language search** — the model extracts keywords; the store does the
  retrieval, so results are still real rows.
- **Cleanup reports before it deletes.** Pinned memories are never touched.

---

## Command interpretation 2.0

`backend/intelligence/resolver.py`

> "take care of this" · "finish what I was doing yesterday" · "handle the boring stuff"

The resolver assembles real candidates — what's on screen, what's running,
what failed, which goals are warm, what happened yesterday — scores them, and
either resolves or asks a question naming the actual options.

**It never guesses on a destructive command.** Verified by test: with a single
candidate available, `"delete that"` still refuses to resolve and asks.

```
"delete that"                        -> destructive, asks, never resolves
"finish what I was doing yesterday"  -> historical, 4 real candidates, asks
"take care of this"  (nothing running) -> "I can't see anything obvious you'd
                                          be pointing at. What should I take care of?"
```

The candidate list is injected into the system prompt, so the model resolves
the reference with the same evidence rather than inventing a referent.

---

## Field Dynamics — the three-layer field

`frontend/src/gl/shaders2.ts`

| Layer | Holds | Behaviour |
| --- | --- | --- |
| Ambient | context, memory, background awareness | slow, dim, wide |
| Task | active processes and agents | directed, brighter, mid-radius |
| Core | JARVIS's own presence | tight, bright, central |

Particles **migrate between layers** as priority shifts — `uLayerPull` moves
ambient into task when work starts, task into core while listening, and
`uRelease` pushes core outward on completion.

Measured radial signatures (% of lit pixels per ring, centre → edge):

| Mode | Coverage | Signature |
| --- | --- | --- |
| neutral | 31.2% | `0.3, 10.7, 23.7, 22.8, 25.9, 16.5` |
| research | 19.2% | `2.2, 13, 15.4, 18.4, 18.2, 32.8` — branches outward |
| coding | 22.8% | `0.4, 12.5, 21.6, 20.7, 24.6, 20.3` — tight lattice |
| meeting | 31.6% | `0.1, 4, 10.1, 17.8, 28.2, 39.8` — outer orbital rings |
| focus | 27.6% | ambient faded |
| agents | 27.7% | clusters at mid-radius |
| **collapsed** | **0.18%** | `21.2, 78.8, 0, 0, 0, 0` — a single point |

Also implemented: agent sub-clusters tinted by the backend's own agent colours,
communication bridges lit when agents message each other, reasoning ripples
from the core, confidence tightening/dispersing the field, localised
distortion zones in the error state, contextual particle silhouettes (diamond
for research, square for code, ring for meetings), and a sleep collapse that
takes ~2s in and ~4s out — the field **reconstructs gradually**, never snaps.

---

## JARVIS Continuity

`GET /api/continuity` returns everything a device needs to pick up where
another left off. Tasks, goals, agents, notifications and settings are already
shared — they live in one database on one machine. Continuity adds the
*session*: which conversation was open, which panel, what was being worked on.

`PUT /api/continuity` checkpoints a device's session;
`GET /api/continuity/{device_id}` resumes the most recent session from **any
other** device.

---

## What is *not* built

| Asked for | Status |
| --- | --- |
| Project-wide code understanding, test running, CI monitoring, Git status/diffs, commit messages (#31–50) | **Not built.** The coding agent reads files and explains errors, but there is no repo indexer, test runner integration or Git surface |
| Email thread summarisation, response prioritisation, duplicate contact detection, "catch me up" (#71–80) | **Not built.** A mailbox can now be connected (Email panel, IMAP with an app password); these features on top of it are not built yet |
| Meeting note generation from live content, action-item extraction (#86–90) | **Not built.** `meeting_prep` gathers context and drafts an agenda; capturing meeting content needs platform integration and consent handling |
| Scheduling optimiser, "protect my free time" / "focus day" / "light day" modes (#61, 65–68) | **Partial.** Conflict detection, free blocks, deadline risk and focus sessions exist; automatic reorganisation of flexible commitments does not |
| Visual memory browser (#51) | **Partial.** Full REST surface and the existing Memory panel; no dedicated graph view |

Everything above the line is implemented and covered by tests. **178 tests
pass.**
