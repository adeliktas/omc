import importlib.util
import os
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mc-disk-janitor.py"


def test_janitor_removes_only_old_attempt_worktrees(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("janitor", SCRIPT)
    j = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(j)
    old, new, other = tmp_path / "mc-team-work-team-dev.old", tmp_path / "mc-team-work-team-qa.new", tmp_path / "keep-me"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    ran = []
    monkeypatch.setattr(j, "WORK", tmp_path)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    stale, live, foreign = tmp / "mc-claude-qa.x", tmp / "mc-team-attempt.y.evidence", tmp / "doc-handoff"
    for d in (stale, live, foreign):
        d.mkdir()
    for d in (stale, foreign):
        os.utime(d, (past, past))
    monkeypatch.setattr(j, "TMP", tmp)
    monkeypatch.setattr(j, "LOG", tmp_path / "janitor.log")
    monkeypatch.setattr(j.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    j.main()
    assert not old.exists() and new.exists() and other.exists()
    assert not stale.exists() and live.exists() and foreign.exists()  # tmpfs: old runner temp dirs only
    assert ["docker", "volume", "prune", "-f"] in ran and not any("image" in c for c in ran)  # rollback images kept
