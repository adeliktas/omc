"""Synthetic sensitive-root refusal before worker projection or evidence export."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / "scripts/mc-team-runner.py"
    spec = importlib.util.spec_from_file_location("private_source_denial_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    protected = tmp_path / "restricted"
    protected.mkdir()
    monkeypatch.setattr(module, "LOCAL_ONLY_ROOTS", (protected,), raising=False)
    return module, protected


def test_sandbox_refuses_restricted_projection_before_inspection(
    runner, tmp_path, monkeypatch
):
    r, protected = runner
    check = Mock(
        side_effect=AssertionError("no worker preparation for restricted source")
    )
    monkeypatch.setattr(r.shutil, "which", check)
    safe = tmp_path / "safe"
    safe.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    for cwd, scratch in [
        (protected, safe),
        (alias, safe),
        (safe, protected),
        (tmp_path, safe),
    ]:
        with pytest.raises(RuntimeError, match="local-only"):
            r.sandbox_command(["/usr/bin/python3"], cwd, scratch, writable=False)
    check.assert_not_called()


def test_dependency_mount_refuses_protected_executable(runner):
    r, protected = runner
    argv = []
    with pytest.raises(RuntimeError, match="local-only"):
        r._ro_bind(argv, protected / "synthetic-script", [])
    assert argv == []


def test_collector_refuses_restricted_root_before_walking(runner, monkeypatch):
    r, protected = runner
    walk = Mock(side_effect=AssertionError("no raw export walk"))
    monkeypatch.setattr(Path, "rglob", walk)
    with pytest.raises(RuntimeError, match="local-only"):
        r.collect_evidence(protected)
    walk.assert_not_called()


def test_cycle_refuses_protected_repo_before_git_or_prompt(
    runner, tmp_path, monkeypatch
):
    r, protected = runner
    monkeypatch.setattr(r, "REPO", protected)
    operation = Mock(
        side_effect=AssertionError("no archive, worktree, prompt or inference")
    )
    monkeypatch.setattr(r.subprocess, "check_output", operation)
    cycle = r.Cycle.__new__(r.Cycle)
    cycle.remaining = lambda: 90
    with pytest.raises(RuntimeError, match="local-only"):
        cycle._execute(
            {"attempt": {"id": "synthetic"}, "role_kind": "pm"},
            tmp_path / "scratch",
            None,
        )
    operation.assert_not_called()


@pytest.mark.parametrize("relative", ["backend/.venv", "frontend/node_modules"])
@pytest.mark.parametrize("kind", ["direct-protected", "protected-alias", "safe"])
def test_reused_dependency_mount_checks_source_before_effects(
    runner, tmp_path, monkeypatch, relative, kind
):
    r, protected = runner
    repo = tmp_path / "repo"
    dependency = repo / relative
    dependency.parent.mkdir(parents=True)
    if kind == "protected-alias":
        dependency.symlink_to(protected, target_is_directory=True)
    else:
        dependency.mkdir()
        if kind == "direct-protected":
            monkeypatch.setattr(r, "LOCAL_ONLY_ROOTS", (dependency,))
    cwd, scratch = tmp_path / "worktree", tmp_path / "scratch"
    (cwd / relative).parent.mkdir(parents=True)
    scratch.mkdir()
    monkeypatch.setattr(r, "REPO", repo)
    monkeypatch.setattr(r, "MANAGED_RO", ())
    monkeypatch.setattr(r, "PONYTAIL", str(tmp_path / "absent-skill"))
    monkeypatch.setattr(r, "_hidden_host_paths", lambda: [])
    monkeypatch.setattr(r, "_mount_program", lambda *args: None)
    monkeypatch.setattr(r, "fixtures_enabled", lambda: False)
    if kind != "safe":
        with pytest.raises(RuntimeError, match="local-only"):
            r.sandbox_command(["/usr/bin/true"], cwd, scratch, writable=False)
        assert not (cwd / relative).exists(), "refuse before creating mount point"
    else:
        command = r.sandbox_command(["/usr/bin/true"], cwd, scratch, writable=False)
        assert any(
            command[i:i + 3] == ["--ro-bind", str(dependency), str(cwd / relative)]
            for i in range(len(command) - 2)
        )
