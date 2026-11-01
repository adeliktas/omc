"""Owner cancellation retains evidence; resumption is explicit, never deletion."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.api import tasks as api
from app.api.deps import ActorContext
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from app.schemas.tasks import TaskUpdate
from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env  # noqa: F401 - existing disposable HTTP fixture
from tests.test_autonomous_review import sidecar  # noqa: F401 - existing isolated sidecar
from tests.test_team_cycle import RANKING
from tests.test_team_cycle import env as native_env  # noqa: F401


@pytest.mark.parametrize("status", ["inbox", "in_progress", "review", "blocked", "cancelled"])
async def test_owner_cancel_preserves_history_and_dependency_hold(env, status):
    from fastapi_pagination import add_pagination

    env["app"].include_router(api.router, prefix="/api/v1")
    add_pagination(env["app"])
    task = await _seed_review_task(env, status=status)
    dependency = await _seed_review_task(env, status="blocked", with_cycle=False)
    stamp = utcnow()
    async with env["maker"]() as db:
        stored = await db.get(Task, task.id)
        stored.in_progress_at = stamp
        stored.repeat_enabled = True
        stored.repeat_interval_minutes = 60
        stored.tokens_used = 500
        stored.max_tokens_budget = 1
        stored.budget_exhausted_at = stamp
        board = await db.get(Board, stored.board_id)
        board.block_status_changes_with_pending_approval = True
        approval = Approval(
            board_id=stored.board_id,
            task_id=stored.id,
            status="pending",
            action_type="owner_decision",
            confidence=0,
            payload={"keep": "pending"},
        )
        old = ActivityEvent(
            task_id=stored.id,
            board_id=stored.board_id,
            event_type="task.comment",
            message="original FAIL evidence",
        )
        db.add(approval)
        db.add(old)
        await api.replace_task_dependencies(
            db, board_id=stored.board_id, task_id=stored.id, depends_on_task_ids=[dependency.id]
        )
        await db.commit()
        version = stored.updated_at.isoformat()
        history = [
            (a.id, a.status, a.artifact)
            for a in (
                await db.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))
            ).all()
        ]
    url = f"/api/v1/boards/{task.board_id}/tasks/{task.id}"
    async with _client(env) as client:
        denied = await client.patch(url, json={"status": "cancelled"})
        assert denied.status_code == 401
        response = await client.patch(
            url,
            headers=AUTH,
            json={
                "status": "cancelled",
                "expected_status": status,
                "expected_updated_at": version,
                "comment": "Owner stopped diagnostic; retain original failure and scope.",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert not response.json()["repeat_enabled"]
        assert response.json()["assigned_agent_id"] is None
        assert response.json()["in_progress_at"] is None
        active = await client.get(f"/api/v1/boards/{task.board_id}/tasks", headers=AUTH)
        assert task.id.hex not in str(active.json()).replace("-", "")
        explicit = await client.get(
            f"/api/v1/boards/{task.board_id}/tasks?status=cancelled", headers=AUTH
        )
        assert explicit.status_code == 200, explicit.text
        assert str(task.id) in {t["id"] for t in explicit.json()["items"]}
        # A pending decision still prevents resuming: cancellation did not approve it.
        resumed = await client.patch(url, headers=AUTH, json={"status": "inbox"})
        assert resumed.status_code == 409, resumed.text
    async with env["maker"]() as db:
        stored = await db.get(Task, task.id)
        assert stored.status == "cancelled" and stored.description == task.description
        assert stored.tokens_used == 500 and stored.budget_exhausted_at == stamp
        assert stored.previous_in_progress_at == stamp
        assert (await db.get(Approval, approval.id)).status == "pending"
        assert (await db.get(ActivityEvent, old.id)).message == "original FAIL evidence"
        assert history == [
            (a.id, a.status, a.artifact)
            for a in (
                await db.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))
            ).all()
        ]
        assert await api._task_dep_ids(db, board_id=stored.board_id, task_id=stored.id) == [
            dependency.id
        ]
        for target in ("inbox", "in_progress", "review", "done", "cancelled"):
            with pytest.raises(HTTPException) as exc:
                await api.update_task(
                    TaskUpdate(status=target),
                    stored,
                    db,
                    ActorContext(actor_type="agent", agent=SimpleNamespace(is_board_lead=True)),
                )
            assert exc.value.status_code == 403


async def test_native_cancel_fences_late_result_and_owner_can_resume(native_env, monkeypatch):
    db, board, task = native_env
    # This direct route call isolates native fencing, not HTTP authorization (covered above).
    from unittest.mock import AsyncMock

    monkeypatch.setattr(api, "_require_task_user_write_access", AsyncMock())
    task.execution_lane = "harness"
    task.max_tokens_budget, task.tokens_used = 1, 1000
    cycle, _ = await svc.start_cycle(db, board, task, RANKING)
    attempt, token = await svc.claim_next(db, cycle, base_sha="a" * 40, ranking=RANKING)
    task_id, cycle_id, attempt_id = task.id, cycle.id, attempt.id
    await db.commit()
    updated = await api.update_task(
        TaskUpdate(status="cancelled"), task, db, ActorContext(actor_type="user")
    )
    assert updated.status == "cancelled"
    await db.refresh(cycle)
    await db.refresh(attempt)
    assert (
        cycle.state == "cancelled" and attempt.status == "cancelled" and attempt.claim_hash is None
    )
    with pytest.raises(svc.TeamFenced, match="stale or cancelled"):
        await svc.complete_attempt(
            db, board, cycle, attempt, token, ok=True, artifact="late result"
        )
    await db.rollback()
    task = await db.get(Task, task_id)
    await api.update_task(TaskUpdate(status="blocked"), task, db, ActorContext(actor_type="user"))
    assert task.status == "blocked"
    await api.update_task(TaskUpdate(status="inbox"), task, db, ActorContext(actor_type="user"))
    assert task.status == "inbox" and not task.repeat_enabled
    assert (await db.get(TeamCycle, cycle_id)).state == "cancelled"
    assert (await db.get(TeamAttempt, attempt_id)).status == "cancelled"


async def test_cancelled_cannot_skip_back_to_execution_or_done():
    for target in ("in_progress", "review", "done"):
        with pytest.raises(HTTPException, match="Invalid status transition"):
            await api._require_valid_status_transition(
                None, board_id=uuid4(), previous_status="cancelled", target_status=target
            )


@pytest.mark.parametrize("status", ["blocked", "cancelled", "paused"])
@pytest.mark.parametrize("actor", ["owner", "agent"])
async def test_dependency_reopen_cannot_resume_stopped_task(env, status, actor):
    from app.api import agent as agent_api
    from app.core.agent_tokens import hash_agent_token

    env["app"].include_router(api.router, prefix="/api/v1")
    env["app"].include_router(agent_api.router, prefix="/api/v1")
    dependency = await _seed_review_task(env, status="done", with_cycle=False)
    stopped = await _seed_review_task(env, status=status, with_cycle=False)
    token = "synthetic-worker-token-only-for-tests"
    async with env["maker"]() as db:
        board = await db.get(Board, stopped.board_id)
        board.only_lead_can_change_status = False
        board.block_status_changes_with_pending_approval = True
        worker = Agent(
            board_id=board.id,
            gateway_id=board.gateway_id,
            name="worker",
            status="online",
            agent_token_hash=hash_agent_token(token),
        )
        db.add(worker)
        await db.flush()
        source = await db.get(Task, dependency.id)
        source.repeat_enabled = True
        source.assigned_agent_id = worker.id
        held = await db.get(Task, stopped.id)
        held.tokens_used = 400
        approval = Approval(
            board_id=board.id,
            task_id=held.id,
            action_type="owner-decision",
            confidence=0,
            status="pending",
            payload={},
        )
        db.add(approval)
        await api.replace_task_dependencies(
            db, board_id=board.id, task_id=held.id, depends_on_task_ids=[source.id]
        )
        await db.commit()
        snapshot = held.model_dump()
    route = "/api/v1" if actor == "owner" else "/api/v1/agent"
    headers = AUTH if actor == "owner" else {"X-Agent-Token": token}
    async with _client(env) as client:
        response = await client.patch(
            f"{route}/boards/{stopped.board_id}/tasks/{dependency.id}",
            headers=headers,
            json={"status": "inbox"},
        )
        assert response.status_code == 200, response.text
        if status == "cancelled":
            for target in ("review", "in_progress"):
                denied = await client.patch(
                    f"/api/v1/boards/{stopped.board_id}/tasks/{stopped.id}",
                    headers=AUTH,
                    json={"status": target},
                )
                assert denied.status_code == 409 and "Invalid status transition" in denied.text
    async with env["maker"]() as db:
        assert (await db.get(Task, stopped.id)).model_dump() == snapshot
        assert (await db.get(Approval, approval.id)).status == "pending"


@pytest.mark.parametrize("kind", ["interval", "cron"])
@pytest.mark.parametrize("pause", [False, True])
async def test_stale_recurrence_scan_cannot_undo_owner_stop(env, monkeypatch, kind, pause):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from app.services import watchdog_loop as watchdog

    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(
        env, status="review" if kind == "interval" else "done", with_cycle=False
    )
    async with env["maker"]() as db:
        board = await db.get(Board, task.board_id)
        board.cron_tasks_enabled = True
        held = await db.get(Task, task.id)
        held.repeat_enabled, held.repeat_interval_minutes, held.repeat_cron = True, 1, "* * * * *"
        held.updated_at = utcnow() - timedelta(days=2)
        held.tokens_used = 888
        await db.commit()

    class Scan:
        async def __aenter__(self):
            self.db = env["maker"]()
            return self

        async def __aexit__(self, *args):
            await self.db.close()

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def execute(self, statement, params=None):
            sql = str(statement)
            result = await self.db.execute(statement, params)
            if "FROM tasks" in sql and "SELECT id, title" in sql:
                rows = [tuple(row) for row in result.fetchall()]
                assert len(rows) == 1
                index = 5 if kind == "interval" else 7
                rows = [
                    row[:index] + (datetime.fromisoformat(row[index]),) + row[index + 1 :]
                    for row in rows
                ]
                async with _client(env) as client:
                    status = "paused" if pause else "cancelled" if kind == "interval" else "done"
                    stopped = await client.patch(
                        f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
                        headers=AUTH,
                        json={"status": status} if pause else {"status": status, "repeat_enabled": False},
                    )
                    assert stopped.status_code == 200, stopped.text
                return SimpleNamespace(fetchall=lambda: rows)
            return result

    monkeypatch.setattr("app.db.session.async_session_maker", Scan)
    monkeypatch.setattr(watchdog, "_cron_matches", lambda cron: True)
    run = (
        watchdog._process_interval_repeats if kind == "interval" else watchdog._process_cron_repeats
    )
    assert await run() == 0
    async with env["maker"]() as db:
        held = await db.get(Task, task.id)
        assert held.status == ("paused" if pause else "cancelled" if kind == "interval" else "done")
        assert held.repeat_enabled is pause and held.tokens_used == 888
        assert len((await db.exec(select(Task))).all()) == 1


@pytest.mark.parametrize("status", ["blocked", "cancelled", "paused", "trash", "done"])
async def test_stopped_legacy_task_cannot_start_or_advance_pev(env, status):
    from app.services import pev_loop

    task = await _seed_review_task(env, status=status, with_cycle=False)
    async with env["maker"]() as db:
        assert await pev_loop._fetch_task(db, task.id) is None
        assert not (await pev_loop.start_pev(db, task.id))["ok"]
        assert not (await pev_loop.advance_pev(db, task.id))["ok"]


async def test_token_budgets_remain_telemetry_not_admission():
    from app.services.watchdog_loop import _enforce_task_budgets

    statements = []

    class ReadOnlyDB:
        async def execute(self, statement):
            statements.append(str(statement))
            return SimpleNamespace(fetchall=lambda: [])

    assert await _enforce_task_budgets(ReadOnlyDB()) == 0
    sql = statements[0]
    assert "tokens" not in sql.split("CASE", 1)[1].split("END AS trigger", 1)[0]
    assert "tokens" not in sql.split("WHERE t.status", 1)[1]
    assert "'wall-clock'" in sql
