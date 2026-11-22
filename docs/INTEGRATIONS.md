# Integrations

Every connector declares what it can do and what it needs. The Integrations
panel renders that declaration directly, so an integration lacking credentials
shows exactly which environment variables are missing and where to get them —
rather than a button that looks live and silently does nothing.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| `connected` | Credentials present, last probe succeeded |
| `needs_config` | The connector works; credentials are missing |
| `error` | Credentials present but the provider rejected or failed |
| `not_implemented` | The surface is declared; the backend is not finished |

After editing `.env`, restart the backend and press **Re-check** in the panel.

---

## Working with no account

### Weather — Open-Meteo
No key, no account. Optionally set a default location:

```bash
JARVIS_WEATHER_LOCATION=Manchester, UK
```

Current conditions, five-day forecast, geocoding for any place name.

### Web search — DuckDuckGo
No key. Powers `web_search`, `news_search`, `read_webpage` and the `research`
background task.

---

## Working once you add credentials

### GitHub

```bash
GITHUB_TOKEN=ghp_your_token
```

Create at <https://github.com/settings/tokens>. A classic token with `repo`
scope (or a fine-grained token with repository read access) is enough for
reading. Writing (`github_create_issue`) also needs the `github.write`
permission enabled in Settings, and asks for confirmation each time.

Capabilities: repositories, issues, pull requests, commits, CI workflow runs,
create issue.

### Home Assistant

```bash
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=your_long_lived_token
```

Create the token at the bottom of your Home Assistant profile page.

Capabilities: read every entity state; turn lights and switches on/off; set
brightness; set thermostats; activate scenes; locks.

Locks and cameras are behind a **separate** permission (`smarthome.security`)
from ordinary devices, and it defaults to off.

---

## Email (Gmail, iCloud, Yahoo, any IMAP mailbox)

Connected in the **Email** panel, not `.env`: your address plus an *app
password* (for Gmail: turn on 2-Step Verification, then create one at
<https://myaccount.google.com/apppasswords> named "JARVIS"). JARVIS signs in
for real before saving anything; the password is sealed with Windows DPAPI in
`data/email.json`, never returned to the browser, and only sent to the
provider's IMAP/SMTP servers. Disconnect (in the Email panel or here) deletes it.

Tools: `check_email`, `search_email` (Gmail search syntax on Gmail, across All
Mail), `read_email` — reading never changes the mailbox (folders opened
read-only, messages fetched with PEEK, so nothing is marked read) — and
`send_email`, which is HIGH risk: before you are asked, JARVIS works out the
real recipient (from your contacts or their last email) and which email a reply
answers, the confirmation shows the exact message, and nothing is sent until you
approve. Away mode can never send. Email contents are screened for prompt
injection like any web page.

Outlook.com may refuse app passwords (Microsoft has been moving it to browser
sign-in only); the panel says so.

---

## Declared but not finished

These connectors have a real interface, a real settings entry and a real data
path into JARVIS. What is missing is the OAuth authorisation-code exchange,
which needs an application you register with the provider plus a redirect back
to this machine. They report `not_implemented` rather than pretending.

| Integration | Needs | What is already built |
| --- | --- | --- |
| Google Calendar | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` + OAuth flow | Connector, config UI, and `CalendarService.sync_external()` which upserts external events into the local calendar by `external_id` |
| Spotify | `SPOTIFY_CLIENT_ID/SECRET` + OAuth flow | Connector, config UI. Playback control already works through system media keys without it |
| Zoom | `ZOOM_CLIENT_ID/SECRET` + OAuth flow | Connector, config UI. Joining a meeting by opening its link already works |
| Microsoft Teams | `MS_CLIENT_ID/SECRET` + OAuth flow | Connector, config UI |

To finish one, implement the token exchange in the connector's `probe()` and
add a `/api/integrations/{key}/oauth/callback` route. The calendar data path
downstream is already in place.

Meanwhile the **local calendar is fully functional** — create, edit, cancel and
query events, with meeting reminders and the `meeting_prep` task working
against it.

---

## Recording and consent

JARVIS does not record or transcribe meetings. If you build that on top of this
foundation, note that recording and transcription are subject to platform
policy and to consent law that varies by jurisdiction — in many places every
participant must be informed. The meeting tooling here deliberately stops at
preparation, notes you write, and follow-up reminders.

---

## Disconnecting

**Disconnect** clears JARVIS's stored config for that integration and marks it
disconnected. It cannot delete the credentials from your `.env` file — the
response says so explicitly. To revoke access fully, remove the variables from
`.env` and revoke the token at the provider.
