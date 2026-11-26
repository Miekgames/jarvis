"""
The action vocabulary.

Schedules, automations, custom commands and modes all express "do this" using
the same action objects, so a capability added here is immediately available
everywhere. Every action returns a structured result -- including failures --
which is what makes the automation run history trustworthy.

    {"kind": "notify",      "title": ..., "body": ..., "priority": ..., "speak": bool}
    {"kind": "speak",       "text": ...}
    {"kind": "create_task", "name": ..., "type": ..., "params": {...}, "priority": ...}
    {"kind": "run_tool",    "tool": ..., "arguments": {...}}
    {"kind": "ai",          "prompt": ..., "purpose": ..., "notify": bool, "speak": bool}
    {"kind": "launch_app",  "name": ...}
    {"kind": "set_mode",    "mode": ...}
    {"kind": "sleep"} / {"kind": "wake"}
    {"kind": "webhook",     "url": ..., "method": ..., "body": {...}}
    {"kind": "sequence",    "actions": [...]}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import truncate

logger = logging.getLogger("jarvis.actions")


class ActionRunner:
    def __init__(self, services: Dict[str, Any]):
        self.services = services

    def _service(self, name: str):
        service = self.services.get(name)
        if service is None:
            raise RuntimeError(f"The '{name}' service isn't available.")
        return service

    # ==================================================================
    async def run(self, action: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        kind = (action or {}).get("kind", "")
        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            message = f"Unknown action kind '{kind}'."
            logger.warning(message)
            return {"status": "error", "kind": kind, "message": message}

        try:
            result = await handler(action, context)
            return {"status": "ok", "kind": kind, **(result or {})}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("action '%s' failed", kind)
            return {"status": "error", "kind": kind, "message": f"{type(exc).__name__}: {exc}"}

    async def run_all(self, actions: List[Dict[str, Any]], context: Optional[Dict[str, Any]] = None
                      ) -> List[Dict[str, Any]]:
        results = []
        for action in actions or []:
            result = await self.run(action, context)
            results.append(result)
            if result["status"] == "error" and action.get("stop_on_error", True):
                break
        return results

    # ==================================================================
    async def _do_notify(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        notifications = self._service("notifications")
        record = await notifications.create(
            title=action.get("title", "JARVIS"),
            body=action.get("body", ""),
            priority=action.get("priority", "normal"),
            source=action.get("source", context.get("source", "automation")),
            action=action.get("action"),
        )
        if action.get("speak"):
            await self._speak(f"{action.get('title', '')}. {action.get('body', '')}".strip())
        shown = False
        if action.get("desktop"):
            # Reaches the owner with no JARVIS tab open.
            from ..integrations import windows_toast

            shown = await windows_toast.show(f"JARVIS · {action.get('title', 'Reminder')}",
                                             action.get("body", ""))
        return {"notification_id": record.get("id"), "windows_notification": shown}

    async def _do_speak(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        text = action.get("text", "")
        await self._speak(text)
        return {"spoken": truncate(text, 120)}

    async def _speak(self, text: str) -> None:
        """Speaking happens in the browser (that's where the speakers are), so
        this publishes an event the dashboard picks up."""
        if not text:
            return
        bus.publish(Events.SPEAKING_START, {"text": text, "source": "automation"}, source="voice")

    async def _do_create_task(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        engine = self._service("tasks")
        record = await engine.create(
            name=action.get("name") or action.get("type", "Background task"),
            task_type=action.get("type", "agent"),
            params=action.get("params", {}),
            description=action.get("description", ""),
            priority=action.get("priority", "normal"),
            schedule_id=context.get("schedule_id"),
        )
        return {"task_id": record["id"], "task_name": record["name"]}

    async def _do_run_tool(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        registry = self._service("registry")
        result = await registry.dispatch(
            action.get("tool", ""),
            action.get("arguments", {}) or {},
            task_id=context.get("task_id"),
        )
        return {"ok": result.ok, "message": truncate(result.message or result.to_model_string(), 500)}

    async def _do_ai(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        orchestrator = self._service("orchestrator")
        answer = await orchestrator.ask(
            action.get("prompt", ""), purpose=action.get("purpose", "fast")
        )
        if action.get("notify", True):
            await self._service("notifications").create(
                title=action.get("title", "JARVIS"),
                body=truncate(answer, 900),
                priority=action.get("priority", "normal"),
                source="automation",
            )
        if action.get("speak"):
            await self._speak(answer)
        return {"answer": truncate(answer, 2000)}

    async def _do_launch_app(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        registry = self._service("registry")
        result = await registry.dispatch("launch_app", {"name": action.get("name", "")})
        return {"ok": result.ok, "message": result.message}

    async def _do_set_mode(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        modes = self._service("modes")
        record = await modes.activate(action.get("mode", "normal"))
        return {"mode": record.get("key")}

    async def _do_sleep(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        await self._service("presence").sleep(reason=action.get("reason", "automation"))
        return {"sleeping": True}

    async def _do_wake(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        await self._service("presence").wake(reason=action.get("reason", "automation"))
        return {"sleeping": False}

    async def _do_webhook(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        url = action.get("url", "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must be http(s).")
        method = (action.get("method") or "POST").upper()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method, url, json=action.get("body"), headers=action.get("headers")
            )
        return {"status_code": response.status_code, "body": truncate(response.text, 500)}

    async def _do_sequence(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        results = await self.run_all(action.get("actions", []), context)
        failures = [r for r in results if r["status"] == "error"]
        return {"steps": len(results), "failed": len(failures), "results": results}


# ---------------------------------------------------------------------------
# Human-readable rendering -- used by the automation builder preview so the
# user sees exactly what will happen before enabling anything.
# ---------------------------------------------------------------------------
def describe_action(action: Dict[str, Any]) -> str:
    kind = (action or {}).get("kind", "?")
    if kind == "notify":
        return f"Send a {action.get('priority', 'normal')}-priority notification: \"{action.get('title', '')}\""
    if kind == "speak":
        return f"Say aloud: \"{truncate(action.get('text', ''), 80)}\""
    if kind == "create_task":
        return f"Start a background '{action.get('type', 'agent')}' task: {action.get('name', '')}"
    if kind == "run_tool":
        return f"Run the {action.get('tool', '?')} tool"
    if kind == "ai":
        return f"Ask the AI: \"{truncate(action.get('prompt', ''), 80)}\""
    if kind == "launch_app":
        return f"Open {action.get('name', '?')}"
    if kind == "set_mode":
        return f"Switch to {action.get('mode', '?')} mode"
    if kind == "sleep":
        return "Put JARVIS to sleep"
    if kind == "wake":
        return "Wake JARVIS"
    if kind == "webhook":
        return f"Call {action.get('method', 'POST')} {action.get('url', '?')}"
    if kind == "sequence":
        return "Run in order: " + "; ".join(describe_action(a) for a in action.get("actions", []))
    return f"Unknown action '{kind}'"


ACTION_KINDS = [
    "notify", "speak", "create_task", "run_tool", "ai",
    "launch_app", "set_mode", "sleep", "wake", "webhook", "sequence",
]
