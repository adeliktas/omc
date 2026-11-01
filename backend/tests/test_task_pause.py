"""Owner pause stops dispatch without erasing evidence or replenishing budgets."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi_pagination import add_pagination
from sqlmodel import select

from app.api import tasks as api
from app.api.deps import ActorContext
from app.core.time import utcnow
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamAttempt
from app.schemas.tasks import TaskUpdate
from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env, sidecar  # noqa: F401
from tests.test_team_cycle import RANKING
from tests.test_team_cycle import env as native_env  # noqa: F401


@pytest.mark.parametrize("source,target", [("inbox", "inbox"), ("review", "review"), ("blocked", "blocked"), ("done", "inbox"), ("in_progress", "blocked")])
async def test_pause_resume_retains_history_schedule_and_budget(env, source, target):
    env["app"].include_router(api.router, prefix="/api/v1")
    add_pagination(env["app"])
    task = await _seed_review_task(env, status=source)
    stamp = utcnow()
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        task.repeat_enabled, task.repeat_interval_minutes = True, 67
        task.tokens_used, task.budget_exhausted_at = 501, stamp
        task.scheduled_at = stamp
        task.in_progress_at = stamp if source == "in_progress" else None
        await db.commit()
        before = task.model_dump()
        history = [a.model_dump() for a in (await db.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))).all()]
    url = f"/api/v1/boards/{task.board_id}/tasks/{task.id}"
    async with _client(env) as client:
        response = await client.patch(url, headers=AUTH, json={"status": "paused", "expected_status": source})
        assert response.status_code == 200, response.text
        assert response.json()["paused_from_status"] == target
        assert response.json()["assigned_agent_id"] is None
        listed = await client.get(f"/api/v1/boards/{task.board_id}/tasks?status=paused", headers=AUTH)
        assert task.id.hex in str(listed.json()).replace("-", "")
        repeat = await client.patch(url, headers=AUTH, json={"status": "paused"})
        assert repeat.status_code == 200 and repeat.json()["paused_from_status"] == target
        stale = await client.patch(url, headers=AUTH, json={"status": target, "expected_status": source})
        assert stale.status_code == 409
        denied = await client.patch(url, headers=AUTH, json={"status": "in_progress"})
        assert denied.status_code == 409
        resumed = await client.patch(url, headers=AUTH, json={"status": target, "expected_status": "paused"})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == target and resumed.json()["paused_from_status"] is None
    async with env["maker"]() as db:
        task = await db.get(Task, task.id)
        for key in ("description", "repeat_enabled", "repeat_interval_minutes", "scheduled_at", "tokens_used", "budget_exhausted_at", "max_seconds_budget", "max_tokens_budget"):
            assert getattr(task, key) == before[key], key
        assert [a.model_dump() for a in (await db.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))).all()] == history


async def test_pause_allowed_while_gated_but_resume_is_not(env):
    env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(env, status="review", with_cycle=False)
    dependency = await _seed_review_task(env, status="blocked", with_cycle=False)
    async with env["maker"]() as db:
        board = await db.get(Board, task.board_id)
        board.block_status_changes_with_pending_approval = True
        db.add(Approval(board_id=board.id, task_id=task.id, action_type="decision", status="pending", confidence=0, payload={}))
        await api.replace_task_dependencies(db, board_id=board.id, task_id=task.id, depends_on_task_ids=[dependency.id])
        await db.commit()
    url = f"/api/v1/boards/{task.board_id}/tasks/{task.id}"
    async with _client(env) as client:
        response = await client.patch(url, headers=AUTH, json={"status": "paused"})
        assert response.status_code == 200, response.text
        resumed = await client.patch(url, headers=AUTH, json={"status": "review"})
        assert resumed.status_code == 409
    async with env["maker"]() as db:
        assert (await db.get(Task, task.id)).status == "paused"
        assert (await db.get(Task, task.id)).paused_from_status == "review"


@pytest.mark.parametrize("rotate", [False, True])
async def test_stale_inbox_scan_cannot_override_pause(monkeypatch, rotate):
    from datetime import timedelta
    from uuid import uuid4
    from app.services import watchdog_loop as w

    tid, bid, aid = str(uuid4()), str(uuid4()), str(uuid4())
    stamp = utcnow() - timedelta(hours=2)
    async def counts(db, task_id, marker):
        return 0 if "rotation" in marker else 3
    monkeypatch.setattr(w, "_count_comments_this_cycle", counts)
    monkeypatch.setattr(w, "_pick_worker_for_role", AsyncMock(return_value=str(uuid4()) if rotate else None))
    monkeypatch.setattr(w, "_seconds_since_comment_matching", AsyncMock(return_value=None))
    posted = AsyncMock()
    monkeypatch.setattr(w, "_post_comment", posted)
    class StaleScan:
        updates = 0
        refused = False
        async def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT t.id" in sql:
                return SimpleNamespace(fetchall=lambda: [(tid, "poem", "original", bid, aid, "Atlas", stamp)])
            # Owner paused after SELECT; a guarded SQL UPDATE affects no rows.
            self.updates += 1
            assert "AND status = 'inbox' AND updated_at = :stamp" in sql
            assert params["stamp"] == stamp
            self.refused = True
            return SimpleNamespace(rowcount=0, scalar_one_or_none=lambda: None)
        async def rollback(self):
            if not self.refused:
                pytest.fail("stale scan must refuse cleanly, not swallow a failed assertion")
    scan = StaleScan()
    assert await w._recover_stuck_claimed_inbox(scan) == 0
    assert scan.updates == 1
    posted.assert_not_called()


async def test_pause_fences_native_claim_and_agents_cannot_resume(native_env, monkeypatch):
    db, board, task = native_env
    monkeypatch.setattr(api, "_require_task_user_write_access", AsyncMock())
    cycle, _ = await svc.start_cycle(db, board, task, RANKING)
    attempt, token = await svc.claim_next(db, cycle, base_sha="a" * 40, ranking=RANKING)
    await db.commit()
    result = await api.update_task(TaskUpdate(status="paused"), task, db, ActorContext(actor_type="user"))
    assert result.status == "paused" and result.paused_from_status == "blocked"
    await db.refresh(cycle)
    await db.refresh(attempt)
    assert cycle.state == "cancelled" and attempt.status == "cancelled" and attempt.claim_hash is None
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(db, board, cycle, attempt, token, ok=True, artifact="late result")
    await db.rollback()
    await db.refresh(task)
    actor = ActorContext(actor_type="agent", agent=SimpleNamespace(is_board_lead=True))
    for status in ("paused", "inbox", "review", "done", "blocked", "cancelled"):
        with pytest.raises(HTTPException) as exc:
            await api.update_task(TaskUpdate(status=status), task, db, actor)
        assert exc.value.status_code == 403
