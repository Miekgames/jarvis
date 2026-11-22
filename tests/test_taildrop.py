"""Sending files to the owner's own devices over Tailscale.

The guard that matters most here is the address check. "Send this to my
phone" resolving to the wrong destination would mean quietly copying private
files somewhere they should not go, so a send can only ever target a
Tailscale address.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.integrations.taildrop import Device, Taildrop, _first_ip, _is_tailnet


class _Settings:
    DATA_DIR = Path(".")


@pytest.fixture
def taildrop():
    return Taildrop(_Settings())


# -------------------------------------------------------------- addresses
def test_tailnet_addresses_are_recognised():
    assert _is_tailnet("100.64.0.10")
    assert _is_tailnet("100.64.0.20")
    assert _is_tailnet("fd7a:115c:a1e0::5501:3df7")


def test_everything_else_is_refused():
    """A send must never leave the tailnet."""
    for address in ("192.168.1.5", "8.8.8.8", "10.0.0.1", "127.0.0.1",
                    "100.63.255.255", "100.128.0.1", "2001:db8::1",
                    "", "not-an-ip", "localhost"):
        assert not _is_tailnet(address), address


def test_ipv4_is_preferred_when_a_device_has_both():
    node = {"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.20"]}
    assert _first_ip(node) == "100.64.0.20"


def test_a_device_with_no_address_yields_nothing():
    assert _first_ip({}) == ""
    assert _first_ip({"TailscaleIPs": []}) == ""


# ---------------------------------------------------------------- devices
@pytest.fixture
def stocked(taildrop, monkeypatch):
    devices = [
        Device("DESKTOP-EXAMPLE", "windows", "100.64.0.10", True, is_self=True),
        Device("localhost", "iOS", "100.64.0.20", True),
    ]

    async def fake(online_only=True):
        return devices

    monkeypatch.setattr(taildrop, "devices", fake)
    return taildrop


@pytest.mark.asyncio
async def test_a_phone_is_found_by_its_operating_system(stocked):
    """This phone's hostname is literally "localhost", so matching on the
    name is both useless and alarming. The OS is what identifies it."""
    for wanted in ("phone", "my phone", "iphone", "mobile"):
        found = await stocked.find_device(wanted)
        assert found is not None, wanted
        assert found.address == "100.64.0.20"


@pytest.mark.asyncio
async def test_a_device_can_be_named_exactly(stocked):
    found = await stocked.find_device("100.64.0.20")
    assert found is not None and found.os == "iOS"


@pytest.mark.asyncio
async def test_this_machine_is_never_a_destination(stocked):
    """Sending a file to the machine it is already on is always a mistake."""
    found = await stocked.find_device("DESKTOP-EXAMPLE")
    assert found is None


@pytest.mark.asyncio
async def test_an_unknown_device_is_not_guessed_at(stocked):
    assert await stocked.find_device("the printer") is None
    assert await stocked.find_device("") is None


# ----------------------------------------------------------------- sending
@pytest.mark.asyncio
async def test_sending_a_missing_file_fails_clearly(stocked, tmp_path):
    result = await stocked.send(tmp_path / "nope.png", "phone")
    assert not result["ok"]
    assert "not a file" in result["detail"]


@pytest.mark.asyncio
async def test_sending_a_directory_is_refused(stocked, tmp_path):
    result = await stocked.send(tmp_path, "phone")
    assert not result["ok"]


@pytest.mark.asyncio
async def test_an_unknown_device_lists_what_is_online(stocked, tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"x")
    result = await stocked.send(target, "nonexistent-device")
    assert not result["ok"]
    assert "localhost" in result["detail"], "say what could be sent to instead"


@pytest.mark.asyncio
async def test_a_device_outside_the_tailnet_is_refused(taildrop, tmp_path, monkeypatch):
    """The guard behind everything else: even if resolution went wrong, the
    copy must not go to a non-Tailscale address."""
    async def fake(online_only=True):
        return [Device("spoofed", "linux", "8.8.8.8", True)]

    monkeypatch.setattr(taildrop, "devices", fake)
    target = tmp_path / "a.png"
    target.write_bytes(b"x")

    result = await taildrop.send(target, "spoofed")
    assert not result["ok"]
    assert "not a Tailscale address" in result["detail"]


@pytest.mark.asyncio
async def test_sending_several_reports_each(stocked, tmp_path, monkeypatch):
    """A partial send is worth knowing the shape of, so one failure does not
    hide the rest."""
    good = tmp_path / "a.png"
    good.write_bytes(b"x")
    missing = tmp_path / "gone.png"

    async def fake_send(path, device):
        return {"ok": Path(path).exists(), "detail": "", "device": {"name": "p"}}

    monkeypatch.setattr(stocked, "send", fake_send)
    result = await stocked.send_many([good, missing], "phone")
    assert result["sent"] == 1
    assert result["total"] == 2
    assert result["ok"] is False


def test_availability_is_honest_when_tailscale_is_absent(taildrop, monkeypatch):
    monkeypatch.setattr(type(taildrop), "executable", property(lambda self: None))
    state = taildrop.availability()
    assert state["available"] is False
    assert "not installed" in state["detail"]


# ------------------------------------------------- choosing what to send
def test_wallpaper_names_are_readable():
    """"Sent 623530011" tells nobody anything. The seed is the first
    all-digit part, and the palette before it may contain hyphens of its
    own, so splitting from the left guesses wrong."""
    from backend.ai.tools.business_tools import _describe

    assert _describe(Path("ember-mesh-623550010-1290x2796.png")) == "ember mesh"
    assert _describe(Path("midnight-harbour-aurora-623480001-1290x2796.png")) \
        == "midnight harbour aurora"
    # Nothing numeric: fall back to the stem rather than an empty string.
    assert _describe(Path("something-odd.png")) == "something-odd"


@pytest.mark.asyncio
async def test_an_unspecified_request_draws_at_random_across_packs(tmp_path):
    """"Send an image to my phone" asked twice should not send the same
    picture twice, which is what picking the newest pack's first file did."""
    from backend.ai.tools import build_registry

    packs = []
    for name in ("alpha", "beta"):
        folder = tmp_path / name / "wallpapers" / "mobile"
        folder.mkdir(parents=True)
        for index in range(8):
            (folder / f"{name}-mesh-{index:06d}-1290x2796.png").write_bytes(b"x")
        packs.append({"slug": name, "name": name, "directory": str(tmp_path / name)})

    class _Business:
        def products(self): return packs
        def find(self, slug): return next((p for p in packs if p["slug"] == slug), None)

    picked = set()

    class _Taildrop:
        async def send_many(self, paths, device):
            for p in paths:
                picked.add(p.name)
            return {"ok": True, "sent": len(paths), "total": len(paths),
                    "results": [{"ok": True, "device": {"name": "phone"}}]}

    registry = build_registry({"business": _Business(), "taildrop": _Taildrop()})
    tool = registry.get("send_wallpapers_to_phone")
    for _ in range(25):
        await tool.handler(registry.context)

    assert len(picked) > 1, "repeated requests must not send the same file"
    # Both packs should be reachable, not just the first.
    assert any(n.startswith("alpha") for n in picked)
    assert any(n.startswith("beta") for n in picked)


@pytest.mark.asyncio
async def test_a_named_pack_is_respected(tmp_path):
    from backend.ai.tools import build_registry

    folder = tmp_path / "alpha" / "wallpapers" / "mobile"
    folder.mkdir(parents=True)
    (folder / "alpha-mesh-000001-1290x2796.png").write_bytes(b"x")
    other = tmp_path / "beta" / "wallpapers" / "mobile"
    other.mkdir(parents=True)
    (other / "beta-mesh-000001-1290x2796.png").write_bytes(b"x")

    packs = [{"slug": "alpha", "name": "alpha", "directory": str(tmp_path / "alpha")},
             {"slug": "beta", "name": "beta", "directory": str(tmp_path / "beta")}]

    class _Business:
        def products(self): return packs
        def find(self, slug): return next((p for p in packs if p["slug"] == slug), None)

    seen = []

    class _Taildrop:
        async def send_many(self, paths, device):
            seen.extend(p.name for p in paths)
            return {"ok": True, "sent": len(paths), "total": len(paths),
                    "results": [{"ok": True, "device": {"name": "phone"}}]}

    registry = build_registry({"business": _Business(), "taildrop": _Taildrop()})
    tool = registry.get("send_wallpapers_to_phone")
    for _ in range(5):
        await tool.handler(registry.context, pack="beta")

    assert seen and all(n.startswith("beta") for n in seen)
