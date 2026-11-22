# The Intelligence Layer

Update 1 built the command centre: something you open and tell what to do.
This layer makes JARVIS ambient — it watches the state of your computer,
understands what you're doing, learns your habits, runs long work
autonomously, and offers help when you allow it.

Everything here is **local**, **inspectable** and **individually switchable**.
Settings → Awareness.

---

## Ambient awareness

`backend/intelligence/awareness.py`

Polls the focused window every 3 seconds: which application, its title, how
long you've been there, and whether you've gone idle. That becomes:

- **Context for every answer.** "What's this error?" resolves against the app
  you're actually in. The system prompt carries a line like
  `On screen right now: Code.exe — "orchestrator.py" (development), focused for 24 minutes.`
- **Usage statistics.** Where your time actually went, by app and by category.
- **Hang detection.** Three consecutive non-responses from a window before it's
  reported, so a slow frame isn't called a crash.
- **Performance advice.** Not generic tips — "Memory is at 91%, Chrome is
  holding 3.2 GB, close it" with a button that does.

Idle (3 minutes without input) suspends tracking entirely.

**Privacy:** window titles are the only text captured. They never leave the
machine unless you include them in a question. One switch turns it all off.

---

## Learning

`backend/intelligence/patterns.py`

Commands are fingerprinted (`"JARVIS, could you open Spotify"` and
`"open spotify"` collapse to one pattern), counted, and stamped with the hour
and weekday. Consecutive commands within 10 minutes record a transition.

That gives three things:

- **Frequency** — what you actually use, versus what you asked once.
- **Time anchoring** — commands you run around this hour, on this weekday,
  that you haven't run recently.
- **Sequences** — what usually follows what.

No embeddings and no model. For one person's command history, counting is both
sufficient and *explainable* — every prediction shows its evidence:
`"You've run this 14 times, 78% of them around 09:00, and last 26h ago."`

**Personal vocabulary.** Tell JARVIS "the site means my portfolio at example.dev"
and later commands using that shorthand carry the meaning.

Rows are visible and deletable in Settings → Awareness → Learned habits.

---

## Proactive suggestions

`backend/intelligence/proactive.py`

**Off by default.** This one is opinionated, so it must be chosen deliberately.

Four generators run every two minutes:

| Generator | Fires on |
| --- | --- |
| routine | a time-anchored command you're overdue for |
| performance | real resource pressure with an identified cause |
| meeting | an event 12–75 minutes away with no prep task |
| recovery | a task that failed within the hour |

Rules it holds itself to:

- **Suggests, never acts.** An offer with a Yes / Not now / Why? — never
  something already done.
- **One at a time.** The best candidate above 0.45 confidence, not four
  mediocre ones.
- **Rate limited** (3/hour by default), silent in quiet modes and during sleep.
- **Never repeats** something you dismissed, for 20 hours.
- **Always shows its reasoning** behind the "Why?" button.

---

## Vision

`backend/intelligence/vision.py`

Screenshot → downscale to 1400px → vision model, with six task modes:

`describe` · `error` · `ocr` · `elements` · `diagram` · `troubleshoot`

Plus `compare`, for before/after pairs.

Three gates, all of which must pass: the `system.screenshot` permission, the
Awareness → Screen vision switch, and a confirmation prompt naming exactly what
will be captured and why. Privacy mode refuses outright — screenshots are not
sent to a cloud model when it's on, and the reason is stated rather than
silently failing.

Every capture is recorded in the observations table, and screenshots are pruned
aggressively (7 days by default) with the files deleted alongside the rows.

---

## Adaptive personality and transparency

`backend/intelligence/reasoning.py`

**Style adapts** to observable state, appended to the system prompt each turn:
coding → precise and technical; gaming → extremely brief, no long tasks; late
at night → calm and short; 45 minutes of unbroken focus → don't volunteer
distractions; two failed tasks in half an hour → be upfront, not optimistic.

**Confusion is detected** from re-asked questions, corrections and frustration
markers. When it fires, the instruction is explicit: *do not repeat the same
explanation in the same shape* — change approach, use a concrete example, or
ask one focused question.

**Ambiguity is caught** before acting. A short command built from referential
words ("delete that") is flagged, and the resolution context — what's on
screen, active tasks, next events — is supplied so JARVIS resolves it rather
than guessing. Vague *and* destructive means it must ask.

**"Why did you do that?"** answers from a real decision log — what was decided,
the rationale, the inputs, and the alternatives considered — cross-referenced
with the tool calls and tasks that actually ran. Not a plausible-sounding
reconstruction after the fact.

---

## Goals — agent 2.0

`backend/intelligence/goals.py`

State an outcome. JARVIS decomposes it into up to 8 concrete steps, works out
the dependency graph, assigns each step a specialist agent, and runs them
through the existing task engine — so every step inherits checkpointing,
retries, cancellation and crash recovery.

The plan is written to the goal record **before anything runs**, so you can see
it and cancel first. `plan_only: true` shows the decomposition without
executing.

Independent steps run in parallel; dependent ones wait. Goal status is derived
from its tasks, so it can't drift out of sync.

```
"figure out why my builds got slow and write it up"
  1. Search        (researcher)
  2. Read logs     (files)      depends on: —
  3. Diagnose      (system)     depends on: 1, 2
  4. Write summary (general)    depends on: 3
```

---

## Planning

`backend/intelligence/planning.py`

The arithmetic is computed deterministically; the model only phrases it. That
distinction matters — a planner that invents a free slot you don't have is
worse than no planner.

- **Conflicts** — genuine calendar overlaps, with the overlap in minutes.
- **Free blocks** — gaps in the working day, merged across double-bookings,
  filtered to lengths that are actually useful.
- **What next** — one recommendation with reasoning, prioritising imminent
  meetings → failed tasks → near deadlines → free time.
- **Deadline risk** — open work versus remaining free capacity, not a vibe.
- **Daily / weekly plans**, **focus sessions**, **end-of-day summary**,
  **weekly productivity report**.

If the model is unreachable, these fall back to plain rendered facts rather
than returning nothing.

---

## Environment triggers

`backend/intelligence/watchers.py`

New automation triggers, published to the same event bus the automation engine
already listens to:

`file.changed` · `app.launched` · `app.closed` · `app.crashed` ·
`network.online` · `network.offline` · `awareness.idle` · `awareness.active`

Watch a folder with `POST /api/awareness/watch`. The first scan establishes a
baseline, so you don't get told every existing file is new.

---

## Gaming

`backend/intelligence/gaming.py`

Detects a game in the foreground (known list, plus a conservative heuristic for
unlisted ones), distinguishing a launcher in the tray from an actual game.
Tracks playtime per title, samples GPU/CPU during the session, optionally
switches to gaming mode and back, and writes a session summary.

**FPS is not claimed.** Measuring frame rate needs overlay-level
instrumentation this build doesn't ship, so the report gives GPU load, GPU
temperature and CPU — and says plainly that frame rate is unavailable rather
than inventing a number.

---

## Connected devices

`backend/services/devices.py`

One machine runs the backend; other devices pair with it and share the same
brain — one task list, one memory, one history.

Pairing is a 6-digit code shown on the host and typed on the device, valid for
5 minutes and single-use. The device gets a token whose hash alone is stored.

Devices carry **their own** permission set, narrower than the host's, so a
phone can ask questions and read status without being able to shut down the
desktop. Default grant is `ask`, `tasks.read`, `status`, `handoff` — never
`control`.

Handoff is a claim-once queue: "send this to my PC" waits until that device
next connects.

---

## Running entirely on Ollama

Every intelligence feature works without a cloud key. What that took, and what
was measured rather than assumed:

**Capability-aware routing.** Ollama reports per-model capabilities via
`/api/show`, so JARVIS derives its local routing instead of guessing:

| Purpose | Chosen on this machine | Why |
| --- | --- | --- |
| conversation / fast / coding / reasoning | `qwen2.5` | reliable tool calling |
| vision | `gemma3` | the only model reporting `vision` |

**Vision falls back automatically.** A rejected cloud key isn't a dead end —
the request retries against the local vision model, and the response says which
model actually looked (`provider`, `model`, `local`). In privacy mode the local
model is used *first*, and if none is installed it says so and names the fix
(`ollama pull gemma3`) rather than silently refusing.

**Thinking models are avoided, not preferred.** `deepseek-r1` (8.2B) looks like
the best local model on paper. In practice it spent its entire token budget
inside `<think>` and returned *empty* content, and narrated tool use instead of
calling tools. The scorer now penalises the `thinking` capability, and if a
model does return only reasoning, that text is surfaced with a warning instead
of handing back a blank string.

**Family rank beats parameter count.** Measured against the real tool set:

| Model | Size | Tool calls emitted |
| --- | --- | --- |
| `llama3.2` | 3.2B | 2/2 |
| `qwen3` | 2.0B | 2/2 |
| `mistral` | 7.2B | **0/2** — narrated "I will call system_status", then invented the numbers |

So the preference order is ordered by observed reliability, not size. A smaller
model that actually calls tools beats a larger one that hallucinates.

**Local models get a smaller prompt and fewer, terser tools.** The full system
prompt plus 84 verbose tool schemas stops small models calling tools at all.
Local requests get a ~500-character prompt and up to 8 relevance-ranked
schemas, stripped of usage examples and per-property prose.

**Known limitation:** a 3B model reads structured tool output imprecisely. With
the tightened prompt it now reports the right figures (verified: 7.7 GB free
disk, 99.5% memory against ground truth), but it can still attach the wrong
label to a secondary number. A cloud key removes this entirely; it is a model
capability limit, not a wiring bug.

---

## What is *not* built

Being specific, because the update spec was ambitious:

| Asked for | Status |
| --- | --- |
| Speaker identification | Not built — needs a speaker-embedding model |
| FPS monitoring | Not built — needs overlay instrumentation; GPU/temp provided instead |
| Camera input, QR codes, object recognition | Not built — the vision path exists and would take these, but no camera capture is wired |
| Visual drag-and-flow automation builder | Not built — automations are created conversationally and via API; the editor is a list, not a canvas |
| Whisper/low-volume recognition, voice undo, per-mode voices | Not built — browser speech recognition doesn't expose the needed control |
| mDNS device auto-discovery | Not built — pairing is by code and address; discovery would be the next step |
| Loops and wait conditions in automations | Partial — `sequence` and conditional branching exist; loops do not |

Everything in the tables above the line is implemented and covered by tests.

---

## Choosing which tools to offer

A local model gets 8 of the 115 tools, picked by keyword overlap, because a
7B model given the full set stops calling tools at all and narrates instead.

That selection had a failure which looked exactly like the model being dim:

```
"list unofficial ps5 emulators"
  -> list_apps, list_automations, list_folder, list_processes,
     list_schedules, list_tasks, create_task, devices
```

Every slot went to a tool whose name begins with `list_`, on the strength of
the word "list". There was no web search in the set, so JARVIS answered
*"I don't have that on this computer. Let me check the web for you."* -- and
then didn't, because it had nothing to check it with. "What are the best X"
failed the same way through `what_am_i_doing` and `what_should_i_do_next`.

Three changes:

1. **Scaffolding words are ignored.** `list`, `what`, `show`, `find`, `tell`,
   `best` and friends describe how a request is phrased, not what it is
   about, and they were deciding the entire selection by matching tool names.
2. **`web_search` and `read_webpage` are always offered.** Looking something
   up is the universal fallback and cannot win a keyword contest: a question
   about the outside world rarely contains the word "search". Reading a page
   comes with it, because finding a link and not being able to open it is no
   better than not searching at all.
3. **Singulars and plurals match.** "the *file* called invoice" now reaches
   `find_*files*`, which exact matching missed.

The result, measured:

| Asked | Tool called |
| --- | --- |
| "list unofficial ps5 emulators" | `web_search` |
| "who won the 2026 world cup" | `web_search` |
| "what is the weather in Pittsburgh" | `weather` |
| "what is the capital of japan" | none -- answered directly |
| "what is 23 times 4" | none -- answered directly |

It still answers from its own knowledge when it has it, which is the other
half of the problem: a model that searches for "what is 23 times 4" is as
broken as one that cannot search at all.

### Search region

`JARVIS_SEARCH_REGION` defaults to `us-en`, not DuckDuckGo's "no region"
code `wt-wt`. The search library turns the language half of the region into a
Wikipedia subdomain, and `wt.wikipedia.org` does not exist -- so every single
search opened with a DNS lookup that could never resolve. With a real region
Wikipedia answers 200 and becomes a useful source, and the search is about
twice as fast.

---

## What it heard

An always-on microphone should be able to show its work.

The pieces were already being recorded -- a detection logs its confidence, a
transcription logs the text -- but they sat scattered through a general
activity feed among schedule ticks and health checks, which is no use to
someone asking the only two questions that matter: *is it listening to me,
and what does it think I said?*

`GET /api/voice/log` pairs them: every moment the wake word fired, with the
command that followed or an honest note that nothing did. The panel puts the
confidence next to each line, because a low number beside a strange
transcript is the whole story:

```
 98%  list the prices of vision from the Avengers Tower LEGO set.
 88%  Jivess Name one fruit            <- garbled, and the score says so
 99%  (nothing followed)
 83%  Open quad.
 77%  Is there a PS5 emulator for Android?
```

No audio is stored, and this does not change that. It is a record of
decisions, not a recording.

### The ordering trap

Activity rows arrive newest first, and a transcript is logged *after* the
detection it belongs to -- so in that order the detection appears **later**
in the list, not earlier. Pairing forwards hands each transcript to the next
detection up, which is a different, newer utterance. The first version did
exactly that, and reported one command against someone else's detection
before the real data showed it up. `pair_voice_events` holds a transcript
until its detection arrives, and only pairs them if they are within a minute
of each other.
