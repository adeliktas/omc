"""Actual deadline scan/effects, owner API and durable markers in disposable SQLite."""

import re
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as api
from app.api.deps import ActorContext
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.models.organization_members import OrganizationMember
from app.models.tasks import Task
from app.models.users import User
from app.schemas.tasks import TaskUpdate
from app.services import watchdog_loop as wl
from tests import test_team_backlog
from tests.test_team_legacy_marker_races import patch_sql

env = test_team_backlog.env


@pytest.mark.parametrize("path", ["finalize", "requeue"])
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "goal",
        "budget",
        "pause",
        "cancel",
        "harness",
        "zero",
        "marker_zero",
        "marker_error",
        "marker_false",
        "expiry",
        "usage",
    ],
)
async def test_deadline_effect_requires_original_current_generation_and_commit(
    env, monkeypatch, path, change
):
    s, board, task = env
    worker = Agent(
        board_id=board.id, gateway_id=board.gateway_id, name="deadline worker"
    )
    user = User(
        clerk_user_id=str(uuid4()),
        email="deadline@invalid",
        active_organization_id=board.organization_id,
    )
    s.add_all([worker, user])
    await s.flush()
    s.add(
        OrganizationMember(
            organization_id=board.organization_id,
            user_id=user.id,
            role="owner",
            all_boards_write=True,
        )
    )
    task.execution_mode, task.execution_lane, task.status = (
        "single",
        "agent",
        "in_progress",
    )
    task.title, task.description = "legacy deadline", "ORIGINAL GOAL"
    task.updated_at = task.in_progress_at = utcnow() - timedelta(hours=1)
    task.max_seconds_budget, task.tokens_used = 60, 0
    task.assigned_agent_id = worker.id if path == "finalize" else None
    await s.commit()
    tid, uid, stamp, original_assignee = (
        task.id,
        user.id,
        task.updated_at,
        task.assigned_agent_id,
    )
    raw_execute = s.execute
    if s.bind.dialect.name == "sqlite":
        patch_sql(s, monkeypatch)
    else:

        async def no_notify(*args, **kwargs):
            return None

        monkeypatch.setattr(api, "_notify_task_update_assignment_changes", no_notify)
    execute, scanned, edits = s.execute, [], []

    async def owner_edit():
        async with AsyncSession(s.bind, expire_on_commit=False) as owner:
            current = await owner.get(Task, tid)
            actor = await owner.get(User, uid)
            if change == "budget":
                # This field is not exposed by TaskUpdate; use a real disposable
                # owner transaction, not a silently ignored API payload.
                current.max_seconds_budget, current.updated_at = 7200, utcnow()
                owner.add(current)
                await owner.commit()
            else:
                payload = {
                    "goal": {"description": "NEW OWNER GOAL"},
                    "pause": {"status": "paused"},
                    "cancel": {"status": "cancelled"},
                }[change]
                await api.update_task(
                    TaskUpdate(**payload),
                    task=current,
                    session=owner,
                    actor=ActorContext(actor_type="user", user=actor),
                )
        edits.append(change)

    async def sqlite_sql(stmt, *args, **kwargs):
        sql = str(stmt)
        is_scan = "END AS trigger" in sql
        # Syntax adapter only: same real SELECT/WHERE/effects, no fabricated rows.
        if s.bind.dialect.name == "sqlite":
            sql = sql.replace(
                "EXTRACT(EPOCH FROM (NOW() - t.in_progress_at))::int",
                "CAST((julianday('now') - julianday(t.in_progress_at))*86400 AS INTEGER)",
            )
            sql = re.sub(
                r"\(NOW\(\) - ((?:t\.)?in_progress_at)\) > make_interval\(secs => ((?:t\.)?max_seconds_budget)\)",
                r"(julianday('now') - julianday(\1))*86400 > \2",
                sql,
            )
        if change == "zero" and sql.lstrip().startswith("UPDATE tasks"):
            sql += " AND 1=0"
        if "INSERT INTO activity_events" in sql:
            if change == "marker_zero":
                sql += " AND 1=0"
            if change == "marker_error":
                sql = sql.replace(
                    "INSERT INTO activity_events",
                    "INSERT INTO absent_deadline_marker_fixture",
                )
        result = await execute(text(sql), *args, **kwargs)
        if is_scan:
            frozen = result.freeze()  # Keep actual scan rows across the owner commit.
            scanned.append(True)
            if change in ("goal", "budget", "pause", "cancel"):
                await owner_edit()
            elif change in ("harness", "expiry", "usage"):
                values = (
                    {"execution_lane": "harness", "updated_at": utcnow()}
                    if change == "harness"
                    else {"max_seconds_budget": 7200}
                    if change == "expiry"
                    else {"tokens_used": 123}
                )
                # Expiry/telemetry without a stamp change still must be re-read.
                await raw_execute(
                    Task.__table__.update().where(Task.id == tid).values(**values)
                )
                await s.commit()
                edits.append(change)
            return frozen()
        return result

    async def refused(*args, **kwargs):
        return False

    monkeypatch.setattr(s, "execute", sqlite_sql)
    if change == "marker_false":
        monkeypatch.setattr(wl, "_post_comment", refused)
    count = await wl._enforce_task_budgets(s)
    await s.commit()  # An innocent later commit cannot resurrect a refused effect.
    await s.refresh(task)
    markers = [
        e
        for e in await s.exec(select(ActivityEvent).where(ActivityEvent.task_id == tid))
        if (e.message or "").startswith(
            ("[watchdog-finalize]", "[watchdog-budget-exhausted]")
        )
    ]
    success = change in ("none", "usage")
    assert scanned == [True]
    assert count == len(markers) == int(success)
    expected = (
        "blocked"
        if path == "finalize" and change == "none"
        else "review"
        if path == "finalize" and success
        else "inbox"
        if success
        else "paused"
        if change == "pause"
        else "cancelled"
        if change == "cancel"
        else "in_progress"
    )
    assert task.status == expected
    assert (
        task.budget_exhausted_at is not None
        if success
        else task.budget_exhausted_at is None
    )
    assert task.description == (
        "NEW OWNER GOAL" if change == "goal" else "ORIGINAL GOAL"
    )
    assert task.max_seconds_budget == (7200 if change in ("budget", "expiry") else 60)
    assert task.tokens_used == (123 if change == "usage" else 0)
    if success:
        assert task.assigned_agent_id == (
            original_assignee if path == "finalize" else None
        )
        assert ("tokens_used=123" in markers[0].message) == (change == "usage")
    if change in ("zero", "marker_zero", "marker_error", "marker_false", "expiry"):
        assert task.updated_at == stamp and task.assigned_agent_id == original_assignee
    if change in ("goal", "budget", "pause", "cancel", "harness", "expiry", "usage"):
        assert edits == [change]
