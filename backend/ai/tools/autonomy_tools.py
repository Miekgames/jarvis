"""Asking about work done while away, and collecting it.

The While away tab shows all of this. These exist so it can simply be asked
for as well -- "I'm back, what did you build?", "send the app to my phone"
-- because an assistant that built something and can only tell you about it
through one particular screen has not really handed it over.

What is said here comes from the same record the tab reads: the changes
that were kept, which are by construction the ones in the file, and the
ones that were thrown away with the reason. Nothing is summarised from
memory of having done it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ...core.permissions import Risk
from .registry import ToolRegistry, fail, ok


def _account(service) -> Dict[str, Any]:
    """The current or latest run, as facts and as a paragraph."""
    made = service.produced()
    state = service.status()
    current = state.get("current") or {}
    running = bool(state.get("busy"))

    steps: List[Dict[str, Any]] = made.get("steps") or []
    kept = [step["title"] for step in steps if step.get("kept")]
    dropped = [{"change": step["title"], "why": step.get("detail", "")}
               for step in steps if not step.get("kept")]
    app = made.get("app") or {}
    artifact = made.get("artifact") or ""
    workspace = made.get("workspace") or ""
    trial = app.get("trial") or {}
    still_wrong: List[str] = list(trial.get("problems") or [])

    # Written in the first person, as JARVIS's own account. A small model
    # relays the shape of what it is handed: given "It built index.html" it
    # told the owner "it seems you're working on a habit tracker".
    said: List[str] = []
    if running:
        said.append(
            f"I am still working ({current.get('phase', 'working')}): "
            f"{current.get('elapsed_minutes', 0):.0f} of "
            f"{current.get('work_minutes', 0):.0f} minutes in.")

    if artifact:
        said.append(f"While you were away I built {artifact} "
                    f"({app.get('lines', '?')} lines, version "
                    f"{app.get('version', '?')}).")
        checked = app.get("checked")
        if checked == "browser":
            said.append("I opened every version in a real browser with the "
                        "network off and pressed its buttons, and kept only "
                        "versions with no errors.")
        elif checked == "syntax":
            said.append("I syntax-checked it but have NOT run it.")
        elif checked == "untested":
            said.append("I have NOT been able to test it in a browser.")
        # Running is not working. If using it turned up a fault that is
        # still there, that goes in before the list of what was added.
        if still_wrong:
            said.append("When I used it at the end this was STILL WRONG: "
                        + "; ".join(still_wrong) + ".")
        elif trial.get("ran") and trial.get("saved"):
            said.append("When I used it at the end the way a person would, "
                        "what I added survived a reload and nothing went "
                        "wrong.")
        if len(kept) > 1:
            said.append("In the app: " + "; ".join(kept[1:]) + ".")
        if dropped:
            said.append("I tried these and they are NOT in the app: "
                        + "; ".join(item["change"] for item in dropped) + ".")
        said.append("You can open it, or see and copy its full HTML code "
                    "with Show the code, in the While away tab.")
    elif made.get("mode") == "build":
        said.append("I did not manage to build an app. "
                    + (made.get("report") or ""))
    elif made.get("files"):
        said.append(f"I left {len(made['files'])} file(s) in {workspace}.")
        if made.get("report"):
            said.append(made["report"])

    return {
        "data": {
            "running": running,
            "request": made.get("prompt") or current.get("prompt", ""),
            "app_file": str(Path(workspace) / artifact) if artifact else "",
            "lines": app.get("lines"),
            "tested": app.get("checked", ""),
            "in_the_app": kept,
            "still_wrong": still_wrong,
            "tried_and_not_kept": dropped,
            "held_for_owner": current.get("held") or [],
        },
        "said": " ".join(said),
        "artifact": artifact,
        "workspace": workspace,
        "anything": bool(made.get("files")) or running,
    }


def register(registry: ToolRegistry) -> None:

    @registry.add(
        "away_work_report",
        "Report what JARVIS built or did while the user was away or out: the "
        "app it made, which changes are really in the app, which were tried "
        "and thrown away, and whether it is still working. Use for 'what did "
        "you build', 'what did you do while I was out', 'is the app "
        "finished', 'how is the build going', 'give me the code of the "
        "app'.",
        {"type": "object", "properties": {}},
        permission="files.read",
        category="away work app build",
    )
    async def away_work_report(ctx):
        service = ctx.get("autonomy")
        if service is None:
            return fail("Working while you are away isn't available.")
        account = _account(service)
        if not account["anything"]:
            return fail("Nothing has been built while you were away. A run "
                        "is started from the While away tab.")
        return ok(account["data"], account["said"])

    @registry.add(
        "welcome_back",
        "The user is back or home: hand over what was built while they were "
        "away and free the slot for the next run. If it is still working "
        "this asks it to wrap up. Use for 'I'm back', 'I'm home', 'I have "
        "returned'.",
        {"type": "object", "properties": {}},
        permission="files.read",
        category="away work app build",
    )
    async def welcome_back(ctx):
        service = ctx.get("autonomy")
        if service is None:
            return fail("Working while you are away isn't available.")
        # Read before acknowledging: once the slot is cleared the held list
        # goes with the live session, and that is the part worth saying.
        account = _account(service)
        reply = await service.welcome_back()
        if not account["anything"]:
            return ok(reply, "Welcome back. Nothing was left running.")
        lead = ("Welcome back. " if reply.get("acknowledged")
                else reply.get("detail", "") + " ")
        return ok({**account["data"], "acknowledged": bool(reply.get("acknowledged"))},
                  lead + account["said"])

    @registry.add(
        "send_built_app_to_device",
        "Send the app JARVIS built while the user was away to their phone or "
        "another of their own devices over Tailscale. Use for 'send the app "
        "to my phone', 'send me what you built', 'put the app on my phone'.",
        {"type": "object", "properties": {
            "device": {"type": "string",
                       "description": "Device name; defaults to the phone"},
        }},
        permission="files.read",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Send the app from the last away run to "
            f"{a.get('device') or 'your phone'}?"),
        category="away work app build",
    )
    async def send_built_app_to_device(ctx, device: str = "phone"):
        service = ctx.get("autonomy")
        taildrop = ctx.get("taildrop")
        if service is None or taildrop is None:
            return fail("Sending the app isn't available.")

        made = service.produced()
        artifact = made.get("artifact") or ""
        if not artifact:
            return fail("There is no built app to send yet.")
        # The path comes from the run's own record and is resolved by the
        # service inside that run's folder. Nothing the model supplies names
        # a file here, so this cannot be talked into sending something else.
        path = service.resolve_file(artifact, made.get("stamp", ""))
        if path is None:
            return fail(f"{artifact} is no longer in {made.get('workspace')}.")

        result = await taildrop.send(path, device or "phone")
        if not result.get("ok"):
            return fail(result.get("detail", "The send failed."))
        return ok(result, f"Sent {artifact}. " + result.get("detail", "")
                  + " On an iPhone it arrives in Files; open it in Safari to "
                    "use it, or open it straight from the While away tab.")
