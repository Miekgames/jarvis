"""Wallpaper generators.

Five styles, each one seeded and therefore reproducible: the same seed, size
and palette always produce the same image. That matters more than it sounds.
It means a pack can be described by a small manifest rather than stored as
gigabytes of PNG, a customer's "can I get that one in ultrawide" is a
one-line answer, and a generator change that quietly ruins an existing pack
shows up as a diff rather than as a refund request.

Every style renders its field at a fraction of the output size and resamples
up. For smooth organic fields that is not a compromise -- it is how you get
smoothness -- and it is what makes 4K output take a second rather than a
minute. The line-based style is the exception and draws at full resolution.

Composition note running through all of them: the busiest region is kept away
from the upper left, because that is where every operating system puts desktop
icons, and a wallpaper that makes them unreadable gets deleted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import render
from .palettes import Palette, ramp

# Field resolution as a fraction of output. A smooth field upsampled from a
# quarter-size render is indistinguishable from one computed at full size,
# and sixteen times cheaper.
FIELD_SCALE = 0.25
MIN_FIELD = 180

# Standard sizes. Named for what people actually search for.
RESOLUTIONS: Dict[str, Tuple[int, int]] = {
    "desktop-4k": (3840, 2160),
    "desktop-1440p": (2560, 1440),
    "desktop-1080p": (1920, 1080),
    "ultrawide": (3440, 1440),
    "macbook-16": (3456, 2234),
    "phone": (1290, 2796),
    "phone-android": (1440, 3120),
    "tablet": (2048, 2732),
}

DESKTOP_SIZES = ("desktop-4k", "desktop-1440p", "desktop-1080p", "ultrawide")
MOBILE_SIZES = ("phone", "phone-android", "tablet")


@dataclass(frozen=True)
class Spec:
    """Everything needed to reproduce one image exactly."""

    style: str
    palette: str
    seed: int
    width: int
    height: int

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def filename(self, index: Optional[int] = None) -> str:
        stem = f"{self.palette}-{self.style}-{self.seed:06d}"
        if index is not None:
            stem = f"{index:02d}-{stem}"
        return f"{stem}-{self.width}x{self.height}.png"


def _field_shape(width: int, height: int) -> Tuple[int, int]:
    """The reduced shape a field is computed at."""
    scale = FIELD_SCALE
    h = max(MIN_FIELD, int(height * scale))
    w = max(MIN_FIELD, int(width * scale))
    return (h, w)


def _upsample(field: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resample a 0..1 field up to the output size, smoothly."""
    if field.shape == (height, width):
        return field
    image = Image.fromarray((np.clip(field, 0, 1) * 65535).astype(np.uint16), mode="I;16")
    resized = image.resize((width, height), Image.BICUBIC)
    return np.asarray(resized, dtype=np.float32) / 65535.0


# ===========================================================================
# the styles
# ===========================================================================
def gradient_mesh(width: int, height: int, palette: Palette,
                  rng: np.random.Generator) -> np.ndarray:
    """Soft blended colour fields. The most broadly appealing of the five."""
    shape = _field_shape(width, height)
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    x_norm = (xs / max(w - 1, 1)).astype(np.float32)
    y_norm = (ys / max(h - 1, 1)).astype(np.float32)

    # A directional sweep is the backbone, not noise and not blobs.
    #
    # Two earlier attempts failed the same way. Warped fractal noise has no
    # large structure and resolves into murk; distance-weighted colour
    # centres average towards the middle of the ramp, so after normalising,
    # most of the frame lands on the palette's darkest entry -- Copper Dusk
    # came out as a near-black rectangle with one orange smear.
    #
    # Starting from a sweep guarantees the full ramp is used end to end, and
    # the frame reads as composed rather than as an accident. The pools below
    # then bend it, which is where the organic quality comes from.
    angle = float(rng.uniform(0.15, 0.85))
    sweep = (x_norm * angle + y_norm * (1.0 - angle)).astype(np.float32)

    # Soft pools that push the sweep locally lighter or darker.
    displacement = np.zeros(shape, dtype=np.float32)
    for _ in range(int(rng.integers(2, 5))):
        cx = float(rng.uniform(0.2, 0.95))
        cy = float(rng.uniform(0.15, 0.95))
        if cx < 0.4 and cy < 0.4:          # keep clear of the icon corner
            cx += 0.4
        radius = float(rng.uniform(0.25, 0.55))
        strength = float(rng.uniform(-0.3, 0.3))
        distance = np.sqrt((x_norm - cx) ** 2 + (y_norm - cy) ** 2)
        displacement += np.exp(-(distance / radius) ** 2).astype(np.float32) * strength

    field = render.normalise(sweep + displacement)
    field = render.warp(field, rng, strength=0.22, grid=3)

    # Quiet texture: enough to read as a surface, not enough to compete.
    detail = render.fractal_noise(shape, rng, octaves=4, grid=5, persistence=0.5)
    field = render.normalise(field * 0.91 + detail * 0.09)

    full = _upsample(field, width, height)
    image = render.apply_ramp(full, ramp(palette))
    image = render.vignette(image, 0.26)
    return render.grain(image, rng, 0.010)


def aurora(width: int, height: int, palette: Palette,
           rng: np.random.Generator) -> np.ndarray:
    """Layered flowing bands with a glow. Dramatic, reads well on phones."""
    shape = _field_shape(width, height)
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    y_norm = ys / max(h - 1, 1)

    accumulated = np.zeros(shape, dtype=np.float32)
    bands = rng.integers(3, 6)
    for band in range(int(bands)):
        drift = render.fractal_noise(shape, rng, octaves=3, grid=2) - 0.5
        centre = 0.25 + 0.5 * (band + 0.5) / bands
        thickness = 0.045 + 0.05 * float(rng.random())
        distance = np.abs(y_norm + drift * 0.45 - centre)
        ribbon = np.exp(-(distance / thickness) ** 2).astype(np.float32)
        accumulated += ribbon * (0.5 + 0.5 * float(rng.random()))

    field = render.normalise(accumulated)
    full = _upsample(field, width, height)

    # Deep background to light ribbons, rather than mapping straight through
    # the ramp -- an aurora is light *added* to a dark sky, not a recolouring.
    colours = ramp(palette)
    background = render.gradient_backdrop((height, width), colours[: len(colours) // 3], 95.0)
    light = render.apply_ramp(full, colours[len(colours) // 3:])
    image = background + light * full[..., None] * 1.15

    image = render.vignette(image, 0.4)
    return render.grain(image, rng, 0.012)


def topographic(width: int, height: int, palette: Palette,
                rng: np.random.Generator) -> np.ndarray:
    """Contour lines over a soft field. Looks like a map, sells like one."""
    shape = _field_shape(width, height)

    # Few octaves and a coarse grid, deliberately. Six octaves of detail
    # produced a terrain with features at every scale, and contouring that
    # gives a dense tangle of rings that reads as camouflage or a circuit
    # board -- not as a map. A real contour map is mostly open space with a
    # few large landforms, so the base has to be smooth before it is sliced.
    base = render.fractal_noise(shape, rng, octaves=3, grid=2, persistence=0.45)
    base = render.warp(base, rng, strength=0.22, grid=2)
    full = _upsample(render.normalise(base), width, height)

    colours = ramp(palette)
    # A restrained backdrop: the contours are the subject, so the underlying
    # field should not also be doing full-range colour work.
    low = int(len(colours) * 0.1)
    high = int(len(colours) * 0.75)
    image = render.apply_ramp(full, colours[low:high])

    lines = int(rng.integers(7, 12))
    scaled = full * lines
    edge = np.abs(scaled - np.rint(scaled))

    # Thickness in *field* units so lines keep the same visual weight at every
    # output resolution: at 4K a fixed fraction becomes a hairline, at 1080p
    # it becomes a smear.
    thickness = 0.055 * (1920.0 / max(width, 1))
    contour = np.clip(1.0 - edge / max(thickness, 1e-4), 0.0, 1.0) ** 1.5

    ink = np.asarray(colours[-1] if palette.dark else colours[0], dtype=np.float32)
    image = image * (1 - contour[..., None] * 0.7) + ink * contour[..., None] * 0.7

    image = render.vignette(image, 0.28)
    return render.grain(image, rng, 0.010)


def flow_field(width: int, height: int, palette: Palette,
               rng: np.random.Generator) -> np.ndarray:
    """Thousands of fine particle trails. The one that looks hand-made.

    Drawn at full resolution rather than upsampled: a thin line resampled up
    becomes a soft smear, and the appeal of this style is entirely in the
    crispness of the strokes.
    """
    colours = ramp(palette)
    background = render.gradient_backdrop((height, width), colours[:3], 115.0)

    # The direction field, computed small and read per step.
    shape = _field_shape(width, height)
    angles = render.fractal_noise(shape, rng, octaves=4, grid=2) * math.tau * 1.5
    fh, fw = shape

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Scale particle count and stroke length with area so density looks the
    # same at every resolution.
    area_factor = (width * height) / (1920 * 1080)
    particles = int(2200 * math.sqrt(area_factor))
    steps = 70
    step_length = max(1.4, 2.2 * math.sqrt(area_factor))

    ink_choices = colours[len(colours) // 2:]
    for _ in range(particles):
        x = float(rng.random()) * width
        y = float(rng.random()) * height
        colour = ink_choices[int(rng.integers(0, len(ink_choices)))]
        rgb = tuple(int(c * 255) for c in colour)

        points: List[Tuple[float, float]] = [(x, y)]
        for _ in range(steps):
            fx = min(fw - 1, max(0, int(x / max(width, 1) * fw)))
            fy = min(fh - 1, max(0, int(y / max(height, 1) * fh)))
            theta = float(angles[fy, fx])
            x += math.cos(theta) * step_length
            y += math.sin(theta) * step_length
            if not (0 <= x < width and 0 <= y < height):
                break
            points.append((x, y))

        if len(points) > 2:
            draw.line(points, fill=rgb, width=1)

    # Blur a copy and add it back: the strokes keep their edge while gaining
    # a glow, which is what stops dense line art reading as scratchy.
    glow = canvas.filter(ImageFilter.GaussianBlur(radius=max(2.0, width / 900)))
    strokes = np.asarray(canvas, dtype=np.float32) / 255.0
    bloom = np.asarray(glow, dtype=np.float32) / 255.0

    image = background + strokes * 0.55 + bloom * 0.75
    image = render.vignette(image, 0.38)
    return render.grain(image, rng, 0.011)


def dunes(width: int, height: int, palette: Palette,
          rng: np.random.Generator) -> np.ndarray:
    """Layered horizon curves. Minimal, calm, lots of negative space."""
    colours = ramp(palette)
    image = render.gradient_backdrop((height, width), colours[: len(colours) // 2], 90.0)

    layers = int(rng.integers(4, 7))
    ys, xs = np.mgrid[0:height, 0:width]
    x_norm = xs / max(width - 1, 1)
    y_norm = ys / max(height - 1, 1)

    for layer in range(layers):
        depth = (layer + 1) / layers
        # Each layer sits lower and is drawn in a lighter colour, which reads
        # as distance.
        base_height = 0.45 + 0.5 * depth
        wave = (np.sin(x_norm * math.tau * (0.6 + 0.5 * layer)
                       + float(rng.random()) * math.tau) * 0.035
                + np.sin(x_norm * math.tau * (1.7 + layer) + float(rng.random()) * math.tau)
                * 0.018)
        horizon = base_height + wave

        mask = (y_norm > horizon).astype(np.float32)
        # Soften the edge by a couple of pixels so it is not aliased.
        soft = np.clip((y_norm - horizon) * height / 2.5, 0.0, 1.0).astype(np.float32)
        mask = mask * soft

        index = min(len(colours) - 1, int((0.35 + 0.6 * depth) * (len(colours) - 1)))
        colour = np.asarray(colours[index], dtype=np.float32)
        image = image * (1 - mask[..., None]) + colour * mask[..., None]

    image = render.vignette(image, 0.22)
    return render.grain(image, rng, 0.009)


STYLES: Dict[str, Callable[..., np.ndarray]] = {
    "mesh": gradient_mesh,
    "aurora": aurora,
    "topographic": topographic,
    "flow": flow_field,
    "dunes": dunes,
}

STYLE_DESCRIPTIONS = {
    "mesh": "soft blended colour fields",
    "aurora": "layered flowing light over a deep sky",
    "topographic": "contour lines over a soft landscape",
    "flow": "thousands of fine particle trails",
    "dunes": "minimal layered horizons",
}


# ===========================================================================
def render_spec(spec: Spec) -> Image.Image:
    """Render one spec to an image. Deterministic for a given spec."""
    from .palettes import get

    if spec.style not in STYLES:
        raise KeyError(f"No style called {spec.style!r}. "
                       f"Available: {', '.join(sorted(STYLES))}")
    if spec.width < 1 or spec.height < 1:
        raise ValueError("Width and height must both be positive.")

    palette = get(spec.palette)
    # One generator, derived from the seed alone, so the same spec is the same
    # picture on any machine and in any order.
    rng = np.random.default_rng(spec.seed)
    array = STYLES[spec.style](spec.width, spec.height, palette, rng)

    # The dither generator is separate and seeded from the spec, so that
    # re-rendering gives a byte-identical file rather than one that merely
    # looks the same.
    dither_rng = np.random.default_rng(spec.seed ^ 0x5F3759DF)
    return render.to_image(array, dither_rng)
