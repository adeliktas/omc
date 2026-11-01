"""One leaf per cycle (2026-10-26): a PM plan of 2+ workers becomes child tasks.

In one cycle, 2 leaves published 3 of 39 times and 3+ leaves 0 of 61, so each worker
now gets its own task, cycle, QA and pull request; the parent delegates.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.services import team_cycle as svc
from tests.test_team_backlog import BASE, RANKING

PLAN = {"summary": "two halves", "qa_checks": ["go test ./..."], "workers": [
    {"key": "api", "title": "API half", "scope": ["internal/api"], "instructions": "do api\nGitHub issues: #7", "complexity": 2},
    {"key": "docs", "title": "Docs half", "scope": ["docs"], "instructions": "do docs", "complexity": 1,
     "mode": "verify"},
]}


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setattr(svc, "SPLIT_MIN_WORKERS", 2)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(id=uuid4(), name="o")
        gw = Gateway(id=uuid4(), organization_id=org.id, name="g", url="https://g.local", workspace_root="/tmp/w")
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id)
        task = Task(id=uuid4(), board_id=board.id, title="Ship it", status="in_progress",
                    description="Goal.\nGitHub issues: #7\n\n## Re-scope (fleet)\n\nsplit it",
                    execution_lane="harness", execution_mode="team", complexity=4,
                    harness_provider="claude-code", harness_model="opus-5", max_seconds_budget=1800)
        session.add_all([org, gw, board, task])
        await session.commit()
        yield session, board, task
    await engine.dispose()


async def plan(session, board, task, p=PLAN):
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, token = await svc.claim_next(session, cycle, base_sha=BASE)
    await session.commit()
    await svc.complete_attempt(session, board, cycle, pm, token, ok=True,
                               artifact="```json\n" + json.dumps(p) + "\n```", ranking=RANKING)
    await session.commit()
    return cycle


async def children(session, task):
    return list(await session.exec(select(Task).where(Task.parent_task_id == task.id).order_by(Task.split_position)))


async def test_multi_worker_plan_becomes_child_tasks(env):
    session, board, task = env
    cycle = await plan(session, board, task)

    assert cycle.state == "delegated" and task.status == "in_progress" and task.split_count == 2
    assert {a.stage_key for a in await svc._attempts(session, cycle.id)} == {"pm"}  # no in-cycle leaves
    api, docs = await children(session, task)
    assert (api.title, api.complexity, api.status, api.execution_mode) == ("API half", 2, "inbox", "team")
    assert "do api" in api.description and "Scope: internal/api" in api.description
    assert "Mode: verify" in docs.description and "Mode: verify" not in api.description
    # The parent's issues and replan note stay with the parent.
    assert "Parent GitHub issues: #7" in api.description and "\nGitHub issues:" not in api.description
    assert "Re-scope" not in api.description
    # Each child runs its own (unsplit, single-leaf) cycle under the delegated parent.
    api.status = "in_progress"
    await session.commit()
    child_cycle = await plan(session, board, api, {**PLAN, "workers": PLAN["workers"][:1]})
    assert child_cycle.state == "developing" and api.split_count == 0


async def test_single_worker_plan_runs_in_cycle(env):
    session, board, task = env
    cycle = await plan(session, board, task, {**PLAN, "workers": PLAN["workers"][:1]})
    assert cycle.state == "developing" and task.split_count == 0


async def test_split_stops_at_the_ancestry_bound(env):
    # At the ancestry limit, multiple writers cannot split and must not run together.
    session, board, task = env
    node = task
    for _ in range(svc.MAX_BACKLOG_ANCESTRY):
        parent = Task(id=uuid4(), board_id=board.id, title="p", status="in_progress", execution_mode="team")
        session.add(parent)
        node.parent_task_id, node = parent.id, parent
    await session.commit()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, token = await svc.claim_next(session, cycle, base_sha=BASE)
    member = await session.get(svc.TeamMember, pm.member_id)
    assert "exactly one worker" in svc.brief(task, cycle, pm, member, None)
    # A PM already running before deployment has no new assignment flag.
    pm.assignment = json.dumps({k: v for k, v in json.loads(pm.assignment).items()
                                if k != "single_worker"})
    await session.commit()
    await svc.complete_attempt(session, board, cycle, pm, token, ok=True,
                               artifact=json.dumps(PLAN), ranking=RANKING)
    assert pm.status == "failed" and "exactly one worker" in pm.error
    assert not [a for a in await svc._attempts(session, cycle.id)
                if a.role_kind in ("developer", "specialist")]
    pm, token = await svc.claim_next(session, cycle, base_sha=BASE)
    await session.commit()
    await svc.complete_attempt(session, board, cycle, pm, token, ok=True,
                               artifact=json.dumps({**PLAN, "workers": PLAN["workers"][:1]}),
                               ranking=RANKING)
    assert cycle.state == "developing" and task.split_count == 0
    assert len([a for a in await svc._attempts(session, cycle.id)
                if a.role_kind == "developer"]) == 1


async def test_reopen_replans_legacy_multi_writer_cycle_at_split_limit(env):
    session, board, task = env
    node = task
    for _ in range(svc.MAX_BACKLOG_ANCESTRY):
        parent = Task(id=uuid4(), board_id=board.id, title="p", status="in_progress", execution_mode="team")
        session.add(parent)
        node.parent_task_id, node = parent.id, parent
    await session.commit()
    old, _ = await svc.start_cycle(session, board, task, RANKING)
    old.plan = json.dumps(PLAN)  # pre-deployment plan, valid then but unsafe to resume
    await svc._block(session, old, "QA failed in round 2/2")
    task.status = "in_progress"
    await session.commit()
    cycle, resumed = await svc.start_cycle(session, board, task, RANKING)
    assert not resumed and cycle.id != old.id and cycle.state == "planning"


def test_pm_brief_says_workers_become_tasks():
    board = uuid4()
    task = Task(id=uuid4(), board_id=board, title="t", execution_mode="team")
    cycle = svc.TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = svc.TeamMember(id=uuid4(), board_id=board, name="pm", role_kind="pm", provider="p", model="m")
    a = svc.TeamAttempt(id=uuid4(), board_id=board, task_id=task.id, cycle_id=cycle.id, member_id=member.id,
                        role_kind="pm", stage_key="pm")
    assert "split into separate board tasks" in svc.brief(task, cycle, a, member, None)
