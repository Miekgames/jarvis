"""Colour, which is most of the work.

The honest truth about procedural wallpaper art is that the algorithm matters
far less than the palette. The same gradient routine produces something people
pay for or something that looks like a test pattern, and the only difference
is which five colours went in. So these are curated and named rather than
generated: a random walk through colour space produces mud most of the time.

Each palette carries a mood word used in listing copy and in filenames, and
a `dark` flag, because a wallpaper's job includes not blinding someone at
night and not washing out their desktop icons at noon.
"""
from __future__ import annotations

import colorsys
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class Palette:
    """An ordered set of colours, darkest-to-lightest by convention."""

    name: str
    mood: str
    colors: Tuple[str, ...]
    dark: bool = True

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "-")

    def rgb(self) -> List[Tuple[float, float, float]]:
        """Colours as 0..1 float triples."""
        return [hex_to_rgb(c) for c in self.colors]


def hex_to_rgb(value: str) -> Tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return (int(value[0:2], 16) / 255.0,
            int(value[2:4], 16) / 255.0,
            int(value[4:6], 16) / 255.0)


def rgb_to_hex(rgb: Sequence[float]) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        *(max(0, min(255, int(round(c * 255)))) for c in rgb[:3]))


def luminance(rgb: Sequence[float]) -> float:
    """Perceptual lightness, for deciding whether a palette reads as dark."""
    r, g, b = rgb[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------
# Deliberately a small, strong collection rather than a large weak one. Every
# one of these has been chosen so that a smooth blend between any two of its
# members stays clean -- no muddy midpoints, which is what kills
# procedurally generated gradients.
PALETTES: List[Palette] = [
    Palette("Midnight Harbour", "calm",
            ("#05080f", "#0d1b2a", "#1b3a5b", "#2e6f95", "#8ecae6"), dark=True),
    Palette("Ember", "warm",
            ("#0b0405", "#2d0f10", "#7a1f1c", "#c84b31", "#ecaa54"), dark=True),
    Palette("Deep Forest", "grounded",
            ("#040a07", "#0f2417", "#1f4d35", "#3c8c5a", "#9fd8a0"), dark=True),
    Palette("Nebula", "vivid",
            ("#07030f", "#1e0a36", "#45197d", "#8b3fc4", "#e0a9f5"), dark=True),
    Palette("Slate Rain", "quiet",
            ("#0a0c0e", "#1c2126", "#3a444e", "#6b7a88", "#b9c6d2"), dark=True),
    Palette("Copper Dusk", "warm",
            ("#0d0806", "#2b1810", "#5e3220", "#a85c32", "#e8a461"), dark=True),
    Palette("Arctic", "cold",
            ("#f2f7fa", "#d4e4ee", "#9dc0d6", "#5c88a8", "#2b4f6b"), dark=False),
    Palette("Sand", "soft",
            ("#fbf6ee", "#efe2cd", "#d9c2a0", "#b8946b", "#7d5f3e"), dark=False),
    Palette("Monochrome", "minimal",
            ("#050505", "#1f1f1f", "#4a4a4a", "#8a8a8a", "#d6d6d6"), dark=True),
    Palette("Tidepool", "fresh",
            ("#03090c", "#07323a", "#0d6b6b", "#2faa8e", "#9ae6b8"), dark=True),
    Palette("Bloom", "soft",
            ("#fdf3f3", "#f7d9dd", "#e9a7b4", "#c4728c", "#8a4260"), dark=False),
    Palette("Storm Front", "dramatic",
            ("#06070a", "#12161f", "#283449", "#4d6382", "#93a9c6"), dark=True),
]

BY_NAME = {p.slug: p for p in PALETTES}


def get(name: str) -> Palette:
    """Look a palette up by name or slug. Raises KeyError with the options."""
    slug = name.lower().replace(" ", "-")
    if slug not in BY_NAME:
        raise KeyError(
            f"No palette called {name!r}. Available: "
            + ", ".join(sorted(BY_NAME)))
    return BY_NAME[slug]


def ramp(palette: Palette, steps: int = 256) -> List[Tuple[float, float, float]]:
    """The palette as a smooth lookup table of `steps` colours.

    Interpolated through HSV rather than RGB. Blending #c84b31 to #8ecae6 in
    RGB passes through a dead grey at the midpoint; through hue it travels the
    short way round the wheel and stays saturated the whole distance, which is
    the difference between a gradient that looks designed and one that looks
    like a crossfade.
    """
    if steps < 2:
        raise ValueError("A ramp needs at least two steps.")

    stops = palette.rgb()
    if len(stops) == 1:
        return [stops[0]] * steps

    out: List[Tuple[float, float, float]] = []
    segments = len(stops) - 1
    for index in range(steps):
        position = index / (steps - 1) * segments
        low = min(int(position), segments - 1)
        t = position - low
        out.append(_blend_hsv(stops[low], stops[low + 1], t))
    return out


def _blend_hsv(a: Sequence[float], b: Sequence[float], t: float) -> Tuple[float, float, float]:
    ah, as_, av = colorsys.rgb_to_hsv(*a[:3])
    bh, bs, bv = colorsys.rgb_to_hsv(*b[:3])

    # Travel the short way around the wheel.
    delta = bh - ah
    if delta > 0.5:
        delta -= 1.0
    elif delta < -0.5:
        delta += 1.0
    hue = (ah + delta * t) % 1.0

    # A colour with no saturation has no meaningful hue, so interpolating
    # towards or away from grey must not drag the hue along with it.
    if as_ < 1e-4:
        hue = bh
    elif bs < 1e-4:
        hue = ah

    return colorsys.hsv_to_rgb(hue,
                               as_ + (bs - as_) * t,
                               av + (bv - av) * t)


def describe(palette: Palette) -> str:
    """A sentence for listing copy."""
    tone = "dark" if palette.dark else "light"
    return (f"{palette.name} — a {palette.mood}, {tone} palette built around "
            f"{palette.colors[2]} and {palette.colors[-1]}.")
