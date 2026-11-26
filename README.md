# JARVIS

A personal AI command centre that runs on your own computer, powered by the AI
of your choice: bring your own API key from **OpenAI, Anthropic (Claude),
Google Gemini, Groq, Mistral, xAI, DeepSeek, OpenRouter, Together**, any
OpenAI-compatible service, or run fully local with **Ollama**.

It isn't a chat window. JARVIS can work with your files, open apps, search the
web, read and write your email, run background tasks and automations, index
your documents, build small apps while you're away, and remember what matters
to you. All of it runs on your machine, and every capability has its own switch.

The interface is a single field of GPU particles that reacts to what JARVIS is
doing: converging when it listens, accelerating when it thinks, pulsing when it
speaks.

---

## Download

The easiest way: get **JARVIS-…-windows.zip** from the
[latest release](https://github.com/Miekgames/jarvis/releases/latest), unzip
it, and double-click **JARVIS.bat**.

You need **Windows 10 or 11** and **Python 3.9+**
([python.org](https://www.python.org/downloads/); tick "Add python.exe to
PATH"). Node.js isn't needed: the interface comes already built. The first run
takes a few minutes while it installs its Python packages into a `.venv`
folder beside it; after that it starts straight away and opens in your
browser.

## Quick start (from source)

You need **Windows 10 or 11**, **Python 3.9+** and **Node.js 18+**.

```bash
git clone https://github.com/Miekgames/jarvis.git
cd jarvis
pip install -r requirements.txt
python start.py --build
```

(On Windows, `start.bat --build` in a terminal does the same.) The first run
builds the interface, starts JARVIS on http://127.0.0.1:8000 and opens your
browser.

Optional: let *While away* mode test the apps it builds in a real browser.

```bash
pip install playwright
python -m playwright install chromium
```

Without it those apps are still built, but reported as "not browser-tested".

### The setup screen

The first time it opens, JARVIS asks:

1. **Your name**, what it should be called, and how it should talk.
2. **Which AI to run on.** Pick a provider, paste your API key, press
   **Load models** (the list comes from the provider itself, so it's always
   current), choose one, and press **Test it**. That sends one tiny real
   message and shows you exactly what came back.
3. **What it may do** on your computer: *Careful*, *Recommended* or
   *Everything*. Each capability also has its own switch in Settings.
4. A summary, then **Start**.

You can come back to it any time from **Settings → AI provider → Change AI,
key or name**.

### Where to get a key

| Provider | Get a key |
| --- | --- |
| Anthropic (Claude) | https://console.anthropic.com/settings/keys |
| OpenAI | https://platform.openai.com/api-keys |
| Google Gemini | https://aistudio.google.com/apikey |
| Groq | https://console.groq.com/keys |
| Mistral | https://console.mistral.ai/api-keys |
| xAI (Grok) | https://console.x.ai |
| DeepSeek | https://platform.deepseek.com/api_keys |
| OpenRouter | https://openrouter.ai/keys (one key, many models) |
| Together AI | https://api.together.ai/settings/api-keys |
| Anything OpenAI-compatible | Choose *Other* and give its address, e.g. LM Studio or vLLM |
| Ollama (local, free) | No key: install https://ollama.com and `ollama pull qwen2.5` |

You pay your provider directly for what you use. JARVIS shows tokens, speed and
an estimated cost under each reply.

---

## Your key and your data

- **Your API key is sealed** with Windows' own per-user encryption (DPAPI)
  before it's written to disk. The file is useless to anyone not signed in as
  you on that PC. The key is **never sent back to the browser**: the interface
  only ever sees whether one is saved and its last four characters.
- On systems without built-in sealing, JARVIS refuses to write the key to a
  file. Put it in `.env` instead (`OPENAI_API_KEY=…`; see `.env.example`).
- Everything else (conversations, memories, tasks, the document index) lives in
  a local SQLite database in `data/`. Nothing is uploaded anywhere except what
  you send to the AI provider you chose, and the services you connect yourself.
- **Privacy mode** (Settings) switches to local models only.

---

## Safety

JARVIS can act on your computer, so it is built to be stopped, checked and
trusted:

- **Permission switches.** About thirty capabilities, from reading files to
  deleting them, running shell commands, sending email and power control. The
  risky ones start **off**. A switched-off capability isn't even offered to the
  model.
- **Confirmations.** Anything risky (deleting files, sending an email, running
  a command) shows you exactly what will happen and waits for your yes.
- **Unattended work never decides alone.** In *While away* mode, anything that
  would have needed your confirmation is held for you instead.
- **Prompt-injection screening.** Web pages, documents and emails are treated
  as information, never as instructions. Suspicious content is flagged and
  shown to you.
- **Audit log** of every risky action, and **Stop everything** (`Ctrl+Shift+X`).

---

## What it does

- **Conversation** with streaming replies, tool use, voice in and out, and a
  wake word ("Jarvis, …").
- **Just say it:** everything in the sidebar can be done by asking, with no
  menus. "Open my reminders", "close that", "turn on the wake word", "make
  your answers shorter", "switch to the Ember theme", "speak a bit slower",
  "while I'm away for an hour, build me a habit tracker", "stop the
  research". Plain requests like these happen at once, without waiting on the
  model. The sidebar lists what JARVIS can do and something to say for each.
- **Reminders and scheduled notifications:** "remind me at 7 to call Dad",
  "every weekday at 7:30 tell me to stretch", "in 20 minutes check the oven".
  The time is worked out from your words, not guessed by the model, and you
  see when it will go off before it's saved. Reminders also appear as Windows
  notifications, so they reach you with no JARVIS tab open.
- **Long research:** "research Switch 2 emulators for Android" spends 15 to 20
  minutes searching, reading pages and following up on what turns up, then
  reports in four paragraphs: the answer, the details, the caveats, and what's
  next, with its sources. It stays on the question asked, flags lookalike
  sites named after the topic, and if nothing new turns up it stops early and
  says so.
- **Email** (Gmail, Outlook, iCloud, Yahoo, or any IMAP/SMTP account, using an
  app password): inbox, search, read, reply. Reading never marks mail as read.
  Sending always asks first.
- **Countdowns:** up to three on the main screen, ticking in months, days,
  hours, minutes and seconds. Click one for the latest news about it. Set them
  by hand (date and time), or just ask: "countdown to the World Cup". Dates
  come from you, the calendar, or several websites that agree, never from the
  model's memory; when sources disagree, JARVIS asks you.
- **Today's facts, looked up and read:** "who's projected to win the Bears v
  Lions game?", "who won the Lakers game last night?", "latest news on the AI
  Act". JARVIS searches the day's news, reads several articles, and answers
  from what they say, naming each source (and saying so when sources
  disagree), instead of guessing or telling you to check a website. A short
  follow-up ("and the over/under?") is looked up too.
- **Skill colours:** each skill has its own colour, and the screen fades to it
  while the skill is in use: orange while researching, green for reminders.
  Ordinary replies keep the theme's own blue and violet. "Stop changing
  colours" turns it off.
- **Progress you can see:** while research or other long work runs, the
  bottom-left corner of the screen fills as a progress bar with its real
  progress.
- **Everyday lookups:** convert units ("70 F in C", "5 miles in km") and
  money ("50 dollars in euros", at the European Central Bank's published
  rate), the time anywhere ("time in Tokyo"), a Wikipedia summary, a word's
  definition (Wiktionary), and coin flips, dice and random picks. Ask in chat
  or use the Everyday panel. Every number is computed or looked up, never
  recalled by the model. Currency, Wikipedia and definitions need "Open web
  pages", which is on by default.
- **Your documents:** add folders and ask questions. Answers cite the file and
  section they came from.
- **Memory** that learns lasting facts you mention ("I'm vegetarian") without
  being asked. Each one is marked, visible and deletable.
- **Skills:** reusable instructions (`/meeting-notes`, `/email-draft`,
  `/countdown`, `/remind`, `/deep-research`, …). Seventeen are included, and
  you can write or import your own.
- **Plug-ins** via the Model Context Protocol (MCP): paste a server's config
  and its tools become JARVIS's tools.
- **While away:** give JARVIS a goal and a time span (30 minutes to a day).
  It can build a small web app, test it the way a person would, fix what it
  finds, and report honestly what works and what doesn't.
- **Tasks, schedules and automations:** background work you can pause, resume
  and cancel; cron and one-off schedules; trigger → condition → action rules.
- **System:** CPU, memory, disk, GPU and network, with alerts, and energy use
  per reply on NVIDIA GPUs.
- **Developer tools:** read and index your code, explain errors against the
  real source, review diffs, run tests. Optional and permission-gated.
- **Integrations:** weather and web search (no account needed), GitHub, Home
  Assistant.

---

## Configuration

Setup covers what most people need. For everything else, copy `.env.example`
to `.env`; every setting is explained there. Useful ones:

| Setting | What it does |
| --- | --- |
| `JARVIS_AI_PROVIDER=ollama` | Local models only; cloud providers are never constructed |
| `JARVIS_PORT` | Port to serve on (default 8000) |
| `JARVIS_AUTH_MODE=password` + `JARVIS_PASSWORD` | Require a password, for reaching JARVIS from your phone |
| `JARVIS_FILE_ROOTS` | Folders the file tools may touch (default: your home folder) |

To reach JARVIS from your phone, see `docs/REMOTE-ACCESS.md`. The simplest
safe route is [Tailscale](https://tailscale.com); don't expose it to the open
internet.

---

## Platform notes

JARVIS is built and tested on **Windows**. Desktop features (opening and
closing apps, notifications, key sealing, start with Windows) use Windows
APIs. The core (conversation, email, documents, memory, tasks) is
cross-platform Python, but macOS and Linux aren't tested; reports and fixes
are welcome.

---

## Development

```bash
python start.py           # backend + interface dev server with hot reload
python -m pytest          # the test suite (about a thousand tests)
```

Architecture notes are in `docs/`.

---

## Credits and license

Several features (skills, MCP plug-ins, document search, learning memory,
reply telemetry, prompt-injection screening) are adapted from
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) (Apache-2.0); see
`THIRD_PARTY_NOTICES.md`.

Not affiliated with any film studio. JARVIS is just a good name for an
assistant.

Copyright (C) 2026 Miekgames. Released under the **GNU General Public
License v3.0** (see `LICENSE`). You're free to use it, study it and change it.
If you share it, original or modified, it has to stay open source under the
same license, with credit kept. The parts adapted from OpenJarvis keep their
Apache-2.0 notices.
