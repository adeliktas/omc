"""Task execution_mode: team mode is opt-in, harness-only, and fenced against status changes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.api.deps import ActorContext
from app.core.time import utcnow
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.users import User
from app.schemas.tasks import TaskUpdate
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING


@pytest.fixture
async def world(monkeypatch):
    async def allow(*_a, **_k):
        return None

    monkeypatch.setattr(tasks_api, "_require_task_user_write_access", allow)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        org = Organization(id=uuid4(), name="o"); s.add(org)
        gw = Gateway(id=uuid4(), organization_id=org.id, name="g", url="https://g.local", workspace_root="/tmp/w"); s.add(gw)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id); s.add(board)
        task = Task(id=uuid4(), board_id=board.id, title="t", status="inbox", execution_lane="harness")
        s.add(task); await s.commit()
        yield s, board, task, ActorContext(actor_type="user", user=User(id=uuid4(), clerk_user_id="u"))
    await engine.dispose()


def test_check_team_mode_rules():
    tasks_api._check_team_mode("single", "agent", "/x", object())
    tasks_api._check_team_mode("team", "harness", None, None)
    for args in (("team", "agent", None, None), ("team", "harness", "/x", None), ("team", "harness", None, uuid4()), (None, "harness", None, None)):
        with pytest.raises(HTTPException) as exc:
            tasks_api._check_team_mode(*args)
        assert exc.value.status_code == 422


async def test_default_mode_is_single_and_team_can_be_enabled_on_inbox_harness_task(world):
    s, _board, task, actor = world
    assert task.execution_mode == "single"
    out = await tasks_api.update_task(TaskUpdate(execution_mode="team"), task=task, session=s, actor=actor)
    assert out.execution_mode == "team"


async def test_owner_can_convert_review_harness_without_resetting_usage_or_dispatch(world):
    s, _board, task, actor = world
    task.status, task.workspace_path, task.description = "review", "/original/worktree", "whole original goal"
    task.tokens_used, task.budget_exhausted_at = 501, utcnow()
    await s.commit()
    before = task.model_dump()
    out = await tasks_api.update_task(TaskUpdate(
        execution_mode="team", workspace_path=None,
        expected_status="review", expected_updated_at=task.updated_at,
    ), task=task, session=s, actor=actor)
    assert out.status == "review" and out.execution_mode == "team" and out.workspace_path is None
    for key in ("title", "description", "tokens_used", "budget_exhausted_at", "max_seconds_budget", "max_tokens_budget"):
        assert getattr(out, key) == before[key], key
    assert await svc.active_cycle(s, task.id) is None  # Conversion does not grant work or replay PM.


@pytest.mark.parametrize("refusal", ["stamp", "preconditions", "status", "history", "reverse", "agent"])
async def test_review_conversion_refuses_unowned_stale_running_or_historical_work(world, refusal):
    s, board, task, actor = world
    task.status = "in_progress" if refusal == "status" else "review"
    if refusal == "reverse":
        task.execution_mode = "team"
    await s.commit()
    if refusal == "history":
        s.add(svc.TeamCycle(board_id=board.id, task_id=task.id, state="blocked",
                            task_fingerprint=svc.task_fingerprint(task), deadline_at=utcnow()))
        await s.commit()
    stamp = task.updated_at
    payload = {"execution_mode": "single" if refusal == "reverse" else "team"}
    if refusal != "preconditions":
        payload.update(expected_status="review", expected_updated_at=stamp)
    if refusal == "stamp":
        payload["expected_updated_at"] = utcnow()
    if refusal == "agent":
        actor = ActorContext(actor_type="agent")
    with pytest.raises(HTTPException) as exc:
        await tasks_api.update_task(TaskUpdate(**payload), task=task, session=s, actor=actor)
    assert exc.value.status_code == 409
    await s.rollback()
    await s.refresh(task)
    assert task.updated_at == stamp


async def test_team_mode_refuses_agent_lane_workspace_null_and_running_tasks(world):
    s, _board, task, actor = world
    for payload in (TaskUpdate(execution_mode="team", workspace_path="/srv/site"), TaskUpdate(execution_mode="team", execution_lane="agent")):
        with pytest.raises(HTTPException) as exc:
            await tasks_api.update_task(payload, task=task, session=s, actor=actor)
        assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await tasks_api.update_task(TaskUpdate.model_validate({"execution_mode": None}), task=task, session=s, actor=actor)
    assert exc.value.status_code == 422
    task.status = "in_progress"
    await s.commit()
    with pytest.raises(HTTPException) as exc:
        await tasks_api.update_task(TaskUpdate(execution_mode="team"), task=task, session=s, actor=actor)
    assert exc.value.status_code == 409


async def test_moving_a_team_task_off_in_progress_cancels_its_cycle_and_fences_workers(world):
    s, board, task, actor = world
    task.execution_mode, task.status = "team", "in_progress"
    await s.commit()
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    a, token = await svc.claim_next(s, cycle, base_sha=BASE)  # type: ignore[misc]
    await s.commit()
    await tasks_api.update_task(TaskUpdate(status="blocked"), task=task, session=s, actor=actor)
    await s.refresh(cycle); await s.refresh(a)
    assert cycle.state == "cancelled" and a.status == "cancelled"
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(s, board, cycle, a, token, ok=True, artifact="{}")
    with pytest.raises(HTTPException):  # a blocked task is not in inbox: mode is locked
        await tasks_api.update_task(TaskUpdate(execution_mode="single"), task=task, session=s, actor=actor)
