"""Trusted board scope selects a fail-closed profile, never prompt text."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("private_profile_runner", SCRIPTS / "mc-team-runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
PRIVATE_BOARD = "00000000-0000-4000-8000-00000000b0a7"


@pytest.mark.parametrize("scope", ["runner", "reviewer"])
def test_private_board_unsupported_transport_refuses_before_build_or_prompt(monkeypatch, tmp_path, scope):
    monkeypatch.setattr(r, "BOARD", PRIVATE_BOARD if scope == "runner" else "")
    monkeypatch.setattr(r, "harness_cmd", lambda *a: pytest.fail("ordinary Claude must not be built"))
    claim = {"attempt": {"provider": "claude-code", "model": "opus-5-5",
                         "thinking_level": "high", "id": "synthetic-unique"},
             "board_id": PRIVATE_BOARD if scope == "reviewer" else None}
    result = r._run_harness(claim, "synthetic prompt", tmp_path, tmp_path, 10)
    assert result["success"] is False and result["retryable"] is False
    assert result["usage_observed"] is False
    assert result["text"] == "private-board transport required"


def test_native_private_profile_command_and_unknown_startup_are_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "BOARD", PRIVATE_BOARD)
    monkeypatch.setattr(r, "STOP", r.threading.Event())
    runtime = tmp_path / "api-runtime"
    runtime.mkdir()
    (runtime / "settings.json").write_text('{"compaction":{"enabled":true,"reserveTokens":16384,"keepRecentTokens":12000}}')
    # Existing fixture opt-in skips only provider-key preparation; no model or API.
    monkeypatch.setenv("RUNNER_FIXTURES", "1")
    monkeypatch.setenv("FIXTURE_LOG", str(tmp_path / "fixture.log"))
    monkeypatch.setattr(r, "harness_cmd", lambda *a: (["pi", "--provider", "fixture", "--model", "fixture"],
                                                    {"PI_CODING_AGENT_DIR": str(runtime)}))
    observed = []
    def sandbox(argv, *a, **kw):
        observed.append(argv)
        # Real _run_harness supervision/parser, a deterministic startup refusal only.
        return ["/bin/sh", "-c", "echo 'private-board tool-profile failed closed'; exit 78"]
    monkeypatch.setattr(r, "sandbox_command", sandbox)
    monkeypatch.setattr(r, "api", lambda *a, **kw: pytest.fail("unobserved usage must not be posted"))
    claim = {"attempt": {"id": "synthetic-unique", "provider": "omniroute-codex",
                         "model": "gpt-6.1-sol", "thinking_level": "high"}, "role_kind": "qa"}
    prompt = "synthetic prompt " * 9000  # > 128 KiB: one argv string that large fails execve (E2BIG)
    result = r._run_harness(claim, prompt, tmp_path / "source", tmp_path, 10)
    assert result["success"] is False and result["retryable"] is False
    assert result["usage_observed"] is False
    argv = observed[0]
    assert argv[0].endswith("/bin/node")
    assert Path(argv[1]).read_bytes() == (SCRIPTS / "private-pi-worker.mjs").read_bytes()
    meta = json.loads(argv[2])
    assert meta["session_id"] == "mc-team-synthetic-unique"
    assert meta["write_worktree"] is False
    assert meta["provider"] == meta["model"] == "fixture"
    assert meta["settings"]["compaction"]["keepRecentTokens"] == 12000
    assert Path(meta["prompt_file"]).read_text() == prompt
    assert prompt not in argv and max(map(len, argv)) < 131072


def test_private_board_foreign_runtime_refuses_before_copy_or_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "BOARD", PRIVATE_BOARD)
    monkeypatch.setenv("RUNNER_FIXTURES", "1")
    monkeypatch.setenv("FIXTURE_LOG", str(tmp_path / "fixture.log"))
    monkeypatch.setattr(r, "harness_cmd", lambda *a: (["pi", "--provider", "fixture", "--model", "fixture"],
                                                    {"PI_CODING_AGENT_DIR": "/foreign-tenant/synthetic"}))
    monkeypatch.setattr(r, "sandbox_command", lambda *a, **kw: pytest.fail("foreign runtime must not spawn"))
    claim = {"attempt": {"provider": "omniroute-codex", "model": "gpt-6.1-sol",
                         "thinking_level": "high", "id": "synthetic-unique"}, "role_kind": "qa"}
    result = r._run_harness(claim, "synthetic", tmp_path, tmp_path, 10)
    assert result["success"] is False and result["retryable"] is False
    assert result["usage_observed"] is False
    assert not (tmp_path / "private-pi-worker.mjs").exists()


def test_scoped_wrapper_skips_unreachable_database_and_preserves_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "BOARD", PRIVATE_BOARD)
    monkeypatch.setattr(r, "STOP", r.threading.Event())
    monkeypatch.setattr(r, "disposable_postgres", lambda *a: pytest.fail("networkless worker cannot use a DB"))
    observed = []
    monkeypatch.setattr(r, "_run_harness", lambda *a: observed.append(a) or {"success": True})
    claim = {"attempt": {"provider": "omniroute-codex", "model": "gpt-6.1-sol"}, "role_kind": "qa"}
    assert r.run_harness(claim, "synthetic", tmp_path, tmp_path, 20)["success"]
    assert observed[0][-1] is None
    r.STOP.set()
    result = r.run_harness(claim, "synthetic", tmp_path, tmp_path, 20)
    assert result["success"] is False and result["retryable"] is False
    assert result["text"] == "interrupted: runner stopping; worker not started"
    assert len(observed) == 1
