"""Run the actual sandbox/cancellation path and preserve round-two work."""

import importlib.util
import json
import uuid
from copy import deepcopy
from datetime import timedelta
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.time import utcnow
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, INTEGRATED, RANKING, claim, env, to_qa


def runner():
    path = Path(__file__).resolve().parents[2] / "scripts/mc-team-runner.py"
    spec = importlib.util.spec_from_file_location("team_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_os_sandbox_keeps_pm_and_qa_read_only_even_inside_writable_attempt_tmp(
    tmp_path, monkeypatch
):
    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap not installed")
    r = runner()
    repo = tmp_path / "production"
    repo.mkdir()
    (repo / ".env").write_text("PRIVATE_OPERATOR_SECRET=fake\n")
    monkeypatch.setattr(r, "REPO", repo)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    snapshot = attempt / "snapshot"
    snapshot.mkdir()
    # The sandbox only mounts what it is told to; directories it merely creates
    # on the way to a mount point are private sandbox space, so the forbidden
    # sentinel must live in a directory no mount path references.
    outside_dir = tmp_path.parent / f"mc-outside-{uuid.uuid4().hex[:8]}"
    outside_dir.mkdir()
    outside = outside_dir / "outside.txt"
    check = f"""from pathlib import Path
try: assert Path({str(repo / '.env')!r}).read_text() == ''
except OSError: pass
for path in [Path('forbidden.txt'), Path({str(outside)!r})]:
 try: path.write_text('bad')
 except OSError: pass
 else: raise AssertionError('sandbox allowed forbidden write')
Path({str(attempt / 'allowed.txt')!r}).write_text('ok')
"""
    subprocess.run(
        r.sandbox_command([sys.executable, "-c", check], snapshot, attempt, writable=False),
        check=True,
    )
    assert not (snapshot / "forbidden.txt").exists() and not outside.exists()
    assert (attempt / "allowed.txt").read_text() == "ok"
    subprocess.run(
        r.sandbox_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('change.txt').write_text('ok')",
            ],
            snapshot,
            attempt,
            writable=True,
        ),
        check=True,
    )
    assert (snapshot / "change.txt").exists()


def test_native_auth_lock_is_shared_without_writable_host_or_lg(tmp_path, monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap not installed")
    r = runner()
    accounts = tmp_path / "accounts"
    selected = accounts / "openai-codex-2"
    selected.mkdir(parents=True)
    auth = selected / "auth.json"
    auth.write_text("{}")
    models = selected / "models-store.json"
    models.write_text("{}")
    monkeypatch.setattr(r, "NATIVE_ACCOUNTS", accounts)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    check = f"""from pathlib import Path
root = Path({str(selected)!r})
(root / 'auth.json.lock').mkdir()
(root / 'auth.json').write_text('{{"rotated":"fake"}}')
for path in (root / 'models-store.json', Path('forbidden.txt')):
 try: path.write_text('bad')
 except OSError: pass
 else: raise AssertionError('unexpected writable path')
"""
    subprocess.run(
        r.sandbox_command(
            [sys.executable, "-c", check],
            snapshot,
            scratch,
            writable=False,
            native_account=selected,
        ),
        check=True,
    )
    assert (selected / "auth.json.lock").is_dir()  # real lock visible outside sandbox
    assert json.loads(auth.read_text()) == {"rotated": "fake"}
    assert models.read_text() == "{}"
    for forbidden in (accounts / "openai-codex-1", tmp_path / "agent"):
        with pytest.raises(RuntimeError, match="LG credential"):
            r.sandbox_command(["true"], snapshot, scratch, writable=False, native_account=forbidden)


def test_missing_sandbox_fails_closed(tmp_path, monkeypatch):
    r = runner()
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="refusing an unsandboxed"):
        r.sandbox_command(["true"], tmp_path, tmp_path, writable=False)


def test_cancellation_kills_worker_descendants_and_hides_operator_environment(
    tmp_path, monkeypatch
):
    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap not installed")
    r = runner()
    bins = tmp_path / "bin"
    bins.mkdir()
    fake = bins / "claude"
    child = (
        "import os,time; from pathlib import Path; p=Path(os.environ['TMPDIR'])/'ticks'; "
        'exec(\'while True:\\n p.open("a").write("x")\\n time.sleep(0.03)\')'
    )
    fake.write_text(
        "#!" + sys.executable + "\nimport os,subprocess,time,sys\n"
        "assert not os.environ.get('MC_TOKEN')\nassert not os.environ.get('DATABASE_URL')\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(60)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bins) + ":" + os.environ["PATH"])
    monkeypatch.setenv("MC_TOKEN", "private-test-only")
    monkeypatch.setenv("DATABASE_URL", "private-test-only")
    monkeypatch.setattr(r, "api", lambda *args: {})
    cwd = tmp_path / "qa"
    cwd.mkdir()
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    cancelled = threading.Event()
    started = time.monotonic()
    timer = threading.Timer(0.4, cancelled.set)
    timer.start()
    result = r.run_harness(
        {
            "role_kind": "qa",
            "attempt": {
                "id": "abc",
                "provider": "claude-code",
                "model": "sonnet",
                "thinking_level": "low",
            },
        },
        "test",
        cwd,
        attempt,
        60,
        cancelled,
    )
    timer.join()
    assert time.monotonic() - started < 4
    assert not result["success"]
    assert (attempt / "ticks").exists()  # a real descendant did start
    size = (attempt / "ticks").stat().st_size
    time.sleep(0.15)
    assert (attempt / "ticks").stat().st_size == size


async def test_legacy_repeat_scheduler_cannot_rearm_stopped_team_cycles(env, monkeypatch):
    from app.db import session as db_module
    from app.services.watchdog_loop import _process_interval_repeats

    s, board, task = env
    board.cron_tasks_enabled = True
    task.repeat_enabled = True
    task.repeat_interval_minutes = 1
    monkeypatch.setattr(db_module, "async_session_maker", lambda: s)
    for status in ("blocked", "review", "done"):
        task.status = status
        task.updated_at = utcnow() - timedelta(hours=1)
        await s.commit()
        assert await _process_interval_repeats() == 0


async def test_permanent_refresh_failure_blocks_without_replaying_credentials(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        attempt,
        token,
        ok=False,
        error="OAuth 401 refresh_token_reused",
    )
    assert cycle.state == "blocked"
    assert len(await svc._attempts(s, cycle.id)) == 1


async def test_changed_task_instructions_fence_existing_claim(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    task.description = "owner changed the requested outcome"
    await s.commit()
    with pytest.raises(svc.TeamFenced):
        await svc.heartbeat(s, cycle, attempt, token)
    assert cycle.state == "cancelled" and attempt.status == "cancelled"


async def test_qa_fix_round_starts_from_failed_integration_not_old_production(env):
    s, board, task = env
    cycle, _ = await to_qa(s, board, task)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="real failure evidence\nCORRECTION_KIND: CODE",
        verdict="fail",
        head_sha=INTEGRATED,
    )
    await s.commit()
    developer, _ = await claim(s, cycle)
    assert cycle.base_sha == BASE
    assert developer.base_sha == INTEGRATED
    assert json.loads(developer.assignment)["feedback"] == "real failure evidence\nCORRECTION_KIND: CODE"


async def test_dispatch_rechecks_availability_and_reassigns_only_unpinned_roles(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    held = deepcopy(RANKING)
    held["levels"][0]["entries"][0]["unavailable_until"] = (
        utcnow() + timedelta(hours=1)
    ).isoformat()
    attempt, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    assert (attempt.provider, attempt.model) == ("pi-neuralwatt", "glm-5.3-flash")


async def test_bounded_specialist_inherits_only_board_approved_credits_engine(env):
    s, board, task = env
    paid = deepcopy(RANKING)
    for e in paid["levels"][0]["entries"]:
        e["cost_class"] = "credits"
    for role in ("pm", "developer", "qa"):
        engine = ("deepseek-sub", "deepseek-flash") if role == "qa" else ("claude-code", "sonnet")
        await svc.recruit(s, board, role, paid, engine=engine, created_by="admin")
    cycle, _ = await svc.start_cycle(s, board, task, paid)
    pm, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=paid)
    plan = {
        "workers": [
            {
                "key": "security",
                "role": "specialist:security-architect",
                "description": "review",
                "scope": ["only.txt"],
            }
        ],
        "qa_criteria": "check file",
    }
    await svc.complete_attempt(
        s, board, cycle, pm, token, ok=True, artifact=json.dumps(plan), ranking=paid
    )
    specialist, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=paid)
    assert specialist.role_kind == "specialist"
    assert (specialist.provider, specialist.model) == ("claude-code", "sonnet")


def test_blocked_tasks_are_a_supported_status_filter():
    from app.api.tasks import _status_values

    assert _status_values("blocked,inbox") == ["blocked", "inbox"]
