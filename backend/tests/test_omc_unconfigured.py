"""Instance features stay off until configured in data/etc/mc-team/fleet.json (OMC spec C2)."""
import importlib.util
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _forbid(*a, **k):
    raise AssertionError("an unconfigured feature must not call out")


@pytest.mark.parametrize("script", ["omc-intake.py", "omc-github-responder.py", "omc-next-batch.py"])
def test_unconfigured_fleet_features_are_off(script, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMC_DATA", str(tmp_path))
    for var in ("INTAKE_WORK", "INTAKE_STATE", "INTAKE_LOG", "OMC_GH_REPO", "OMC_GH_BOARD", "OMC_GH_STATE", "OMC_GH_LOG"):
        monkeypatch.delenv(var, raising=False)
    import omc_paths  # noqa: F401 - reloaded so OMC_DATA applies

    importlib.reload(omc_paths)
    spec = importlib.util.spec_from_file_location(script.replace("-", "_")[:-3], SCRIPTS / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for target in (urllib.request, subprocess):
        for name in ("urlopen", "run", "Popen", "check_output"):
            if hasattr(target, name):
                monkeypatch.setattr(target, name, _forbid)
    assert mod.main() in (0, None)
    logs = capsys.readouterr().out + "".join(p.read_text() for p in (tmp_path / "log").glob("*.log"))
    assert "not configured" in logs
    monkeypatch.undo()
    importlib.reload(omc_paths)
