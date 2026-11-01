"""Real completion caller/downstream effects, deterministic verdict and owner API."""
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
from app.services import llm_evaluator, watchdog_loop as wl
from tests.test_team_backlog import env
from tests.test_team_legacy_marker_races import patch_sql


@pytest.mark.parametrize("change", ["none", "goal", "pause", "harness", "zero", "error", "no"])
async def test_completion_keeps_original_generation_and_actual_marker(env, monkeypatch, change):
    s, board, task = env
    worker = Agent(board_id=board.id, gateway_id=board.gateway_id, name="original worker")
    user = User(clerk_user_id=str(uuid4()), email="completion@invalid",
                active_organization_id=board.organization_id)
    s.add_all([worker, user])
    await s.flush()
    s.add(OrganizationMember(organization_id=board.organization_id, user_id=user.id,
                            role="owner", all_boards_write=True))
    task.status, task.execution_mode, task.execution_lane = "in_progress", "single", "agent"
    task.title, task.description = "original work", "ORIGINAL GOAL"
    task.updated_at = task.in_progress_at = utcnow() - timedelta(hours=1)
    task.assigned_agent_id, task.complexity = worker.id, 1
    message = "Analysis finished successfully with an answer."
    s.add(ActivityEvent(task_id=task.id, board_id=board.id, agent_id=worker.id,
                        event_type="task.comment", message=message, created_at=utcnow()))
    await s.commit()
    tid, uid = task.id, user.id
    patch_sql(s, monkeypatch)
    execute = s.execute

    async def sqlite_scan(stmt, *args, **kwargs):
        sql = str(stmt)
        if "JOIN LATERAL" in sql:
            # One current assignee comment: equivalent real SQLite read scan.
            # Require production to retain the original stamp when it adds that column.
            columns = "t.id, t.board_id, ae.message"
            if "SELECT t.id, t.board_id, ae.message, t.updated_at" in sql:
                columns += ", t.updated_at"
            stmt = text(f"""SELECT {columns} FROM tasks t
                JOIN activity_events ae ON ae.task_id=t.id AND ae.agent_id=t.assigned_agent_id
                WHERE t.status='in_progress' AND t.execution_mode='single'
                  AND t.execution_lane='agent' AND ae.event_type='task.comment'
                  AND ae.created_at > COALESCE(t.in_progress_at,t.updated_at)
                ORDER BY t.updated_at DESC LIMIT 5""")
        elif "INSERT INTO activity_events" in sql and change in ("zero", "error"):
            if change == "error":
                raise RuntimeError("marker fixture statement failure")
            stmt = text(sql + " AND 1=0")  # Genuine rowcount zero.
        else:
            stmt = text(sql.replace(" ILIKE ", " LIKE ").replace(" ~* ", " REGEXP "))
        return await execute(stmt, *args, **kwargs)

    monkeypatch.setattr(s, "execute", sqlite_scan)
    seen, edited = [], []

    async def local_verdict(value):
        seen.append(value)
        if change in ("goal", "pause", "harness"):
            async with AsyncSession(s.bind, expire_on_commit=False) as owner:
                current = await owner.get(Task, tid)
                actor = await owner.get(User, uid)
                context = ActorContext(actor_type="user", user=actor)
                if change == "harness":
                    await api.update_task(TaskUpdate(status="inbox", assigned_agent_id=None),
                                          task=current, session=owner, actor=context)
                    payload = {"execution_lane": "harness"}
                else:
                    payload = ({"description": "NEW OWNER GOAL"} if change == "goal"
                               else {"status": "paused"})
                await api.update_task(TaskUpdate(**payload), task=current, session=owner,
                                      actor=context)
                edited.append(change)
        return change != "no"  # No inference/provider call.

    monkeypatch.setattr(llm_evaluator, "is_completion_comment", local_verdict)
    count = await wl._detect_llm_completion(s)
    await s.commit()  # A later innocent commit cannot resurrect refused effects.
    await s.refresh(task)
    markers = [e for e in await s.exec(select(ActivityEvent).where(ActivityEvent.task_id == tid))
               if (e.message or "").startswith("COMPLETED: LLM-evaluated")]
    assert seen == [message]
    success = change == "none"
    assert count == len(markers) == int(success)
    if change in ("goal", "pause", "harness"):
        assert edited == [change]
    if change == "pause":
        assert task.status == "paused"
    if change == "harness":
        assert task.execution_lane == "harness" and task.status == "inbox"
    if change in ("none", "goal", "no"):
        promoted = await wl._detect_freeform_completion(s)
        await s.refresh(task)
        assert promoted == int(success)
        assert task.status == ("done" if success else "in_progress")
        assert task.description == ("NEW OWNER GOAL" if change == "goal" else "ORIGINAL GOAL")
