# API

Base URL `http://127.0.0.1:8000/api`. Interactive docs at `/api/docs` while
running.

## Authentication

Two modes, set by `JARVIS_AUTH_MODE`.

**`local` (default).** Requests from this machine are trusted and transparently
issued a real session; the token comes back as an `httpOnly` cookie and in the
`X-Jarvis-Token` response header. Non-loopback requests still get `401`.

**`password`.** Everyone authenticates:

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"your-password"}'
```

Then send `Authorization: Bearer <token>`, `X-Jarvis-Token: <token>`, or the
cookie. Only the SHA-256 hash of a token is stored, so a copied database cannot
be replayed against a live server.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/login` | Start a session |
| POST | `/auth/logout` | End this session |
| POST | `/auth/logout-all` | Revoke every session |
| GET | `/auth/me` | Current user + non-secret config |
| GET | `/auth/sessions` | Active sessions |

---

## Conversation

### `POST /ai/chat` — streaming

Returns `text/event-stream`. Each line is `data: {json}`, terminated by
`data: [DONE]`.

```json
{"message": "what's my CPU doing?", "conversation_id": null, "stream": true}
```

Event types:

| `type` | Fields | Meaning |
| --- | --- | --- |
| `start` | `conversation_id` | Turn accepted |
| `delta` | `text` | Streamed text |
| `tool_start` | `tool`, `input` | A tool is about to run |
| `tool_end` | `tool`, `ok`, `summary` | It finished (`ok` is truthful) |
| `final` | `text`, `conversation_id` | Complete answer |
| `error` | `error` | Failed — no partial success is implied |

### `POST /ai/ask` — non-streaming

Same body; returns `{text, conversation_id, tools_used}`. Convenient for
scripts and a mobile companion.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/ai/conversations` | List conversations |
| GET | `/ai/conversations/{id}` | Full transcript |
| DELETE | `/ai/conversations/{id}` | Delete a conversation |
| GET | `/ai/tools` | Every tool, its permission, risk and whether it is enabled |
| GET | `/ai/providers` | Provider availability, local models, model routing |
| GET | `/ai/usage?hours=24` | Requests, tokens and estimated cost by model |
| GET | `/commands` | Command history (`?favourites_only=true`, `?search=`) |
| PATCH | `/commands/{id}` | Pin, favourite, label |

---

## Tasks

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/tasks` | List (`?active_only=true`, `?status=`, `?project_id=`) plus engine stats |
| POST | `/tasks` | Create — `{name, type, params, priority, depends_on}` |
| GET | `/tasks/types` | Available task types and their parameters |
| GET | `/tasks/{id}` | Full record plus its log |
| POST | `/tasks/{id}/pause` | Pause (checkpoint kept) |
| POST | `/tasks/{id}/resume` | Resume from checkpoint |
| POST | `/tasks/{id}/cancel` | Cancel safely |
| POST | `/tasks/{id}/retry` | Clear backoff and requeue |
| POST | `/tasks/{id}/prioritise?priority=high` | Reprioritise |
| POST | `/tasks/control/stop-all` | Emergency stop |

```bash
curl -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"name":"Research local LLMs","type":"research",
       "params":{"query":"local LLM inference in 2026","depth":"standard"},
       "priority":"normal"}'
```

Priorities: `critical`, `high`, `normal`, `low`, `background`.

---

## Schedules

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/schedules` | List plus stats |
| POST | `/schedules` | Create |
| PATCH | `/schedules/{id}` | Update |
| DELETE | `/schedules/{id}` | Delete |
| POST | `/schedules/{id}/run` | Fire now |
| GET | `/schedules/validate/cron?expression=0+8+*+*+1` | Validate and preview the next run |

```json
{
  "name": "Morning briefing",
  "kind": "cron",
  "cron": "0 8 * * 1-5",
  "action": {"kind": "create_task", "type": "briefing", "name": "Daily briefing"}
}
```

`kind` is `once` (needs `run_at`), `interval` (needs `interval_seconds`) or
`cron` (needs `cron`).

---

## Automations

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/automations` | List, each with a plain-English preview |
| GET | `/automations/schema` | Valid triggers, conditions, actions, task types, tools |
| POST | `/automations` | Create |
| POST | `/automations/preview` | Render in English **without** creating |
| PATCH | `/automations/{id}` | Update (including `enabled`) |
| DELETE | `/automations/{id}` | Delete |
| POST | `/automations/{id}/run` | Run manually |
| GET | `/automations/{id}/runs` | Run history with skip/failure reasons |

```json
{
  "name": "Hot GPU warning",
  "trigger": {"kind": "metric", "metric": "gpu_temp", "op": "above", "value": 84},
  "conditions": [{"kind": "not_sleeping"}],
  "actions": [
    {"kind": "notify", "title": "GPU running hot", "priority": "high"},
    {"kind": "speak", "text": "Your GPU temperature is elevated."}
  ],
  "cooldown": 900
}
```

---

## System

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | State, presence, health, task stats, boot log |
| GET | `/health` | Unauthenticated liveness probe |
| GET | `/health/full` | Deep check: network, providers, voice, integrations |
| GET | `/boot` | The startup sequence |
| GET | `/system/metrics` | Latest sample plus the recent ring |
| GET | `/system/metrics/history?hours=6` | Downsampled history |
| GET | `/system/info` | Static machine information |
| GET | `/system/processes?sort_by=cpu` | Top processes |
| GET | `/system/observability` | Latency, queue depth, DB size, integrity |
| GET | `/activity` | Activity feed (`?level=`, `?source=`, `?search=`) |
| GET | `/audit` | Security audit trail |

---

## Presence, data, configuration

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/presence`, `/presence/sleep`, `/presence/wake`, `/presence/toggle` | Sleep and wake |
| GET/POST | `/confirmations`, `/confirmations/{id}` | Pending confirmations; `{"approved": true\|false}` |
| GET/PUT | `/settings`, `/settings/{key}` | Runtime preferences |
| GET | `/themes` | The eight presets plus which is active |
| POST | `/themes/{key}/apply` | Apply a palette, preserving display preferences |
| GET/PUT | `/permissions`, `/permissions/{key}` | Capability toggles |
| POST | `/permissions/revoke-all` | Disable everything |
| GET/POST | `/integrations`, `/integrations/{key}/refresh\|sync\|disconnect` | Integrations |
| GET/POST | `/modes`, `/modes/{key}/activate` | Modes |
| GET/POST/DELETE | `/memory` | Inspect, add and delete memory |
| GET/POST | `/notifications` | Notification centre |
| GET/POST | `/calendar` | Calendar |
| GET/POST | `/contacts`, `/projects` | People and projects |
| GET | `/files/list\|search\|read\|trash` | Sandboxed file access |
| GET | `/search?q=` | Universal search across everything |
| GET | `/export?include=all` | Download all local data as JSON |
| GET/POST | `/backups` | Verified database backups |
| DELETE | `/data/{category}` | Permanently delete a category |

---

## WebSocket

`ws://127.0.0.1:8000/api/ws` — in `password` mode append `?token=...`.

```json
{"type": "task.progress", "data": {...}, "ts": "2026-08-13T18:00:00Z", "source": "tasks"}
```

Narrow the stream (useful for a phone that doesn't want 3-second metrics):

```json
{"type": "subscribe", "topics": ["task.*", "notification.new", "jarvis.state"]}
```

Principal event types: `jarvis.state`, `jarvis.sleep`, `jarvis.wake`,
`conversation.message`, `conversation.delta`, `conversation.tool.start|end`,
`task.created|started|progress|log|completed|failed|cancelled|paused|resumed`,
`schedule.fired`, `automation.fired`, `system.metrics`, `system.health`,
`system.alert`, `notification.new`, `confirmation.requested|resolved`,
`activity.new`, `mode.changed`, `integration.changed`, `calendar.upcoming`,
`boot.step`, `boot.complete`.

Any of these can be an automation trigger.

---

## Errors

Failures return a JSON body with `detail`. Status codes are used honestly:
`400` bad request, `401` not authenticated, `403` permission denied, `404` not
found, `503` a service or provider is unavailable. A `503` from `/ai/chat`
means no provider could handle the request — not that the answer was empty.
