"""The fleet helper talks to the host's init, not to a hardcoded OpenRC (2026-11-01).

Before this, the watchdog's health check ran `rc-service` unconditionally, so on a
systemd host it raised FileNotFoundError and the pool was never reported or repaired.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("mc_service", SCRIPTS / "mc_service.py")
mc_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc_service)


class Calls:
    def __init__(self, returncode=0, stdout=""):
        self.argv = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, args, **_kw):
        self.argv = args
        return type("P", (), {"returncode": self.returncode, "stdout": self.stdout})()


@pytest.fixture
def systemd(monkeypatch):
    monkeypatch.setattr(mc_service, "_systemd", lambda: True)


@pytest.fixture
def openrc(monkeypatch):
    monkeypatch.setattr(mc_service, "_systemd", lambda: False)


def test_running_asks_systemd(monkeypatch, systemd):
    calls = Calls()
    monkeypatch.setattr(mc_service.subprocess, "run", calls)
    assert mc_service.running("mc-team-pool") is True
    assert calls.argv == ["systemctl", "--user", "is-active", "--quiet", "mc-team-pool"]


def test_running_asks_openrc(monkeypatch, openrc):
    calls = Calls(stdout=" * status: started\n")
    monkeypatch.setattr(mc_service.subprocess, "run", calls)
    assert mc_service.running("mc-team-pool") is True
    assert calls.argv == ["rc-service", "-U", "mc-team-pool", "status"]

    monkeypatch.setattr(mc_service.subprocess, "run", Calls(returncode=1, stdout=" * status: stopped\n"))
    assert mc_service.running("mc-team-pool") is False


def test_restart_uses_the_host_init(monkeypatch, systemd):
    calls = Calls()
    monkeypatch.setattr(mc_service.subprocess, "run", calls)
    assert mc_service.restart("mc-team-pool") is True
    assert calls.argv == ["systemctl", "--user", "restart", "mc-team-pool"]

    monkeypatch.setattr(mc_service, "_systemd", lambda: False)
    monkeypatch.setattr(mc_service.subprocess, "run", calls)
    assert mc_service.restart("mc-team-pool") is True
    assert calls.argv == ["rc-service", "-U", "mc-team-pool", "restart"]


def test_a_missing_init_command_is_not_a_crash(monkeypatch, systemd):
    def explode(_args, **_kw):
        raise FileNotFoundError("rc-service")

    monkeypatch.setattr(mc_service.subprocess, "run", explode)
    assert mc_service.running("mc-team-pool") is False
    assert mc_service.restart("mc-team-pool") is False
