# Architecture

## Shape

```
┌─ Browser ─────────────────────────────────────────────────────────────┐
│  WebGL particle field   Conversation   Command bar   Panels           │
│  Web Speech API (STT + TTS)                                           │
└───────────┬──────────────────────┬────────────────────┬───────────────┘
            │ REST                 │ SSE (chat stream)  │ WebSocket (events)
┌───────────▼──────────────────────▼────────────────────▼───────────────┐
│  FastAPI                                                              │
│  ┌──────────────┐  ┌────────────────┐  ┌───────────────────────────┐  │
│  │ Orchestrator │──│ Tool registry  │──│ Permissions/Confirmations │  │
│  │  routing     │  │  64 tools      │  │  + audit trail            │  │
│  │  fallback    │  └───────┬────────┘  └───────────────────────────┘  │
│  │  streaming   │          │                                          │
│  └──────┬───────┘          │                                          │
│         │          ┌───────▼──────────┬──────────────┬─────────────┐  │
│  ┌──────▼──────┐   │ Task engine      │ Scheduler    │ Automations │  │
│  │ Anthropic   │   │ workers,         │ cron/        │ trigger →   │  │
│  │   ↓fallback │   │ checkpoints,     │ interval/    │ conditions  │  │
│  │ Ollama      │   │ retries, recovery│ one-shot     │ → actions   │  │
│  └─────────────┘   └──────────────────┴──────┬───────┴─────────────┘  │
│                                              │                        │
│  Event bus ──────────────────────────────────┴──► WebSocket hub       │
│  Monitoring · Health · Integrations · Files · Calendar · Presence     │
└──────────────────────────┬────────────────────────────────────────────┘
                           │
                    SQLite (WAL) — data/jarvis.db
```

## Principles

**Local first.** The database, the task queue, conversations, memory, logs and
settings never leave the machine. External calls happen only for the AI
provider you configured and integrations you connected.

**The event bus is the spine.** Every subsystem publishes to it; the WebSocket
hub, the automation engine and the activity log subscribe. Adding an event
makes it automatically available as an automation trigger and as a live UI
update — no extra wiring.

**Nothing is claimed that didn't happen.** Tool results carry a real `ok` flag.
Confirmations block. Integrations report exactly which credentials are missing.
The AI's system prompt makes honest reporting a hard rule.

**Least privilege by default.** Delete, power, email, calls and physical
security all ship disabled.

---

## Request path: a message

1. `POST /api/ai/chat` opens a server-sent event stream.
2. The orchestrator persists the user turn, sets state → `thinking` (the
   particle field converges and speeds up), and builds a system prompt from
   personality settings, retrieved memory and live context (time, active mode,
   running tasks, next calendar events, unread notifications).
3. It asks the provider with the tool schemas the **current permissions allow** —
   disabled tools are not offered at all.
4. Text deltas stream to the browser as they arrive.
5. `tool_use` blocks are dispatched through the registry:
   permission gate → integration check → confirmation (if the risk tier
   demands it) → run with a timeout → audit if sensitive.
6. Results are fed back and the loop continues, up to `max_tool_iterations`.
7. The final turn is persisted and state returns to `idle`.

Provider failures retry with backoff, then fall back to the next provider, and
the switch is written to the activity feed rather than hidden.

---

## Task engine

A task is a **row**, not a coroutine tied to a request.

```
queued ──► running ──► completed
   ▲          │  └───► failed        (after retries are exhausted)
   │          │  └───► cancelled
   │          └─────► paused ──┘     (checkpoint kept)
waiting                              (blocked on a dependency)
```

- **Claiming** is atomic: `UPDATE … WHERE id = ? AND status = 'queued'`. Two
  workers can never take the same task.
- **Cancellation is cooperative.** Handlers call `ctx.checkpoint_now()` at safe
  points; that raises, so cleanup runs. If a handler ignores it for 350 ms the
  engine cancels the asyncio task outright.
- **Checkpoints** are JSON written by the handler. A task that dies at step 7
  of 12 resumes at 7.
- **Retries** back off exponentially (60 s → 900 s cap) and resume from the
  checkpoint rather than restarting.
- **Recovery on boot**: anything left `running` belonged to a dead process.
  Resumable types are requeued; non-resumable ones are failed with an honest
  reason rather than silently retried.
- **Graceful shutdown** asks running tasks to pause so checkpoints survive,
  then sweeps anything still marked running back into the queue.

Handlers live in `backend/tasks/handlers/`: `research`, `agent`, `meeting_prep`,
`briefing`, `diagnostics`, `backup`, `organise_files`, `housekeeping`.

---

## Scheduler

One tick loop, three kinds: `once`, `interval`, `cron`.

The cron parser (`backend/scheduler/cron.py`) is a ~150-line implementation of
the 5-field format with `*`, `*/n`, `a,b,c`, `a-b`, `a-b/n`, named months and
days, and `@daily`-style shortcuts. It is evaluated in **local** time, because
"8am" means 8am where you are.

**Misfire handling** matters on a laptop that sleeps. Each schedule has a grace
window: inside it, a late occurrence still runs; beyond it, the occurrence is
consumed and skipped, and that is written to the activity log rather than
firing a reminder six hours late.

---

## Automations

`trigger → conditions → actions`, evaluated against the event bus.

Triggers: any event (with `prefix.*` wildcards), a metric threshold, N minutes
before a calendar event, a mode change, or manual only.

Conditions: time-of-day window, day of week, active mode, live metric
comparisons, event-field comparisons, awake-only. Any condition can be negated.

Actions are the **shared vocabulary** also used by schedules and modes:
`notify`, `speak`, `create_task`, `run_tool`, `ai`, `launch_app`, `set_mode`,
`sleep`, `wake`, `webhook`, `sequence`.

Every run is recorded as fired / skipped / partial / failed, with the reason —
so "why didn't my automation run?" is answerable.

`AutomationEngine.preview()` renders an automation in plain English, which is
what the UI shows before you enable one.

---

## The particle field

`frontend/src/gl/` — WebGL2, no 3D library.

Per frame:

1. A fullscreen quad darkens the trail buffer by `1 - trail`. This is what
   turns points into flow.
2. Connection lines (one draw call).
3. Energy rings (three line loops).
4. Particles (one draw call, `gl.POINTS`).
5. Composite to the screen with a cheap bloom and vignette.

The CPU never touches a particle position. Each particle carries a static seed
(`angle`, `radius`, `speed`, `size`) plus a depth; the vertex shader derives its
position every frame from that seed, the clock, and a handful of **continuous**
state uniforms:

| Uniform | Driven by | Effect |
| --- | --- | --- |
| `uEnergy` | thinking | orbital speed, turbulence |
| `uConverge` | listening | pull toward the core, travelling pulse |
| `uOrbit` | working | quantises radii into banded streams |
| `uAmplitude` | live audio | radial waveform |
| `uDispersion` | error | outward stagger, warm colour shift |
| `uAlpha` | sleep/offline | global fade |

Because they are eased rather than switched, every state change is a morph.
Connections are precomputed **pairs**, and the shader recomputes the live
distance each frame to fade links in and out — network behaviour with no
per-frame CPU neighbour search.

Quality adapts: particle count scales to viewport and core count, and if the
frame rate sits below ~34 fps the renderer halves the count and drops
connections automatically.

**Sleep is real.** The canvas is removed from the DOM and the GL context is
released — there is no black rectangle over a running simulation. Waking builds
a fresh context.

---

## Data model

SQLite in WAL mode, all access funnelled through one worker thread, which
removes every cross-thread connection hazard and keeps the API async.

Migrations are a versioned list in `backend/db/schema.py`. Never edit a shipped
migration — add a new one.

25 tables, notably: `tasks`/`task_logs`/`task_deps`, `schedules`, `automations`/
`automation_runs`, `conversations`/`messages`, `memory`, `permissions`,
`confirmations`, `audit_log`, `activity_log`, `metrics`, `usage`,
`calendar_events`, `projects`, `contacts`, `integrations`, `settings`.

---

## Extending

**A tool** — add a function in `backend/ai/tools/`:

```python
@registry.add(
    "my_tool",
    "What it does, written for the model to read.",
    {"type": "object", "properties": {"thing": {"type": "string"}}, "required": ["thing"]},
    permission="files.read",
    risk=Risk.MEDIUM,
    confirm_summary=lambda a: f"Do the thing to {a.get('thing')}?",
    category="custom",
)
async def my_tool(ctx, thing: str):
    return ok({"did": thing}, "Done.")
```

It is now gated, confirmable, auditable and visible to the model.

**A task type** — write `async def handler(ctx)` and register it in
`backend/tasks/handlers/__init__.py`. Use `ctx.step()`, `ctx.log()` and
`ctx.save_checkpoint()`; call `ctx.checkpoint_now()` in loops so it stays
interruptible.

**An integration** — subclass `Integration`, declare `required_env` and
`capabilities`, implement `probe()`, and register it in
`IntegrationRegistry.build()`. The settings UI renders it automatically.

**A permission** — append to `REGISTRY` in `backend/core/permissions.py`. It is
inserted on next boot without disturbing existing choices.
