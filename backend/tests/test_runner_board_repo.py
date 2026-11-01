import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"


def load(monkeypatch, tmp_path, board):
    mapping = tmp_path / "board-repos.json"
    mapping.write_text(json.dumps({"board-x": str(tmp_path / "other")}))
    monkeypatch.setenv("RUNNER_BOARD_REPOS", str(mapping))
    monkeypatch.setenv("RUNNER_BOARD", board)
    spec = importlib.util.spec_from_file_location("runner_board_repo", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mapped_board_targets_its_repo_and_never_deploys(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path, "board-x")
    assert r.REPO == tmp_path / "other" and r.MC_REPO != r.REPO and r.DEPLOY is False


def test_unmapped_board_keeps_mission_control(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path, "board-y")
    assert r.REPO == r.MC_REPO


def test_foreign_publication_pushes_branch_and_opens_linked_pr(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path, "board-x")
    calls, said = [], []
    monkeypatch.setattr(r, "git", lambda cwd, *a, **k: calls.append(a) or ("origin/main" if a[0] == "symbolic-ref" else ""))
    monkeypatch.setattr(r.subprocess, "run", lambda argv, **k: calls.append(tuple(argv)) or subprocess.CompletedProcess(argv, 0, "https://github.com/o/r/pull/7\n", ""))
    cycle = r.Cycle.__new__(r.Cycle)
    cycle.task_id, cycle.task = "abcdef1234", {"title": "Fix X", "description": "Context\nGitHub issues: #42, #7\n"}
    cycle.comment = said.append
    qa = [{"stage_key": "qa", "verdict": "fail", "round": 1, "attempt_no": 1, "artifact": "Follow-ups:\nold\nVERDICT: FAIL"},
          {"stage_key": "qa", "verdict": "pass", "round": 2, "attempt_no": 1,
           "artifact": "Checks passed.\n**Follow-ups:**\n- SIGTERM race at final build (probe /tmp/p.py)\n\nVERDICT: PASS"}]
    cycle.view = lambda: ({}, qa)
    cycle.open_pr("0123456789abcdef")
    assert ("push", "-q", "origin", "0123456789abcdef:refs/heads/omc/abcdef12-01234567") in calls
    pr = next(c for c in calls if c[:3] == ("gh", "pr", "create"))
    assert "main" in pr and "Fix X" in pr and any("Closes #7\nCloses #42" in part for part in pr)
    body = pr[pr.index("--body") + 1]
    assert body.endswith("## QA follow-ups (non-blocking)\n- SIGTERM race at final build (probe /tmp/p.py)\n")
    assert "old" not in body
    assert "pull/7" in said[0]


@pytest.mark.parametrize("dirty", [False, True])
def test_pr_publication_ignores_conflicting_staging_head_but_not_dirt(monkeypatch, tmp_path, dirty):
    r = load(monkeypatch, tmp_path, "board-x")
    repo = r.REPO
    repo.mkdir()
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    r.git(repo, "init", "-q")
    r.git(repo, "config", "user.name", "fixture")
    r.git(repo, "config", "user.email", "fixture@invalid")
    r.git(repo, "remote", "add", "origin", str(remote))
    (repo / "api.txt").write_text("base\n")
    r.git(repo, "add", ".")
    r.git(repo, "commit", "-qm", "base")
    base = r.git(repo, "rev-parse", "HEAD")
    (repo / "api.txt").write_text("reviewed\n")
    r.git(repo, "commit", "-qam", "reviewed")
    reviewed = r.git(repo, "rev-parse", "HEAD")
    r.git(repo, "checkout", "-q", "--detach", base)
    (repo / "api.txt").write_text("unrelated staged dependency\n")
    r.git(repo, "commit", "-qam", "other task")
    staging = r.git(repo, "rev-parse", "HEAD")
    if dirty:
        (repo / "api.txt").write_text("operator edits\n")
    calls, notes = [], []
    monkeypatch.setattr(r, "api", lambda method, path, data: calls.append((path, data)))
    real_run = r.subprocess.run

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r/pull/8\n", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(r.subprocess, "run", run)
    cycle = r.Cycle.__new__(r.Cycle)
    cycle.task_id, cycle.task = "abcdef1234", {"title": "API docs"}
    cycle.base, cycle.cleanup, cycle.refreshes = "/fixture", [], 0
    cycle.comment, cycle.view, cycle.budget_ready = notes.append, lambda: ({}, []), lambda: True
    cycle._publish_locked({"id": "cycle", "version": 3, "reviewed_sha": reviewed})
    assert r.git(repo, "rev-parse", "HEAD") == staging
    if dirty:
        assert (repo / "api.txt").read_text() == "operator edits\n"
        assert calls[-1][0].endswith("/publication/fail")
        assert not r.git(remote, "for-each-ref", "--format=%(refname)")
    else:
        assert r.git(remote, "rev-parse", f"refs/heads/omc/abcdef12-{reviewed[:8]}") == reviewed
        assert calls[-1] == ("/fixture/cycles/cycle/publication/complete", {"sha": reviewed, "version": 4})
        assert any("published as pull request" in note for note in notes)


def test_rejected_push_keeps_the_commit_reachable_for_a_later_push(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path, "board-x")
    calls, said = [], []

    def git(cwd, *a, **k):
        calls.append(a)
        if a[0] == "push":
            raise RuntimeError("git push failed: refusing to allow an OAuth App to create or update workflow")
        return "origin/main" if a[0] == "symbolic-ref" else ""

    monkeypatch.setattr(r, "git", git)
    cycle = r.Cycle.__new__(r.Cycle)
    cycle.task_id, cycle.task, cycle.comment, cycle.view = "abcdef1234", {"title": "X"}, said.append, lambda: ({}, [])
    cycle.open_pr("0123456789abcdef")
    assert ("update-ref", "refs/omc-unpushed/omc/abcdef12-01234567", "0123456789abcdef") in calls
    assert "0123456789 is committed locally, but opening the PR failed" in said[0]


def test_workflow_scope_rejection_publishes_without_ci_files_and_carries_their_diff(monkeypatch, tmp_path):
    r = load(monkeypatch, tmp_path, "board-x")
    repo, remote = r.REPO, tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    hook = remote / "hooks/pre-receive"  # GitHub without the `workflow` scope: every new commit is checked
    hook.write_text("#!/bin/sh\nwhile read old new ref; do for c in $(git rev-list $new --not --all); do\n"
                    "  git diff-tree -m -r --name-only --no-commit-id $c | grep -q '^.github/workflows/' && {\n"
                    "  echo 'refusing to allow an OAuth App to create or update workflow without `workflow` scope' >&2; exit 1; }\n"
                    "done; done\nexit 0\n")
    hook.chmod(0o755)
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "f"), ("config", "user.email", "f@invalid"),
                 ("remote", "add", "origin", str(remote))):
        r.git(repo, *args)
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("on: push\n")
    (repo / "a.go").write_text("base\n")
    r.git(repo, "add", ".")
    r.git(repo, "commit", "-qm", "base")
    r.git(repo, "push", "-q", "origin", "main:refs/heads/dep")  # an already-published dependency branch
    r.git(repo, "push", "-q", "origin", "main")
    r.git(repo, "fetch", "-q", "origin")
    r.git(repo, "remote", "set-head", "origin", "main")
    (repo / ".github/workflows/ci.yml").write_text("on: [push, pull_request]\n")
    (repo / "a.go").write_text("feature\n")
    r.git(repo, "commit", "-qam", "feature with CI")
    r.git(repo, "merge", "-q", "--no-ff", "-m", "merge dep", "origin/dep")
    (repo / "b.go").write_text("more\n")
    r.git(repo, "add", ".")
    r.git(repo, "commit", "-qm", "more")
    sha = r.git(repo, "rev-parse", "HEAD")
    r.git(repo, "update-ref", f"refs/omc-unpushed/omc/abcdef12-{sha[:8]}", sha)
    r.git(repo, "checkout", "-q", "--detach", "origin/main")  # runners reset the shared clone to upstream
    with pytest.raises(RuntimeError, match="`workflow` scope"):
        r.git(repo, "push", "-q", "origin", f"{sha}:refs/heads/probe")
    real_run, said, bodies = r.subprocess.run, [], []

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "create"]:
            bodies.append(argv[argv.index("--body") + 1])
            return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r/pull/9\n", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(r.subprocess, "run", run)
    cycle = r.Cycle.__new__(r.Cycle)
    cycle.task_id, cycle.task = "abcdef1234", {"title": "CI", "description": "GitHub issues: #5\n"}
    cycle.comment, cycle.view = said.append, lambda: ({}, [])
    cycle.open_pr(sha)
    pushed = r.git(remote, "rev-parse", f"refs/heads/omc/abcdef12-{sha[:8]}")
    assert r.git(remote, "show", f"{pushed}:.github/workflows/ci.yml") == "on: push"
    assert r.git(repo, "diff", "--stat", pushed, sha, "--", "a.go", "b.go") == ""
    assert "Closes #5" in bodies[0] and "+on: [push, pull_request]" in bodies[0]
    assert "pull/9" in said[0] and not r.git(repo, "for-each-ref", "refs/omc-unpushed")
    assert r.without_workflows(sha)[0] == pushed  # stacked dependents rewrite to the same shared commits
