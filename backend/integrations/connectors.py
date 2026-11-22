"""
The bundled connectors.

Fully working with no account at all:
    weather   -- Open-Meteo
    search    -- DuckDuckGo

Fully working once you add credentials:
    github          -- personal access token
    home_assistant  -- long-lived access token + your instance URL
    email           -- an app password, entered in the Email panel

Declared surfaces that need an OAuth application you create yourself (the
connector, config UI and data path exist; the OAuth exchange is the missing
piece and is reported as such rather than faked):
    google_calendar, spotify, zoom, teams
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

from ..core.util import iso, now_iso, parse_iso, truncate
from .base import Capability, Integration, Status

logger = logging.getLogger("jarvis.integrations")


# ---------------------------------------------------------------------------
class WeatherIntegration(Integration):
    key = "weather"
    name = "Weather (Open-Meteo)"
    description = "Current conditions, 5-day forecast and severe-weather context. No account needed."
    category = "information"
    docs_url = "https://open-meteo.com/"
    required_env: Dict[str, str] = {}
    optional_env = {"JARVIS_WEATHER_LOCATION": "Default city, e.g. 'Manchester, UK'"}
    capabilities = [
        Capability("current", "Current conditions for any location"),
        Capability("forecast", "Five-day forecast"),
        Capability("geocode", "Resolve a place name to coordinates"),
    ]

    def __init__(self, settings, db=None, weather_service=None):
        super().__init__(settings, db)
        self.weather = weather_service

    async def probe(self) -> Dict[str, Any]:
        if self.weather is None:
            return {"status": Status.ERROR, "message": "Weather service not wired up."}
        result = await self.weather.current()
        if result.get("ok"):
            return {"status": Status.CONNECTED, "message": result["summary"], "account": result["location"]}
        return {"status": Status.NEEDS_CONFIG, "message": result.get("message", "No location set.")}


# ---------------------------------------------------------------------------
class SearchIntegration(Integration):
    key = "search"
    name = "Web search (DuckDuckGo)"
    description = "Web and news search with page reading. No account needed."
    category = "information"
    docs_url = "https://duckduckgo.com/"
    required_env: Dict[str, str] = {}
    capabilities = [
        Capability("search", "Web search"),
        Capability("news", "News search"),
        Capability("fetch", "Read and extract page text"),
    ]

    def __init__(self, settings, db=None, web_service=None):
        super().__init__(settings, db)
        self.web = web_service

    async def probe(self) -> Dict[str, Any]:
        if self.web is None:
            return {"status": Status.ERROR, "message": "Web service not wired up."}
        results = await self.web.search("test", max_results=1)
        return {
            "status": Status.CONNECTED if results else Status.ERROR,
            "message": "Search responding." if results else "Search returned nothing.",
        }


# ---------------------------------------------------------------------------
class GitHubIntegration(Integration):
    key = "github"
    name = "GitHub"
    description = "Repositories, issues, pull requests, commits and CI status."
    category = "developer"
    docs_url = "https://github.com/settings/tokens"
    required_env = {"GITHUB_TOKEN": "A personal access token from github.com/settings/tokens"}
    capabilities = [
        Capability("repos", "List and search your repositories"),
        Capability("issues", "Read and create issues"),
        Capability("pulls", "Read pull requests"),
        Capability("commits", "Recent commit activity"),
        Capability("actions", "Workflow run status"),
    ]

    API = "https://api.github.com"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def probe(self) -> Dict[str, Any]:
        response = await self.http().get(f"{self.API}/user", headers=self._headers())
        if response.status_code == 401:
            return {"status": Status.ERROR, "message": "GitHub rejected the token (401)."}
        response.raise_for_status()
        user = response.json()
        return {
            "status": Status.CONNECTED,
            "message": f"Signed in as {user.get('login')}.",
            "account": user.get("login"),
        }

    # -- capabilities ---------------------------------------------------
    async def repos(self, limit: int = 20) -> List[Dict[str, Any]]:
        response = await self.http().get(
            f"{self.API}/user/repos",
            headers=self._headers(),
            params={"sort": "updated", "per_page": min(limit, 100)},
        )
        response.raise_for_status()
        return [
            {
                "name": r["full_name"],
                "private": r["private"],
                "description": r.get("description") or "",
                "language": r.get("language"),
                "stars": r.get("stargazers_count", 0),
                "open_issues": r.get("open_issues_count", 0),
                "updated": r.get("updated_at"),
                "url": r.get("html_url"),
            }
            for r in response.json()
        ]

    async def issues(self, repo: Optional[str] = None, state: str = "open", limit: int = 20):
        url = f"{self.API}/repos/{repo}/issues" if repo else f"{self.API}/issues"
        response = await self.http().get(
            url, headers=self._headers(), params={"state": state, "per_page": min(limit, 100)}
        )
        response.raise_for_status()
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "repo": (i.get("repository") or {}).get("full_name", repo or ""),
                "url": i["html_url"],
                "is_pull_request": "pull_request" in i,
                "updated": i.get("updated_at"),
            }
            for i in response.json()
        ]

    async def pulls(self, repo: str, state: str = "open", limit: int = 20):
        response = await self.http().get(
            f"{self.API}/repos/{repo}/pulls",
            headers=self._headers(),
            params={"state": state, "per_page": min(limit, 100)},
        )
        response.raise_for_status()
        return [
            {
                "number": p["number"], "title": p["title"], "state": p["state"],
                "author": (p.get("user") or {}).get("login"), "url": p["html_url"],
                "draft": p.get("draft", False), "updated": p.get("updated_at"),
            }
            for p in response.json()
        ]

    async def commits(self, repo: str, limit: int = 15):
        response = await self.http().get(
            f"{self.API}/repos/{repo}/commits", headers=self._headers(), params={"per_page": min(limit, 100)}
        )
        response.raise_for_status()
        return [
            {
                "sha": c["sha"][:8],
                "message": truncate((c.get("commit") or {}).get("message", ""), 120),
                "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                "url": c["html_url"],
            }
            for c in response.json()
        ]

    async def workflow_runs(self, repo: str, limit: int = 10):
        response = await self.http().get(
            f"{self.API}/repos/{repo}/actions/runs",
            headers=self._headers(),
            params={"per_page": min(limit, 100)},
        )
        response.raise_for_status()
        return [
            {
                "name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"),
                "branch": r.get("head_branch"), "url": r.get("html_url"), "created": r.get("created_at"),
            }
            for r in response.json().get("workflow_runs", [])
        ]

    async def create_issue(self, repo: str, title: str, body: str = "") -> Dict[str, Any]:
        response = await self.http().post(
            f"{self.API}/repos/{repo}/issues", headers=self._headers(), json={"title": title, "body": body}
        )
        response.raise_for_status()
        issue = response.json()
        return {"number": issue["number"], "url": issue["html_url"], "title": issue["title"]}


# ---------------------------------------------------------------------------
class HomeAssistantIntegration(Integration):
    key = "home_assistant"
    name = "Home Assistant"
    description = "Lights, plugs, thermostats, sensors, scenes and locks through your own instance."
    category = "smart_home"
    docs_url = "https://www.home-assistant.io/docs/authentication/#your-account-profile"
    required_env = {
        "HOME_ASSISTANT_URL": "Your instance URL, e.g. http://homeassistant.local:8123",
        "HOME_ASSISTANT_TOKEN": "A long-lived access token from your HA user profile",
    }
    capabilities = [
        Capability("states", "Read the state of every device"),
        Capability("lights", "Turn lights on/off and set brightness"),
        Capability("switches", "Control plugs and switches"),
        Capability("climate", "Read and set thermostats"),
        Capability("scenes", "Activate scenes"),
        Capability("locks", "Lock and unlock (requires the smarthome.security permission)"),
    ]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.HOME_ASSISTANT_TOKEN}",
            "Content-Type": "application/json",
        }

    def _base(self) -> str:
        return self.settings.HOME_ASSISTANT_URL.rstrip("/")

    async def probe(self) -> Dict[str, Any]:
        response = await self.http().get(f"{self._base()}/api/", headers=self._headers())
        response.raise_for_status()
        states = await self.http().get(f"{self._base()}/api/states", headers=self._headers())
        count = len(states.json()) if states.status_code == 200 else 0
        return {
            "status": Status.CONNECTED,
            "message": f"Connected. {count} entities visible.",
            "account": self._base(),
            "entity_count": count,
        }

    async def states(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        response = await self.http().get(f"{self._base()}/api/states", headers=self._headers())
        response.raise_for_status()
        entities = response.json()
        if domain:
            entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]
        return [
            {
                "entity_id": e["entity_id"],
                "name": (e.get("attributes") or {}).get("friendly_name", e["entity_id"]),
                "state": e["state"],
                "domain": e["entity_id"].split(".")[0],
                "attributes": {k: v for k, v in (e.get("attributes") or {}).items()
                               if k in ("brightness", "temperature", "current_temperature", "unit_of_measurement")},
            }
            for e in entities
        ]

    async def call_service(self, domain: str, service: str, entity_id: str,
                           data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"entity_id": entity_id, **(data or {})}
        response = await self.http().post(
            f"{self._base()}/api/services/{domain}/{service}", headers=self._headers(), json=payload
        )
        response.raise_for_status()
        return {"ok": True, "changed": len(response.json() or []), "entity_id": entity_id}


# ---------------------------------------------------------------------------
class OAuthPendingIntegration(Integration):
    """Shared base for providers that need an OAuth application the user
    registers themselves. The surface, config UI and data path are real; the
    token exchange is what's missing, and that is stated plainly."""

    implemented = False

    async def probe(self) -> Dict[str, Any]:
        return {
            "status": Status.NOT_IMPLEMENTED,
            "message": (
                f"{self.name} needs an OAuth application you register with the provider, plus a "
                f"redirect back to this machine. The connector, settings UI and data path are in "
                f"place -- add {', '.join(self.required_env)} and the OAuth callback to finish it. "
                f"See docs/INTEGRATIONS.md."
            ),
        }


class GoogleCalendarIntegration(OAuthPendingIntegration):
    key = "google_calendar"
    name = "Google Calendar"
    description = "Two-way sync with Google Calendar."
    category = "calendar"
    docs_url = "https://console.cloud.google.com/apis/credentials"
    required_env = {
        "GOOGLE_CLIENT_ID": "OAuth client ID from Google Cloud Console",
        "GOOGLE_CLIENT_SECRET": "OAuth client secret from Google Cloud Console",
    }
    capabilities = [
        Capability("sync", "Pull events into the local calendar", implemented=False),
        Capability("create", "Create events in Google Calendar", implemented=False),
    ]


class EmailIntegration(Integration):
    """The owner's mailbox over IMAP/SMTP (see `email_account`). Credentials
    are entered in the Email panel, not .env, and kept sealed."""

    key = "email"
    name = "Email"
    description = ("Read, search and -- after you approve each one -- send email from Gmail, "
                   "iCloud, Yahoo or any IMAP mailbox, with an app password.")
    category = "communication"
    docs_url = "https://myaccount.google.com/apppasswords"
    required_env: Dict[str, str] = {}
    capabilities = [
        Capability("read", "List and read messages without marking them read"),
        Capability("search", "Search all mail (Gmail search syntax on Gmail)"),
        Capability("send", "Send after you approve the exact message"),
    ]

    def __init__(self, settings, db=None, account=None):
        super().__init__(settings, db)
        self.account = account

    async def forget(self) -> bool:
        return bool(self.account is not None and await self.account.disconnect())

    async def probe(self) -> Dict[str, Any]:
        if self.account is None:
            return {"status": Status.ERROR, "message": "The email service isn't wired up."}
        if not self.account.connected:
            return {"status": Status.NEEDS_CONFIG,
                    "message": "Not connected. Open Email in JARVIS's menu and add an app password."}
        from .email_account import EmailError

        try:
            unread = await self.account.unread_count(fresh=True)
        except EmailError as exc:
            return {"status": Status.ERROR, "message": str(exc)}
        return {"status": Status.CONNECTED, "message": f"{unread} unread in the inbox.",
                "account": self.account.address}


class SpotifyIntegration(OAuthPendingIntegration):
    key = "spotify"
    name = "Spotify"
    description = "Playback control and now-playing metadata."
    category = "media"
    docs_url = "https://developer.spotify.com/dashboard"
    required_env = {
        "SPOTIFY_CLIENT_ID": "Client ID from the Spotify developer dashboard",
        "SPOTIFY_CLIENT_SECRET": "Client secret from the Spotify developer dashboard",
    }
    capabilities = [
        Capability("playback", "Play, pause, skip", implemented=False),
        Capability("now_playing", "Current track metadata", implemented=False),
    ]


class ZoomIntegration(OAuthPendingIntegration):
    key = "zoom"
    name = "Zoom"
    description = "Meeting detection, join links and metadata."
    category = "meetings"
    docs_url = "https://marketplace.zoom.us/"
    required_env = {"ZOOM_CLIENT_ID": "From the Zoom marketplace app",
                    "ZOOM_CLIENT_SECRET": "From the Zoom marketplace app"}
    capabilities = [
        Capability("meetings", "List upcoming meetings", implemented=False),
        Capability("join", "Open a meeting link", implemented=True),
    ]


class TeamsIntegration(OAuthPendingIntegration):
    key = "teams"
    name = "Microsoft Teams"
    description = "Meeting detection and calendar sync via Microsoft Graph."
    category = "meetings"
    docs_url = "https://portal.azure.com/"
    required_env = {"MS_CLIENT_ID": "Azure app registration client ID",
                    "MS_CLIENT_SECRET": "Azure app registration client secret"}
    capabilities = [
        Capability("calendar", "Sync the Outlook calendar", implemented=False),
        Capability("meetings", "Detect upcoming Teams meetings", implemented=False),
    ]


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )
