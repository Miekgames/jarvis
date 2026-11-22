"""
Built-in operational task handlers: daily briefing, self-diagnostics, backup,
and bulk file organisation.
"""
from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from ...core.util import ahead, humanize_duration, new_id, now_iso, since, truncate
from ..engine import TaskContext

logger = logging.getLogger("jarvis.tasks.maintenance")


# ---------------------------------------------------------------------------
# daily briefing
# ---------------------------------------------------------------------------
async def briefing_handler(ctx: TaskContext) -> Dict[str, Any]:
    services = ctx.services
    settings_store = services["settings_store"]
    orchestrator = services["orchestrator"]

    config = await settings_store.get("briefing", {})
    include = set(ctx.params.get("include") or config.get("include") or
                  ["time", "weather", "calendar", "tasks", "notifications", "system"])
    parts: List[str] = []
    total = len(include) + 1
    step = 0

    if "time" in include:
        step += 1
        await ctx.step(step, total, "Time")
        parts.append(f"TIME: {datetime.now().astimezone().strftime('%A %d %B %Y, %H:%M')}")

    if "weather" in include:
        step += 1
        await ctx.step(step, total, "Weather")
        try:
            weather = await services["weather"].current()
            if weather.get("ok"):
                parts.append(f"WEATHER: {weather['summary']}")
            else:
                parts.append(f"WEATHER: unavailable ({weather.get('message')})")
        except Exception as exc:
            parts.append(f"WEATHER: unavailable ({exc})")

    if "calendar" in include:
        step += 1
        await ctx.step(step, total, "Calendar")
        events = await services["calendar"].upcoming(limit=6, hours=36)
        if events:
            listing = "; ".join(f"{e['title']} at {e['start_at'][11:16]}" for e in events)
            parts.append(f"CALENDAR: {listing}")
        else:
            parts.append("CALENDAR: nothing scheduled in the next 36 hours")

    if "tasks" in include:
        step += 1
        await ctx.step(step, total, "Tasks")
        active = await services["tasks"].list(active_only=True, limit=10)
        if active:
            parts.append("TASKS: " + "; ".join(f"{t['name']} ({t['status']})" for t in active))
        else:
            parts.append("TASKS: nothing running")

    if "notifications" in include:
        step += 1
        await ctx.step(step, total, "Notifications")
        summary = await services["notifications"].summarise()
        parts.append(f"NOTIFICATIONS: {summary['summary']}")

    if "system" in include:
        step += 1
        await ctx.step(step, total, "System")
        latest = services["metrics"].latest()
        if latest:
            parts.append(
                f"SYSTEM: CPU {latest.get('cpu')}%, memory {latest.get('mem')}%, "
                f"disk {latest.get('disk')}% used, uptime {humanize_duration(latest.get('uptime_seconds', 0))}"
            )

    await ctx.step(total, total, "Composing")
    spoken = await orchestrator.ask(
        "Turn these notes into a spoken morning briefing. Conversational, no markdown, no lists, "
        "no URLs. Lead with what actually matters today. Keep it under 120 words.\n\n"
        + "\n".join(parts),
        purpose="fast",
    )

    await services["notifications"].create(
        title="Daily briefing ready", body=truncate(spoken, 300), priority="normal", source="briefing"
    )
    return {"briefing": spoken, "raw": parts}


# ---------------------------------------------------------------------------
# self-diagnostics
# ---------------------------------------------------------------------------
async def diagnostics_handler(ctx: TaskContext) -> Dict[str, Any]:
    services = ctx.services
    health = services["health"]

    await ctx.progress(0.1, "Running checks")
    report = await health.full_check()
    await ctx.progress(0.8, "Summarising")

    failures = [name for name, check in report["checks"].items() if check["status"] in ("offline", "error")]
    degraded = [name for name, check in report["checks"].items() if check["status"] == "degraded"]

    lines = [f"{name}: {check['status'].upper()} — {check['detail']}" for name, check in report["checks"].items()]
    summary = "All systems nominal." if not failures and not degraded else (
        f"{len(failures)} offline, {len(degraded)} degraded."
    )

    try:
        spoken = await services["orchestrator"].ask(
            "Summarise this diagnostic report in two or three spoken sentences. Lead with anything broken.\n\n"
            + "\n".join(lines),
            purpose="fast",
        )
    except Exception:
        spoken = summary

    await ctx.progress(1.0, "Done")
    if failures:
        await services["notifications"].create(
            title="Diagnostics found problems",
            body=", ".join(failures),
            priority="high",
            source="diagnostics",
        )
    return {"summary": summary, "spoken": spoken, "report": report}


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
async def backup_handler(ctx: TaskContext) -> Dict[str, Any]:
    services = ctx.services
    db = services["db"]
    settings = services["settings"]

    await ctx.progress(0.1, "Preparing")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(settings.BACKUP_DIR) / f"jarvis_backup_{stamp}.db"

    await ctx.progress(0.4, "Copying the database")
    size = await db.backup_to(destination)

    await ctx.progress(0.75, "Verifying")
    verified = False
    try:
        import sqlite3

        connection = sqlite3.connect(str(destination))
        result = connection.execute("PRAGMA integrity_check").fetchone()
        row_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        connection.close()
        verified = bool(result and result[0] == "ok")
        await ctx.log(f"Verification: integrity={result[0] if result else '?'}, tasks table readable ({row_count} rows)")
    except Exception as exc:
        await ctx.log(f"Verification failed: {exc}", "error")

    backup_id = new_id("bkp")
    await db.execute(
        "INSERT INTO backups (id, ts, path, size, verified, note) VALUES (?,?,?,?,?,?)",
        (backup_id, now_iso(), str(destination), size, 1 if verified else 0, ctx.params.get("note", "")),
    )

    keep = int(ctx.params.get("keep", 10))
    await ctx.progress(0.9, "Pruning old backups")
    old = await db.fetch_all(
        "SELECT id, path FROM backups ORDER BY ts DESC LIMIT -1 OFFSET ?", (keep,)
    )
    removed = 0
    for row in old:
        try:
            Path(row["path"]).unlink(missing_ok=True)
            await db.execute("DELETE FROM backups WHERE id = ?", (row["id"],))
            removed += 1
        except OSError:
            continue

    await ctx.progress(1.0, "Done")
    if not verified:
        raise RuntimeError(f"Backup written to {destination} but failed verification.")
    return {
        "path": str(destination),
        "size_bytes": size,
        "verified": verified,
        "pruned": removed,
    }


# ---------------------------------------------------------------------------
# bulk file organisation
# ---------------------------------------------------------------------------
_CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tiff"},
    "Documents": {".pdf", ".docx", ".doc", ".txt", ".rtf", ".odt", ".md", ".epub"},
    "Spreadsheets": {".xlsx", ".xls", ".csv", ".ods"},
    "Presentations": {".pptx", ".ppt", ".odp"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".appimage", ".deb"},
    "Code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".rs", ".go", ".html", ".css"},
}


async def organise_handler(ctx: TaskContext) -> Dict[str, Any]:
    """Sort a folder into category subfolders. Dry-run by default so the user
    always sees the plan before anything moves."""
    files = ctx.services["files"]
    directory = ctx.params.get("path") or ""
    apply_changes = bool(ctx.params.get("apply", False))

    root = files.resolve(directory)
    if not root.is_dir():
        raise ValueError(f"'{directory}' is not a folder I'm allowed to organise.")

    await ctx.progress(0.1, "Scanning")
    entries = [p for p in root.iterdir() if p.is_file()]
    plan: Dict[str, List[str]] = defaultdict(list)

    for entry in entries:
        suffix = entry.suffix.lower()
        category = next((name for name, extensions in _CATEGORIES.items() if suffix in extensions), "Other")
        plan[category].append(entry.name)

    await ctx.log(f"Found {len(entries)} files across {len(plan)} categories.")
    if not apply_changes:
        await ctx.progress(1.0, "Plan ready (dry run)")
        return {
            "applied": False,
            "path": str(root),
            "plan": {k: v for k, v in plan.items()},
            "message": "Dry run only. Re-run with apply=true to move these files.",
        }

    moved, skipped = 0, []
    total_files = max(1, len(entries))
    for index, entry in enumerate(entries):
        ctx.checkpoint_now()
        suffix = entry.suffix.lower()
        category = next((name for name, extensions in _CATEGORIES.items() if suffix in extensions), "Other")
        target_dir = root / category
        target_dir.mkdir(exist_ok=True)
        target = target_dir / entry.name
        counter = 1
        while target.exists():
            target = target_dir / f"{entry.stem}_{counter}{entry.suffix}"
            counter += 1
        try:
            shutil.move(str(entry), str(target))
            moved += 1
        except OSError as exc:
            skipped.append(f"{entry.name}: {exc}")
        if index % 10 == 0:
            await ctx.progress(0.1 + 0.85 * index / total_files, f"Moved {moved} files")

    await ctx.progress(1.0, "Done")
    return {
        "applied": True,
        "path": str(root),
        "moved": moved,
        "skipped": skipped,
        "categories": sorted(plan),
    }


# ---------------------------------------------------------------------------
# housekeeping (retention)
# ---------------------------------------------------------------------------
async def housekeeping_handler(ctx: TaskContext) -> Dict[str, Any]:
    services = ctx.services
    db = services["db"]
    auth = services.get("auth")

    await ctx.progress(0.2, "Pruning old tasks")
    tasks_removed = await services["tasks"].purge(keep_days=int(ctx.params.get("keep_days", 30)))

    await ctx.progress(0.4, "Pruning metrics")
    metrics_removed = await db.execute(
        "DELETE FROM metrics WHERE ts < ?", (since(services["settings"].METRICS_RETENTION_HOURS * 3600),)
    )

    await ctx.progress(0.6, "Pruning activity log")
    activity_removed = await db.execute("DELETE FROM activity_log WHERE ts < ?", (since(30 * 86400),))

    await ctx.progress(0.75, "Expiring sessions")
    sessions_removed = await auth.purge_expired() if auth else 0

    await ctx.progress(0.9, "Compacting the database")
    await db.vacuum()

    await ctx.progress(1.0, "Done")
    return {
        "tasks_removed": tasks_removed,
        "metrics_removed": metrics_removed,
        "activity_removed": activity_removed,
        "sessions_removed": sessions_removed,
    }
