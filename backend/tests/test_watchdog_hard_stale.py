"""Real SQLite effects of the stale-scan phase; unrelated tick I/O is excluded."""
import ast
from contextlib import asynccontextmanager
from datetime import timedelta
import inspect
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.services import watchdog_loop as wl
from tests.test_team_backlog import env
from tests.test_team_legacy_marker_races import patch_sql


def stale_phase():
    # Execute the actual production phase, not a copied implementation or live tick.
    fn = ast.parse(inspect.getsource(wl._process_once)).body[0]
    stats = next(n for n in fn.body if isinstance(n, ast.Assign)
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "stats")
    phase = next(n for n in fn.body if isinstance(n, ast.AsyncWith)
                 and any(isinstance(c, ast.Name) and c.id == "_stale_tasks" for c in ast.walk(n)))
    fn.body = [stats, phase, ast.Return(value=ast.Name(id="stats", ctx=ast.Load()))]
    scope = dict(vars(wl))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "<production stale phase>", "exec"), scope)
    return scope["_process_once"]


@pytest.mark.parametrize("status,meta,refusal,expected", [
    ("review", False, None, "review"),
    ("review", True, None, "review"),
    ("in_progress", False, None, "inbox"),
    ("in_progress", True, None, "done"),
    ("in_progress", False, "update", "in_progress"),
    ("in_progress", True, "update", "in_progress"),
    ("in_progress", False, "comment", "in_progress"),
    ("in_progress", True, "comment", "in_progress"),
    ("in_progress", False, "owner", "paused"),
    ("in_progress", False, "harness", "in_progress"),
])
async def test_hard_stale_effects_are_real_atomic_and_current(env, monkeypatch, status, meta, refusal, expected):
    s, board, task = env
    agent = Agent(id=uuid4(), name="legacy worker", board_id=board.id, gateway_id=board.gateway_id)
    s.add(agent)
    await s.flush()
    task.execution_mode, task.execution_lane, task.title = "single", "agent", "legacy task"
    task.status, task.assigned_agent_id, task.updated_at = status, agent.id, utcnow() - timedelta(days=2)
    s.add(task)
    await s.commit()
    tid, bid, assigned, stamp = task.id.hex, board.id.hex, agent.id, task.updated_at
    scanned = {"id": tid, "board_id": bid, "status": status, "updated_at": stamp,
               "age_hours": 48, "agent_name": "legacy worker", "assigned_agent_id": str(assigned)}
    if refusal in ("owner", "harness"):
        if refusal == "owner":
            task.status = "paused"
        else:
            task.execution_lane = "harness"
        task.updated_at = utcnow()
        s.add(task)
        await s.commit()
    patch_sql(s, monkeypatch)
    execute = s.execute

    async def fenced(stmt, *args, **kwargs):
        sql = str(stmt)
        if ((refusal == "update" and sql.startswith("UPDATE tasks SET status="))
                or (refusal == "comment" and "INSERT INTO activity_events" in sql)):
            stmt = text(sql + " AND 1=0")  # Actual zero-row SQL, not a fabricated cursor.
        return await execute(stmt, *args, **kwargs)

    @asynccontextmanager
    async def session():
        yield s

    async def scan(*args):
        return [scanned]

    async def count(*args):
        return 100, None  # Existing open-review cooldown; no further nudge allowed.

    async def no_clone(*args):
        return None

    monkeypatch.setattr(s, "execute", fenced)
    monkeypatch.setattr(wl, "async_session_maker", session)
    monkeypatch.setattr(wl, "_stale_tasks", scan)
    monkeypatch.setattr(wl, "_existing_nudge_count", count)
    monkeypatch.setattr(wl, "_find_senior_variant", no_clone)
    monkeypatch.setattr(wl, "_find_idle_clone", no_clone)
    monkeypatch.setattr(wl, "_is_meta_task", lambda _: meta)
    monkeypatch.setattr(wl, "_escalate_to_owner_review", lambda *a, **kw: False)
    result = await stale_phase()()
    await s.refresh(task)
    events = list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id == task.id)))
    markers = [e for e in events if (e.message or "").startswith("[watchdog-nudge]")]
    changed = expected in ("inbox", "done")
    assert task.status == expected
    assert result["reassigned"] == int(changed)
    assert len(markers) == int(changed)
    assert task.assigned_agent_id == (None if expected == "inbox" else assigned)
    if status == "review" or refusal in ("update", "comment"):
        assert task.updated_at == stamp


@pytest.mark.parametrize("path", ["senior", "clone"])
@pytest.mark.parametrize("refusal", [None, "update", "comment", "goal", "pause", "harness",
                                     "after_goal", "after_pause"])
async def test_clone_assignment_marker_and_original_generation(env, monkeypatch, path, refusal):
    from app.api import tasks as api
    from app.api.deps import ActorContext
    from app.models.tasks import Task
    from app.models.users import User
    from app.models.organization_members import OrganizationMember
    from app.schemas.tasks import TaskUpdate
    from sqlmodel.ext.asyncio.session import AsyncSession

    s, board, task = env
    old = Agent(board_id=board.id, gateway_id=board.gateway_id, name="legacy worker")
    new = Agent(board_id=board.id, gateway_id=board.gateway_id, name="legacy worker Senior")
    user = User(clerk_user_id=str(uuid4()), email="reassign@invalid",
                active_organization_id=board.organization_id)
    s.add_all([old, new, user])
    await s.flush()
    s.add(OrganizationMember(organization_id=board.organization_id, user_id=user.id,
                            role="owner", all_boards_write=True))
    task.execution_mode, task.execution_lane, task.status = "single", "agent", "in_progress"
    task.title, task.description = "legacy task", "ORIGINAL GOAL"
    task.assigned_agent_id, task.updated_at = old.id, utcnow() - timedelta(days=2)
    await s.commit()
    tid, bid, old_id, new_id, stamp, user_id = (
        task.id.hex, board.id.hex, old.id, new.id, task.updated_at, user.id)
    scanned = {"id": tid, "board_id": bid, "status": "in_progress", "updated_at": stamp,
               "age_hours": 48 if path == "senior" else 1, "agent_name": old.name,
               "assigned_agent_id": str(old_id)}
    patch_sql(s, monkeypatch)
    execute, commit = s.execute, s.commit
    edited = []

    async def owner_change(kind):
        async with AsyncSession(s.bind, expire_on_commit=False) as owner:
            current = await owner.get(Task, task.id)
            actor = await owner.get(User, user_id)
            payload = ({"description": "NEW OWNER GOAL"} if kind == "goal"
                       else {"status": "paused"})
            await api.update_task(TaskUpdate(**payload), task=current, session=owner,
                                  actor=ActorContext(actor_type="user", user=actor))
        edited.append(kind)

    async def fenced(stmt, *args, **kwargs):
        sql = str(stmt)
        if ((refusal == "update" and sql.startswith("UPDATE tasks SET assigned_agent_id"))
                or (refusal == "comment" and "INSERT INTO activity_events" in sql)):
            stmt = text(sql + " AND 1=0")
        return await execute(stmt, *args, **kwargs)

    async def finish():
        await commit()
        if refusal in ("after_goal", "after_pause") and not edited:
            await owner_change(refusal.removeprefix("after_"))

    async def target(*args):
        if refusal in ("goal", "pause") and not edited:
            await owner_change(refusal)
        elif refusal == "harness" and not edited:
            task.execution_lane, task.updated_at = "harness", utcnow()
            await commit()
            edited.append("harness")
        return str(new_id)

    @asynccontextmanager
    async def session():
        yield s

    async def scan(*args):
        return [scanned]

    async def count(*args):
        return (100, None) if path == "senior" else (1, utcnow().timestamp() - 7200)

    monkeypatch.setattr(s, "execute", fenced)
    monkeypatch.setattr(s, "commit", finish)
    monkeypatch.setattr(wl, "async_session_maker", session)
    monkeypatch.setattr(wl, "_stale_tasks", scan)
    monkeypatch.setattr(wl, "_existing_nudge_count", count)
    monkeypatch.setattr(wl, "_find_senior_variant", target)
    monkeypatch.setattr(wl, "_find_idle_clone", target)
    monkeypatch.setattr(wl, "_escalate_to_owner_review", lambda *a, **kw: False)
    result = await stale_phase()()
    await commit()
    await s.refresh(task)
    markers = [e for e in await s.exec(select(ActivityEvent).where(ActivityEvent.task_id == task.id))
               if (e.message or "").startswith("[watchdog-nudge]")]
    succeeded = refusal in (None, "after_goal", "after_pause")
    assert result["reassigned"] == int(succeeded)
    assert len(markers) == int(succeeded)
    assert task.assigned_agent_id == (None if refusal in ("pause", "after_pause")
                                     else new_id if succeeded else old_id)
    if refusal in ("goal", "after_goal"):
        assert task.description == "NEW OWNER GOAL" and edited
    if refusal in ("pause", "after_pause"):
        assert task.status == "paused" and edited
    if refusal in ("update", "comment"):
        assert task.updated_at == stamp
