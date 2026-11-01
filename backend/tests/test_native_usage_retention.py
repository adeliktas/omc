"""Native owner lifecycle edits must not erase observed historical task usage."""
from datetime import datetime, timezone

import pytest

from app.api import tasks as tasks_api
from app.models.tasks import Task
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env, sidecar


@pytest.mark.parametrize("previous,target", [("blocked", "inbox"), ("inbox", "in_progress"), ("cancelled", "inbox"), ("paused", "inbox")])
def test_native_lifecycle_keeps_usage_and_exhaustion(previous, target):
    stamp = datetime.now(timezone.utc)
    task = Task(title="native", execution_mode="team", tokens_used=201779,
                budget_exhausted_at=stamp)
    tasks_api._reset_attempt_budget(task, previous, target)
    assert task.tokens_used == 201779 and task.budget_exhausted_at == stamp


async def test_owner_native_plan_reconciliation_keeps_usage_via_real_patch(env):
    env["app"].include_router(tasks_api.router, prefix="/api/v1")
    task = await _seed_review_task(env, with_cycle=False, status="blocked")
    stamp = datetime.now(timezone.utc)
    async with env["maker"]() as s:
        row = await s.get(Task, task.id)
        row.execution_mode, row.execution_lane = "team", "harness"
        row.tokens_used, row.budget_exhausted_at = 201779, stamp
        original = row.description
        await s.commit()
    async with _client(env) as client:
        response = await client.patch(f"/api/v1/boards/{task.board_id}/tasks/{task.id}",
            headers=AUTH, json={"status": "inbox", "description": original + "\nOwner plan reconciliation", "comment": "Original-scope planning correction"})
        assert response.status_code == 200, response.text
    async with env["maker"]() as s:
        row = await s.get(Task, task.id)
        assert row.status == "inbox" and row.tokens_used == 201779
        assert row.budget_exhausted_at.replace(tzinfo=timezone.utc) == stamp
