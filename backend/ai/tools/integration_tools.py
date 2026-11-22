"""Tools backed by external integrations.

Each declares `requires_integration`, so if the credentials aren't there the
registry short-circuits with a message naming what's missing instead of failing
somewhere deep in an HTTP call.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import truncate
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:

    def integration(ctx, key: str):
        registry_service = ctx.get("integrations")
        if registry_service is None:
            return None
        return registry_service.get(key)

    # ==================================================================
    # GitHub
    # ==================================================================
    @registry.add(
        "github",
        "Read GitHub: your repositories, issues, pull requests, recent commits and CI status.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["repos", "issues", "pulls", "commits", "ci"]},
            "repo": {"type": "string", "description": "owner/name, required for pulls/commits/ci"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": {"type": "integer"},
        }, "required": ["action"]},
        permission="github.read",
        requires_integration="github",
        category="developer",
        timeout=45.0,
    )
    async def github(ctx, action: str, repo: str = "", state: str = "open", limit: int = 15):
        connector = integration(ctx, "github")
        if connector is None:
            return fail("GitHub connector unavailable.")
        try:
            if action == "repos":
                return ok({"repos": await connector.repos(limit)})
            if action == "issues":
                return ok({"issues": await connector.issues(repo or None, state, limit)})
            if action == "pulls":
                if not repo:
                    return fail("Pull requests need a repo (owner/name).")
                return ok({"pulls": await connector.pulls(repo, state, limit)})
            if action == "commits":
                if not repo:
                    return fail("Commits need a repo (owner/name).")
                return ok({"commits": await connector.commits(repo, limit)})
            if action == "ci":
                if not repo:
                    return fail("CI status needs a repo (owner/name).")
                return ok({"runs": await connector.workflow_runs(repo, limit)})
        except Exception as exc:
            return fail(f"GitHub request failed: {exc}")
        return fail(f"Unknown action '{action}'.")

    @registry.add(
        "github_create_issue",
        "Open an issue on a GitHub repository.",
        {"type": "object", "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["repo", "title"]},
        permission="github.write",
        requires_integration="github",
        risk=Risk.HIGH,
        confirm_summary=lambda a: (
            f"Create a public-facing issue on {a.get('repo')} titled \"{a.get('title')}\"?"
        ),
        category="developer",
    )
    async def github_create_issue(ctx, repo: str, title: str, body: str = ""):
        connector = integration(ctx, "github")
        try:
            result = await connector.create_issue(repo, title, body)
        except Exception as exc:
            return fail(f"Couldn't create the issue: {exc}")
        return ok(result, f"Opened issue #{result['number']}: {result['url']}")

    # ==================================================================
    # Home Assistant
    # ==================================================================
    @registry.add(
        "smart_home_status",
        "Read the state of smart-home devices: lights, plugs, sensors, thermostats.",
        {"type": "object", "properties": {
            "domain": {"type": "string", "description": "light, switch, sensor, climate, lock, scene"}
        }},
        permission="smarthome.control",
        requires_integration="home_assistant",
        category="smart_home",
        timeout=30.0,
    )
    async def smart_home_status(ctx, domain: str = ""):
        connector = integration(ctx, "home_assistant")
        try:
            entities = await connector.states(domain or None)
        except Exception as exc:
            return fail(f"Home Assistant request failed: {exc}")
        return ok({"count": len(entities), "entities": entities[:60]})

    @registry.add(
        "smart_home_control",
        "Control a smart-home device: turn lights or plugs on/off, set brightness, activate a scene, "
        "set a thermostat.",
        {"type": "object", "properties": {
            "entity_id": {"type": "string", "description": "e.g. light.kitchen"},
            "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle", "set_temperature"]},
            "brightness": {"type": "integer", "description": "0-255, for lights"},
            "temperature": {"type": "number"},
        }, "required": ["entity_id", "action"]},
        permission="smarthome.control",
        requires_integration="home_assistant",
        risk=Risk.HIGH,
        confirm_summary=lambda a: f"{a.get('action', '').replace('_', ' ').title()} {a.get('entity_id')}?",
        category="smart_home",
    )
    async def smart_home_control(ctx, entity_id: str, action: str,
                                 brightness: Optional[int] = None, temperature: Optional[float] = None):
        connector = integration(ctx, "home_assistant")
        domain = entity_id.split(".")[0]

        if domain in ("lock",) and not ctx.get("permissions").allowed("smarthome.security"):
            return fail(
                "Locks are covered by the separate 'Locks and cameras' permission, which is off. "
                "Enable it in Settings -> Permissions if you want me to control door locks."
            )

        data: Dict[str, Any] = {}
        if brightness is not None and domain == "light":
            data["brightness"] = max(0, min(255, brightness))
        if action == "set_temperature":
            if temperature is None:
                return fail("Setting a thermostat needs a temperature.")
            data["temperature"] = temperature

        try:
            result = await connector.call_service(domain, action, entity_id, data)
        except Exception as exc:
            return fail(f"Home Assistant rejected that: {exc}")
        return ok(result, f"{entity_id}: {action.replace('_', ' ')} sent.")

    @registry.add(
        "integration_status",
        "Report which integrations are connected, which need credentials, and exactly what's missing.",
        {"type": "object", "properties": {}},
        category="system",
    )
    async def integration_status(ctx):
        registry_service = ctx.get("integrations")
        if registry_service is None:
            return fail("Integration registry unavailable.")
        items = await registry_service.list()
        return ok({"integrations": [
            {"key": i["key"], "name": i["name"], "status": i["status"], "message": i["message"],
             "missing_env": i["missing_env"]}
            for i in items
        ]})
