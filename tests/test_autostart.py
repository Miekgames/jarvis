"""The autostart launcher: env parsing, remote readiness, crash-loop policy.

These matter because the launcher runs unattended at logon with no console to
complain to. Everything it decides there, it decides from .env and from the
exit code of a process nobody is watching.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_autostart(monkeypatch=None):
    """Import scripts/autostart.py by path -- it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "jarvis_autostart", ROOT / "scripts" / "autostart.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def autostart():
    return load_autostart()


# ---------------------------------------------------------------- env parsing
def test_env_values_reads_pairs(autostart, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "JARVIS_PORT=8123\n"
        'JARVIS_PASSWORD="quoted value"\n'
        "JARVIS_HOST = 0.0.0.0 \n"
        "MALFORMED_NO_EQUALS\n",
        encoding="utf-8")
    monkeypatch.setattr(autostart, "ROOT", tmp_path)

    values = autostart.env_values()
    assert values["JARVIS_PORT"] == "8123"
    assert values["JARVIS_PASSWORD"] == "quoted value"
    # Whitespace either side of the '=' is how people actually write these.
    assert values["JARVIS_HOST"] == "0.0.0.0"
    assert "MALFORMED_NO_EQUALS" not in values
    assert autostart.backend_port() == 8123


def test_env_values_survives_a_missing_file(autostart, tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "ROOT", tmp_path)
    assert autostart.env_values() == {}
    # A missing .env must not stop the launcher; it means "the defaults".
    assert autostart.backend_port() == 8000


def test_a_nonsense_port_falls_back_rather_than_crashing(autostart, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JARVIS_PORT=notanumber\n", encoding="utf-8")
    monkeypatch.setattr(autostart, "ROOT", tmp_path)
    assert autostart.backend_port() == 8000


# ------------------------------------------------------------ remote readiness
def readiness(autostart, tmp_path, monkeypatch, env_text, dist=True):
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.setattr(autostart, "ROOT", tmp_path)
    dist_path = tmp_path / "dist"
    if dist:
        dist_path.mkdir(exist_ok=True)
    monkeypatch.setattr(autostart, "DIST", dist_path)
    return autostart.remote_readiness()


def test_the_default_setup_is_reported_as_unreachable(autostart, tmp_path, monkeypatch):
    checks = readiness(autostart, tmp_path, monkeypatch,
                       "JARVIS_HOST=127.0.0.1\nJARVIS_AUTH_MODE=local\n")
    assert [ok for ok, _, _ in checks] == [False, False, True]
    # And each failure says what to set, since that is the whole point.
    assert any("JARVIS_HOST=0.0.0.0" in remedy for _, _, remedy in checks)
    assert any("JARVIS_AUTH_MODE=password" in remedy for _, _, remedy in checks)


def test_no_env_file_at_all_is_also_unreachable(autostart, tmp_path, monkeypatch):
    # The defaults are loopback + local mode, so an absent .env must not read
    # as "configured for remote access".
    monkeypatch.setattr(autostart, "ROOT", tmp_path)
    monkeypatch.setattr(autostart, "DIST", tmp_path / "dist")
    assert [ok for ok, _, _ in autostart.remote_readiness()] == [False, False, False]


def test_password_mode_with_no_password_is_not_ready(autostart, tmp_path, monkeypatch):
    """The dangerous half-state: open to the network, nothing able to log in."""
    checks = readiness(autostart, tmp_path, monkeypatch,
                       "JARVIS_HOST=0.0.0.0\nJARVIS_AUTH_MODE=password\nJARVIS_PASSWORD=\n")
    assert checks[0][0] is True
    assert checks[1][0] is False
    assert "no password" in checks[1][1]


def test_a_fully_configured_setup_reports_ready(autostart, tmp_path, monkeypatch):
    checks = readiness(
        autostart, tmp_path, monkeypatch,
        "JARVIS_HOST=0.0.0.0\nJARVIS_AUTH_MODE=password\nJARVIS_PASSWORD=hunter2hunter2\n")
    assert all(ok for ok, _, _ in checks)


def test_an_unbuilt_interface_is_called_out(autostart, tmp_path, monkeypatch):
    checks = readiness(
        autostart, tmp_path, monkeypatch,
        "JARVIS_HOST=0.0.0.0\nJARVIS_AUTH_MODE=password\nJARVIS_PASSWORD=hunter2hunter2\n",
        dist=False)
    assert checks[2][0] is False
    assert "install --build" in checks[2][2]


def test_localhost_by_name_counts_as_loopback(autostart, tmp_path, monkeypatch):
    # Someone writing JARVIS_HOST=localhost has not opened anything up, and
    # being told they have would send them hunting the wrong problem.
    for host in ("localhost", "::1", "127.0.0.1"):
        checks = readiness(autostart, tmp_path, monkeypatch, f"JARVIS_HOST={host}\n")
        assert checks[0][0] is False, host


# ------------------------------------------------------------ restart policy
def test_the_crash_loop_guard_gives_up_before_looping_forever(autostart):
    assert autostart.MAX_CONSECUTIVE_CRASHES >= 3
    # Backoff has to actually grow, and has to stop growing.
    backoffs = [min(autostart.RESTART_BASE_S * (2 ** n), autostart.RESTART_MAX_S)
                for n in range(autostart.MAX_CONSECUTIVE_CRASHES)]
    assert backoffs == sorted(backoffs)
    assert backoffs[-1] <= autostart.RESTART_MAX_S
    # Six crashes should not take all night to establish.
    assert sum(backoffs) < 600


def test_a_long_run_is_not_treated_as_a_crash(autostart):
    # The distinction the supervisor turns on: something that ran for hours and
    # stopped gets restarted at once, something that dies on startup backs off.
    assert autostart.HEALTHY_RUN_S >= 60
    assert autostart.RESTART_BASE_S <= 10
