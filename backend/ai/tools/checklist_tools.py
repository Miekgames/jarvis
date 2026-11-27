"""Checklists by saying it.

"show me my checklists", "make a grocery list: eggs, milk and bread",
"add butter to my grocery list", "check off eggs", "uncheck eggs",
"remove bread from my grocery list", "reset my workout list", "clear the
checked ones from my grocery list", "delete my packing list".
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ...core.events import bus
from ...services.checklists import describe, split_items, tidy_title
from .registry import ToolRegistry, fail, ok

_LIST = r"(?:check\s*list|list)"
_SAID: List[Tuple[str, "re.Pattern[str]"]] = [
    ("show_all", re.compile(r"(?:show|open|pull\s+up|bring\s+up|see)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:my|the)\s+"
                            r"(?:check\s*lists|lists)|(?:my\s+)?check\s*lists|what\s+(?:check\s*lists|lists)\s+do\s+i\s+have", re.I)),
    ("show", re.compile(rf"(?:show|open|pull\s+up|bring\s+up|read)\s+(?:me\s+)?(?:my|the)\s+(?P<name>.+?)\s+{_LIST}"
                        rf"|what'?s\s+on\s+(?:my|the)\s+(?P<name2>.+?)\s+{_LIST}", re.I)),
    ("make", re.compile(rf"(?:make|create|start|new)\s+(?:me\s+)?(?:a\s+|an\s+|my\s+)?(?:new\s+)?(?P<name>[\w' -]*?)\s*(?P<kind>{_LIST})"
                        rf"(?:\s+(?:for|called|named)\s+(?P<name2>[^:]+?))?(?:\s*(?::|with|of|-)\s*(?P<items>.+))?", re.I)),
    ("add", re.compile(rf"(?:add|put)\s+(?P<items>.+?)\s+(?:to|on(?:to)?)\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST}", re.I)),
    ("uncheck_all", re.compile(rf"(?:reset|uncheck\s+(?:everything|all)\s+(?:on|in)|untick\s+(?:everything|all)\s+(?:on|in))"
                               rf"\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST}", re.I)),
    ("clear", re.compile(rf"(?:clear|remove|delete)\s+(?:the\s+|all\s+the\s+)?(?:checked|ticked|done|finished)"
                         rf"(?:\s+(?:items|ones|things))?\s+(?:from|on|off)\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST}", re.I)),
    ("uncheck", re.compile(rf"(?:uncheck|untick|unmark)\s+(?P<item>.+?)(?:\s+(?:on|from|in)\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST})?", re.I)),
    ("check", re.compile(rf"(?:(?:check|cross|tick)\s+off|check)\s+(?P<item>.+?)(?:\s+(?:on|from|in)\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST})?"
                         rf"|mark\s+(?P<item2>.+?)\s+(?:as\s+)?(?:done|complete|finished)", re.I)),
    ("remove", re.compile(rf"(?:remove|take|delete)\s+(?P<item>.+?)\s+(?:off|from)\s+(?:my|the)\s+(?P<name>.*?)\s*{_LIST}", re.I)),
    ("delete", re.compile(rf"delete\s+(?:my|the)\s+(?P<name>.+?)\s*{_LIST}", re.I)),
]


def checklist_said(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(tool, arguments) for a plainly worded checklist request, else None."""
    said = " ".join((message or "").strip().strip("?!.").replace("’", "'").split())
    said = re.sub(r"^(?:hey\s+)?jarvis[,:]?\s+", "", said, flags=re.I)
    said = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+)", "", said, flags=re.I)
    said = re.sub(r",?\s+(?:please|for me|jarvis)$", "", said, flags=re.I)
    for action, pattern in _SAID:
        m = pattern.fullmatch(said)
        if m is None:
            continue
        group = m.groupdict()
        name = (group.get("name") or group.get("name2") or "").strip()
        if action == "show_all":
            return "show_checklists", {}
        if action == "show":
            return "show_checklists", {"checklist": name}
        if action == "make":
            base = (group.get("name2") or group.get("name") or "").strip()
            kind = (group.get("kind") or "").lower().replace(" ", "")
            title = base if (kind == "checklist" or not base or base.lower().endswith("list")) else f"{base} list"
            return "make_checklist", {"title": tidy_title(title or "Checklist"),
                                      "items": split_items(group.get("items") or "")}
        if action == "add":
            return "add_to_checklist", {"items": split_items(group["items"]), "checklist": name}
        if action in ("check", "uncheck"):
            item = re.sub(r"^(?:the|my)\s+", "", (group.get("item") or group.get("item2") or "").strip(), flags=re.I)
            # "check the weather", "check my email": not a checklist item.
            if action == "check" and not m.group(0).lower().startswith(("check off", "cross off", "tick off", "mark")) \
                    and not name:
                return None
            return "check_off", {"item": item, "checklist": name, "done": action == "check"}
        if action == "remove":
            return "remove_from_checklist", {"item": group["item"].strip(), "checklist": name}
        if action == "uncheck_all":
            return "reset_checklist", {"checklist": name, "mode": "uncheck"}
        if action == "clear":
            return "reset_checklist", {"checklist": name, "mode": "clear_checked"}
        if action == "delete":
            return "delete_checklist", {"checklist": name}
    return None


def _show_on_screen(checklist_id: str = "") -> None:
    bus.publish("ui.open_panel", {"panel": "checklists"}, source="ui")
    bus.publish("checklists.show", {"id": checklist_id}, source="checklists")


async def _which(service, words: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The checklist the owner means, or why it isn't clear."""
    found = await service.find(words or "")
    if found is not None:
        return await service.get(found["id"]), ""
    lists = await service.all()
    if not lists:
        return None, "You don't have any checklists yet. Say “make a grocery list: eggs, milk and bread”."
    names = ", ".join(c["title"] for c in lists)
    if words:
        return None, f"There's no checklist called “{words}”. You have: {names}."
    return None, f"Which checklist? You have: {names}."


def register(registry: ToolRegistry) -> None:

    def service_of(ctx):
        return ctx.get("checklists")

    @registry.add(
        "show_checklists",
        "Show the owner's checklists (grocery list, workout...), or one of them: 'show me my "
        "checklists', 'what's on my grocery list?'. Opens the Checklists panel.",
        {"type": "object", "properties": {"checklist": {"type": "string", "description": "Which one, if named"}}},
        category="checklist checklists list lists grocery shopping workout todo show",
    )
    async def show_checklists(ctx, checklist: str = ""):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        if checklist:
            chosen, why = await _which(service, checklist)
            if chosen is None:
                return fail(why)
            _show_on_screen(chosen["id"])
            return ok(chosen, describe(chosen))
        lists = await service.all()
        _show_on_screen()
        if not lists:
            return ok({"checklists": []}, "You don't have any checklists yet. Say “make a grocery list: "
                                          "eggs, milk and bread” to start one.")
        rows = [f"- **{c['title']}** ({c['done']} of {c['total']} done)" for c in lists]
        return ok({"checklists": lists}, "Your checklists:\n" + "\n".join(rows))

    @registry.add(
        "make_checklist",
        "Make a new checklist with items to tick off: 'make a grocery list with eggs, milk and "
        "bread', 'make a workout checklist: squats, push-ups, plank'.",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "items": {"type": "array", "items": {"type": "string"}},
        }, "required": ["title"]},
        category="checklist list make create new grocery shopping workout todo",
    )
    async def make_checklist(ctx, title: str, items: Optional[List[str]] = None):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        made = await service.create(title, [str(i) for i in (items or [])])
        _show_on_screen(made["id"])
        return ok(made, f"Made it. {describe(made)}" if made["items"] else
                  f"Made **{made['title']}**. Say “add eggs to my {made['title'].lower()}” to fill it.")

    @registry.add(
        "add_to_checklist",
        "Add items to one of the owner's checklists: 'add butter and jam to my grocery list'.",
        {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "checklist": {"type": "string"},
        }, "required": ["items"]},
        category="checklist list add put grocery shopping workout todo",
    )
    async def add_to_checklist(ctx, items: List[str], checklist: str = ""):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        chosen, why = await _which(service, checklist)
        if chosen is None:
            return fail(why)
        updated = await service.add(chosen["id"], [str(i) for i in items])
        added = len(updated["items"]) - len(chosen["items"])
        return ok(updated, f"Added {added} to **{updated['title']}**." if added else
                  f"Those are already on **{updated['title']}**.")

    @registry.add(
        "check_off",
        "Tick off (or untick) an item on a checklist: 'check off eggs', 'uncheck squats'.",
        {"type": "object", "properties": {
            "item": {"type": "string"},
            "checklist": {"type": "string"},
            "done": {"type": "boolean", "description": "False to untick"},
        }, "required": ["item"]},
        category="checklist list check off tick cross done uncheck",
    )
    async def check_off(ctx, item: str, checklist: str = "", done: bool = True):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        if checklist:
            chosen, why = await _which(service, checklist)
            if chosen is None:
                return fail(why)
            found = service.find_item(chosen, item)
            matches = [{"checklist": chosen, "item": found}] if found else []
        else:
            matches = await service.find_item_anywhere(item)
        if not matches:
            return fail(f"I couldn't find “{item}” on {('**' + checklist + '**') if checklist else 'any checklist'}.")
        if len(matches) > 1:
            names = ", ".join(m["checklist"]["title"] for m in matches)
            return fail(f"“{item}” is on more than one checklist ({names}). Which one?")
        target = matches[0]
        updated = await service.set_done(target["checklist"]["id"], target["item"]["id"], bool(done))
        left = sum(1 for i in updated["items"] if not i["done"])
        state = "Checked off" if done else "Unchecked"
        tail = " That's everything!" if done and left == 0 else f" {left} left on **{updated['title']}**."
        return ok(updated, f"{state} {target['item']['text']}.{tail}")

    @registry.add(
        "remove_from_checklist",
        "Take an item off a checklist altogether: 'remove bread from my grocery list'.",
        {"type": "object", "properties": {"item": {"type": "string"}, "checklist": {"type": "string"}},
         "required": ["item"]},
        category="checklist list remove take off delete item",
    )
    async def remove_from_checklist(ctx, item: str, checklist: str = ""):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        chosen, why = await _which(service, checklist)
        if chosen is None:
            return fail(why)
        found = service.find_item(chosen, item)
        if found is None:
            return fail(f"“{item}” isn't on **{chosen['title']}**.")
        await service.remove_item(chosen["id"], found["id"])
        return ok({"removed": found["text"]}, f"Took {found['text']} off **{chosen['title']}**.")

    @registry.add(
        "reset_checklist",
        "Untick everything on a checklist so it can be used again ('reset my workout list'), or "
        "remove the ticked items ('clear the checked ones from my grocery list').",
        {"type": "object", "properties": {
            "checklist": {"type": "string"},
            "mode": {"type": "string", "enum": ["uncheck", "clear_checked"]},
        }},
        category="checklist list reset uncheck clear checked",
    )
    async def reset_checklist(ctx, checklist: str = "", mode: str = "uncheck"):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        chosen, why = await _which(service, checklist)
        if chosen is None:
            return fail(why)
        if mode == "clear_checked":
            updated = await service.clear_checked(chosen["id"])
            gone = len(chosen["items"]) - len(updated["items"])
            return ok(updated, f"Cleared {gone} checked item{'s' if gone != 1 else ''} from **{updated['title']}**.")
        updated = await service.uncheck_all(chosen["id"])
        return ok(updated, f"Everything on **{updated['title']}** is unchecked again.")

    @registry.add(
        "delete_checklist",
        "Delete a whole checklist: 'delete my packing list'. Asks first.",
        {"type": "object", "properties": {"checklist": {"type": "string"}}, "required": ["checklist"]},
        risk="medium",
        confirm_summary=lambda args: f"Delete the checklist “{args.get('checklist', '')}” and everything on it",
        category="checklist list delete remove",
    )
    async def delete_checklist(ctx, checklist: str):
        service = service_of(ctx)
        if service is None:
            return fail("Checklists aren't available.")
        chosen, why = await _which(service, checklist)
        if chosen is None:
            return fail(why)
        await service.delete(chosen["id"])
        return ok({"deleted": chosen["title"]}, f"Deleted **{chosen['title']}**.")
