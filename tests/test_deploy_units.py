"""
tests/test_deploy_units.py
==========================
Static checks on the Linux deployment files in deploy/linux/ and the deploy
workflow: the systemd units launch the two bots exactly as BOTS.md describes,
every CLI flag they pass exists in bot.py, and the installer, workflow and
units all agree on the unit names.
"""

from __future__ import annotations

import configparser
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINUX = ROOT / "deploy" / "linux"
UNITS = ["calendar-bot-paper", "calendar-bot-test"]


def _unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # systemd keys are case-sensitive
    cp.read(LINUX / f"{name}.service", encoding="utf-8")
    return cp


def _exec_args(name: str) -> list[str]:
    return shlex.split(_unit(name)["Service"]["ExecStart"])


@pytest.mark.parametrize("name", UNITS)
class TestUnitFiles:
    def test_runs_bot_module_from_venv(self, name):
        args = _exec_args(name)
        assert args[:3] == ["@BOT_DIR@/.venv/bin/python", "-m", "bot"]

    def test_runs_from_repo_dir_as_bot_user(self, name):
        svc = _unit(name)["Service"]
        assert svc["WorkingDirectory"] == "@BOT_DIR@"
        assert svc["User"] == "@BOT_USER@"

    def test_restarts_and_starts_on_boot(self, name):
        cp = _unit(name)
        assert cp["Service"]["Restart"] == "always"
        assert cp["Install"]["WantedBy"] == "multi-user.target"
        assert "network-online.target" in cp["Unit"]["After"]

    def test_stops_with_sigterm(self, name):
        # bot.py / monitor/loop.py register a clean-shutdown handler for SIGTERM
        assert _unit(name)["Service"]["KillSignal"] == "SIGTERM"

    def test_flags_exist_in_bot_parser(self, name):
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        for arg in _exec_args(name)[3:]:
            if arg.startswith("--"):
                flag = arg.split("=", 1)[0]
                assert f'"{flag}"' in src, f"{flag} is not a bot.py argument"

    def test_lf_line_endings(self, name):
        assert b"\r" not in (LINUX / f"{name}.service").read_bytes()


def test_paper_unit_uses_default_env_db_and_log():
    args = _exec_args("calendar-bot-paper")
    assert "--portfolio=10000" in args
    for flag in ("--env", "--db", "--log"):
        assert not any(a.startswith(flag) for a in args)


def test_test_unit_uses_test_env_db_and_log():
    args = _exec_args("calendar-bot-test")
    pairs = dict(zip(args[3::2], args[4::2]))
    assert pairs["--env"] == ".env.test"
    assert pairs["--db"] == "db/calendar_bot_test.db"
    assert pairs["--log"] == "logs/bot_test.log"


def test_installer_and_workflow_reference_same_units():
    install = (LINUX / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"^UNITS=\(([^)]*)\)", install, re.M)
    assert m and m.group(1).split() == UNITS

    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert f"systemctl restart {' '.join(UNITS)}" in workflow
    assert "calendar-bot-vm" in workflow


def test_install_script_lf_line_endings():
    assert b"\r" not in (LINUX / "install.sh").read_bytes()
