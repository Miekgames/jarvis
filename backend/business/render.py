"""The rendering primitives: noise, colour mapping, and the finishing passes.

Three things in here are what separate a wallpaper somebody pays for from an
obvious computer-generated gradient, and none of them are the interesting part
of the algorithm:

**Dithering.** A smooth gradient across 3840 pixels in 8-bit colour has far
fewer available steps than it has pixels to fill, so it lands as visible
banding -- broad stripes across the sky of every image. Adding a sub-pixel of
noise before quantising breaks the bands up into texture the eye reads as
smooth. Without this, every single output looks cheap at full size and fine in
a thumbnail, which is the worst possible way to find out.

**Grain.** A faint, even film grain over the whole frame. It reads as
deliberate, and it also disguises whatever banding survives the dithering.

**Vignette.** A gentle darkening at the edges, which keeps the eye in the
middle of the frame and stops a bright corner fighting with desktop icons.

The noise itself is fractal value noise built on numpy, generated at low
resolution and resampled up with PIL's bicubic filter. That is both faster
than evaluating noise per pixel at 4K and visually better: these fields want
to be smooth and organic, and interpolating a coarse grid gives exactly that.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

# Below this the grid is too coarse to interpolate into anything organic.
MIN_GRID = 2

# Dither amplitude, in output levels. One full level is the textbook figure
# for triangular dither: it is the point at which the quantisation error
# stops correlating with the signal, which is the entire objective. Less than
# that leaves the error partly correlated and the bands partly visible.
DITHER_STRENGTH = 1.0


# ---------------------------------------------------------------------------
# noise
# ---------------------------------------------------------------------------
def value_noise(shape: Tuple[int, int], grid: int, rng: np.random.Generator) -> np.ndarray:
    """One octave: a random grid, smoothly resampled to `shape`.

    Returns float32 in 0..1. `shape` is (height, width), as numpy has it.
    """
    height, width = shape
    grid = max(MIN_GRID, int(grid))
    aspect = max(1e-6, width / height)

    # Keep the noise cells square regardless of the frame's aspect, otherwise
    # every field comes out visibly stretched on an ultrawide.
    cells_y = grid
    cells_x = max(MIN_GRID, int(round(grid * aspect)))

    coarse = rng.random((cells_y, cells_x), dtype=np.float32)
    image = Image.fromarray((coarse * 255).astype(np.uint8), mode="L")
    resized = image.resize((width, height), Image.BICUBIC)
    return np.asarray(resized, dtype=np.float32) / 255.0


def fractal_noise(shape: Tuple[int, int], rng: np.random.Generator,
                  octaves: int = 5, grid: int = 4,
                  persistence: float = 0.5, lacunarity: float = 2.0) -> np.ndarray:
    """Several octaves of value noise summed, normalised to 0..1."""
    total = np.zeros(shape, dtype=np.float32)
    amplitude = 1.0
    total_amplitude = 0.0
    current = float(grid)

    for _ in range(max(1, octaves)):
        total += value_noise(shape, int(round(current)), rng) * amplitude
        total_amplitude += amplitude
        amplitude *= persistence
        current *= lacunarity

    total /= max(total_amplitude, 1e-6)
    return normalise(total)


def normalise(array: np.ndarray) -> np.ndarray:
    """Stretch to exactly 0..1. Flat input returns flat output, not NaN."""
    low = float(array.min())
    high = float(array.max())
    if high - low < 1e-9:
        return np.zeros_like(array, dtype=np.float32)
    return ((array - low) / (high - low)).astype(np.float32)


def warp(field: np.ndarray, rng: np.random.Generator,
         strength: float = 0.25, grid: int = 3) -> np.ndarray:
    """Push a field around with another field.

    Domain warping: the single cheapest way to make noise stop looking like
    noise. Straight fractal noise reads as cloud; warped noise reads as
    marble, smoke or landscape.
    """
    height, width = field.shape
    if strength <= 0:
        return field

    dx = (fractal_noise((height, width), rng, octaves=3, grid=grid) - 0.5) * 2.0
    dy = (fractal_noise((height, width), rng, octaves=3, grid=grid) - 0.5) * 2.0

    ys, xs = np.mgrid[0:height, 0:width]
    amount = strength * min(height, width) * 0.25
    sample_x = np.clip(xs + dx * amount, 0, width - 1).astype(np.float32)
    sample_y = np.clip(ys + dy * amount, 0, height - 1).astype(np.float32)
    return _sample_bilinear(field, sample_x, sample_y)


def _sample_bilinear(field: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Read a field at fractional coordinates, interpolating.

    Nearest-neighbour sampling here was a real, visible defect rather than a
    theoretical one. Neighbouring output pixels round to the same source
    pixel, so the warped field comes out in flat rectangular patches; the
    field is then resampled up by four, which turns each patch into an
    obvious block. Every warped image had them in the smooth regions, which
    is exactly where the eye looks.
    """
    height, width = field.shape
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)

    fx = (x - x0).astype(np.float32)
    fy = (y - y0).astype(np.float32)

    top = field[y0, x0] * (1 - fx) + field[y0, x1] * fx
    bottom = field[y1, x0] * (1 - fx) + field[y1, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------
def apply_ramp(field: np.ndarray, ramp: Sequence[Tuple[float, float, float]]) -> np.ndarray:
    """Map a 0..1 field through a colour ramp. Returns float32 (h, w, 3)."""
    table = np.asarray(ramp, dtype=np.float32)
    steps = table.shape[0]
    index = np.clip((field * (steps - 1)).astype(np.int32), 0, steps - 1)
    return table[index]


def gradient_backdrop(shape: Tuple[int, int],
                      ramp: Sequence[Tuple[float, float, float]],
                      angle: float = 90.0) -> np.ndarray:
    """A clean linear gradient across the frame at `angle` degrees."""
    height, width = shape
    radians = np.deg2rad(angle)
    ys, xs = np.mgrid[0:height, 0:width]
    projected = (xs / max(width - 1, 1)) * np.cos(radians) + \
                (ys / max(height - 1, 1)) * np.sin(radians)
    return apply_ramp(normalise(projected.astype(np.float32)), ramp)


# ---------------------------------------------------------------------------
# finishing
# ---------------------------------------------------------------------------
def vignette(image: np.ndarray, strength: float = 0.35) -> np.ndarray:
    """Darken towards the corners."""
    if strength <= 0:
        return image
    height, width = image.shape[:2]
    ys, xs = np.mgrid[0:height, 0:width]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    # Normalised radius, so the falloff is circular rather than stretched.
    radius = np.sqrt(((xs - cx) / max(cx, 1)) ** 2 + ((ys - cy) / max(cy, 1)) ** 2)
    mask = 1.0 - strength * np.clip(radius / np.sqrt(2.0), 0, 1) ** 2
    return image * mask[..., None].astype(np.float32)


def grain(image: np.ndarray, rng: np.random.Generator, amount: float = 0.012) -> np.ndarray:
    """Even film grain across the frame."""
    if amount <= 0:
        return image
    height, width = image.shape[:2]
    noise = rng.normal(0.0, amount, (height, width)).astype(np.float32)
    return image + noise[..., None]


def to_png_array(image: np.ndarray, rng: Optional[np.random.Generator] = None,
                 dither: bool = True) -> np.ndarray:
    """Float 0..1 to uint8, with dithering to kill gradient banding.

    This is the step that decides whether the output looks professional at
    full size. A 4K frame has 3840 columns and 8-bit colour offers 256 levels,
    so any gradient spanning the width produces bands roughly fifteen pixels
    across -- clearly visible, and present in every smooth image. A fraction
    of a level of noise added before rounding converts those hard steps into
    fine texture.
    """
    working = np.clip(image, 0.0, 1.0) * 255.0

    if dither:
        generator = rng if rng is not None else np.random.default_rng(0)
        # Triangular probability density noise: two uniforms summed. It gives
        # a flatter error spectrum than a single uniform, which is why it is
        # the standard choice for audio and image requantisation alike.
        shape = working.shape
        noise = (generator.random(shape, dtype=np.float32)
                 - generator.random(shape, dtype=np.float32)) * DITHER_STRENGTH
        working = working + noise

    return np.clip(np.rint(working), 0, 255).astype(np.uint8)


def to_image(array: np.ndarray, rng: Optional[np.random.Generator] = None) -> Image.Image:
    return Image.fromarray(to_png_array(array, rng), mode="RGB")


def mean_flat_run(array: np.ndarray) -> float:
    """Average length, in pixels, of a run of identical values along a row.

    A measurable stand-in for "does this look banded", because "it looks
    fine" is not something a test suite can assert.

    The obvious metric -- the fraction of neighbouring pixels that are equal
    -- turns out not to separate the two cases, and understanding why is the
    point. Dithering does *not* make every adjacent pixel differ. Most pixels
    still round to the nearest level either way, so that fraction stays high
    in both. What dithering changes is *where* the step from one level to the
    next happens: undithered it is at the same column on every row, which is
    precisely the visible stripe. Dithered, the steps scatter, so runs become
    short and ragged.

    So: a gradient crossing N levels over W pixels has runs of about W/N when
    undithered, and runs of a few pixels when dithered.
    """
    if array.ndim == 3:
        array = array[..., 0]
    row = array[array.shape[0] // 2].astype(np.int32)
    if row.size < 2:
        return float(row.size)

    # Number of runs = number of places the value changes, plus one.
    changes = int(np.count_nonzero(np.diff(row)))
    return float(row.size) / float(changes + 1)
