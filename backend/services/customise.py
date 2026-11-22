"""
Interface customisation: custom themes, saved layouts, density and typography.

A custom theme is validated before it is stored -- every colour must parse, and
the palette must clear a real contrast check against its own backdrop. A theme
that renders unreadable text is a broken theme, and catching that here is
cheaper than discovering it in a dark room at midnight.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("services.customise")

REQUIRED_COLOURS = ["accent", "accent_secondary", "particle_near", "particle_far", "backdrop"]
HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Interface preferences that are not themes: sizing, motion, layout.
DENSITY = {"comfortable", "compact", "spacious"}
MOTION = {"full", "reduced", "still"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rgb(value: str):
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(value: str) -> float:
    def channel(component: int) -> float:
        c = component / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio -- a real calculation, not a guess."""
    light = _luminance(foreground)
    dark = _luminance(background)
    if light < dark:
        light, dark = dark, light
    return (light + 0.05) / (dark + 0.05)


def validate_theme(colours: Dict[str, Any]) -> Dict[str, Any]:
    """Check every colour parses and the accent is legible on the backdrop."""
    problems: List[str] = []
    cleaned: Dict[str, str] = {}

    for key in REQUIRED_COLOURS:
        value = str(colours.get(key, "")).strip()
        if not value:
            problems.append("'{}' is missing.".format(key))
            continue
        if not HEX.match(value):
            problems.append("'{}' is not a hex colour: {}".format(key, value))
            continue
        cleaned[key] = value.lower()

    warnings: List[str] = []
    if not problems:
        ratio = contrast_ratio(cleaned["accent"], cleaned["backdrop"])
        if ratio < 3.0:
            problems.append(
                "The accent has {:.1f}:1 contrast against the backdrop. It needs at "
                "least 3:1 to be readable.".format(ratio))
        elif ratio < 4.5:
            warnings.append(
                "Accent contrast is {:.1f}:1 -- fine for large text and glows, below "
                "the 4.5:1 that body text wants.".format(ratio))

        if contrast_ratio(cleaned["particle_far"], cleaned["backdrop"]) < 1.15:
            warnings.append(
                "The far particles are nearly the backdrop colour, so the field will "
                "look flat.")

    return {"valid": not problems, "problems": problems, "warnings": warnings,
            "colours": cleaned}


class CustomiseService:
    """Custom themes, saved layouts, and interface preferences."""

    def __init__(self, db, settings_store=None, themes=None):
        self.db = db
        self.settings_store = settings_store
        self.themes = themes

    # ==================================================================
    # themes
    # ==================================================================
    async def create_theme(self, name: str, colours: Dict[str, Any],
                           description: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A theme needs a name.")

        checked = validate_theme(colours or {})
        if not checked["valid"]:
            raise ValueError(" ".join(checked["problems"]))

        theme_id = "theme_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24]
        if not theme_id.strip("theme_"):
            theme_id = "theme_" + uuid.uuid4().hex[:8]

        payload = dict(checked["colours"])
        payload["name"] = name
        payload["description"] = description or "Custom theme."

        existing = await self.db.fetch_one(
            "SELECT id FROM custom_themes WHERE id = ?", (theme_id,))
        if existing:
            await self.db.execute(
                "UPDATE custom_themes SET name = ?, colours = ? WHERE id = ?",
                (name, json.dumps(payload), theme_id))
        else:
            await self.db.execute(
                "INSERT INTO custom_themes (id, name, colours, created_at) "
                "VALUES (?, ?, ?, ?)", (theme_id, name, json.dumps(payload), _now()))

        return {"id": theme_id, "name": name, "colours": payload,
                "warnings": checked["warnings"], "updated": bool(existing)}

    async def custom_themes(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM custom_themes ORDER BY name")
        out = []
        for row in rows:
            try:
                colours = json.loads(row["colours"])
            except (ValueError, TypeError):
                continue
            out.append({"id": row["id"], "name": row["name"], "custom": True, **colours})
        return out

    async def all_themes(self) -> List[Dict[str, Any]]:
        """Built-in presets plus custom themes, in one list for the picker."""
        presets: List[Dict[str, Any]] = []
        if self.themes:
            listing = self.themes.listing() if hasattr(self.themes, "listing") else []
            for theme in listing:
                entry = dict(theme)
                entry["custom"] = False
                presets.append(entry)
        return presets + await self.custom_themes()

    async def delete_theme(self, theme_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one(
            "SELECT name FROM custom_themes WHERE id = ?", (theme_id,))
        if not record:
            raise LookupError(
                "No custom theme with id {}. Built-in presets cannot be "
                "deleted.".format(theme_id))
        await self.db.execute("DELETE FROM custom_themes WHERE id = ?", (theme_id,))

        # If it was the active theme, fall back rather than leaving a dangling id.
        if self.settings_store:
            appearance = await self.settings_store.get("appearance", {})
            if appearance.get("theme") == theme_id:
                appearance["theme"] = "arc_reactor"
                await self.settings_store.set("appearance", appearance)
        return {"deleted": True, "name": record["name"]}

    async def preview_contrast(self, colours: Dict[str, Any]) -> Dict[str, Any]:
        """Report every meaningful contrast pair without saving anything."""
        checked = validate_theme(colours or {})
        if not checked["valid"]:
            return {"ok": False, "problems": checked["problems"]}

        clean = checked["colours"]
        pairs = [
            ("accent on backdrop", clean["accent"], clean["backdrop"]),
            ("near particles on backdrop", clean["particle_near"], clean["backdrop"]),
            ("far particles on backdrop", clean["particle_far"], clean["backdrop"]),
            ("secondary on backdrop", clean["accent_secondary"], clean["backdrop"]),
        ]
        return {
            "ok": True,
            "warnings": checked["warnings"],
            "contrast": [{"pair": label, "ratio": round(contrast_ratio(a, b), 2),
                          "passes_aa": contrast_ratio(a, b) >= 4.5,
                          "passes_large": contrast_ratio(a, b) >= 3.0}
                         for label, a, b in pairs],
        }

    # ==================================================================
    # layouts
    # ==================================================================
    async def save_layout(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A layout needs a name.")
        if not isinstance(config, dict):
            raise ValueError("A layout config must be an object.")

        layout_id = "layout_" + uuid.uuid4().hex[:10]
        existing = await self.db.fetch_one(
            "SELECT id FROM layouts WHERE lower(name) = lower(?)", (name,))
        if existing:
            layout_id = existing["id"]
            await self.db.execute("UPDATE layouts SET config = ? WHERE id = ?",
                                  (json.dumps(config), layout_id))
        else:
            await self.db.execute(
                "INSERT INTO layouts (id, name, config, created_at) VALUES (?, ?, ?, ?)",
                (layout_id, name, json.dumps(config), _now()))
        return {"id": layout_id, "name": name, "saved": True, "updated": bool(existing)}

    async def layouts(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM layouts ORDER BY name")
        out = []
        for row in rows:
            try:
                config = json.loads(row["config"])
            except (ValueError, TypeError):
                config = {}
            out.append({"id": row["id"], "name": row["name"], "config": config,
                        "created_at": row["created_at"]})
        return out

    async def layout(self, layout_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT * FROM layouts WHERE id = ?", (layout_id,))
        if not record:
            raise LookupError("No layout with id {}.".format(layout_id))
        return {"id": record["id"], "name": record["name"],
                "config": json.loads(record["config"])}

    async def delete_layout(self, layout_id: str) -> Dict[str, Any]:
        record = await self.db.fetch_one("SELECT name FROM layouts WHERE id = ?", (layout_id,))
        if not record:
            raise LookupError("No layout with id {}.".format(layout_id))
        await self.db.execute("DELETE FROM layouts WHERE id = ?", (layout_id,))
        return {"deleted": True, "name": record["name"]}

    # ==================================================================
    # interface preferences
    # ==================================================================
    async def set_interface(self, **prefs: Any) -> Dict[str, Any]:
        """Density, font scale, motion and field intensity, all validated."""
        if not self.settings_store:
            raise RuntimeError("Settings are not available.")

        current = await self.settings_store.get("appearance", {})
        problems = []

        if "density" in prefs and prefs["density"] is not None:
            value = str(prefs["density"]).lower()
            if value not in DENSITY:
                problems.append("Density must be one of: " + ", ".join(sorted(DENSITY)))
            else:
                current["density"] = value

        if "motion" in prefs and prefs["motion"] is not None:
            value = str(prefs["motion"]).lower()
            if value not in MOTION:
                problems.append("Motion must be one of: " + ", ".join(sorted(MOTION)))
            else:
                current["motion"] = value

        if "font_scale" in prefs and prefs["font_scale"] is not None:
            try:
                scale = float(prefs["font_scale"])
            except (TypeError, ValueError):
                problems.append("Font scale must be a number.")
            else:
                if not 0.8 <= scale <= 1.6:
                    problems.append("Font scale must be between 0.8 and 1.6.")
                else:
                    current["font_scale"] = round(scale, 2)

        if "particle_count" in prefs and prefs["particle_count"] is not None:
            try:
                count = int(prefs["particle_count"])
            except (TypeError, ValueError):
                problems.append("Particle count must be a whole number.")
            else:
                if not 2000 <= count <= 120000:
                    problems.append("Particle count must be between 2000 and 120000.")
                else:
                    current["particle_count"] = count

        if problems:
            raise ValueError(" ".join(problems))

        await self.settings_store.set("appearance", current)
        return {"appearance": current}

    async def interface(self) -> Dict[str, Any]:
        current = await self.settings_store.get("appearance", {}) if self.settings_store else {}
        return {
            "theme": current.get("theme", "arc_reactor"),
            "density": current.get("density", "comfortable"),
            "motion": current.get("motion", "full"),
            "font_scale": current.get("font_scale", 1.0),
            "particle_count": current.get("particle_count"),
        }
