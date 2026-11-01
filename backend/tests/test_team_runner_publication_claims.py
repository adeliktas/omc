"""Runner comments never claim a cycle was not published without evidence.

2026-10-07: independent QA reproduced a block after the host fast-forward being
requeued as "production moved ... nothing published", and a lost reservation
race reported as "nothing published" although the cycle was published.
Harness adapted from that review's fixture probes.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import team_cycle as svc
from tests import test_team_cycle as tc
from tests.test_team_cycle import claim, to_qa

env = tc.env  # the shared SQLite team fixture

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"


def _runner():
    spec = importlib.util.spec_from_file_location("runner_publication_claims", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path):
    repo = tmp_path / "production"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@invalid")
    for content in ("base\n", "reviewed\n"):
        (repo / "f.txt").write_text(content)
        git("add", ".")
        git("commit", "-qm", content)
    reviewed = git("rev-parse", "HEAD")
    base = git("rev-parse", "HEAD~1")
    return repo, git, base, reviewed


async def _qa_passed(env, sha):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    cycle.integration_sha = sha
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True, artifact="ok", head_sha=sha, verdict="pass")
    return cycle


def _main_on_blocked(r, cycle, task, monkeypatch):
    notes, patches = [], []

    class FakeRun:
        base = "/fixture"

        def __init__(self, *_):
            pass

        def view(self):
            return cycle, []

        def drive(self):
            pass

        def comment(self, text):
            notes.append(text)

    def fake_api(method, path, data=None):
        if method == "get":
            return [task]
        if method == "patch":
            patches.append(data)
            return {}
        return {"cycle": cycle, "resumed": True}

    monkeypatch.setattr(r, "Cycle", FakeRun)
    monkeypatch.setattr(r, "api", fake_api)
    monkeypatch.setattr(r, "git", lambda *args, **kw: "" if args[-1] == "--porcelain" else "x")
    monkeypatch.setattr(r, "task_lock", lambda *_: object())
    monkeypatch.setattr(r, "log", lambda *_: None)
    monkeypatch.setattr(r, "BOARD", "fixture-board")
    monkeypatch.setenv("RUNNER_APPLY", "1")
    monkeypatch.setattr(sys, "argv", ["runner", task["id"]])
    with patch.object(r.signal, "signal"):
        assert r.main() == 0
    return notes, patches


@pytest.mark.parametrize("fast_forwarded", [True, False])
async def test_production_moved_block_requeues_only_without_the_reviewed_commit(env, tmp_path, monkeypatch, fast_forwarded):
    session, board, task = env
    repo, git, base, reviewed = _repo(tmp_path)
    if not fast_forwarded:
        git("reset", "--hard", "-q", base)
    cycle = await _qa_passed(env, reviewed)
    await svc.reserve_publication(session, cycle, reviewed, cycle.version)
    await svc.fail_publication(session, cycle, cycle.version, "production moved; completion report lost")
    r = _runner()
    monkeypatch.setattr(r, "REPO", repo)
    notes, patches = _main_on_blocked(r, cycle.model_dump(mode="json"), task.model_dump(mode="json"), monkeypatch)
    assert not any("nothing published" in n for n in notes)
    if fast_forwarded:  # production contains the reviewed commit: inspect, never requeue
        assert patches[-1]["status"] == "blocked" and notes[-1].startswith("**DECISION NEEDED**")
    else:
        assert patches[-1]["status"] == "inbox" and notes[-1] == "**team-runner**: production moved during the cycle; requeued."


async def test_lost_reservation_speaks_only_for_this_runner(env, monkeypatch):
    session, board, task = env
    cycle = await _qa_passed(env, "a" * 40)
    snapshot = cycle.model_dump(mode="json")
    r = _runner()
    notes = []
    run = object.__new__(r.Cycle)
    run.base, run.comment = "/fixture", notes.append

    def fake_api(method, path, data=None):
        raise r.ApiError(409, "cycle is not QA-passed at that version")

    monkeypatch.setattr(r, "api", fake_api)
    monkeypatch.setattr(r, "git", lambda *args, **kw: "" if args[-1] == "--porcelain" else "a" * 40)
    monkeypatch.setattr(r.time, "sleep", lambda *_: None)
    monkeypatch.setattr(r, "log", lambda *_: None)
    run._publish_locked(snapshot)
    assert notes and "nothing published" not in notes[-1] and "this runner did not publish" in notes[-1]
