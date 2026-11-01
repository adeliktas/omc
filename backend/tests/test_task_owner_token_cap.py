"""Existing owner PATCH changes only a validated total cap, never historical usage."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api import tasks as api
from app.api.deps import ActorContext
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.models.boards import Board
from app.models.organization_members import OrganizationMember
from app.models.task_dependencies import TaskDependency
from app.models.task_stage_timings import TaskStageTiming
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from app.services import task_event_bus, team_cycle
from app.schemas.tasks import TaskUpdate
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env, sidecar  # noqa: F401


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "120000", 2_147_483_648])  # null = cap disabled
def test_cap_rejects_invalid_values(bad):
    with pytest.raises(ValidationError):
        TaskUpdate(max_tokens_budget=bad)
    assert "max_tokens_budget" not in TaskUpdate().model_fields_set


@pytest.mark.parametrize("mode", ["single", "team"])
async def test_owner_cap_patch_preserves_history_and_requires_CAS(env, mode):
    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(env, status="blocked")
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        task.execution_mode, task.execution_lane = mode, "harness"
        task.tokens_used, task.max_tokens_budget, task.max_seconds_budget = 49962, 40000, 1200
        task.budget_exhausted_at = utcnow()
        await db.commit()
        before = task.model_dump()
        cycles = [x.model_dump() for x in (await db.exec(select(TeamCycle))).all()]
        attempts = [x.model_dump() for x in (await db.exec(select(TeamAttempt))).all()]
    url = f"/api/v1/boards/{task.board_id}/tasks/{task.id}"
    stamp = task.updated_at.isoformat()
    data = {"max_tokens_budget": 120000, "expected_status": "blocked", "expected_updated_at": stamp}
    async with _client(env) as client:
        for change, code in (
            ({"expected_status": "review"}, 409),
            ({"expected_updated_at": (task.updated_at - timedelta(seconds=1)).isoformat()}, 409),
            ({"expected_updated_at": None}, 422),
            ({"expected_status": None}, 422),
            ({"max_tokens_budget": 49961}, 409),
            ({"status": "inbox"}, 422),
        ):
            response = await client.patch(url, headers=AUTH, json={**data, **change})
            assert response.status_code == code, response.text
        accepted = await client.patch(url, headers=AUTH, json=data)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["max_tokens_budget"] == 120000
        assert accepted.json()["tokens_used"] == 49962
        stale = await client.patch(url, headers=AUTH, json={**data, "max_tokens_budget": 130000})
        assert stale.status_code == 409, stale.text
    async with env["maker"]() as db:
        after = (await db.get(Task, task.id)).model_dump()
        assert after["max_tokens_budget"] == 120000
        assert after["updated_at"] != before["updated_at"]
        for key in before.keys() - {"updated_at", "max_tokens_budget"}:
            assert after[key] == before[key], key
        assert [x.model_dump() for x in (await db.exec(select(TeamCycle))).all()] == cycles
        assert [x.model_dump() for x in (await db.exec(select(TeamAttempt))).all()] == attempts
        member = (await db.exec(select(OrganizationMember))).one()
        member.role = "admin"
        await db.commit()
    async with _client(env) as client:
        denied = await client.patch(
            url, headers=AUTH, json={**data, "expected_updated_at": after["updated_at"].isoformat()}
        )
        assert denied.status_code == 403, denied.text
        unauthorized = await client.patch(url, json=data)
        assert unauthorized.status_code == 401, unauthorized.text
        schema = (await client.get("/openapi.json")).json()["components"]["schemas"]["TaskUpdate"][
            "properties"
        ]["max_tokens_budget"]
        assert any(
            x.get("type") == "integer" and x.get("exclusiveMinimum") == 0 for x in schema["anyOf"]
        )


@pytest.mark.parametrize("lead", [False, True])
async def test_agent_callers_cannot_update_cap(monkeypatch, lead):
    task = Task(board_id=uuid4(), title="test", status="blocked", tokens_used=49962)
    db = SimpleNamespace(refresh=AsyncMock())
    actor = ActorContext(actor_type="agent", agent=SimpleNamespace(is_board_lead=lead))
    with pytest.raises(HTTPException) as exc:
        await api.update_task(TaskUpdate(max_tokens_budget=120000), task, db, actor)
    assert exc.value.status_code == 403
    assert task.tokens_used == 49962 and task.max_tokens_budget is None


async def _rows(db, model):
    return [row.model_dump() for row in (await db.exec(select(model).order_by(model.id))).all()]


@pytest.mark.parametrize("status", ["in_progress", "inbox", "paused", "cancelled"])
async def test_dependency_cap_patch_preserves_state_or_rejects_without_effects(env, status):
    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(env, status=status, with_cycle=False)
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        board = await db.get(Board, task.board_id)
        agent = Agent(board_id=board.id, gateway_id=board.gateway_id, name="assigned")
        dependency = Task(board_id=board.id, title="unfinished", status="inbox")
        db.add_all([agent, dependency])
        await db.flush()
        task.execution_lane = "agent"
        task.assigned_agent_id = agent.id
        task.in_progress_at = utcnow() - timedelta(minutes=3)
        task.previous_in_progress_at = utcnow() - timedelta(minutes=8)
        task.tokens_used, task.max_tokens_budget = 49962, 40000
        task.budget_exhausted_at = utcnow()
        db.add(TaskDependency(board_id=board.id, task_id=task.id, depends_on_task_id=dependency.id))
        db.add(TaskStageTiming(task_id=task.id, status=status, agent_id=agent.id))
        await db.commit()
        before = task.model_dump()
        stages = await _rows(db, TaskStageTiming)
        activities = await _rows(db, ActivityEvent)
        dependencies = await _rows(db, TaskDependency)
    async with _client(env) as client:
        response = await client.patch(
            f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
            headers=AUTH,
            json={
                "max_tokens_budget": 120000,
                "expected_status": status,
                "expected_updated_at": before["updated_at"].isoformat(),
            },
        )
    assert response.status_code == 409, response.text
    async with env["maker"]() as db:
        assert (await db.get(Task, task.id)).model_dump() == before
        assert await _rows(db, TaskStageTiming) == stages
        assert await _rows(db, ActivityEvent) == activities
        assert await _rows(db, TaskDependency) == dependencies


@pytest.mark.parametrize("failure", ["helper", "insert"])
@pytest.mark.parametrize("active", [False, True])
async def test_audit_failure_rolls_back_cap_comment_and_native_fencing(
    env, monkeypatch, failure, active
):
    env["app"].include_router(api.router, prefix="/api/v1")
    status = "in_progress" if active else "blocked"
    task = await _seed_review_task(env, status=status)
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        task.execution_lane, task.execution_mode = "harness", "team"
        task.tokens_used, task.max_tokens_budget = 49962, 40000
        task.budget_exhausted_at = utcnow()
        if active:
            cycle = (await db.exec(select(TeamCycle))).one()
            cycle.state, cycle.task_fingerprint = "developing", team_cycle.task_fingerprint(task)
            attempt = (
                await db.exec(select(TeamAttempt).where(TeamAttempt.role_kind == "developer"))
            ).one()
            attempt.status, attempt.claim_hash, attempt.total_tokens = (
                "running",
                "retained-claim",
                49962,
            )
        await db.commit()
        before = {
            model: await _rows(db, model)
            for model in [Task, ActivityEvent, TaskStageTiming, TeamCycle, TeamAttempt]
        }
    calls = []
    monkeypatch.setattr(task_event_bus, "notify_task_changed", lambda *args: calls.append("bus"))
    monkeypatch.setattr(api, "_notify_task_update_assignment_changes", AsyncMock())
    original = api._record_task_update_activity

    async def fail_after_audit(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic audit persistence failure")

    def fail_insert(mapper, connection, row):
        if row.task_id == task.id and row.event_type == "task.updated":
            raise RuntimeError("synthetic audit persistence failure")

    if failure == "helper":
        monkeypatch.setattr(api, "_record_task_update_activity", fail_after_audit)
    else:
        event.listen(ActivityEvent, "before_insert", fail_insert)
    try:
        async with _client(env) as client:
            with pytest.raises(RuntimeError, match="synthetic audit persistence failure"):
                await client.patch(
                    f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
                    headers=AUTH,
                    json={
                        "max_tokens_budget": 120000,
                        "expected_status": status,
                        "expected_updated_at": task.updated_at.isoformat(),
                        "comment": "cap audit test",
                    },
                )
    finally:
        if failure == "insert":
            event.remove(ActivityEvent, "before_insert", fail_insert)
    async with env["maker"]() as db:
        for model, rows in before.items():
            assert await _rows(db, model) == rows, model
    assert calls == []
    api._notify_task_update_assignment_changes.assert_not_awaited()


async def test_cap_audit_contains_old_new_and_notifications_follow_commit(env, monkeypatch):
    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(env, status="blocked")
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        task.tokens_used, task.max_tokens_budget = 49962, 40000
        await db.commit()
    calls = []
    monkeypatch.setattr(task_event_bus, "notify_task_changed", lambda *args: calls.append("bus"))

    async def notify(db, *, update):
        async with env["maker"]() as fresh:
            assert (await fresh.get(Task, task.id)).max_tokens_budget == 120000
            audit = (
                await fresh.exec(
                    select(ActivityEvent).where(ActivityEvent.event_type == "task.updated")
                )
            ).one()
            assert "40000 -> 120000" in audit.message and str(env["user_id"]) in audit.message
        calls.append("notification")

    def committed(session):
        calls.append("commit")

    monkeypatch.setattr(api, "_notify_task_update_assignment_changes", notify)
    event.listen(Session, "after_commit", committed)
    try:
        async with _client(env) as client:
            response = await client.patch(
                f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
                headers=AUTH,
                json={
                    "max_tokens_budget": 120000,
                    "expected_status": "blocked",
                    "expected_updated_at": task.updated_at.isoformat(),
                    "comment": "owner allowance",
                },
            )
        assert response.status_code == 200, response.text
    finally:
        event.remove(Session, "after_commit", committed)
    assert calls == ["commit", "bus", "notification"]


@pytest.mark.parametrize("state", ["developing", "publishing"])
async def test_cap_raise_keeps_the_running_cycle_and_restamps_it(env, state):
    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(env, status="in_progress")
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        task.execution_mode, task.execution_lane = "team", "harness"
        task.tokens_used, task.max_tokens_budget = 49962, 40000
        task.budget_exhausted_at = utcnow()
        cycle = (await db.exec(select(TeamCycle))).one()
        cycle.state, cycle.task_fingerprint = state, team_cycle.task_fingerprint(task)
        attempt = (
            await db.exec(select(TeamAttempt).where(TeamAttempt.role_kind == "developer"))
        ).one()
        attempt.status, attempt.claim_hash, attempt.total_tokens = "running", "claim", 49962
        await db.commit()
        before = task.model_dump()
        cycle_before, attempt_before = cycle.model_dump(), attempt.model_dump()
    async with _client(env) as client:
        response = await client.patch(
            f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
            headers=AUTH,
            json={
                "max_tokens_budget": 120000,
                "expected_status": "in_progress",
                "expected_updated_at": task.updated_at.isoformat(),
            },
        )
    # A cap raise is no new instruction (owner 2026-10-25, resume in place): the paid cycle
    # keeps running, publication included, and its fingerprint follows the new budget.
    assert response.status_code == 200, response.text
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        cycle = await db.get(TeamCycle, cycle_before["id"])
        attempt = await db.get(TeamAttempt, attempt_before["id"])
        assert task.max_tokens_budget == 120000
        for key in before.keys() - {"updated_at", "max_tokens_budget"}:
            assert getattr(task, key) == before[key], key
        assert cycle.state == state and attempt.status == "running"
        assert cycle.task_fingerprint == team_cycle.task_fingerprint(task) != cycle_before["task_fingerprint"]
