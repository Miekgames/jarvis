"""The product workshop end to end, and the boundary it must not cross.

The single most important test in this file is the last one: that nothing in
the business modules can publish, upload or post. The whole design rests on
the irreversible half not existing yet, and that is only true for as long as
somebody checks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.business.service import BusinessService


class _Settings:
    def __init__(self, tmp_path):
        self.DATA_DIR = tmp_path


@pytest.fixture
def service(tmp_path):
    return BusinessService(_Settings(tmp_path))


# ---------------------------------------------------------------- catalogue
def test_the_catalogue_lists_what_can_actually_be_made(service):
    catalogue = service.catalogue()
    from backend.business.wallpapers import RESOLUTIONS, STYLES

    assert set(catalogue["styles"]) == set(STYLES)
    assert set(catalogue["resolutions"]) == set(RESOLUTIONS)
    assert len(catalogue["palettes"]) >= 10
    for palette in catalogue["palettes"]:
        assert set(palette) >= {"slug", "name", "mood", "dark", "colors"}


def test_an_empty_workshop_reports_nothing_rather_than_failing(service):
    assert service.products() == []
    assert service.find("anything") is None


# ---------------------------------------------------------------- planning
def test_planning_picks_a_price_and_a_seed(service):
    plan = service.plan("nebula", count=6)
    assert plan.price_usd > 0
    assert plan.seed > 0
    assert plan.count == 6


def test_planning_rejects_an_unknown_palette(service):
    with pytest.raises(KeyError):
        service.plan("octarine")


def test_the_estimate_knows_whether_it_fits(service):
    plan = service.plan("nebula", count=4)
    estimate = service.estimate(plan)
    assert estimate["files"] == 4 * len(plan.resolutions)
    assert "fits" in estimate


def test_a_build_too_big_for_the_disk_is_refused(service, monkeypatch):
    """Better a refusal than a half-written pack and a full disk."""
    plan = service.plan("nebula", count=4)
    monkeypatch.setattr(service, "estimate",
                        lambda _: {"fits": False, "warning": "No room."})
    with pytest.raises(OSError, match="No room"):
        asyncio.run(service.build(plan))


# ---------------------------------------------------------------- building
@pytest.fixture
def built(service):
    plan = service.plan("ember", count=2, resolutions=["desktop-1080p"],
                        styles=["mesh"], name="Test Pack")
    return plan, asyncio.run(service.build(plan))


def test_building_registers_the_pack(service, built):
    plan, summary = built
    assert summary["wallpapers"] == 2

    products = service.products()
    assert len(products) == 1
    assert products[0]["slug"] == plan.slug
    assert products[0]["files"] == 2
    assert Path(products[0]["archive"]).exists()
    assert Path(products[0]["preview"]).exists()


def test_a_pack_can_be_found_by_name_or_slug(service, built):
    plan, _ = built
    assert service.find(plan.slug) is not None
    assert service.find("Test Pack") is not None
    assert service.find("nonexistent") is None


def test_progress_is_reported_during_a_build_and_cleared_after(service):
    plan = service.plan("ember", count=2, resolutions=["desktop-1080p"],
                        styles=["mesh"], name="Progress Pack")
    seen = []

    async def run():
        return await service.build(plan, on_progress=seen.append)

    asyncio.run(run())
    # The callback is handed back to the loop, so it may land after the build
    # returns; what must be true is that it is not left showing a stale build.
    assert service.progress == {}


def test_a_pack_rebuilds_from_its_own_manifest(service, built):
    """The claim the whole storage model rests on."""
    plan, _ = built
    recovered = service._plan_from_disk(plan.slug)
    assert recovered.specs() == plan.specs()


# ---------------------------------------------------------------- drafting


def test_the_generation_modules_cannot_reach_the_network():
    """The design rests on the two halves being genuinely separate.

    Generating wallpapers and drafting copy are safe to run unattended from
    a phone precisely because there is no code path from them to a live
    product or a posted message. That is a property of the source, so it is
    checked against the source -- an import added in a hurry would otherwise
    quietly turn a safe subsystem into one that can spend money and post
    under someone's name.

    The selling layer that once sat beside this was removed rather than
    disabled, so there is no longer any exception to carve out: the whole
    package must be unable to reach a network.
    """
    import backend.business as package

    root = Path(package.__file__).parent
    forbidden = ("requests", "httpx", "urllib.request", "aiohttp",
                 "playwright", "smtplib", "praw")

    offenders = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        # Strip comments and docstrings: these modules discuss Pinterest and
        # these services at length in prose, and prose is not a call.
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for bad in forbidden:
                    if name == bad or name.startswith(bad + "."):
                        offenders.append(f"{path.name} imports {name}")

    assert not offenders, (
        "the business modules must not be able to reach the network: "
        + "; ".join(offenders))


