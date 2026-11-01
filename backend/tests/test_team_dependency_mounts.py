"""Every sandbox build of a worktree gets the read-only dependency mounts.

2026-10-07: QA's evidence preflight built a sandbox first; the empty mount
points it left made the QA sandbox skip both mounts, so QA had no pytest.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"


@pytest.fixture
def runner(monkeypatch, tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap not installed")
    spec = importlib.util.spec_from_file_location("team_runner_mounts", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    for dep in ("backend/.venv/bin", "frontend/node_modules/typescript"):
        (repo / dep).mkdir(parents=True)
    monkeypatch.setattr(module, "REPO", repo)
    return module


def _mounts(args):
    return {args[i + 2] for i, a in enumerate(args) if a == "--ro-bind" and "/repo/" in args[i + 1]}


def test_second_build_still_mounts_dependencies(runner, tmp_path):
    wt = tmp_path / "wt"
    for part in ("backend", "frontend"):
        (wt / part).mkdir(parents=True)
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    expected = {str(wt / "backend/.venv"), str(wt / "frontend/node_modules")}
    for writable in (False, False, True):  # preflight, QA, and a developer rebuild
        args = runner.sandbox_command(["true"], wt, tmp, writable=writable)
        assert _mounts(args) == expected


def test_go_cache_mount_and_harness_env_are_explicit(runner, monkeypatch, tmp_path):
    wt, scratch, module_cache = tmp_path / "wt", tmp_path / "scratch", tmp_path / "gomod"
    wt.mkdir()
    scratch.mkdir()
    module_cache.mkdir()
    (wt / "go.mod").write_text("module fixture\\n")
    monkeypatch.setattr(runner, "GO_MOD_CACHE", module_cache)
    for name in ("HOME", "GOPATH", "GOMODCACHE"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        runner,
        "harness_cmd",
        lambda *a: (
            ["sh", "-c", "env | grep -E '^(GOMODCACHE|GOCACHE|HOME|GOPATH)='; go env GOMODCACHE"],
            {},
        ),
    )
    sandbox = runner.sandbox_command
    built_command = []

    def capture_sandbox(*args, **kwargs):
        command = sandbox(*args, **kwargs)
        built_command.extend(command)
        return command

    monkeypatch.setattr(runner, "sandbox_command", capture_sandbox)
    monkeypatch.setattr(runner, "api", lambda *a, **k: {})
    claim = {
        "role_kind": "developer",
        "attempt": {
            "id": "cache",
            "provider": "claude-code",
            "model": "fixture",
            "thinking_level": "low",
        },
    }
    runner._run_harness(claim, "brief", wt, scratch, 30)

    output = (scratch / "out.raw").read_text()
    assert f"GOMODCACHE={module_cache}" in output
    assert f"GOCACHE={scratch / 'go-cache'}" in output
    assert "GOPATH=" not in output and "HOME=" not in output
    assert output.rstrip().endswith(str(module_cache))
    mount = ["--ro-bind", str(module_cache), str(module_cache)]
    assert any(built_command[i : i + 3] == mount for i in range(len(built_command) - 2))


def test_real_content_is_never_shadowed(runner, tmp_path):
    wt = tmp_path / "wt"
    (wt / "backend/.venv/bin").mkdir(parents=True)
    (wt / "frontend").mkdir()
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    args = runner.sandbox_command(["true"], wt, tmp, writable=False)
    assert _mounts(args) == {str(wt / "frontend/node_modules")}


def test_worker_tmp_is_a_host_directory_bound_before_other_tmp_binds(runner, tmp_path):
    wt = tmp_path / "wt"
    (wt / "backend").mkdir(parents=True)
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    runner.evidence_dir(tmp).mkdir()
    args = runner.sandbox_command(["true"], wt, tmp, writable=False)
    first = args.index("/tmp") - 2
    assert args[first : first + 3] == ["--bind", str(runner.evidence_dir(tmp)), "/tmp"]
    later = [
        i
        for i, a in enumerate(args)
        if a.startswith("/tmp/") and args[i - 2] in ("--bind", "--ro-bind")
    ]
    assert all(i > first for i in later)


def test_collect_evidence_takes_only_plain_small_text_files(runner, tmp_path):
    root = tmp_path / "ev"
    (root / "sub").mkdir(parents=True)
    (root / "mc-team-attempt.x").mkdir()
    (root / "probe.py").write_bytes(b"print(1)\r\n")  # kept verbatim, no newline translation
    (root / "sub/run.sh").write_text("echo ok\n")
    (root / "mc-team-attempt.x/inner.py").write_text("print(2)\n")
    (root / "claude-0").mkdir()
    (root / "claude-0/notes.md").write_text("harness scratch\n")
    (root / ".hidden.py").write_text("print(3)\n")
    (root / "out.raw").write_text("raw\n")
    (root / "big.txt").write_text("x" * (runner.EVIDENCE_MAX_BYTES + 1))
    (root / "bin.txt").write_bytes(b"\xff\xfe")
    (root / "empty.md").write_text("")
    (root / "link.py").symlink_to(root / "probe.py")
    (root / "hard.py").hardlink_to(root / "sub/run.sh")
    assert runner.collect_evidence(root) == {"probe.py": "print(1)\r\n"}


def test_probe_written_to_tmp_inside_the_real_sandbox_survives(runner, tmp_path):
    import subprocess

    wt = tmp_path / "wt"
    (wt / "backend").mkdir(parents=True)
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    runner.evidence_dir(tmp).mkdir()
    cmd = runner.sandbox_command(
        ["sh", "-c", "printf 'assert True\\n' > /tmp/probe.py && test -d " + str(tmp)],
        wt,
        tmp,
        writable=False,
    )
    assert subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    assert runner.collect_evidence(runner.evidence_dir(tmp)) == {"probe.py": "assert True\n"}
