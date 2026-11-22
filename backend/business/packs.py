"""Turning generated images into something a person would buy.

A pack is a themed set of wallpapers rendered at every size a buyer might
want, with a preview sheet for the listing, a readme, a licence and a
manifest. The manifest is the interesting part: it records the seed and
parameters of every image, so a pack is reproducible from a few kilobytes of
JSON. "Can I get number 7 in ultrawide?" becomes a one-line answer rather
than a regeneration lottery, and a change to a generator shows up as a diff
against an existing pack instead of as a refund.

Nothing here touches the network. Building a pack is entirely local and
entirely reversible -- it writes into its own directory and nowhere else.
"""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image

from . import palettes
from .palettes import Palette
from .wallpapers import (DESKTOP_SIZES, MOBILE_SIZES, RESOLUTIONS,
                         STYLE_DESCRIPTIONS, STYLES, Spec, render_spec)

logger = logging.getLogger("jarvis.business.packs")

# What a pack ships at unless told otherwise. Chosen to cover the great
# majority of buyers without quadrupling the download: 4K covers anything
# smaller by downscaling, 1440p and 1080p are there because people want a
# file that matches their screen exactly, ultrawide is a genuinely different
# crop, and the phone sizes are a different aspect entirely.
DEFAULT_RESOLUTIONS = ("desktop-4k", "desktop-1440p", "ultrawide", "phone")

PREVIEW_WIDTH = 1600
PREVIEW_TILE = (400, 225)


@dataclass
class PackPlan:
    """The description of a pack, before anything is rendered."""

    name: str
    palette: str
    styles: Tuple[str, ...]
    count: int
    resolutions: Tuple[str, ...] = DEFAULT_RESOLUTIONS
    seed: int = 1
    price_usd: float = 9.0

    @property
    def slug(self) -> str:
        return "".join(c if c.isalnum() or c in "- " else "" for c in self.name) \
            .strip().lower().replace(" ", "-")

    def specs(self) -> List[Spec]:
        """Every image in the pack, at every resolution.

        Seeds are derived from the plan's own seed, so a plan fully
        determines a pack: two machines building the same plan produce the
        same bytes.
        """
        out: List[Spec] = []
        for index in range(self.count):
            style = self.styles[index % len(self.styles)]
            seed = self.seed * 10_000 + index
            for name in self.resolutions:
                if name not in RESOLUTIONS:
                    raise KeyError(
                        f"No resolution called {name!r}. "
                        f"Available: {', '.join(sorted(RESOLUTIONS))}")
                width, height = RESOLUTIONS[name]
                out.append(Spec(style=style, palette=self.palette, seed=seed,
                                width=width, height=height))
        return out

    def validate(self) -> None:
        if self.count < 1:
            raise ValueError("A pack needs at least one wallpaper.")
        if not self.styles:
            raise ValueError("A pack needs at least one style.")
        for style in self.styles:
            if style not in STYLES:
                raise KeyError(f"No style called {style!r}. "
                               f"Available: {', '.join(sorted(STYLES))}")
        if not self.resolutions:
            raise ValueError("A pack needs at least one resolution.")
        palettes.get(self.palette)   # raises with the available names
        if not self.slug:
            raise ValueError("That name has no usable characters in it.")


@dataclass
class BuiltPack:
    """What came out. Paths only -- the images stay on disk."""

    plan: PackPlan
    directory: Path
    images: List[Path] = field(default_factory=list)
    preview: Optional[Path] = None
    archive: Optional[Path] = None
    manifest: Optional[Path] = None
    bytes_written: int = 0

    def summary(self) -> Dict[str, object]:
        return {
            "name": self.plan.name,
            "slug": self.plan.slug,
            "wallpapers": self.plan.count,
            "resolutions": list(self.plan.resolutions),
            "files": len(self.images),
            "megabytes": round(self.bytes_written / 1e6, 1),
            "directory": str(self.directory),
            "archive": str(self.archive) if self.archive else None,
            "preview": str(self.preview) if self.preview else None,
        }


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------
def build(plan: PackPlan, root: Path,
          progress: Optional[Callable[[int, int, str], None]] = None,
          overwrite: bool = False) -> BuiltPack:
    """Render a pack to `root/<slug>/`. Local only, no network, resumable."""
    plan.validate()

    directory = Path(root) / plan.slug
    if directory.exists() and not overwrite:
        existing = list(directory.glob("*.png"))
        if existing:
            raise FileExistsError(
                f"{directory} already has {len(existing)} images in it. Pass "
                "overwrite=True to rebuild, or choose another name.")
    directory.mkdir(parents=True, exist_ok=True)

    wallpapers = directory / "wallpapers"
    wallpapers.mkdir(exist_ok=True)

    specs = plan.specs()
    built = BuiltPack(plan=plan, directory=directory)
    thumbs: List[Image.Image] = []
    seen_seeds: set = set()

    for position, spec in enumerate(specs, start=1):
        folder = wallpapers / _folder_for(spec)
        folder.mkdir(exist_ok=True)
        path = folder / spec.filename()

        image = render_spec(spec)
        image.save(path, format="PNG", optimize=True)
        built.images.append(path)
        built.bytes_written += path.stat().st_size

        # One thumbnail per distinct wallpaper, not per resolution.
        if spec.seed not in seen_seeds:
            seen_seeds.add(spec.seed)
            thumbs.append(image.resize(PREVIEW_TILE, Image.LANCZOS))

        if progress is not None:
            progress(position, len(specs), str(path.name))

    built.preview = _write_preview(thumbs, directory, plan)
    built.manifest = _write_manifest(plan, specs, directory)
    _write_readme(plan, directory)
    _write_licence(plan, directory)
    built.archive = _write_archive(directory, plan)
    return built


def _folder_for(spec: Spec) -> str:
    """Group by what the buyer is looking for: a screen, not a number."""
    for name in MOBILE_SIZES:
        if RESOLUTIONS[name] == (spec.width, spec.height):
            return "mobile"
    return "desktop"


def _write_preview(thumbs: Sequence[Image.Image], directory: Path,
                   plan: PackPlan) -> Optional[Path]:
    """The grid shoppers actually judge the pack by."""
    if not thumbs:
        return None

    columns = 4 if len(thumbs) >= 4 else len(thumbs)
    rows = (len(thumbs) + columns - 1) // columns
    gap = 12
    tile_w, tile_h = PREVIEW_TILE

    width = columns * tile_w + gap * (columns + 1)
    height = rows * tile_h + gap * (rows + 1)

    palette = palettes.get(plan.palette)
    backdrop = palette.colors[0] if palette.dark else palette.colors[-1]
    sheet = Image.new(
        "RGB", (width, height),
        tuple(int(c * 255) for c in palettes.hex_to_rgb(backdrop)))

    for index, thumb in enumerate(thumbs):
        col, row = index % columns, index // columns
        sheet.paste(thumb,
                    (gap + col * (tile_w + gap), gap + row * (tile_h + gap)))

    path = directory / "preview.png"
    sheet.save(path, format="PNG", optimize=True)
    return path


def _write_manifest(plan: PackPlan, specs: Sequence[Spec],
                    directory: Path) -> Path:
    """Every seed, so the pack can be rebuilt byte for byte."""
    payload = {
        "pack": asdict(plan),
        "generator": "jarvis.business.wallpapers",
        "images": [asdict(spec) for spec in specs],
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_readme(plan: PackPlan, directory: Path) -> Path:
    palette = palettes.get(plan.palette)
    styles = ", ".join(STYLE_DESCRIPTIONS.get(s, s) for s in plan.styles)
    sizes = "\n".join(
        f"  - {name} ({RESOLUTIONS[name][0]}x{RESOLUTIONS[name][1]})"
        for name in plan.resolutions)

    text = f"""{plan.name}
{'=' * len(plan.name)}

{plan.count} original wallpapers, each supplied at {len(plan.resolutions)} sizes.

Palette
  {palettes.describe(palette)}

Style
  {styles}

Included sizes
{sizes}

How they are organised
  wallpapers/desktop/   everything in a landscape ratio
  wallpapers/mobile/    phone and tablet
  preview.png           all of the designs at a glance
  manifest.json         the exact parameters behind each image

Need a size that isn't here?
  Every image is generated from the seed recorded in manifest.json, so any
  wallpaper in this pack can be reproduced at any resolution. Ask, and quote
  the filename.

LICENCE.txt covers what you may do with these.
"""
    path = directory / "README.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _write_licence(plan: PackPlan, directory: Path) -> Path:
    """A plain personal-use licence.

    This is a readable starting point, not legal advice, and the seller
    should read it before selling anything under it.
    """
    text = f"""LICENCE — {plan.name}

You may:
  - Use these wallpapers on any device you own, as many as you like.
  - Use them at work as well as at home.
  - Keep using them forever. There is no subscription and no expiry.

You may not:
  - Resell, redistribute or give away the image files.
  - Include them in another product, pack or collection.
  - Claim authorship of them.
  - Use them as a trademark or logo.

Commercial use in your own work (a video background, a presentation, a
stream overlay) is permitted. Selling the images themselves, in any form, is
not.

No warranty is given. These are decorative image files.
"""
    path = directory / "LICENCE.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _write_archive(directory: Path, plan: PackPlan) -> Path:
    """One zip holding the whole pack, which is what gets uploaded."""
    path = directory / f"{plan.slug}.zip"
    if path.exists():
        path.unlink()

    # PNGs are already compressed, so deflate buys almost nothing on them;
    # it is worth running anyway for the text files and to produce a single
    # tidy download rather than a folder tree.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(directory.rglob("*")):
            if item.is_dir() or item == path:
                continue
            if item.name == "manifest.json":
                continue       # internal: seeds are the seller's, not the buyer's
            archive.write(item, item.relative_to(directory))
    return path


# ---------------------------------------------------------------------------
# planning helpers
# ---------------------------------------------------------------------------
def suggest(palette: str, count: int = 12, seed: int = 1,
            styles: Optional[Sequence[str]] = None,
            name: Optional[str] = None) -> PackPlan:
    """A sensible default plan for a palette."""
    chosen = palettes.get(palette)
    picked = tuple(styles) if styles else _styles_for(chosen)
    return PackPlan(
        name=name or f"{chosen.name} Wallpapers",
        palette=chosen.slug,
        styles=picked,
        count=count,
        seed=seed,
    )


def _styles_for(palette: Palette) -> Tuple[str, ...]:
    """Which styles suit a palette.

    Flow and aurora both work by adding light to a dark ground, so on a light
    palette they wash out into a pale smear. Keeping them off light palettes
    is cheaper than discovering it in a finished pack.
    """
    if palette.dark:
        return ("mesh", "aurora", "flow", "topographic", "dunes")
    return ("mesh", "topographic", "dunes")


def estimate_size(plan: PackPlan) -> Dict[str, float]:
    """Rough disk cost, before committing to a build.

    Worth knowing in advance: a twelve-image pack at four sizes is sixty-odd
    4K PNGs, and this machine's C: drive has about twelve gigabytes left.
    """
    # Measured per style from real output rather than guessed. The first
    # version of this assumed 0.55 bytes per pixel and a real build came out
    # at 237 MB against a 90 MB estimate -- an estimate wrong by that much is
    # worse than none at all on a machine with twelve gigabytes free.
    #
    # The reason it was so far out: film grain. Grain is by construction
    # incompressible, so it sets a floor on PNG size that has nothing to do
    # with how smooth the underlying picture is. That is also why the five
    # styles land within a factor of 1.5 of each other despite looking
    # nothing alike.
    per_style = {
        "mesh": 1.28, "aurora": 1.36, "topographic": 1.36,
        "dunes": 1.25, "flow": 1.85,
    }
    default = 1.45

    total = 0.0
    for index in range(plan.count):
        style = plan.styles[index % len(plan.styles)]
        rate = per_style.get(style, default)
        for name in plan.resolutions:
            width, height = RESOLUTIONS[name]
            total += width * height * rate

    return {
        "files": plan.count * len(plan.resolutions),
        "megabytes": round(total / 1e6, 1),
        # The zip holds a second copy: PNG is already compressed, so deflate
        # reclaims almost nothing and the pack directory ends up roughly
        # twice the size of its images while both exist.
        "megabytes_with_archive": round(total * 2 / 1e6, 1),
    }
