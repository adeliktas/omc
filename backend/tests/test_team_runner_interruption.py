"""A killed harness reports why it was killed, never an "empty model reply".

2026-10-06: stopping the pool killed three running developers; their partial output
parsed as "empty model reply" and each was requeued as a failed model attempt.
"""

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


def _runner():
    path = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"
    spec = importlib.util.spec_from_file_location("interruption_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hanging_cli(runner, monkeypatch):
    # Prints a JSON start event like a real CLI, then never replies.
    code = "import json,time; print(json.dumps({'type':'agent_start'}), flush=True); time.sleep(60)"
    argv = [sys.executable, "-c", code]
    monkeypatch.setattr(runner, "harness_cmd", lambda *a, **k: (argv, {}))
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, *a, **k: argv)
    monkeypatch.setattr(runner, "api", lambda *a, **k: {})
    monkeypatch.setenv("FIXTURE_LOG", "1")  # skip native account/runtime preparation


def _claim():
    return {"attempt": {"id": "attempt-1", "provider": "pi-neuralwatt", "model": "glm-5.3",
                        "thinking_level": "high"}}


def test_time_limit_is_named_and_not_retryable(tmp_path, monkeypatch):
    runner = _runner()
    _hanging_cli(runner, monkeypatch)
    res = runner._run_harness(_claim(), "prompt", tmp_path, tmp_path, 1)
    assert res["success"] is False and res["retryable"] is False
    assert res["text"] == "interrupted: attempt time limit reached; partial output discarded"


def test_runner_stop_is_named(tmp_path, monkeypatch):
    runner = _runner()
    _hanging_cli(runner, monkeypatch)
    threading.Timer(0.5, runner.STOP.set).start()
    try:
        res = runner._run_harness(_claim(), "prompt", tmp_path, tmp_path, 30)
    finally:
        runner.STOP.clear()
    assert res["text"].startswith("interrupted: runner stopping")
    assert json.dumps(res)  # still a plain JSON-serializable result for /complete


@pytest.mark.parametrize("later_claim", ["capacity_wait", "refused"])
def test_accepted_claim_is_supervised_before_requesting_more(
    tmp_path, monkeypatch, later_claim
):
    runner = _runner()
    monkeypatch.setattr(runner, "PARALLEL", 2)
    monkeypatch.setattr(runner, "evidence_dir", lambda tmp: tmp / "evidence")
    mkdtemp = runner.tempfile.mkdtemp
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **kw: mkdtemp(dir=tmp_path, **kw))
    cycle = runner.Cycle("synthetic-task", {})
    state = {"id": "synthetic-cycle", "state": "developing", "base_sha": "a" * 40}
    cycle.cycle = state
    views = iter([(state, []), (state, []), ({"state": "blocked"}, [])])
    monkeypatch.setattr(cycle, "view", lambda: next(views))
    monkeypatch.setattr(cycle, "budget_ready", lambda: True)
    started, release = threading.Event(), threading.Event()
    completions, supervised_at_next_claim = [], []
    claims = 0
    claim = {
        "attempt": {"id": "synthetic-attempt", "stage_key": "dev-check"},
        "claim_token": "t",
        "role_kind": "specialist",
    }

    def execute(claim, tmp, cancelled):
        # Cycle.attempt has installed the heartbeat before entering the provider.
        started.set()
        assert release.wait(2)
        return {"ok": False, "error": "synthetic provider result"}

    def api(method, path, body=None):
        nonlocal claims
        if path.endswith("/claims"):
            claims += 1
            if claims == 1:
                return claim
            supervised_at_next_claim.append(started.wait(1))
            release.set()
            if later_claim == "refused":
                raise runner.ApiError(503, "synthetic later claim refusal")
            return None  # no further backend capacity; not an inference failure
        if path.endswith("/complete"):
            completions.append(body)
        return {}

    monkeypatch.setattr(cycle, "_execute", execute)
    monkeypatch.setattr(runner, "api", api)
    if later_claim == "refused":
        with pytest.raises(runner.ApiError, match="synthetic later claim refusal"):
            cycle.drive()
    else:
        cycle.drive()
    assert supervised_at_next_claim == [True]
    assert completions == [{
        "ok": False, "error": "synthetic provider result", "claim_token": "t",
    }]
    assert not list(tmp_path.iterdir())  # completion and scratch cleanup both ran
