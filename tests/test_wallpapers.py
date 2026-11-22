"""The wallpaper generators.

Art cannot be unit tested, but the things that make art unsellable can be.
Each test here corresponds to a defect that actually appeared while building
these: visible banding, blocky warp artifacts, muddy palette midpoints, and
images that were not reproducible from their own manifest.
"""
from __future__ import annotations

import io

import numpy as np
import pytest

from backend.business import palettes, render
from backend.business.wallpapers import (RESOLUTIONS, STYLES, Spec, render_spec)


# ===========================================================================
# reproducibility
# ---------------------------------------------------------------------------
# A pack is stored as a manifest of seeds, not as gigabytes of PNG. If a spec
# does not render identically every time, then "the same pack" means nothing:
# a customer asking for one wallpaper in another size gets a different
# picture, and a regression in a generator silently changes products already
# sold rather than showing up as a diff.
# ===========================================================================
def test_the_same_spec_renders_byte_identically():
    spec = Spec(style="mesh", palette="nebula", seed=4242, width=480, height=270)

    first, second = io.BytesIO(), io.BytesIO()
    render_spec(spec).save(first, format="PNG")
    render_spec(spec).save(second, format="PNG")

    assert first.getvalue() == second.getvalue(), (
        "identical specs must produce identical files, including the dither")


def test_a_different_seed_gives_a_different_image():
    a = np.asarray(render_spec(
        Spec(style="mesh", palette="nebula", seed=1, width=320, height=180)))
    b = np.asarray(render_spec(
        Spec(style="mesh", palette="nebula", seed=2, width=320, height=180)))
    assert not np.array_equal(a, b)


def test_render_order_does_not_affect_the_result():
    """Each render seeds its own generator, so nothing leaks between images."""
    one = Spec(style="aurora", palette="ember", seed=7, width=320, height=180)
    other = Spec(style="flow", palette="arctic", seed=9, width=320, height=180)

    alone = np.asarray(render_spec(one))
    render_spec(other)
    after = np.asarray(render_spec(one))

    assert np.array_equal(alone, after)


# ===========================================================================
# banding -- the defect that only shows at full size
# ===========================================================================
def test_dithering_breaks_up_gradient_banding():
    """An 8-bit gradient across 2000 pixels has ~8 pixels per level.

    Undithered that lands as visible stripes; it looks fine in a thumbnail
    and cheap on an actual desktop, which is the worst way to discover it.
    """
    gradient = np.linspace(0.2, 0.8, 2000, dtype=np.float32)
    field = np.repeat(gradient[None, :], 32, axis=0)
    image = np.stack([field] * 3, axis=-1)

    rng = np.random.default_rng(0)
    undithered = render.to_png_array(image, rng, dither=False)
    dithered = render.to_png_array(image, rng, dither=True)

    # ~153 levels over 2000 pixels, so undithered bands are ~13 wide.
    assert render.mean_flat_run(undithered) > 8.0, "the test case must band"
    assert render.mean_flat_run(dithered) < 4.0, "dithering must break it up"


def test_dithering_does_not_shift_the_overall_colour():
    """Noise to break banding, not noise that changes the picture."""
    image = np.full((64, 64, 3), 0.5, dtype=np.float32)
    rng = np.random.default_rng(0)
    dithered = render.to_png_array(image, rng, dither=True)
    assert abs(float(dithered.mean()) - 127.5) < 1.0


def test_output_is_always_in_range():
    # Generators add glow and grain, so values genuinely do exceed 1.0 before
    # quantisation; they must clip rather than wrap around to black.
    image = np.full((16, 16, 3), 1.4, dtype=np.float32)
    image[0, 0] = -0.5
    out = render.to_png_array(image, np.random.default_rng(0))
    assert out.max() <= 255 and out.min() >= 0
    assert out[8, 8, 0] == 255
    assert out[0, 0, 0] == 0


# ===========================================================================
# warping -- the blocky-artifact defect
# ===========================================================================
def test_warping_interpolates_rather_than_snapping():
    """Nearest-neighbour sampling left flat rectangular patches.

    The field is then resampled up by four, so each patch became an obvious
    block in the smooth regions of the image -- precisely where the eye goes.
    """
    rng = np.random.default_rng(3)
    field = render.fractal_noise((120, 200), rng, octaves=3, grid=3)
    warped = render.warp(field, np.random.default_rng(4), strength=0.4)

    row = warped[warped.shape[0] // 2]
    flat_runs = float(np.mean(np.diff(row) == 0))
    assert flat_runs < 0.05, (
        "a warped continuous field should have almost no exactly-equal "
        f"neighbours; got {flat_runs:.2%}")


def test_warping_with_no_strength_changes_nothing():
    rng = np.random.default_rng(3)
    field = render.fractal_noise((64, 64), rng, octaves=2, grid=3)
    assert np.array_equal(render.warp(field, rng, strength=0.0), field)


# ===========================================================================
# palettes
# ===========================================================================
def test_every_palette_is_well_formed():
    for palette in palettes.PALETTES:
        assert len(palette.colors) >= 2, palette.name
        for colour in palette.colors:
            rgb = palettes.hex_to_rgb(colour)
            assert all(0.0 <= c <= 1.0 for c in rgb), (palette.name, colour)
        # The `dark` flag drives wallpaper selection and listing copy, so it
        # has to match the actual colours rather than someone's intention.
        mean = float(np.mean([palettes.luminance(c) for c in palette.rgb()]))
        assert palette.dark == (mean < 0.5), (
            f"{palette.name} is marked dark={palette.dark} but its mean "
            f"luminance is {mean:.2f}")


def test_ramps_do_not_pass_through_grey():
    """The reason blending goes through HSV rather than RGB.

    Straight RGB interpolation between two saturated colours of opposing hue
    passes through a desaturated midpoint -- the muddy middle that makes
    procedural gradients look cheap.
    """
    import colorsys

    warm_to_cool = palettes.Palette("probe", "test", ("#c84b31", "#2e6f95"))
    table = palettes.ramp(warm_to_cool, steps=64)
    saturations = [colorsys.rgb_to_hsv(*c)[1] for c in table]
    assert min(saturations) > 0.3, (
        f"the ramp desaturates to {min(saturations):.2f} at its midpoint")


def test_ramp_endpoints_are_the_palette_colours():
    palette = palettes.get("ember")
    table = palettes.ramp(palette, steps=128)
    for produced, expected in ((table[0], palette.rgb()[0]),
                               (table[-1], palette.rgb()[-1])):
        assert all(abs(a - b) < 0.02 for a, b in zip(produced, expected))


def test_a_greyscale_palette_stays_grey():
    """Hue is meaningless at zero saturation, so it must not be dragged in."""
    import colorsys

    grey = palettes.Palette("probe", "test", ("#101010", "#e0e0e0"))
    for colour in palettes.ramp(grey, steps=32):
        assert colorsys.rgb_to_hsv(*colour)[1] < 0.02


def test_unknown_palette_says_what_is_available():
    with pytest.raises(KeyError) as caught:
        palettes.get("chartreuse-dreams")
    assert "ember" in str(caught.value)


# ===========================================================================
# the generators themselves
# ===========================================================================
@pytest.mark.parametrize("style", sorted(STYLES))
def test_every_style_renders(style):
    image = render_spec(
        Spec(style=style, palette="midnight-harbour", seed=11, width=320, height=180))
    assert image.size == (320, 180)
    assert image.mode == "RGB"


@pytest.mark.parametrize("style", sorted(STYLES))
def test_no_style_produces_a_flat_image(style):
    """A generator that silently degrades to one colour is a broken product."""
    array = np.asarray(render_spec(
        Spec(style=style, palette="ember", seed=5, width=320, height=180)))
    assert float(array.std()) > 6.0, f"{style} came out nearly flat"


@pytest.mark.parametrize("style", sorted(STYLES))
def test_styles_use_a_reasonable_spread_of_the_palette(style):
    """Catches the failure that made Copper Dusk a near-black rectangle.

    Two mesh implementations collapsed most of the frame onto the darkest
    palette entry. The image was not flat, so a standard-deviation check
    passed it; it was simply almost entirely black with one smear of colour.
    """
    array = np.asarray(render_spec(
        Spec(style=style, palette="copper-dusk", seed=21, width=400, height=225)))
    luma = array.mean(axis=2)
    very_dark = float(np.mean(luma < 12))
    assert very_dark < 0.55, (
        f"{style} put {very_dark:.0%} of the frame in near-black")


@pytest.mark.parametrize("name,size", sorted(RESOLUTIONS.items()))
def test_every_advertised_resolution_is_sane(name, size):
    width, height = size
    assert width > 0 and height > 0
    # Sold as desktop or mobile; anything square is a mistake in the table.
    assert abs(width / height - 1.0) > 0.05, name


def test_extreme_aspect_ratios_still_render():
    """Ultrawide is 2.39:1 and a phone is 1:2.17, so the field maths has to
    hold at both ends rather than assuming something near 16:9."""
    for width, height in ((1200, 300), (300, 1200)):
        image = render_spec(
            Spec(style="mesh", palette="arctic", seed=3, width=width, height=height))
        assert image.size == (width, height)


def test_unknown_style_says_what_is_available():
    with pytest.raises(KeyError) as caught:
        render_spec(Spec(style="fractal-dragons", palette="ember",
                         seed=1, width=64, height=64))
    assert "aurora" in str(caught.value)


def test_a_zero_sized_image_is_refused():
    with pytest.raises(ValueError):
        render_spec(Spec(style="mesh", palette="ember", seed=1, width=0, height=100))


def test_filenames_carry_everything_needed_to_find_the_image_again():
    spec = Spec(style="aurora", palette="nebula", seed=77, width=3840, height=2160)
    name = spec.filename(index=3)
    for fragment in ("03", "nebula", "aurora", "000077", "3840x2160"):
        assert fragment in name
