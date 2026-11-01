import importlib.util
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _runner():
    path = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"
    import sys

    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("team_runner_deadline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_accepted_worker_keeps_per_attempt_timeout_across_cycle_deadline(monkeypatch):
    runner = _runner()
    monkeypatch.setattr(runner, "TOTAL_TIMEOUT", 120)
    cycle = runner.Cycle("task", {})
    cycle.cycle = {"deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    claim = {"_runner_deadline": 220.0}
    called = []
    monkeypatch.setattr(
        runner, "run_harness", lambda *args: called.append(args[4]) or {"success": True}
    )

    result = cycle._harness(claim, "prompt", Path("."), Path("."), threading.Event())

    assert result["success"] is True
    assert called == [120]  # cycle deadline no longer truncates a live claim
