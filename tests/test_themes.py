"""Theme presets and how applying one interacts with user display preferences."""
from __future__ import annotations

import re

import pytest

from backend.services import themes


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def test_every_theme_defines_a_complete_palette():
    for key, theme in themes.THEMES.items():
        assert theme["name"], f"{key} has no name"
        assert theme["description"], f"{key} has no description"
        for field in themes.PALETTE_KEYS:
            assert field in theme, f"{key} is missing '{field}'"
            assert HEX.match(theme[field]), f"{key}.{field} is not a hex colour: {theme[field]}"


def test_default_theme_exists():
    assert themes.DEFAULT_THEME in themes.THEMES


def test_unknown_key_falls_back_to_the_default():
    assert themes.get("does-not-exist") == themes.THEMES[themes.DEFAULT_THEME]


def test_palette_returns_only_palette_keys():
    palette = themes.palette("ember")
    assert set(palette) == set(themes.PALETTE_KEYS)
    assert palette["accent"] == themes.THEMES["ember"]["accent"]


def test_listing_includes_the_key():
    listed = themes.listing()
    assert len(listed) == len(themes.THEMES)
    assert all("key" in entry for entry in listed)
    assert {entry["key"] for entry in listed} == set(themes.THEMES)


def test_themes_are_visually_distinct():
    """Two presets that render identically would be pointless."""
    seen = {}
    for key, theme in themes.THEMES.items():
        signature = (theme["accent"], theme["particle_near"], theme["particle_far"])
        assert signature not in seen, f"{key} is identical to {seen[signature]}"
        seen[signature] = key


@pytest.mark.asyncio
async def test_applying_a_theme_preserves_display_preferences(settings_store):
    """Switching preset must not reset the user's own accessibility settings."""
    await settings_store.patch("theme", {"font_scale": 1.25, "reduced_motion": True,
                                         "high_contrast": True, "density": "compact"})

    await settings_store.patch("theme", {"preset": "crimson", **themes.palette("crimson")})
    theme = await settings_store.get("theme")

    assert theme["preset"] == "crimson"
    assert theme["accent"] == themes.THEMES["crimson"]["accent"]
    assert theme["particle_far"] == themes.THEMES["crimson"]["particle_far"]
    # The preferences survive the palette swap.
    assert theme["font_scale"] == 1.25
    assert theme["reduced_motion"] is True
    assert theme["high_contrast"] is True
    assert theme["density"] == "compact"


@pytest.mark.asyncio
async def test_default_settings_ship_a_valid_preset(settings_store):
    theme = await settings_store.get("theme")
    assert theme["preset"] in themes.THEMES
    # The shipped defaults must match the preset they name, or the first paint
    # would disagree with the theme picker.
    palette = themes.palette(theme["preset"])
    for field, value in palette.items():
        assert theme[field] == value, f"default theme.{field} does not match its preset"
