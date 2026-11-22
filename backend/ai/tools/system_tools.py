"""System, application and machine-control tools."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from ...core.permissions import Risk
from ...core.util import humanize_duration
from ...systemmon import apps as apps_module
from ...systemmon import control
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:
    launcher = apps_module.AppLauncher()

    # ==================================================================
    @registry.add(
        "system_status",
        "Read live system state: CPU, memory, disk, network, GPU, battery, uptime and process count. "
        "Use this for 'how's my system', 'what's my CPU at', 'am I running out of space'.",
        {"type": "object", "properties": {
            "detail": {"type": "string", "enum": ["summary", "full"],
                       "description": "summary for a spoken answer, full for every field"}
        }},
        permission="system.info",
        category="system",
    )
    async def system_status(ctx, detail: str = "summary"):
        metrics = ctx.get("metrics")
        if metrics is None:
            return fail("System monitoring isn't running.")
        latest = metrics.latest() or await asyncio.get_event_loop().run_in_executor(None, metrics.sample)
        if detail == "full":
            return ok({**latest, "static": metrics.static_info()})
        return ok(
            {
                "cpu_percent": latest.get("cpu"),
                "memory_percent": latest.get("mem"),
                "memory_used_gb": latest.get("mem_used_gb"),
                "memory_total_gb": latest.get("mem_total_gb"),
                "disk_percent": latest.get("disk"),
                "disk_free_gb": latest.get("disk_free_gb"),
                "gpu_percent": latest.get("gpu"),
                "gpu_temp_c": latest.get("gpu_temp"),
                "battery_percent": latest.get("battery"),
                "uptime": humanize_duration(latest.get("uptime_seconds", 0)),
                "processes": latest.get("processes"),
            }
        )

    # ==================================================================
    @registry.add(
        "system_history",
        "Historical system metrics, for questions like 'has my GPU been hot' or 'what happened to my "
        "memory over the last hour'. Returns downsampled samples.",
        {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "How far back to look (default 3)"},
            "metric": {"type": "string", "enum": ["cpu", "mem", "disk", "gpu", "all"]},
        }},
        permission="system.info",
        category="system",
    )
    async def system_history(ctx, hours: int = 3, metric: str = "all"):
        metrics = ctx.get("metrics")
        if metrics is None:
            return fail("System monitoring isn't running.")
        samples = await metrics.history(hours=min(hours, 48), max_points=80)
        if not samples:
            return ok({"samples": [], "note": "No history recorded yet -- JARVIS may have just started."})

        def series(key):
            values = [s[key] for s in samples if s.get(key) is not None]
            if not values:
                return None
            return {"min": round(min(values), 1), "max": round(max(values), 1),
                    "avg": round(sum(values) / len(values), 1), "latest": round(values[-1], 1)}

        summary = {k: series(k) for k in (["cpu", "mem", "disk", "gpu"] if metric == "all" else [metric])}
        return ok({"hours": hours, "sample_count": len(samples), "summary": summary})

    # ==================================================================
    @registry.add(
        "list_processes",
        "List the heaviest running processes by CPU or memory.",
        {"type": "object", "properties": {
            "sort_by": {"type": "string", "enum": ["cpu", "memory"]},
            "limit": {"type": "integer"},
        }},
        permission="system.info",
        category="system",
    )
    async def list_processes(ctx, sort_by: str = "cpu", limit: int = 15):
        processes = await asyncio.get_event_loop().run_in_executor(
            None, apps_module.list_processes, min(limit, 50), sort_by
        )
        return ok({"processes": processes, "sorted_by": sort_by})

    # ==================================================================
    @registry.add(
        "launch_app",
        "Open an application installed on this computer by name, e.g. 'Spotify', 'VS Code', 'Discord'. "
        "Prefer this over a browser for anything installed locally.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "The application name as the user said it"}
        }, "required": ["name"]},
        permission="apps.launch",
        category="applications",
    )
    async def launch_app(ctx, name: str):
        result = await asyncio.get_event_loop().run_in_executor(None, launcher.launch, name)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"], result)

    @registry.add(
        "list_apps",
        "List applications installed on this computer.",
        {"type": "object", "properties": {"filter": {"type": "string"}}},
        permission="apps.launch",
        category="applications",
    )
    async def list_apps(ctx, filter: str = ""):
        names = await asyncio.get_event_loop().run_in_executor(None, launcher.list_apps, 300)
        if filter:
            names = [n for n in names if filter.lower() in n.lower()]
        return ok({"count": len(names), "apps": names[:120]})

    @registry.add(
        "close_app",
        "Close a running application. Refuses on system-critical processes.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        permission="apps.close",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: f"Close every process matching '{a.get('name')}'? Unsaved work may be lost.",
        category="applications",
    )
    async def close_app(ctx, name: str):
        result = await asyncio.get_event_loop().run_in_executor(None, launcher.close, name)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    # ==================================================================
    @registry.add(
        "volume_control",
        "Change the system volume.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["up", "down", "mute", "set"]},
            "amount": {"type": "integer", "description": "Step size, or the 0-100 level when action is 'set'"},
        }, "required": ["action"]},
        permission="system.control",
        category="system",
    )
    async def volume_control(ctx, action: str, amount: int = 10):
        result = await asyncio.get_event_loop().run_in_executor(None, control.volume, action, amount)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    @registry.add(
        "media_control",
        "Control media playback with the system media keys (works with Spotify, YouTube, any player).",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop", "now_playing"]}
        }, "required": ["action"]},
        permission="media.control",
        category="media",
    )
    async def media_control(ctx, action: str):
        loop = asyncio.get_event_loop()
        if action == "now_playing":
            result = await loop.run_in_executor(None, control.now_playing)
        else:
            result = await loop.run_in_executor(None, control.media, action)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    @registry.add(
        "brightness_control",
        "Read or set display brightness. Works on laptop panels; most desktop monitors don't support it.",
        {"type": "object", "properties": {"level": {"type": "integer", "description": "0-100; omit to read"}}},
        permission="system.control",
        category="system",
    )
    async def brightness_control(ctx, level: Optional[int] = None):
        result = await asyncio.get_event_loop().run_in_executor(None, control.brightness, level)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    # ==================================================================
    @registry.add(
        "power_control",
        "Lock, sleep, restart or shut down the computer. Restart and shutdown are scheduled with a "
        "delay so they can be cancelled. Use action='cancel' to abort a pending shutdown.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown", "cancel"]},
            "delay_seconds": {"type": "integer", "description": "Delay before restart/shutdown (default 30)"},
        }, "required": ["action"]},
        permission="system.power",
        risk=Risk.CRITICAL,
        confirm_summary=lambda a: {
            "lock": "Lock this computer now?",
            "sleep": "Put this computer to sleep now?",
            "restart": f"Restart this computer in {a.get('delay_seconds', 30)} seconds? "
                       "Unsaved work in other applications will be lost.",
            "shutdown": f"Shut down this computer in {a.get('delay_seconds', 30)} seconds? "
                        "Unsaved work in other applications will be lost.",
            "cancel": "Cancel the pending shutdown?",
        }.get(a.get("action", ""), f"Run power action '{a.get('action')}'?"),
        category="system",
    )
    async def power_control(ctx, action: str, delay_seconds: int = 30):
        result = await asyncio.get_event_loop().run_in_executor(
            None, control.power, action, max(0, delay_seconds)
        )
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    # ==================================================================
    @registry.add(
        "take_screenshot",
        "Capture the screen to the local data folder and return the path.",
        {"type": "object", "properties": {}},
        permission="system.screenshot",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: "Take a screenshot of everything currently on screen?",
        category="system",
    )
    async def take_screenshot(ctx):
        settings = ctx.get("settings")
        destination = settings.DATA_DIR / "screenshots"
        result = await asyncio.get_event_loop().run_in_executor(None, control.screenshot, destination)
        return ok(result, result["message"]) if result["ok"] else fail(result["message"])

    @registry.add(
        "clipboard",
        "Read from or write to the system clipboard.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["read", "write"]},
            "text": {"type": "string", "description": "Text to copy when action is 'write'"},
        }, "required": ["action"]},
        permission="system.control",
        category="system",
    )
    async def clipboard(ctx, action: str, text: str = ""):
        loop = asyncio.get_event_loop()
        if action == "read":
            result = await loop.run_in_executor(None, control.clipboard_read)
            return ok({"text": result.get("text", "")}) if result["ok"] else fail(result["message"])
        result = await loop.run_in_executor(None, control.clipboard_write, text)
        return ok(None, result["message"]) if result["ok"] else fail(result["message"])

    # ==================================================================
    @registry.add(
        "run_diagnostics",
        "Run a full self-check of every JARVIS subsystem plus the network and AI providers.",
        {"type": "object", "properties": {}},
        permission="system.info",
        category="system",
        timeout=90.0,
    )
    async def run_diagnostics(ctx):
        health = ctx.get("health")
        if health is None:
            return fail("The health service isn't available.")
        report = await health.full_check()
        problems = [
            f"{name}: {check['detail']}"
            for name, check in report["checks"].items()
            if check["status"] in ("offline", "error", "degraded")
        ]
        return ok(
            {"overall": report["overall"], "checks": report["checks"], "problems": problems},
            f"Overall: {report['overall']}." + (f" Issues: {'; '.join(problems)}" if problems else ""),
        )
