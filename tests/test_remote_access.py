"""The remote-access setup script.

Every claim this script prints is a claim about who can reach the user's
assistant, so the logic behind those claims is worth pinning down. The .env
rewriter is here too: it edits a file holding API keys and tokens.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ra(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "jarvis_remote_access", ROOT / "scripts" / "remote_access.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "ENV", tmp_path / ".env")
    monkeypatch.setattr(module, "BACKUPS", tmp_path / "backups")
    return module


# ------------------------------------------------------------ address ranges
def test_tailnet_addresses_are_recognised_on_both_protocols(ra):
    assert ra.is_tailnet("100.64.0.10")
    assert ra.is_tailnet("100.64.0.1")
    assert ra.is_tailnet("fd7a:115c:a1e0::5501:3df7")


def test_ordinary_addresses_are_not_mistaken_for_tailnet(ra):
    # Saying a LAN address is on the tailnet would under-report exposure,
    # which is the direction that gets someone hurt.
    for address in ("192.168.4.35", "10.0.0.5", "172.16.0.1",
                    "100.63.255.255", "100.128.0.1", "fe80::1", "2001:db8::1"):
        assert not ra.is_tailnet(address), address


def test_nonsense_is_not_tailnet(ra):
    assert not ra.is_tailnet("")
    assert not ra.is_tailnet("hostname.local")


def test_a_v4_mapped_v6_address_does_not_raise(ra):
    # ipaddress raises on a cross-version containment test rather than
    # returning False, so getting this wrong is a crash, not a wrong answer.
    ra.is_tailnet("::ffff:100.64.0.10")


# ------------------------------------------------------------- env rewriting
def test_writing_preserves_everything_it_was_not_asked_to_change(ra):
    ra.ENV.write_text(
        "# JARVIS configuration\n"
        "ANTHROPIC_API_KEY=sk-secret-value\n"
        "JARVIS_HOST=127.0.0.1\n"
        "\n"
        "# voice\n"
        "JARVIS_TTS_VOICE=en-GB-RyanNeural\n",
        encoding="utf-8")

    ra.write_env({"JARVIS_HOST": "0.0.0.0"})
    text = ra.ENV.read_text(encoding="utf-8")

    assert "JARVIS_HOST=0.0.0.0" in text
    assert "JARVIS_HOST=127.0.0.1" not in text
    # The things it was not asked about, including the comments and the key.
    assert "ANTHROPIC_API_KEY=sk-secret-value" in text
    assert "# JARVIS configuration" in text
    assert "# voice" in text
    assert "JARVIS_TTS_VOICE=en-GB-RyanNeural" in text


def test_a_backup_is_taken_before_touching_it(ra):
    ra.ENV.write_text("ANTHROPIC_API_KEY=sk-secret-value\n", encoding="utf-8")
    backup = ra.write_env({"JARVIS_HOST": "0.0.0.0"})
    assert backup.exists()
    # The backup is the file as it was, not as it became.
    saved = backup.read_text(encoding="utf-8")
    assert "sk-secret-value" in saved
    assert "JARVIS_HOST" not in saved


def test_missing_keys_are_appended_not_dropped(ra):
    ra.ENV.write_text("JARVIS_PORT=8000\n", encoding="utf-8")
    ra.write_env({"JARVIS_AUTH_MODE": "password", "JARVIS_PASSWORD": "a" * 14})
    values = ra.read_env()
    assert values["JARVIS_AUTH_MODE"] == "password"
    assert values["JARVIS_PASSWORD"] == "a" * 14
    assert values["JARVIS_PORT"] == "8000"


def test_a_key_is_changed_once_not_duplicated(ra):
    ra.ENV.write_text("JARVIS_HOST=127.0.0.1\n", encoding="utf-8")
    ra.write_env({"JARVIS_HOST": "0.0.0.0"})
    ra.write_env({"JARVIS_HOST": "0.0.0.0"})
    text = ra.ENV.read_text(encoding="utf-8")
    assert text.count("JARVIS_HOST=") == 1


def test_it_works_with_no_env_file_at_all(ra):
    assert not ra.ENV.exists()
    ra.write_env({"JARVIS_HOST": "0.0.0.0"})
    assert ra.read_env()["JARVIS_HOST"] == "0.0.0.0"


def test_a_commented_out_key_is_not_treated_as_set(ra):
    ra.ENV.write_text("#JARVIS_PASSWORD=old\n", encoding="utf-8")
    assert "JARVIS_PASSWORD" not in ra.read_env()
    # ...and setting it adds a live line rather than editing the comment.
    ra.write_env({"JARVIS_PASSWORD": "b" * 14})
    text = ra.ENV.read_text(encoding="utf-8")
    assert "#JARVIS_PASSWORD=old" in text
    assert ra.read_env()["JARVIS_PASSWORD"] == "b" * 14


# ---------------------------------------------------------------- passwords
def test_short_and_obvious_passwords_are_refused(ra):
    for bad in ("", "short", "password", "jarvis", "123456789012345", "changeme"):
        assert ra.choose_password(bad) is None, bad


def test_a_reasonable_password_is_accepted_and_not_altered(ra):
    good = "correct horse battery"
    assert ra.choose_password(good) == good


def test_surrounding_whitespace_is_trimmed(ra):
    # Copy-paste from a password manager brings a trailing newline, and a
    # password that silently includes one is a password that will not work.
    assert ra.choose_password("  a-good-long-password  \n") == "a-good-long-password"


def test_it_never_invents_a_password(ra, monkeypatch):
    # Nothing here may generate a secret. It would have to be printed to a
    # terminal to be usable, and the one secret guarding the machine should be
    # chosen by the person it belongs to.
    monkeypatch.setattr(ra.sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    assert ra.choose_password(None) is None


def test_turning_it_on_refuses_to_change_anything_without_a_password(ra):
    ra.ENV.write_text("JARVIS_HOST=127.0.0.1\nJARVIS_AUTH_MODE=local\n", encoding="utf-8")
    assert ra.turn_on("tooshort") == 1
    # Crucially it must not have opened the port and left the password unset.
    values = ra.read_env()
    assert values["JARVIS_HOST"] == "127.0.0.1"
    assert values["JARVIS_AUTH_MODE"] == "local"


def test_turning_it_on_sets_all_three_together(ra):
    ra.ENV.write_text("JARVIS_HOST=127.0.0.1\nJARVIS_AUTH_MODE=local\n", encoding="utf-8")
    assert ra.turn_on("a-good-long-password") == 0
    values = ra.read_env()
    assert values["JARVIS_HOST"] == "0.0.0.0"
    assert values["JARVIS_AUTH_MODE"] == "password"
    assert values["JARVIS_PASSWORD"] == "a-good-long-password"


def test_turning_it_off_closes_the_port_and_restores_local_mode(ra):
    ra.ENV.write_text("JARVIS_HOST=0.0.0.0\nJARVIS_AUTH_MODE=password\n"
                      "JARVIS_PASSWORD=a-good-long-password\n", encoding="utf-8")
    assert ra.turn_off() == 0
    values = ra.read_env()
    assert values["JARVIS_HOST"] == "127.0.0.1"
    assert values["JARVIS_AUTH_MODE"] == "local"
    # The password survives, so turning it back on needs no new one.
    assert values["JARVIS_PASSWORD"] == "a-good-long-password"


def test_forget_clears_the_password_as_well(ra):
    ra.ENV.write_text("JARVIS_HOST=0.0.0.0\nJARVIS_AUTH_MODE=password\n"
                      "JARVIS_PASSWORD=a-good-long-password\n", encoding="utf-8")
    assert ra.forget_password() == 0
    values = ra.read_env()
    assert values["JARVIS_HOST"] == "127.0.0.1"
    assert values.get("JARVIS_PASSWORD", "") == ""


# --------------------------------------------------------- firewall analysis
def test_an_unrunnable_firewall_check_returns_none_not_empty(ra, monkeypatch):
    """The distinction between finding nothing and not being able to look.

    Returning an empty list when the check failed would make the script print
    that nothing else allows the port -- a security claim it has not actually
    established.
    """
    monkeypatch.setattr(ra, "PROBE", ra.ROOT / "does-not-exist.ps1")
    rules, detail = ra.competing_rules()
    assert rules is None
    assert "missing" in detail
