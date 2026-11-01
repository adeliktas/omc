import importlib
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def reload_paths():
    import omc_paths

    return importlib.reload(omc_paths)


def test_defaults_equal_production(monkeypatch):
    """Production sets no OMC_DATA; conftest does, so the test removes it."""
    monkeypatch.delenv("OMC_DATA", raising=False)
    monkeypatch.delenv("OMC_HOME", raising=False)
    p = reload_paths()
    assert p.OMC_DATA is None
    assert p.OMC_HOME == SCRIPTS.parent
    assert p.state_dir("mc-fleet-watchdog") == Path("/var/lib/mc-fleet-watchdog")
    assert p.log_path("omc-intake") == Path("/var/log/omc-intake.log")
    assert p.lock_path("mc-team-pool") == Path("/run/mc-team-pool.lock")
    assert p.etc_path("mc-team/board-repos.json") == Path("/etc/mc-team/board-repos.json")
    monkeypatch.undo()
    reload_paths()


def test_data_dir_relocates_everything(monkeypatch):
    monkeypatch.setenv("OMC_DATA", "/x")
    monkeypatch.setenv("OMC_HOME", "/srv/omc")
    p = reload_paths()
    assert p.OMC_HOME == Path("/srv/omc")
    assert p.state_dir("mc-fleet-watchdog") == Path("/x/state/mc-fleet-watchdog")
    assert p.log_path("omc-intake") == Path("/x/log/omc-intake.log")
    assert p.lock_path("mc-team-pool") == Path("/x/run/mc-team-pool.lock")
    assert p.etc_path("mc-team/board-repos.json") == Path("/x/etc/mc-team/board-repos.json")
    monkeypatch.undo()
    reload_paths()



def test_home_is_the_running_users_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/omc-x")
    assert reload_paths().HOME == Path("/home/omc-x")
    monkeypatch.undo()
    reload_paths()


def test_fleet_config_missing_is_empty_and_present_is_read(monkeypatch, tmp_path):
    monkeypatch.setenv("OMC_DATA", str(tmp_path))
    p = reload_paths()
    assert p.load_fleet_config() == {}
    (tmp_path / "etc/mc-team").mkdir(parents=True)
    (tmp_path / "etc/mc-team/fleet.json").write_text('{"forbidden_paths": ["/srv/private"]}')
    assert p.load_fleet_config() == {"forbidden_paths": ["/srv/private"]}
    monkeypatch.undo()
    reload_paths()

HOST_PATH = re.compile(r"(?<!\})(/root\b|/var/lib/|/var/log/|/run/[a-z-]+\.lock|/etc/mc|/home/[a-z])")


def host_path_offenders() -> list[str]:
    from omc_ship import shipped_files

    repo = SCRIPTS.parent
    out = []
    for rel in shipped_files(repo):
        if rel.startswith("scripts/") and rel.endswith((".py", ".sh", ".mjs")) and rel != "scripts/omc_paths.py":
            for n, line in enumerate((repo / rel).read_text().splitlines(), 1):
                if HOST_PATH.search(line):
                    out.append(f"{rel}:{n}")
    return out


def test_shipped_scripts_have_no_host_paths():
    assert host_path_offenders() == []
