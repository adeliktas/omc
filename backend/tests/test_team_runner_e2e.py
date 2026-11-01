"""End to end: runner process -> real team API/service (SQLite) -> fake CLIs -> real Git.

No model, network or production service is involved. The fake `claude`/`pi` CLIs
act by the role contract in their prompt, so this proves the path
coordinator -> PM -> parallel developers -> independent QA -> reviewed publication,
and that a failing QA blocks publication.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import Body, FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import deps, team as team_api
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from tests.test_team_cycle import RANKING

ROOT = Path(__file__).resolve().parents[2]

CLI = r"""#!/usr/bin/env python3
import json, os, re, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if name == "claude" else args[-1]
denied = False
Path(os.environ["FIXTURE_LOG"]).open("a").write(f"{name} {os.getcwd()}\n")
if "You are the PLANNER" in prompt:
    plan = {"summary": "two parts", "qa_checks": ["files exist"], "workers": [
        {"key": "api", "role": "developer", "title": "api", "scope": ["backend"], "instructions": "add file"},
        {"key": "ops", "role": "specialist:devops-engineer", "title": "ops", "scope": ["docs"], "instructions": "add doc"}]}
    if os.environ.get("FIXTURE_VERIFY") == "1":  # evidence-only verification plan
        plan = {"summary": "verify only", "qa_checks": ["existing state"], "workers": [
            {"key": "api", "role": "developer", "mode": "verify", "title": "verify",
             "scope": ["backend"], "instructions": "confirm existing behavior, change nothing"}]}
    text = "plan:\n```json\n" + json.dumps(plan) + "\n```"
elif "You are a VERIFIER" in prompt:
    # verification-only developer: run checks, edit nothing, report evidence
    if os.environ.get("FIXTURE_VERIFY_EMPTY") == "1":
        text = ""
    else:
        assert Path('backend/seed.txt').read_text() == 'seed\n'
        assert Path('docs/seed.txt').read_text() == 'seed\n'
        text = ("verified exact-base read-only snapshot: executed seed-file existence/content assertions; "
                "both passed; requested fixture behavior already satisfied, no code change needed")
elif ("You are a DEVELOPER" in prompt or "SPECIALIST developer" in prompt) and (
        os.environ.get("FIXTURE_GIT_STATUS_FIRST") == "1"
        and subprocess.run(["git", "status", "--short"], capture_output=True).returncode
        and "Checkout status, supplied by the supervisor" not in prompt):
    # A worker obeying "Inspect `git status` before editing" stops when Git fails.
    text = "Blocked before implementation: git status failed (not a git repository)"
elif "You are a DEVELOPER" in prompt or "SPECIALIST developer" in prompt:
    scope = re.search(r"Scope: \['([^']+)'", prompt).group(1)
    Path(scope).mkdir(exist_ok=True)
    changed = Path(scope, f"{scope}-work.txt")
    changed.write_text((changed.read_text() if changed.exists() else "") + "work\n")
    if os.environ.get("FIXTURE_STRAY") == "1":
        Path("stray.txt").write_text("outside\n")
    if os.environ.get("FIXTURE_GIT_TAMPER") == "1":
        try: Path(".git").write_text("gitdir: /must-not-be-used\n")
        except OSError: denied = True  # sandbox denies the pointer rewrite
    text = "cannot rewrite Git metadata: read-only" if denied else "implemented"
elif "INDEPENDENT QA" in prompt:
    # No host Git database is exposed to the model; original SHA is host-prepared.
    meta = json.loads(Path(os.environ["TMPDIR"], "qa-evidence.json").read_text())
    head = meta["integration_sha"]
    if os.environ.get("FIXTURE_VERIFY") == "1":
        ok = (os.environ.get("FIXTURE_QA") != "fail"
              and "executed seed-file existence/content assertions" in prompt)
        detail = "no-change cycle: production state already satisfies the request"
    else:
        ok = (Path("backend/backend-work.txt").exists() and Path("docs/docs-work.txt").exists()
              and os.environ.get("FIXTURE_QA") != "fail")
        detail = f"files {'present' if ok else 'MISSING'}"
    text = f"checked {head[:8]}: {detail}\nCORRECTION_KIND: CODE\nVERDICT: {'PASS' if ok else 'FAIL'}"
    # Simulate the owner moving production while the first QA runs: exactly once.
    if os.environ.get("FIXTURE_MOVE_PRODUCTION") and ok:
        log = Path(os.environ["FIXTURE_LOG"])
        if "MOVE" not in log.read_text():
            log.open("a").write("MOVE\n")
            import json as _json, traceback, urllib.request
            try:
                urllib.request.urlopen(urllib.request.Request(
                    os.environ["FIXTURE_MOVE_URL"], data=_json.dumps({}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST"))
            except Exception:
                log.open("a").write("MOVEERR " + traceback.format_exc(limit=3).replace("\n", " | ") + "\n")
else:
    text = "??"
if name == "claude":
    print(json.dumps({"result": text, "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 5000}, "total_cost_usd": 0.5}))
else:
    print(json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": text}],
          "usage": {"input": 7, "output": 3, "cacheRead": 900, "cost": {"total": 0.01}}, "stopReason": "stop"}}))
if denied:
    sys.exit(3)  # the tamper was denied before any host Git could run
"""


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class FakeMC:
    """The real team router on SQLite, plus the few task routes the runner uses."""

    def __init__(self, tmp: Path) -> None:
        self.db = f"sqlite+aiosqlite:///{tmp}/mc.db"
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.ready = threading.Event()
        self.ids: dict[str, UUID] = {}

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True).start()
        assert self.ready.wait(20)

    async def _serve(self) -> None:
        engine = create_async_engine(self.db)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            org = Organization(id=uuid4(), name="o")
            s.add(org)
            gw = Gateway(
                id=uuid4(),
                organization_id=org.id,
                name="g",
                url="https://g.local",
                workspace_root="/tmp/w",
            )
            s.add(gw)
            board = Board(
                id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id
            )
            s.add(board)
            task = Task(
                id=uuid4(),
                board_id=board.id,
                title="audit",
                description="improve things",
                status="inbox",
                execution_lane="harness",
                execution_mode="team",
                complexity=3,
                max_seconds_budget=600,
            )
            s.add(task)
            await s.commit()
            self.ids = {"board": board.id, "task": task.id}
        app = FastAPI()

        # Exercise the actual refresh-integration API/service, not a shadowing mock.
        @app.post("/_test/move-production")
        async def move_production(body: dict = Body(...)):
            """MOCK of the owner committing to the production checkout."""
            repo = Path(self.repo)
            (repo / f"moved-{len(list(repo.glob('moved-*')))}.txt").write_text(
                "owner\n"
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.email=o@x.invalid",
                    "-c",
                    "user.name=owner",
                    "commit",
                    "-qm",
                    "owner moved production",
                ],
                check=True,
            )
            self.moved_sha = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            return {"sha": self.moved_sha}

        async def session_dep():
            async with AsyncSession(engine, expire_on_commit=False) as s:
                yield s

        async def board_dep(board_id: UUID, session: AsyncSession = deps.SESSION_DEP):
            return await session.get(Board, board_id)

        async def task_dep(task_id: UUID, session: AsyncSession = deps.SESSION_DEP):
            return await session.get(Task, task_id)

        app.dependency_overrides.update(
            {
                deps.get_session: session_dep,
                team_api.get_session: session_dep,
                team_api.get_board_for_user_write: board_dep,
                team_api.get_board_for_actor_read: board_dep,
                team_api.get_task_or_404: task_dep,
            }
        )
        team_api._ranking = lambda session: _ranking()  # type: ignore[assignment]
        app.include_router(team_api.router, prefix="/api/v1")

        def row(t: Task) -> dict:
            return {
                "id": str(t.id),
                "title": t.title,
                "status": t.status,
                "execution_lane": t.execution_lane,
                "execution_mode": t.execution_mode,
                "updated_at": t.updated_at.isoformat(),
                "complexity": 3,
                "repeat_enabled": t.repeat_enabled,
            }

        @app.get("/api/v1/boards/{board_id}/tasks")
        async def tasks(board_id: UUID):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return {"items": [row(t) for t in (await s.exec(select(Task)))]}

        @app.patch("/api/v1/boards/{board_id}/tasks/{task_id}")
        async def patch(board_id: UUID, task_id: UUID, body: dict = Body(...)):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                t = await s.get(Task, task_id)
                if body.get("expected_status") not in (None, t.status) or body.get(
                    "expected_updated_at"
                ) not in (None, t.updated_at.isoformat()):
                    from fastapi import HTTPException

                    raise HTTPException(409)
                t.status = body["status"]
                t.updated_at = utcnow()
                if t.status != "in_progress":  # same hook as the real PATCH
                    from app.services import team_cycle

                    c = await team_cycle.active_cycle(s, t.id)
                    if c:
                        await team_cycle.cancel_cycle(s, c, "moved")
                await s.commit()
                return row(t)

        @app.post("/api/v1/boards/{board_id}/tasks/{task_id}/comments")
        async def comment(board_id: UUID, task_id: UUID, body: dict = Body(...)):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                s.add(
                    ActivityEvent(
                        event_type="task.comment",
                        message=body["message"],
                        task_id=task_id,
                        board_id=board_id,
                    )
                )
                await s.commit()
                return {}

        @app.post("/api/v1/ranking/usage")
        async def usage(body: dict = Body(...)):
            return {"ok": True}

        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self.server = server
        threading.Timer(0.5, self.ready.set).start()
        await server.serve()
        await engine.dispose()

    def query(self, fn):
        async def go():
            engine = create_async_engine(self.db)
            async with AsyncSession(engine) as s:
                return await fn(s)

        return asyncio.run(go())


async def _ranking():
    return RANKING


def setup_world(tmp_path: Path, **env):
    repo = tmp_path / "production"
    (repo / "scripts").mkdir(parents=True)
    src = (
        (ROOT / "scripts/mc-claude-runner.sh")
        .read_text()
        .replace('PATH="$HOME/.local/bin:$PATH"', ":")
    )
    (repo / "scripts/mc-claude-runner.sh").write_text(
        src.replace('LOCK="${OMC_DATA:-}/run/mc-claude-runner.lock"', f"LOCK={tmp_path}/lock")
    )
    for f in ("mc", "mc-agent-result.py", "mc-team-runner.py", "mc_pi_runtime.py", "mc_skills.py", "mc_task_budget.py", "omc_paths.py"):
        shutil.copy(ROOT / "scripts" / f, repo / "scripts" / f)
    # The real runner pins managed Pi; this isolated copy must use the fixture CLI,
    # not a host SDK or an inference service. Production argv is tested separately.
    fixture_runner = repo / "scripts/mc-team-runner.py"
    runner_source = fixture_runner.read_text()
    managed_pi = 'str(CANONICAL_PI / "bin/pi")'
    assert runner_source.count(managed_pi) == 1
    fixture_runner.write_text(runner_source.replace(managed_pi, repr(str(tmp_path / "bin/pi"))))
    (repo / "scripts/mc-deploy.sh").write_text(
        f'#!/bin/sh\necho "$1" >> {tmp_path}/deploys\n'
    )
    (repo / "scripts/mc-deploy.sh").chmod(0o755)
    (repo / "backend/app/core").mkdir(parents=True)
    for f in ("evidence_redaction.py", "evidence_attachment.py"):  # runner imports both
        shutil.copy(ROOT / "backend/app/core" / f, repo / "backend/app/core" / f)
    (repo / "backend/seed.txt").write_text("seed\n")
    (repo / "docs").mkdir()
    (repo / "docs/seed.txt").write_text("seed\n")
    subprocess.check_call(["git", "init", "-q", "-b", "production", str(repo)])
    for k, v in (
        ("user.name", "Fixture"),
        ("user.email", "f@x.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(repo, "config", k, v)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    bins = tmp_path / "bin"
    bins.mkdir()
    for n in ("claude", "pi"):
        (bins / n).write_text(CLI)
        (bins / n).chmod(0o755)
    mc = FakeMC(tmp_path)
    mc.repo = repo
    mc.start()
    full = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "MC_URL": f"http://127.0.0.1:{mc.port}",
        "MC_TOKEN": "x",
        "RUNNER_BOARD": str(mc.ids["board"]),
        "RUNNER_APPLY": "1",
        "RUNNER_LOG": str(tmp_path / "log"),
        "RUNNER_WORKDIR": str(tmp_path / "work"),
        "RUNNER_PONYTAIL_SKILL": "/nonexistent",
        "RUNNER_LOCK_DIR": str(tmp_path / "run"),
        "RUNNER_GLOBAL_LOCK": str(tmp_path / "global.lock"),
        "FIXTURE_LOG": str(tmp_path / "cli.log"),
        "RUNNER_TEAM_DEPLOY": "1",
        **env,
    }
    Path(tmp_path / "run").mkdir()
    if "FIXTURE_MOVE_PRODUCTION" in full:
        full["FIXTURE_MOVE_URL"] = f"http://127.0.0.1:{mc.port}/_test/move-production"
    return repo, mc, full


def tick(repo, full):
    return subprocess.run(
        ["bash", str(repo / "scripts/mc-claude-runner.sh")],
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def snapshot(mc):
    async def fn(s):
        t = (await s.exec(select(Task))).one()
        cycles = list(await s.exec(select(TeamCycle)))
        atts = list(await s.exec(select(TeamAttempt)))
        notes = [e.message for e in await s.exec(select(ActivityEvent))]
        return t, cycles, atts, notes

    return mc.query(fn)


def test_team_cycle_publishes_only_independently_qad_commit_and_sends_task_to_review(
    tmp_path,
):
    repo, mc, env = setup_world(tmp_path)
    before = git(repo, "rev-parse", "HEAD")
    p = tick(repo, env)
    log = (tmp_path / "log").read_text()
    task, cycles, atts, notes = snapshot(mc)
    assert task.status == "review", (
        p.stderr,
        log,
        [a.error for a in atts],
    )  # never done
    cycle = cycles[0]
    assert (
        cycle.state == "published"
        and cycle.published_sha == cycle.reviewed_sha == git(repo, "rev-parse", "HEAD")
    )
    assert (
        git(repo, "rev-parse", "HEAD") != before
        and git(repo, "status", "--porcelain") == ""
    )
    assert (repo / "backend/backend-work.txt").exists() and (
        repo / "docs/docs-work.txt"
    ).exists()
    # roles, identities and handoffs are persisted and visible
    by_stage = {a.stage_key: a for a in atts}
    assert set(by_stage) == {"pm", "dev-api", "dev-ops", "qa"}
    assert all(a.status == "succeeded" for a in atts)
    assert by_stage["qa"].member_id not in {
        by_stage["dev-api"].member_id,
        by_stage["dev-ops"].member_id,
        by_stage["pm"].member_id,
    }
    assert (by_stage["qa"].provider, by_stage["qa"].model) != (
        by_stage["dev-api"].provider,
        by_stage["dev-api"].model,
    )
    assert (
        by_stage["qa"].head_sha == cycle.integration_sha
        and "VERDICT: PASS" in by_stage["qa"].artifact
    )
    assert (
        by_stage["pm"].cache_read_tokens == 5000
        and by_stage["qa"].cache_read_tokens == 900
    )
    assert by_stage["qa"].cli_cost_estimate_usd == 0.01
    assert "mc-team/team-developer" in git(repo, "log", "--format=%an")
    text = "\n".join(notes)
    assert (
        "team-coordinator" in text
        and "team/team-qa" in text
        and "team/team-specialist-devops-engineer" in text
    )
    assert (tmp_path / "deploys").read_text().split() == ["backend", "webhook-worker"]
    # each worker ran in its own directory; the production checkout was never a worker cwd
    cwds = {
        line.split(" ", 1)[1].strip()
        for line in (tmp_path / "cli.log").read_text().splitlines()
    }
    assert str(repo) not in cwds and len(cwds) >= 4


def test_production_move_between_qa_and_publish_forces_a_second_independent_qa(
    tmp_path,
):
    """The owner commits to production while QA runs. The runner must NOT
    publish the stale reviewed commit: it merges onto current HEAD in an
    isolated checkout and a SECOND independent QA must pass on the new exact
    commit before the fast-forward publication."""
    repo, mc, env = setup_world(tmp_path, FIXTURE_MOVE_PRODUCTION="1")
    before = git(repo, "rev-parse", "HEAD")
    p = tick(repo, env)
    log = (tmp_path / "log").read_text()
    task, cycles, atts, notes = snapshot(mc)
    assert task.status == "review", (
        p.stderr,
        log,
        [(a.stage_key, a.error) for a in atts],
    )
    cycle = cycles[0]
    final = git(repo, "rev-parse", "HEAD")
    assert final != before
    qas = [a for a in atts if a.stage_key == "qa"]
    assert len(qas) == 2, [a.stage_key for a in atts]
    second = max(qas, key=lambda a: a.attempt_no)
    # the second QA reviewed the merge commit whose parent is the moved production HEAD
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                mc.moved_sha,
                second.head_sha,
            ]
        ).returncode
        == 0
    )
    assert second.head_sha == cycle.reviewed_sha == cycle.published_sha == final
    assert cycle.integration_sha == final  # refreshed integration IS what shipped
    text = "\n".join(notes)
    assert "SECOND independent QA" in text
    # production was fast-forwarded to the re-reviewed merge, then deployed
    assert (repo / "backend/backend-work.txt").exists()
    assert (repo / "moved-0.txt").exists()
    assert (tmp_path / "deploys").read_text().split() == ["backend", "webhook-worker"]


def test_failing_independent_qa_blocks_publication_and_task_never_reaches_review_or_done(
    tmp_path,
):
    repo, mc, env = setup_world(tmp_path, FIXTURE_QA="fail")
    before = git(repo, "rev-parse", "HEAD")
    tick(repo, env)
    task, cycles, atts, notes = snapshot(mc)
    assert task.status == "blocked"
    assert (
        cycles[0].state == "blocked"
        and cycles[0].published_sha is None
        and cycles[0].round == 2
    )
    assert (
        git(repo, "rev-parse", "HEAD") == before
        and not (repo / "backend/backend-work.txt").exists()
    )
    assert sum(a.stage_key == "qa" and a.verdict == "fail" for a in atts) == 2
    assert not (tmp_path / "deploys").exists()
    assert any("DECISION NEEDED" in n for n in notes)


def test_out_of_scope_edit_is_committed_and_reported_not_failed(tmp_path):
    repo, mc, env = setup_world(tmp_path, FIXTURE_STRAY="1")
    tick(repo, env)
    task, cycles, atts, _ = snapshot(mc)
    assert task.status == "review"
    cycle = cycles[0]
    assert cycle.state == "published"
    assert cycle.published_sha == cycle.reviewed_sha == git(repo, "rev-parse", "HEAD")
    assert not any("outside assigned scope" in (a.error or "") for a in atts)
    devs = [a for a in atts if a.role_kind in ("developer", "specialist")]
    assert devs and all(a.status == "succeeded" for a in devs)
    assert any("Also edited outside the assigned scope" in (a.artifact or "") for a in devs)
    assert (repo / "stray.txt").read_text() == "outside\n"
    assert "stray.txt" in git(repo, "diff", "--name-only", cycle.base_sha, cycle.reviewed_sha).splitlines()
    qa = next(a for a in atts if a.role_kind == "qa")
    assert qa.status == "succeeded" and qa.verdict == "pass"
    assert qa.head_sha == cycle.reviewed_sha


def test_dirty_production_checkout_leaves_team_task_unclaimed(tmp_path):
    repo, mc, env = setup_world(tmp_path)
    (repo / "operator-dirt.txt").write_text("hand edit\n")
    tick(repo, env)
    task, cycles, _, _ = snapshot(mc)
    assert task.status == "inbox" and cycles == []
    assert (repo / "operator-dirt.txt").exists()


def test_team_task_is_untouched_without_apply_and_runner_resumes_a_crashed_cycle(
    tmp_path,
):
    repo, mc, env = setup_world(tmp_path, RUNNER_APPLY="0")
    tick(repo, env)
    assert snapshot(mc)[0].status == "inbox"
    # Simulate a crashed runner: claimed cycle with a running PM attempt holding a dead token.
    env["RUNNER_APPLY"] = "1"
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.time import utcnow
    from app.services import team_cycle as svc

    async def crash(s):
        t = (await s.exec(select(Task))).one()
        t.status = "in_progress"
        b = await s.get(Board, t.board_id)
        c, _ = await svc.start_cycle(s, b, t, RANKING)
        a, _ = await svc.claim_next(s, c, base_sha=git(repo, "rev-parse", "HEAD"))
        # The runner no longer calls recover on resume (sibling pool workers may
        # hold live leases); recovery happens by actual lease expiry, so make
        # the crashed attempt's lease genuinely expired.
        a.lease_expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()

    mc.query(crash)
    tick(repo, env)
    task, cycles, atts, _ = snapshot(mc)
    assert task.status == "review" and cycles[0].state == "published"
    assert sorted(a.status for a in atts if a.stage_key == "pm") == [
        "expired",
        "succeeded",
    ]


def test_all_no_change_verify_cycle_reaches_review_with_no_deployment(tmp_path):
    """Evidence-only verification: the plan opts the developer into mode=verify.

    The verifier runs in a genuinely read-only worktree at the exact base,
    changes nothing, and the cycle still gets independent exact-SHA QA, then
    publishes the unchanged current SHA under the publication lock: task to
    review, Git HEAD unchanged, no deployment diff, honest no-change comment.
    """
    repo, mc, env = setup_world(tmp_path, FIXTURE_VERIFY="1")
    before = git(repo, "rev-parse", "HEAD")
    p = tick(repo, env)
    log = (tmp_path / "log").read_text()
    task, cycles, atts, notes = snapshot(mc)
    assert task.status == "review", (p.stderr, log, [(a.stage_key, a.error) for a in atts])
    cycle = cycles[0]
    final = git(repo, "rev-parse", "HEAD")
    assert final == before and git(repo, "status", "--porcelain") == ""
    assert (
        cycle.state == "published"
        and cycle.published_sha == cycle.reviewed_sha == cycle.integration_sha == final
    )
    by_stage = {a.stage_key: a for a in atts}
    assert set(by_stage) == {"pm", "dev-api", "qa"}
    dev = by_stage["dev-api"]
    assert (
        dev.status == "succeeded"
        and dev.head_sha == final
        and json.loads(dev.changed_files) == []
        and "no code change needed" in (dev.artifact or "")
    )
    qa = by_stage["qa"]
    assert (
        qa.status == "succeeded"
        and qa.verdict == "pass"
        and qa.head_sha == final
        and qa.member_id != dev.member_id
    )
    assert not (tmp_path / "deploys").exists()
    text = "\n".join(notes)
    assert "no code change was published" in text and "NO code change" in text
    # the verifier actually ran in a read-only team-verify worktree, never a
    # dev branch worktree and never the production checkout itself
    cwds = {
        line.split(" ", 1)[1].strip() for line in (tmp_path / "cli.log").read_text().splitlines()
    }
    assert str(repo) not in cwds
    assert any("team-verify." in c for c in cwds)
    assert not any("team-dev." in c for c in cwds)


def test_failed_verification_blocks_and_publishes_nothing(tmp_path):
    """A verifier that produces no substantive evidence fails; after retries the
    cycle is blocked and nothing is published or deployed."""
    repo, mc, env = setup_world(tmp_path, FIXTURE_VERIFY="1", FIXTURE_VERIFY_EMPTY="1")
    before = git(repo, "rev-parse", "HEAD")
    tick(repo, env)
    task, cycles, atts, _ = snapshot(mc)
    assert task.status == "blocked" and cycles[0].state == "blocked"
    assert cycles[0].published_sha is None
    assert git(repo, "rev-parse", "HEAD") == before
    devs = [a for a in atts if a.stage_key == "dev-api"]
    assert len(devs) == 2 and all(a.status == "failed" for a in devs)
    assert any(a.error for a in devs)
    assert not (tmp_path / "deploys").exists()
