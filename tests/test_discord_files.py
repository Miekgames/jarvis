"""Sending a finished project to the owner's Discord DMs."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from backend.autonomy.packing import UPLOAD_LIMIT, find_project, pack, screenshots
from backend.integrations.discord_bridge import DiscordBridge

pytest.importorskip("discord")


def _project(root, name):
    folder = root / name
    (folder / "node_modules" / "big").mkdir(parents=True)
    (folder / "screenshots").mkdir()
    (folder / "index.html").write_text("<h1>Tips</h1>", encoding="utf-8")
    (folder / "node_modules" / "big" / "lib.js").write_text("x" * 1000, encoding="utf-8")
    (folder / "screenshots" / "index-390x844.png").write_bytes(b"\x89PNG")
    return folder


def test_a_project_is_zipped_without_the_bulk(tmp_path):
    import zipfile

    folder = _project(tmp_path, "tip-calculator-20261127")
    archive = pack(folder, tmp_path / "outbox")
    names = zipfile.ZipFile(archive).namelist()
    assert names == ["index.html"], "no node_modules, and screenshots go separately"
    assert screenshots(folder) == [folder / "screenshots" / "index-390x844.png"]


def test_finding_the_project_meant(tmp_path):
    tip = _project(tmp_path, "simple-tip-calculator-web-page-20261127-1412")
    time.sleep(0.02)
    habit = _project(tmp_path, "habit-tracker-app-20261127-1500")
    os.utime(habit, None)
    assert find_project(tmp_path, "the tip calculator") == tip
    assert find_project(tmp_path, "it") == habit, "'it' is the newest"
    assert find_project(tmp_path, "the spaceship game") is None


class DM:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, files=None):
        self.sent.append((content, [f.filename for f in files or []]))


@pytest.fixture
async def bridge(settings_store):
    made = DiscordBridge(settings_store, orchestrator=None)
    dm = DM()
    owner = SimpleNamespace(id=42, dm_channel=dm)
    made.client = SimpleNamespace(is_closed=lambda: False, get_user=lambda uid: owner)
    await settings_store.patch("discord", {"owners": [{"id": "42", "name": "owner"}]})
    made.dm = dm
    return made


async def test_files_reach_the_owners_dms(bridge, tmp_path):
    folder = _project(tmp_path, "tip-calculator")
    files = [pack(folder, tmp_path / "outbox")] + screenshots(folder)
    said = await bridge.send_files(files, "📦 **Tip calculator**")
    assert bridge.dm.sent == [("📦 **Tip calculator**", ["tip-calculator.zip", "index-390x844.png"])]
    assert said.startswith("Sent tip-calculator.zip, index-390x844.png")


async def test_too_big_for_discord_says_so(bridge, tmp_path):
    big = tmp_path / "huge.zip"
    with open(big, "wb") as f:
        f.truncate(UPLOAD_LIMIT + 1)
    with pytest.raises(RuntimeError, match="too big"):
        await bridge.send_files([big])
    assert bridge.dm.sent == []


async def test_not_without_pairing_or_permission(bridge, settings_store, tmp_path):
    folder = _project(tmp_path, "x")
    bridge.permissions = SimpleNamespace(allowed=lambda key: False)
    with pytest.raises(RuntimeError, match="permissions"):
        await bridge.send_files([folder / "index.html"])
    bridge.permissions = None
    await settings_store.patch("discord", {"owners": []})
    with pytest.raises(RuntimeError, match="Pair"):
        await bridge.send_files([folder / "index.html"])


@pytest.mark.parametrize("task, wanted", [
    ("build me a tip calculator and send it to my discord", True),
    ("build a website and dm it to me on discord", True),
    ("build me a tip calculator", False),
    ("make a discord bot that sends memes", False),
])
def test_files_go_to_discord_only_when_asked(task, wanted):
    from backend.ai.tools.project_tools import _SEND_TO_DISCORD

    assert bool(_SEND_TO_DISCORD.search(task)) is wanted


async def test_a_finished_project_is_sent_only_if_the_request_asked(tmp_path, monkeypatch):
    from backend.autonomy import claude_code
    from backend.tasks.handlers import project

    folder = _project(tmp_path, "tip")
    sent = []

    async def run_job(task, folder_, minutes, log, cancelled):
        return {"engine": "claude-code", "report": "Built it.", "folder": str(folder), "files": [],
                "passes": 1, "minutes": 1.0}

    async def send_files(paths, text=""):
        sent.append([p.name for p in paths])
        return "Sent."

    async def config():
        return {"owners": [{"id": "42"}]}

    monkeypatch.setattr(claude_code, "run_job", run_job)
    bridge = SimpleNamespace(client=object(), config=config, send_files=send_files)
    for asked in (False, True):
        logs = []

        async def log(message, *a, **k):
            logs.append(message)
        ctx = SimpleNamespace(params={"task": "tip calculator", "folder": str(folder), "send_files": asked},
                              services={"discord": bridge}, engine=SimpleNamespace(settings=SimpleNamespace(DATA_DIR=tmp_path)),
                              cancelled=False, log=log, checkpoint_now=lambda: None)
        await project.project_handler(ctx)
    assert sent == [["tip.zip", "index-390x844.png"]], "once: only the run that asked"
