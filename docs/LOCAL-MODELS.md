# Running on local models

JARVIS runs on [Ollama](https://ollama.com). No API key, no account, no network
call to a model provider. This is the default and does not need configuring.

```bash
# .env
JARVIS_AI_PROVIDER=ollama
```

---

## What "local only" actually means here

It is not a flag checked before each request — those get forgotten. In
local-only mode the cloud provider is **never constructed**:

```python
if not settings.local_only and settings.ANTHROPIC_API_KEY:
    self.providers["anthropic"] = AnthropicProvider(...)
```

`self.providers` contains exactly one entry, `ollama`. There is no object to
call, so no code path — present or future — can reach a cloud API by accident.
You can confirm it from outside:

```bash
curl -s http://127.0.0.1:8000/api/ai/providers
```

```json
{"providers": {"ollama": {"available": true, "name": "ollama"}}}
```

Three places that could otherwise have leaked are pinned explicitly:

| Path | Behaviour |
| --- | --- |
| Conversation and tools | `_provider_order()` returns `["ollama"]` before privacy settings are consulted |
| Screen vision | Local vision model, or an honest "no vision model installed" |
| Self-editing | Forced to Ollama even if `JARVIS_SELFEDIT_PROVIDER=anthropic` is left over — your source code is exactly what must not leak |

That last one has a test of its own.

---

## Choosing models

Leave the routing blank and JARVIS picks per purpose from what you have
installed, using the capabilities Ollama reports through `/api/show` — not a
hardcoded list of names:

```bash
ollama pull llama3.2     # conversation, reasoning, coding, tools  (~2 GB)
ollama pull gemma3       # vision — screenshots, OCR, diagrams     (~3 GB)
```

With those two installed, routing resolves to:

| Purpose | Model |
| --- | --- |
| conversation, fast, reasoning, coding | `llama3.2:latest` |
| vision | `gemma3:latest` |

Override any of them in `.env` (`JARVIS_OLLAMA_CODING=qwen2.5-coder`, etc.).

### Why the preference order is what it is

Model choice was measured, not assumed. Tool calling is the thing that actually
breaks: a model that chats well but emits malformed tool calls makes JARVIS
useless, because almost everything it does is a tool call.

- `llama3.2` and `qwen3` produced valid tool calls 2/2.
- `mistral` produced 0/2 despite advertising `tools`.
- `deepseek-r1` spends its output budget inside `<think>` and returns empty
  content, so "thinking" models are penalised for interactive purposes.
- `phi3` reports no tool support at all and is only used where tools aren't
  needed.

So the router ranks by family reliability first, size second, and skips models
whose advertised capabilities don't match the purpose.

---

## What local models do less well

Stated plainly, because the alternative is you discovering it yourself:

- **Small models over-call tools.** Asked "reply with exactly: LOCAL OK",
  llama3.2 called `check_notifications` first. Answers stay correct; the route
  there is noisier than a frontier model's.
- **Long multi-step plans drift.** Goal decomposition and deep research work,
  but produce shorter, blunter output than a large cloud model would.
- **Self-editing is the weakest use.** `llama3.2` is a 3B model writing
  production code. It is guarded — separate model, diff, syntax check,
  confirmation, backup, one-command rollback — but pull a code-specialised
  model if you intend to use it seriously:

  ```bash
  ollama pull qwen2.5-coder
  ```

- **First call after boot is slow** while Ollama loads the model into memory.
  Subsequent calls are fast. A plain question answered in ~8s on this machine.

Arithmetic JARVIS must not get wrong — milestone dates, free calendar blocks,
conflicts, resource numbers — is computed in Python and only phrased by the
model, precisely so a smaller model cannot corrupt it.

---

## Using a cloud model instead

```bash
# .env
JARVIS_AI_PROVIDER=auto
ANTHROPIC_API_KEY=sk-ant-...
```

Cloud first, Ollama as automatic fallback, and the switch is announced in the
activity log rather than happening silently. Privacy mode still forces local at
any time, without a restart.

---

## Checking it

```bash
curl -s http://127.0.0.1:8000/api/health/full
```

`ai.status` is `online` when Ollama is reachable — local-only is a complete
configuration, not a degraded one. If Ollama is not running, the status is
`offline` and names it, rather than quietly producing nothing.

The boot banner says which mode you are in:

```
AI CORE          ONLINE      0ms  (local only via Ollama (auto-selected per purpose))
```

---

## How much conversation the model sees

Two separate limits were cutting this short, and only one of them was visible
in this codebase.

**The visible one.** History was the last 48 messages, flat. In a conversation
that uses tools that is barely a dozen exchanges, because every tool call and
every tool result is a message of its own.

**The invisible one.** Ollama does not use a model's full context unless it is
asked to. Left alone it applies its own modest default and silently drops
everything past it. qwen2.5 advertises 32768 tokens and was effectively being
given a fraction of that -- so sending more history would not have helped at
all: it was arriving and being thrown away at the far end.

Both are fixed. `JARVIS_OLLAMA_NUM_CTX` is now sent on every request, and
history is trimmed to what actually fits:

```
  context window        16384 tokens   (JARVIS_OLLAMA_NUM_CTX)
  - reply allowance      4096          (JARVIS_MAX_TOKENS)
  - prompt and tools     1800          (measured at ~800; the rest is headroom)
  = conversation        10488 tokens   ~42000 characters
```

Trimming works from the oldest end, so what survives is the most recent
exchange rather than an arbitrary prefix, and `_repair_tool_pairs` keeps tool
calls and their results together.

Verified by conversation rather than by assertion: a fact given at the start
of a thirteen-turn conversation is still recalled correctly at the end.

### The cost

The key/value cache grows with the window -- roughly 0.9 GB at 16384 for a 7B
model, on top of the model itself. Tested on this machine at 8192, 16384 and
32768; all three work, with 3.6 GB to spare at 16384. Lower it if memory is
tight, raise it (up to the model's own maximum) for a longer memory.

### Why the tool list is narrowed

The full set of 107 tool schemas is about 9,300 tokens -- more than Ollama's
default window on its own, before a single word of conversation. The
orchestrator sends a relevance-ranked slice of 8 instead, which measures
around 500 tokens. That is not only a context saving: a 7B model given 84
tools stops calling tools at all and narrates instead.
