"""
Theme presets.

A theme is more than an accent colour: it retints the particle field itself
(near and far particle colours, core glow, background wash), because the field
*is* the interface. Presets live here so the backend can serve them to the UI
and so a mode or an automation can switch theme like any other setting.

Colours are hex strings for the CSS side; the renderer converts them to
normalised RGB.
"""
from __future__ import annotations

from typing import Any, Dict, List

# Each preset supplies the full palette. `particle_near` is the colour of
# foreground particles, `particle_far` the receding haze, `accent` the core and
# highlights, `accent_secondary` the gradient partner used by progress and
# links, and `backdrop` the page wash behind the field.
THEMES: Dict[str, Dict[str, Any]] = {
    "arc_reactor": {
        "name": "Arc Reactor",
        "description": "Cyan core, violet edges. The default.",
        "accent": "#5ad2ff",
        "accent_secondary": "#7b6cff",
        "particle_near": "#3890d9",
        "particle_far": "#17336b",
        "backdrop": "#071322",
    },
    "ember": {
        "name": "Ember",
        "description": "Warm amber, like a forge at low light.",
        "accent": "#ffa63d",
        "accent_secondary": "#ff5f4d",
        "particle_near": "#d97b2f",
        "particle_far": "#5c2410",
        "backdrop": "#1a0e07",
    },
    "verdant": {
        "name": "Verdant",
        "description": "Cool green and teal. Easy on the eyes for long sessions.",
        "accent": "#4fe0a8",
        "accent_secondary": "#31b5c9",
        "particle_near": "#2fa87d",
        "particle_far": "#0e3d33",
        "backdrop": "#05170f",
    },
    "nebula": {
        "name": "Nebula",
        "description": "Magenta and deep violet.",
        "accent": "#e070ff",
        "accent_secondary": "#5b7bff",
        "particle_near": "#a054d9",
        "particle_far": "#2d1652",
        "backdrop": "#10061f",
    },
    "monochrome": {
        "name": "Monochrome",
        "description": "Silver and white. No hue, maximum clarity.",
        "accent": "#e8f2ff",
        "accent_secondary": "#9fb3c8",
        "particle_near": "#8fa4b8",
        "particle_far": "#2b3642",
        "backdrop": "#0c0f13",
    },
    "abyss": {
        "name": "Abyss",
        "description": "Deep ocean blue with a pale aqua core.",
        "accent": "#63e8ff",
        "accent_secondary": "#2f6fd0",
        "particle_near": "#1f6fa8",
        "particle_far": "#0a2340",
        "backdrop": "#020c18",
    },
    "solar": {
        "name": "Solar",
        "description": "Gold and warm white. Bright without being loud.",
        "accent": "#ffd166",
        "accent_secondary": "#ff9f45",
        "particle_near": "#d9a441",
        "particle_far": "#4a3410",
        "backdrop": "#150f04",
    },
    "crimson": {
        "name": "Crimson",
        "description": "Red alert. Deliberately intense.",
        "accent": "#ff5c6e",
        "accent_secondary": "#b4304f",
        "particle_near": "#c2394f",
        "particle_far": "#4a0f1c",
        "backdrop": "#170406",
    },
}

DEFAULT_THEME = "arc_reactor"

# Keys a preset controls. Anything else in the `theme` setting (density,
# reduced_motion, font_scale, high_contrast) is a user preference and is
# preserved when the preset changes.
PALETTE_KEYS = ("accent", "accent_secondary", "particle_near", "particle_far", "backdrop")


def get(key: str) -> Dict[str, Any]:
    return THEMES.get(key, THEMES[DEFAULT_THEME])


def palette(key: str) -> Dict[str, str]:
    theme = get(key)
    return {field: theme[field] for field in PALETTE_KEYS}


def listing() -> List[Dict[str, Any]]:
    return [{"key": key, **theme} for key, theme in THEMES.items()]
