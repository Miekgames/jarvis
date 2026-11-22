"""Pack assembly: planning, building, manifests and archives.

Small resolutions throughout. These tests are about structure -- what ends up
in the directory, what the zip contains, what the manifest records -- and a
4K render proves none of it while costing a hundred times as much.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backend.business import packs
from backend.business.packs import PackPlan
from backend.business.wallpapers import RESOLUTIONS


@pytest.fixture
def small_plan():
    return PackPlan(name="Test Pack", palette="ember", styles=("mesh", "dunes"),
                    count=3, resolutions=("desktop-1080p",), seed=5)


# ---------------------------------------------------------------- planning
def test_a_plan_expands_to_one_spec_per_image_per_resolution():
    plan = PackPlan(name="P", palette="ember", styles=("mesh",), count=4,
                    resolutions=("desktop-4k", "phone"), seed=1)
    specs = plan.specs()
    assert len(specs) == 8
    assert {(s.width, s.height) for s in specs} == {
        RESOLUTIONS["desktop-4k"], RESOLUTIONS["phone"]}


def test_the_same_wallpaper_shares_a_seed_across_resolutions():
    """Otherwise "the same design, but ultrawide" is a different picture."""
    plan = PackPlan(name="P", palette="ember", styles=("mesh",), count=2,
                    resolutions=("desktop-4k", "phone"), seed=1)
    by_seed = {}
    for spec in plan.specs():
        by_seed.setdefault(spec.seed, set()).add((spec.width, spec.height))
    assert len(by_seed) == 2
    for sizes in by_seed.values():
        assert len(sizes) == 2


def test_a_plan_is_fully_determined_by_its_own_seed():
    a = PackPlan(name="P", palette="ember", styles=("mesh",), count=3, seed=9)
    b = PackPlan(name="P", palette="ember", styles=("mesh",), count=3, seed=9)
    assert a.specs() == b.specs()

    c = PackPlan(name="P", palette="ember", styles=("mesh",), count=3, seed=10)
    assert a.specs() != c.specs()


def test_styles_cycle_so_a_pack_is_varied():
    plan = PackPlan(name="P", palette="ember", styles=("mesh", "dunes", "flow"),
                    count=6, resolutions=("desktop-1080p",), seed=1)
    assert [s.style for s in plan.specs()] == [
        "mesh", "dunes", "flow", "mesh", "dunes", "flow"]


def test_slugs_survive_awkward_names():
    assert PackPlan(name="Midnight Harbour!", palette="ember",
                    styles=("mesh",), count=1).slug == "midnight-harbour"
    assert PackPlan(name="  Deep  Forest  ", palette="ember",
                    styles=("mesh",), count=1).slug.startswith("deep")


@pytest.mark.parametrize("bad,error", [
    (dict(count=0), ValueError),
    (dict(styles=()), ValueError),
    (dict(styles=("nonsense",)), KeyError),
    (dict(palette="nonsense"), KeyError),
    (dict(resolutions=()), ValueError),
    (dict(name="!!!"), ValueError),
])
def test_bad_plans_are_refused_before_anything_renders(bad, error):
    fields = dict(name="P", palette="ember", styles=("mesh",), count=2,
                  resolutions=("desktop-1080p",), seed=1)
    fields.update(bad)
    with pytest.raises(error):
        PackPlan(**fields).validate()


def test_an_unknown_resolution_names_the_options():
    plan = PackPlan(name="P", palette="ember", styles=("mesh",), count=1,
                    resolutions=("retina-hyperwide",))
    with pytest.raises(KeyError) as caught:
        plan.specs()
    assert "desktop-4k" in str(caught.value)


def test_light_palettes_do_not_get_the_glow_styles():
    """Flow and aurora add light to a dark ground; on a light palette they
    wash out. Cheaper to exclude here than to find out in a finished pack."""
    light = packs.suggest("sand")
    assert "flow" not in light.styles and "aurora" not in light.styles

    dark = packs.suggest("nebula")
    assert "flow" in dark.styles and "aurora" in dark.styles


# ---------------------------------------------------------------- building
def test_building_produces_the_expected_structure(tmp_path, small_plan):
    built = packs.build(small_plan, tmp_path)

    assert built.directory == tmp_path / "test-pack"
    assert len(built.images) == 3
    for path in built.images:
        assert path.exists() and path.stat().st_size > 0

    for name in ("README.txt", "LICENCE.txt", "manifest.json", "preview.png"):
        assert (built.directory / name).exists(), name
    assert built.archive is not None and built.archive.exists()


def test_the_manifest_can_rebuild_the_pack(tmp_path, small_plan):
    built = packs.build(small_plan, tmp_path)
    payload = json.loads(built.manifest.read_text(encoding="utf-8"))

    assert payload["pack"]["seed"] == small_plan.seed
    assert len(payload["images"]) == 3
    # Every field needed to reconstruct a Spec, so a lost file is recoverable
    # and a customer's "number 2 but ultrawide" is answerable.
    for entry in payload["images"]:
        assert set(entry) >= {"style", "palette", "seed", "width", "height"}


def test_the_archive_holds_the_images_but_not_the_seeds(tmp_path, small_plan):
    """The manifest is the seller's reproduction recipe, not the buyer's."""
    built = packs.build(small_plan, tmp_path)
    with zipfile.ZipFile(built.archive) as archive:
        names = archive.namelist()

    assert any(n.endswith(".png") and "wallpapers/" in n for n in names)
    assert any(n.endswith("README.txt") for n in names)
    assert any(n.endswith("LICENCE.txt") for n in names)
    assert not any("manifest.json" in n for n in names)


def test_the_archive_does_not_contain_itself(tmp_path, small_plan):
    built = packs.build(small_plan, tmp_path)
    with zipfile.ZipFile(built.archive) as archive:
        assert not any(n.endswith(".zip") for n in archive.namelist())


def test_desktop_and_mobile_are_filed_separately(tmp_path):
    plan = PackPlan(name="Split", palette="ember", styles=("mesh",), count=2,
                    resolutions=("desktop-1080p", "phone"), seed=2)
    built = packs.build(plan, tmp_path)
    folders = {p.parent.name for p in built.images}
    assert folders == {"desktop", "mobile"}


def test_rebuilding_over_an_existing_pack_is_refused_by_default(tmp_path, small_plan):
    packs.build(small_plan, tmp_path)
    with pytest.raises(FileExistsError):
        packs.build(small_plan, tmp_path)
    # ...but is allowed when asked for explicitly.
    packs.build(small_plan, tmp_path, overwrite=True)


def test_progress_is_reported_for_every_file(tmp_path, small_plan):
    seen = []
    packs.build(small_plan, tmp_path, progress=lambda i, n, name: seen.append((i, n)))
    assert [i for i, _ in seen] == [1, 2, 3]
    assert all(n == 3 for _, n in seen)


def test_the_preview_has_one_tile_per_design_not_per_file(tmp_path):
    from PIL import Image

    plan = PackPlan(name="Preview", palette="ember", styles=("mesh",), count=2,
                    resolutions=("desktop-1080p", "phone"), seed=3)
    built = packs.build(plan, tmp_path)

    # Four files, two designs: a preview showing each design twice would
    # advertise a pack as half the size it is.
    assert len(built.images) == 4
    width = Image.open(built.preview).width
    assert width < packs.PREVIEW_TILE[0] * 4


# ---------------------------------------------------------------- estimates
def test_the_size_estimate_is_close_to_reality(tmp_path, small_plan):
    """An estimate exists to answer "will this fit?".

    The first version assumed 0.55 bytes per pixel and a real build came out
    at 237 MB against a 90 MB estimate. Film grain is incompressible by
    construction and sets a floor on PNG size regardless of how smooth the
    picture is.
    """
    estimate = packs.estimate_size(small_plan)
    built = packs.build(small_plan, tmp_path)

    predicted = estimate["megabytes"] * 1e6
    actual = built.bytes_written
    assert 0.6 < predicted / actual < 1.6, (
        f"estimated {predicted/1e6:.1f} MB, wrote {actual/1e6:.1f} MB")


def test_the_estimate_counts_every_file():
    plan = PackPlan(name="P", palette="ember", styles=("mesh",), count=5,
                    resolutions=("desktop-4k", "phone", "ultrawide"), seed=1)
    assert packs.estimate_size(plan)["files"] == 15


def test_the_estimate_accounts_for_the_duplicate_in_the_archive():
    plan = PackPlan(name="P", palette="ember", styles=("mesh",), count=2, seed=1)
    estimate = packs.estimate_size(plan)
    assert estimate["megabytes_with_archive"] > estimate["megabytes"]


def test_denser_styles_are_estimated_higher():
    """Flow is line work and compresses worst; dunes is flat and compresses best."""
    flow = PackPlan(name="P", palette="ember", styles=("flow",), count=4, seed=1)
    dunes = PackPlan(name="P", palette="ember", styles=("dunes",), count=4, seed=1)
    assert packs.estimate_size(flow)["megabytes"] > packs.estimate_size(dunes)["megabytes"]


# ---------------------------------------------------------------- the texts
def test_the_readme_describes_what_was_actually_built(tmp_path, small_plan):
    built = packs.build(small_plan, tmp_path)
    text = (built.directory / "README.txt").read_text(encoding="utf-8")
    assert small_plan.name in text
    assert str(small_plan.count) in text
    assert "1920x1080" in text


def test_the_licence_states_the_restriction_that_matters(tmp_path, small_plan):
    built = packs.build(small_plan, tmp_path)
    text = (built.directory / "LICENCE.txt").read_text(encoding="utf-8").lower()
    # The one term a wallpaper pack lives or dies by.
    assert "resell" in text or "redistribute" in text
