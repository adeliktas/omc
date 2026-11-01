import json


def test_actual_qa_gate_can_review_a_change_to_the_runner_itself(tmp_path, monkeypatch):
    # F-01 QA 9db0bf4a: the complete runner source was refused in base AND integration,
    # so no runner change could ever reach exact-head QA. Literal refusal: test_team_failure_first.py.
    import subprocess
    from pathlib import Path

    from tests.test_backlog_independent_regressions import runner

    r = runner()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], text=True
        ).strip()

    source = (Path(__file__).resolve().parents[2] / "scripts" / "mc-team-runner.py").read_text()
    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@invalid")
    (repo / "scripts" / "mc-team-runner.py").write_text(source)
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "scripts" / "mc-team-runner.py").write_text(source + "# reviewed change\n")
    git("commit", "-qam", "candidate")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(r, "REPO", repo)
    monkeypatch.setattr(r, "_hidden_host_paths", lambda: [])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    r.qa_evidence(scratch, base, head, cwd=repo)  # sandbox hash probe is outside this check
    assert json.loads((scratch / "qa-evidence.json").read_text())["evidence_redacted"] is False


def test_a_changed_symlink_is_reviewable_and_a_link_to_private_bytes_is_not(tmp_path, monkeypatch):
    # 0000a004, 2026-10-26: a root Dockerfile -> build/Dockerfile link. Its Git blob is the link
    # text, the hash probe read the target's bytes, and QA was refused in every cycle.
    import subprocess

    from tests.test_backlog_independent_regressions import runner

    r = runner()
    repo = tmp_path / "repo"
    (repo / "build").mkdir(parents=True)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@invalid")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / "build" / "Dockerfile").write_text("FROM alpine\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "Dockerfile").unlink()
    (repo / "Dockerfile").symlink_to("build/Dockerfile")
    git("commit", "-qam", "link")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(r, "REPO", repo)
    monkeypatch.setattr(r, "_hidden_host_paths", lambda: [])
    # The real hash probe, run in the worktree without the bwrap sandbox.
    monkeypatch.setattr(r, "sandbox_command", lambda argv, cwd, tmp, writable: [
        "sh", "-c", 'cd "$0" && exec "$@"', str(cwd), *argv])
    (tmp_path / "s1").mkdir()
    assert r.qa_evidence(tmp_path / "s1", base, head, cwd=repo) is not None
    (repo / "Dockerfile").unlink()
    (repo / "Dockerfile").symlink_to(".env")
    (repo / ".env").write_text("TOKEN=x\n")
    git("add", "Dockerfile")
    git("commit", "-qm", "private link")
    (tmp_path / "s2").mkdir()
    assert r.qa_evidence(tmp_path / "s2", head, git("rev-parse", "HEAD"), cwd=repo) is None
