import importlib.util
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load():
    spec = importlib.util.spec_from_file_location("omc_ship", SCRIPTS / "omc_ship.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_later_rules_win_and_only_tracked_files_ship(tmp_path):
    for rel in ("scripts/a.py", "scripts/seed-x.py", "docs/ops/log.md", "README.md", "untracked.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    (tmp_path / "scripts/omc-export.paths").write_text("# c\nscripts/*\nREADME.md\nuntracked.py\n!scripts/seed-*\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "scripts", "docs", "README.md"], cwd=tmp_path, check=True)
    assert load().shipped_files(tmp_path) == ["README.md", "scripts/a.py", "scripts/omc-export.paths"]
