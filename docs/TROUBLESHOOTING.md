# Troubleshooting

Start here: **`http://127.0.0.1:8000/api/health/full`**, or ask JARVIS
*"run diagnostics"*. It checks every subsystem, the network, the AI providers,
voice and every integration, and tells you which one is unhappy.

Logs are in `data/logs/jarvis.log` (structured JSON, credentials scrubbed) and
in the terminal.

---

## Startup

### "No AI provider could handle the request"

The message names the real cause. The usual ones:

**`Anthropic auth failed: 401 invalid x-api-key`** — the key in `.env` is
wrong, expired, or has a stray quote/space. Get a fresh one at
<https://console.anthropic.com> and restart the backend.

**`Ollama model 'x' is not pulled`** — install a local model:

```bash
ollama pull llama3.2
```

JARVIS auto-selects whichever model you have. Pin one with
`JARVIS_OLLAMA_MODEL=` if you prefer.

With no cloud key and no Ollama, conversation cannot work — but tasks, the
scheduler, monitoring and every non-AI tool still do.

### Port already in use

```bash
# Windows
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

Or move it: `JARVIS_PORT=8010` and `JARVIS_FRONTEND_PORT=3010` in `.env`.

### `ModuleNotFoundError`

```bash
pip install -r requirements.txt
```

If you use a virtual environment, make sure it's the same interpreter
`start.py` runs — it uses `sys.executable`, so activate the venv first.

### The frontend won't start

```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
```

Needs Node 18+ (`node --version`).

---

## Interface

### "This browser doesn't support WebGL2"

Genuine on very old browsers. If your browser *is* modern, hardware
acceleration is probably off:

- Chrome/Edge: Settings → System → *Use graphics acceleration when available*
- Firefox: `about:config` → `webgl.disabled` must be `false`
- Check <https://get.webgl.org/webgl2/>

Everything except the field works regardless.

### The field is choppy

It self-corrects: below ~34 fps it halves the particle count and drops
connections. To force it, Settings → Particle field → Quality → **Low**, or
turn off **Connections**. **Reduced motion** calms everything without losing
the state signalling.

### Nothing updates live

The WebSocket dropped. The identity rail shows `LINK LOST — RECONNECTING` and
reconnects with backoff. If it never recovers, the backend probably died —
check the terminal. A proxy or VPN that mangles WebSocket upgrades can also do
this.

### Blank page

Open the browser console. If you see `Failed to fetch /api/...`, the backend
isn't up: `curl http://127.0.0.1:8000/api/health`.

---

## Voice

### Nothing is spoken

1. Settings → Voice → **Voice enabled**.
2. Browsers block audio until you interact with the page — click once.
3. Server voice needs `edge-tts` (`pip install edge-tts`) and an internet
   connection. Switch **Speech engine** to *Browser* to work offline.

### The microphone button does nothing

Web Speech recognition is Chrome/Edge only. Firefox and Safari have no
equivalent — the interface says so instead of failing silently. Grant the
microphone permission (padlock icon in the address bar). On a non-localhost
host, browsers require HTTPS for microphone access.

### The wake word doesn't trigger

Enable it in Settings → Voice. It re-arms continuous recognition every few
seconds by design, which browsers permit only while the tab is focused-ish. For
true always-on hotword detection, add a Picovoice key and run the desktop
service.

---

## Tasks

### A task is stuck in `queued`

- Workers may all be busy — check `/api/status` → `tasks.workers`.
- It may be waiting on a dependency (`waiting` status).
- It may be in retry backoff — `eta` shows when it resumes.
- Diagnostics reports the task engine as `offline` if all workers died; the
  health monitor restarts it automatically (up to three attempts).

### A task failed and retried forever

It doesn't — retries are capped by `max_retries` (default 3) with exponential
backoff, then it fails permanently with the error recorded. Open the task's log
in the Tasks panel for the full trace.

### Tasks vanished after a restart

They didn't. Anything left `running` is requeued and resumes from its last
checkpoint; anything whose type isn't safely resumable is marked failed with
that reason. Look for *"Startup recovery"* in the activity log.

---

## Permissions and confirmations

### "Permission X is disabled"

Working as intended. Settings → Permissions. Destructive, outbound, financial
and physical-security capabilities ship **off**.

### JARVIS says it can't do something you know it can

If a permission is off, the tool isn't even offered to the model. Turn it on
and ask again.

### A confirmation dialog never appeared

Confirmations are risk-tiered. `low` and `medium` run immediately by default;
`high` and `critical` always ask. Change the policy under the
`confirmation_policy` setting.

### A confirmation timed out

After 180 seconds an unanswered prompt is treated as a **refusal** — never as
consent. The action was not performed. Ask again.

---

## Data

### Where is everything?

```
data/jarvis.db          everything (SQLite, WAL mode)
data/logs/jarvis.log    structured logs
data/trash/             recoverable deleted files
data/screenshots/       captured screenshots
backups/                verified database backups
```

### Recovering a deleted file

Deletion moves files to `data/trash/`. Ask JARVIS to *"show me the trash"*, or
`GET /api/files/trash`. Nothing is erased outright.

### Backup and restore

Ask *"back up now"*, or `POST /api/backups`. Backups are verified with
`PRAGMA integrity_check` before being reported as successful — a backup that
fails verification fails the task rather than lying.

To restore: stop the backend, copy the backup over `data/jarvis.db`, delete any
`-wal`/`-shm` files beside it, and start again.

### Database is locked

Only one backend should run at a time. Check for a stray process:

```bash
netstat -ano | findstr :8000
```

---

## Security

### Locking down a shared machine

```bash
JARVIS_AUTH_MODE=password
JARVIS_PASSWORD=something-long-and-random
```

Restart. `local` mode already rejects non-loopback connections, but on a shared
machine any local user is "you".

### Exposing it to your phone

`JARVIS_HOST=0.0.0.0` **plus** `JARVIS_AUTH_MODE=password`, and add your
machine's LAN origin to `JARVIS_CORS_ORIGINS`. Do not do the first without the
second. Better still, use a VPN or an SSH tunnel and leave it on loopback.

### Reducing what JARVIS can reach

- `JARVIS_FILE_ROOTS` — restrict the filesystem sandbox to specific folders.
- Settings → Permissions → **Revoke all**, then re-enable only what you need.
- Settings → Privacy → **Privacy mode** — local model only, memory paused, no
  external integrations.

---

## Still stuck

```bash
python -m pytest              # 86 tests; failures localise the problem fast
JARVIS_LOG_LEVEL=DEBUG python -m backend
curl http://127.0.0.1:8000/api/system/observability
```
