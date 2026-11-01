"""Offline stdlib tests for the mc-team-pool supervisor and runner integration gating.

No backend, model, network or production service is involved: the pool runs
against a fake `mc` CLI serving a task list from a JSON file, and a fake
runner child that records its lifecycle. The Cycle.integrate gating tests run
the real mc-team-runner.py code against a throwaway git repo with a stubbed
API. These are mocked acceptance tests, not live provider acceptance.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE_RUNNER = """\
import fcntl, os, sys, time
from pathlib import Path
task = sys.argv[1]
rec = Path(os.environ["RUNNER_RECORD"])
with rec.open("a") as fh:
    fh.write(f"start {task} {time.time()}\\n")
# Mimic the real runner: hold the per-task lifecycle lock for the whole run.
fd = os.open(
    os.path.join(os.environ["RUNNER_LOCK_DIR"], f"mc-team-{task}.lock"),
    os.O_WRONLY | os.O_CREAT, 0o600,
)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
with rec.open("a") as fh:
    fh.write(f"locked {task}\\n")
time.sleep(float(os.environ.get("RUNNER_FAKE_SLEEP", "2")))
with rec.open("a") as fh:
    fh.write(f"end {task}\\n")
"""

FAKE_MC = """\
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rows = json.loads(Path(os.environ["POOL_TEST_TASKS_FILE"]).read_text())
# `mc get /api/v1/boards/<b>/tasks?limit=50&offset=0 --json`
path = sys.argv[2]
offset = int(path.split("offset=")[1]) if "offset=" in path else 0
limit = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else len(rows)
print(json.dumps({"items": rows[offset:offset + limit], "total": len(rows)}))
"""


def team_task(id: str, **over) -> dict:
    t = {
        "id": id,
        "title": "[team] work",
        "status": "inbox",
        "execution_mode": "team",
        "execution_lane": "harness",
        "is_blocked": False,
        "blocked_by_task_ids": [],
        "repeat_enabled": False,
        "repeat_interval_minutes": None,
        "updated_at": "2026-01-01T00:00:00",
    }
    t.update(over)
    return t


def setup_pool(tmp_path: Path, tasks: list[dict], **env) -> tuple[Path, dict, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name, body in (
        ("mc", FAKE_MC),
        ("mc-team-runner.py", FAKE_RUNNER),
        ("mc-team-pool.py", (ROOT / "scripts/mc-team-pool.py").read_text()),
        ("omc_paths.py", (ROOT / "scripts/omc_paths.py").read_text()),
    ):
        p = repo / "scripts" / name
        p.write_text(body)
        p.chmod(0o755)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps(tasks))
    record = tmp_path / "record"
    record.touch()
    lock_dir = tmp_path / "run"
    lock_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=f@x.invalid", "-c", "user.name=f", "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=f@x.invalid",
            "-c",
            "user.name=f",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    full = {
        **os.environ,
        "RUNNER_BOARD": "b",
        "RUNNER_APPLY": "1",
        "RUNNER_RECORD": str(record),
        "RUNNER_LOCK_DIR": str(lock_dir),
        "RUNNER_POOL_LOCK": str(tmp_path / "pool.lock"),
        "RUNNER_LOG": str(tmp_path / "pool.log"),
        "RUNNER_TEAM_POLL": "2",
        "RUNNER_FAKE_SLEEP": "2",
        "POOL_TEST_TASKS_FILE": str(tasks_file),
    }
    full.update(env)
    return repo, full, record


def run_pool(repo: Path, full: dict, seconds: float) -> subprocess.Popen:
    p = subprocess.Popen(
        ["python3", str(repo / "scripts/mc-team-pool.py")],
        env=full,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(seconds)
    return p


def stop_pool(p: subprocess.Popen) -> None:
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()


def events(record: Path) -> list[str]:
    return [line.split()[0] + " " + line.split()[1] for line in record.read_text().splitlines()]


def events(record: Path) -> list[str]:
    return [line.split()[0] + " " + line.split()[1] for line in record.read_text().splitlines()]


def test_two_team_tasks_run_simultaneously(tmp_path):
    repo, full, record = setup_pool(tmp_path, [team_task("t1"), team_task("t2")])
    p = run_pool(repo, full, 3.5)
    try:
        lines = record.read_text().splitlines()
        starts = [i for i, l in enumerate(lines) if l.startswith("start")]
        assert len(starts) == 2, lines
        # the second runner started before the first finished -> true concurrency
        first_end = next(i for i, l in enumerate(lines) if l.startswith("end"))
        assert starts[1] < first_end, lines
        assert {l.split()[1] for l in lines if l.startswith("locked")} == {"t1", "t2"}
    finally:
        stop_pool(p)


def test_per_task_lock_excludes_a_second_local_runner(tmp_path):
    # An already-running local runner holds the lifecycle lock: the pool must
    # not spawn a second runner for that task.
    repo, full, record = setup_pool(tmp_path, [team_task("held")])
    lock_dir = Path(full["RUNNER_LOCK_DIR"])
    fd = os.open(str(lock_dir / "mc-team-held.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        p = run_pool(repo, full, 3.5)
    finally:
        stop_pool(p)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert record.read_text() == ""


def test_sigterm_kills_children_within_grace(tmp_path):
    repo, full, record = setup_pool(tmp_path, [team_task("long")], RUNNER_FAKE_SLEEP="60")
    p = run_pool(repo, full, 3.0)
    assert record.read_text().startswith("start long")
    t0 = time.monotonic()
    p.send_signal(signal.SIGTERM)
    rc = p.wait(timeout=15)
    elapsed = time.monotonic() - t0
    assert rc == 0 and elapsed < 12  # terminated children, none left running
    assert not any(l.startswith("end") for l in record.read_text().splitlines())
    # the child itself is gone (no orphaned sleeper)
    time.sleep(0.5)
    out = subprocess.run(["pgrep", "-f", "mc-team-runner.py long"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_scheduling_gates_block_unready_tasks(tmp_path):
    from datetime import datetime, timedelta

    five_min_ago = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
    tasks = [
        team_task("ok"),
        team_task("blocked-flag", is_blocked=True),
        team_task("dependency", blocked_by_task_ids=["upstream"]),
        team_task("review", status="review"),
        team_task("blocked-status", status="blocked"),
        team_task("done", status="done"),
        team_task("legacy-mode", execution_mode="single"),
        team_task("other-lane", execution_lane="docs"),
        # recurring, ran 5 minutes ago with a 60 minute interval -> cooldown
        team_task(
            "cooling",
            repeat_enabled=True,
            repeat_interval_minutes=60,
            updated_at=five_min_ago,
        ),
    ]
    repo, full, record = setup_pool(tmp_path, tasks, RUNNER_FAKE_SLEEP="0.3")
    try:
        p = run_pool(repo, full, 4.0)
    finally:
        stop_pool(p)
    run_ids = {l.split()[1] for l in record.read_text().splitlines() if l.startswith("locked")}
    assert run_ids == {"ok"}


def test_repeat_cooldown_expires_and_pool_then_runs_the_task(tmp_path):
    tasks = [
        team_task(
            "due",
            repeat_enabled=True,
            repeat_interval_minutes=60,
            updated_at="2020-01-01T00:00:00",  # long past the interval
        )
    ]
    repo, full, record = setup_pool(tmp_path, tasks, RUNNER_FAKE_SLEEP="0.2")
    try:
        p = run_pool(repo, full, 4.0)
    finally:
        stop_pool(p)
    assert any(l.startswith("locked due") for l in record.read_text().splitlines())


def test_slots_rotate_instead_of_restarting_the_oldest_task(tmp_path):
    # A runner that only waits on an engine hold exits without changing updated_at; the
    # newer task must still get the slot next (2026-10-27: 0000a017 starved for 6h).
    tasks = [team_task("old"), team_task("new", updated_at="2026-01-02T00:00:00")]
    repo, full, record = setup_pool(tmp_path, tasks, RUNNER_TEAM_TASKS="1",
                                    RUNNER_FAKE_SLEEP="0.2")
    try:
        p = run_pool(repo, full, 5.0)
    finally:
        stop_pool(p)
    starts = [l.split()[1] for l in record.read_text().splitlines() if l.startswith("start")]
    assert starts[:2] == ["old", "new"], starts


def test_singleton_pool_lock_and_cap_of_one_concurrent_child(tmp_path):
    tasks = [team_task("a"), team_task("b")]
    repo, full, record = setup_pool(tmp_path, tasks, RUNNER_TEAM_TASKS="1")
    try:
        p = run_pool(repo, full, 0.5)
        # A second pool instance must exit immediately without spawning anything.
        dup = subprocess.run(
            ["python3", str(repo / "scripts/mc-team-pool.py")],
            env={**full, "RUNNER_RECORD": str(tmp_path / "dup-record")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert dup.returncode == 0
        assert not (tmp_path / "dup-record").exists()
        # wait until the single allowed child has finished its run
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not any(
            l.startswith("end") for l in record.read_text().splitlines()
        ):
            time.sleep(0.2)
    finally:
        stop_pool(p)
    lines = record.read_text().splitlines()
    first_end = next(i for i, l in enumerate(lines) if l.startswith("end"))
    assert sum(l.startswith("start") for l in lines[: first_end + 1]) == 1


def test_dirty_deployment_checkout_fails_safely(tmp_path):
    repo, full, record = setup_pool(tmp_path, [team_task("t1")])
    (repo / "operator-dirt.txt").write_text("hand edit\n")
    try:
        p = run_pool(repo, full, 3.5)
    finally:
        stop_pool(p)
    assert record.read_text() == ""


# ── Cycle.integrate gating (real runner code, stubbed API + throwaway repo) ──


@pytest.fixture()
def runner_mod(tmp_path):
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=f@x.invalid",
            "-c",
            "user.name=f",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    spec = importlib.util.spec_from_file_location(
        "mc_team_runner_under_test", ROOT / "scripts/mc-team-runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    mod.REPO = repo
    mod.WORKDIR = str(tmp_path / "work")
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return {}

    mod.api = fake_api
    mod.LOG = str(tmp_path / "runner.log")
    return mod, repo, calls


def att(stage, kind, status, no=1, sha=None):
    return {
        "stage_key": stage,
        "role_kind": kind,
        "status": status,
        "attempt_no": no,
        "round": 1,
        "head_sha": sha,
    }


def cycle_view(round=1, base_sha=None):
    return {"round": round, "base_sha": base_sha, "id": "c1", "version": 1}


def test_no_integration_while_any_round_attempt_is_queued_or_running(runner_mod):
    mod, repo, calls = runner_mod
    c = mod.Cycle("t", {})
    attempts = [
        att("pm", "pm", "succeeded"),
        att("dev-api", "developer", "queued"),
        att("dev-ops", "specialist", "succeeded"),
    ]
    assert c.integrate(cycle_view(), attempts) is False
    assert calls == []  # no integrate call, no empty integration


def test_no_integration_until_management_stages_finish(runner_mod):
    mod, repo, calls = runner_mod
    c = mod.Cycle("t", {})
    attempts = [att("pm", "pm", "running"), att("manage-scope", "pm", "succeeded")]
    assert c.integrate(cycle_view(), attempts) is False
    assert calls == []


def test_no_integration_when_a_leaf_has_not_succeeded(runner_mod):
    mod, repo, calls = runner_mod
    c = mod.Cycle("t", {})
    attempts = [att("dev-api", "developer", "failed"), att("dev-ops", "specialist", "succeeded")]
    assert c.integrate(cycle_view(), attempts) is False
    assert calls == []


def test_integration_uses_newest_attempt_of_each_stage(runner_mod):
    mod, repo, calls = runner_mod
    c = mod.Cycle("t", {})
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    attempts = [
        att("dev-api", "developer", "failed", no=1),  # stale retry clone
        att("dev-api", "developer", "succeeded", no=2, sha=head),
        att("dev-ops", "specialist", "succeeded", sha=head),
    ]
    assert c.integrate(cycle_view(base_sha=head), attempts) is True
    assert any(p[1].endswith("/integrate") and p[2]["integration_sha"] for p in calls)


def test_leaf_merge_conflict_records_stage_and_paths_before_abort(runner_mod):
    mod, repo, calls = runner_mod
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    heads = []
    for branch, content in (("leaf-a", "from a\\n"), ("leaf-b", "from b\\n")):
        wt = repo.parent / branch
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt), base], check=True, capture_output=True)
        (wt / "f.txt").write_text(content)
        subprocess.run(["git", "-C", str(wt), "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=f@x.invalid", "-c", "user.name=f", "commit", "-qm", branch], check=True)
        heads.append(subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"], text=True).strip())
    c = mod.Cycle("t", {})
    assert c.integrate(cycle_view(base_sha=base), [att("a", "developer", "succeeded", sha=heads[0]), att("b", "specialist", "succeeded", sha=heads[1])]) is True
    block = next(call for call in calls if call[1].endswith("/block"))
    assert "leaf-integration" in block[2]["reason"]
    assert "f.txt" in block[2]["reason"]
    assert "leaf-integration" in (Path(mod.LOG).read_text() if Path(mod.LOG).exists() else block[2]["reason"])


def test_task_lock_is_exclusive_for_the_lifecycle(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "mc_team_runner_locks", ROOT / "scripts/mc-team-runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    mod.LOCK_DIR = str(tmp_path)
    fd = mod.task_lock("task-1")
    assert fd is not None
    try:
        assert mod.task_lock("task-1") is None  # second runner is excluded
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert mod.task_lock("task-1") is not None  # released -> can be taken again


def test_split_parents_are_not_eligible():
    # A team task that already delegated to board children (e.g. a [backlog]
    # manager pass) must never run workers itself; its children execute.
    spec = importlib.util.spec_from_file_location(
        "mc_team_pool_eligible", ROOT / "scripts/mc-team-pool.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.eligible(team_task("split", split_count=3, split_type="dag")) is False
    assert mod.eligible(team_task("leaf")) is True
