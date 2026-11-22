"""The wallpaper workshop, and getting its output onto your devices.

Generating images and packaging them, plus sending files to the owner's own
machines over Tailscale. Nothing here publishes, uploads to a storefront or
posts anywhere -- the selling layer that once sat alongside this was removed
rather than disabled, so there is no dormant code path to a shop.

Building a pack is minutes of CPU and a few hundred megabytes, so it asks
first. Everything else is read-only or sends a file the owner asked for.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.events import bus
from ...core.permissions import Risk
from .registry import ToolRegistry, fail, ok


def register(registry: ToolRegistry) -> None:
    register_taildrop(registry)

    # ==================================================================
    # what can be made
    # ==================================================================
    @registry.add(
        "wallpaper_options",
        "List the wallpaper styles, colour palettes and screen sizes available "
        "for making a wallpaper pack. Use before making one, or when asked "
        "'what palettes do you have' or 'what can you generate'.",
        {"type": "object", "properties": {}},
        permission="files.read",
        category="business",
    )
    async def wallpaper_options(ctx):
        service = ctx.get("business")
        if service is None:
            return fail("The product workshop isn't available.")
        data = service.catalogue()
        return ok(data,
                  f"{len(data['styles'])} styles, {len(data['palettes'])} palettes, "
                  f"{len(data['resolutions'])} screen sizes.")

    @registry.add(
        "wallpaper_packs",
        "List the wallpaper packs already generated, with their size and where "
        "they are on disk. Use for 'what have you made', 'show my packs'.",
        {"type": "object", "properties": {}},
        permission="files.read",
        category="business",
    )
    async def wallpaper_packs(ctx):
        service = ctx.get("business")
        if service is None:
            return fail("The product workshop isn't available.")
        products = service.products()
        if not products:
            return ok({"packs": []}, "No packs have been built yet.")
        newest = products[0]
        return ok({"packs": products},
                  f"{len(products)} pack(s). Most recent: {newest['name']}, "
                  f"{newest['designs']} designs, {newest['megabytes']} MB.")

    # ==================================================================
    # estimating, then making
    # ==================================================================
    @registry.add(
        "wallpaper_estimate",
        "Work out how long a wallpaper pack would take and how much disk it "
        "would use, without making it. Use when asked 'how big would that be'.",
        {"type": "object", "properties": {
            "palette": {"type": "string", "description": "Palette slug, e.g. 'nebula'"},
            "count": {"type": "integer", "description": "How many designs (default 8)"},
            "resolutions": {"type": "array", "items": {"type": "string"}},
        }, "required": ["palette"]},
        permission="files.read",
        category="business",
    )
    async def wallpaper_estimate(ctx, palette: str, count: int = 8,
                                 resolutions: Optional[List[str]] = None):
        service = ctx.get("business")
        if service is None:
            return fail("The product workshop isn't available.")
        try:
            plan = service.plan(palette, count=count, resolutions=resolutions)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))

        numbers = service.estimate(plan)
        message = (f"{numbers['files']} files, about "
                   f"{numbers['megabytes']:.0f} MB.")
        if not numbers.get("fits", True):
            message += " " + numbers.get("warning", "")
        return ok({"plan": _plan_dict(plan), "estimate": numbers}, message)

    @registry.add(
        "wallpaper_pack_create",
        "Generate a themed pack of original wallpapers at several screen sizes, "
        "ready to sell. Takes a few minutes and writes a few hundred megabytes. "
        "Use for 'make a wallpaper pack', 'generate some wallpapers to sell'.",
        {"type": "object", "properties": {
            "palette": {"type": "string",
                        "description": "Palette slug. Call wallpaper_options for the list."},
            "count": {"type": "integer", "description": "How many designs (default 8)"},
            "name": {"type": "string", "description": "Pack name; defaults to the palette"},
            "styles": {"type": "array", "items": {"type": "string"},
                       "description": "Which styles to use; omit for a sensible mix"},
            "resolutions": {"type": "array", "items": {"type": "string"}},
            "overwrite": {"type": "boolean",
                          "description": "Rebuild over an existing pack of the same name"},
        }, "required": ["palette"]},
        permission="files.write",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Generate {a.get('count', 8)} {a.get('palette')} wallpapers at "
            f"several sizes? This takes a few minutes and a few hundred MB."),
        category="business",
    )
    async def wallpaper_pack_create(ctx, palette: str, count: int = 8,
                                    name: Optional[str] = None,
                                    styles: Optional[List[str]] = None,
                                    resolutions: Optional[List[str]] = None,
                                    overwrite: bool = False):
        service = ctx.get("business")
        if service is None:
            return fail("The product workshop isn't available.")
        try:
            plan = service.plan(palette, count=count, styles=styles,
                                resolutions=resolutions, name=name)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))

        def publish(state: Dict[str, Any]) -> None:
            # The event bus is a module-level singleton, not a context
            # service: ctx.get("bus") returns None, so progress would have
            # been silently dropped and a five-minute build would have shown
            # nothing at all in the interface.
            try:
                bus.publish("business.progress", state, source="business")
            except Exception:
                pass

        try:
            summary = await service.build(plan, overwrite=overwrite,
                                          on_progress=publish)
        except FileExistsError as exc:
            return fail(str(exc))
        except OSError as exc:
            return fail(str(exc))

        return ok({"pack": summary},
                  f"Built '{summary['name']}': {summary['wallpapers']} designs, "
                  f"{summary['files']} files, {summary['megabytes']} MB. "
                  f"Preview is in {summary['directory']}.")

    # ==================================================================
    # drafting the sale
    # ==================================================================


def _plan_dict(plan) -> Dict[str, Any]:
    return {
        "name": plan.name, "slug": plan.slug, "palette": plan.palette,
        "styles": list(plan.styles), "count": plan.count,
        "resolutions": list(plan.resolutions), "seed": plan.seed,
        "price_usd": plan.price_usd,
    }





def register_taildrop(registry: ToolRegistry) -> None:
    """Sending files to the owner's own devices over Tailscale."""

    @registry.add(
        "list_my_devices",
        "List the devices on the user's Tailscale network that files can be "
        "sent to. Use for 'what devices can you send to', or before sending.",
        {"type": "object", "properties": {}},
        permission="files.read",
        category="business",
    )
    async def list_my_devices(ctx):
        service = ctx.get("taildrop")
        if service is None:
            return fail("Tailscale sending isn't available.")
        ready = service.availability()
        if not ready.get("available"):
            return fail(ready.get("detail", "Tailscale is not installed."))
        devices = [d.as_dict() for d in await service.devices()]
        others = [d for d in devices if not d["is_self"]]
        return ok({"devices": devices},
                  f"{len(others)} other device(s) online: "
                  + (", ".join(f"{d['name']} ({d['os']})" for d in others)
                     or "none"))

    @registry.add(
        "send_wallpapers_to_phone",
        "Send one or more wallpaper images to the user's phone over Tailscale. "
        "Sends the phone-sized versions, not the desktop ones. Use for 'send "
        "an image from the products to my phone', 'send me a wallpaper', 'put "
        "one of those on my phone', 'send me those wallpapers'. The pack is "
        "optional — without one it picks from the most recent pack.",
        {"type": "object", "properties": {
            "pack": {"type": "string",
                     "description": "Pack name or slug. Leave out to use the "
                                    "most recently built pack."},
            "count": {"type": "integer",
                      "description": "How many to send. Use 1 for 'an image' "
                                     "or 'a wallpaper'. Default 1, max 12."},
            "device": {"type": "string",
                       "description": "Which device; defaults to the phone"},
        }},
        permission="files.read",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Send {a.get('count', 1)} wallpaper(s) from "
            f"{a.get('pack') or 'the latest pack'} to "
            f"{a.get('device') or 'your phone'}?"),
        category="business",
    )
    async def send_wallpapers_to_phone(ctx, pack: str = "", count: int = 1,
                                       device: str = "phone"):
        business = ctx.get("business")
        taildrop = ctx.get("taildrop")
        if business is None or taildrop is None:
            return fail("Sending wallpapers isn't available.")

        import random
        from pathlib import Path

        # "send an image from the products to my phone" names no pack, which
        # is the natural way to ask. Without one, draw from everything that
        # has been built rather than defaulting to a particular pack --
        # otherwise asking twice sends the same picture twice, which is not
        # what anyone means by "an image".
        if not pack.strip():
            products = business.products()
            if not products:
                return fail("No wallpaper packs have been built yet.")
            sources = products
        else:
            product = business.find(pack)
            if product is None:
                known = ", ".join(p["slug"] for p in business.products()) or "none"
                return fail(f"No pack called {pack!r}. Built so far: {known}")
            sources = [product]

        candidates = []
        for item in sources:
            candidates.extend(
                sorted((Path(item["directory"]) / "wallpapers" / "mobile").glob("*.png")))

        if not candidates:
            where = sources[0]["name"] if len(sources) == 1 else "any pack"
            return fail(f"No phone-sized wallpapers found in {where}.")

        # Chosen at random, without replacement. Phone-sized only: a
        # 3840x2160 desktop wallpaper sent to a phone is four times the size
        # and the wrong shape.
        wanted = max(1, min(int(count), 12, len(candidates)))
        chosen = random.sample(candidates, wanted)

        result = await taildrop.send_many(chosen, device)
        if not result.get("sent"):
            first = (result.get("results") or [{}])[0]
            return fail(first.get("detail", "Nothing could be sent."))

        where = (result["results"][0].get("device") or {}).get("name", device)
        return ok(result,
                  f"Sent {result['sent']} of {result['total']} wallpaper(s) to "
                  f"{where}: {', '.join(_describe(c) for c in chosen)}. "
                  "Open Tailscale on the phone to save them.")

    @registry.add(
        "send_file_to_device",
        "Send a file from this computer to one of the user's own devices over "
        "Tailscale. Use for 'send that to my phone'.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "The file to send"},
            "device": {"type": "string", "description": "Device name or 'phone'"},
        }, "required": ["path"]},
        permission="files.read",
        risk=Risk.MEDIUM,
        confirm_summary=lambda a: (
            f"Send {a.get('path')} to {a.get('device') or 'your phone'}?"),
        category="business",
    )
    async def send_file_to_device(ctx, path: str, device: str = "phone"):
        service = ctx.get("taildrop")
        files = ctx.get("files")
        if service is None:
            return fail("Tailscale sending isn't available.")

        from pathlib import Path

        target = Path(path)
        # Confine it to the folders JARVIS is allowed to read, so "send a
        # file to my phone" cannot be turned into exfiltrating anything on
        # the machine.
        if files is not None:
            try:
                target = files.resolve(str(target))
            except Exception as exc:
                return fail(str(exc))

        result = await service.send(target, device)
        if not result.get("ok"):
            return fail(result.get("detail", "The send failed."))
        return ok(result, result["detail"])


def _describe(path) -> str:
    """A readable name for a wallpaper file.

    Filenames are "<palette>-<style>-<seed>-<width>x<height>.png", and the
    palette may itself contain hyphens ("midnight-harbour"). Splitting from
    the left guesses wrong; the seed is the first all-digit part, so
    everything before it is the name.
    """
    parts = path.stem.split("-")
    for index, part in enumerate(parts):
        if part.isdigit():
            return " ".join(parts[:index]) or path.stem
    return path.stem
