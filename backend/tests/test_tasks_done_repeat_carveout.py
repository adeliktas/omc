# ruff: noqa
"""T67 (2026-05-15): `done is final` gate carve-out for repeating tasks.

Done-is-final blocks agents from re-opening completed tasks. Repeating
tasks are explicitly designed to cycle done → inbox → in_progress → done
forever; the gate must NOT fire for them, otherwise watchdog soft-restart
strands the cycle.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.api.deps import ActorContext
from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.schemas.tasks import TaskUpdate


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


async def _seed(session: AsyncSession, *, repeat: bool) -> tuple[Task, Agent]:
    org_id = uuid4()
    board_id = uuid4()
    gateway_id = uuid4()
    worker_id = uuid4()
    task_id = uuid4()

    session.add(Organization(id=org_id, name="org"))
    session.add(Gateway(
        id=gateway_id, organization_id=org_id, name="gw",
        url="http://localhost:18789", workspace_root="/tmp",
    ))
    session.add(Board(
        id=board_id, organization_id=org_id, name="b", slug="b",
        gateway_id=gateway_id, only_lead_can_change_status=False,
    ))
    session.add(Agent(
        id=worker_id, name="worker", board_id=board_id,
        gateway_id=gateway_id, status="online",
    ))
    session.add(Task(
        id=task_id, board_id=board_id, title="t", status="done",
        assigned_agent_id=worker_id,
        repeat_enabled=repeat,
        repeat_interval_minutes=5 if repeat else None,
    ))
    await session.commit()

    task = (await session.exec(select(Task).where(col(Task.id) == task_id))).first()
    actor = (await session.exec(select(Agent).where(col(Agent.id) == worker_id))).first()
    assert task is not None and actor is not None
    return task, actor


@pytest.mark.asyncio
async def test_done_is_final_blocks_agent_on_non_repeat_task() -> None:
    """Original guarantee preserved: agents can't reopen a one-shot done task."""
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            task, actor = await _seed(session, repeat=False)
            with pytest.raises(HTTPException) as exc:
                await tasks_api.update_task(
                    payload=TaskUpdate(status="in_progress"),
                    task=task,
                    session=session,
                    actor=ActorContext(actor_type="agent", agent=actor),
                )
            assert exc.value.status_code == 409
            assert "Done is final" in str(exc.value.detail)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_done_is_final_carveout_lets_repeat_task_go_inbox() -> None:
    """T67: soft-restart path. State-machine allows `done → inbox`; without
    the carve-out the Done-is-final gate would still 409. With the carve-out
    the agent-initiated soft-restart succeeds for repeat tasks.

    (Note: `done → in_progress` is rejected by the state-machine itself —
    independent of this carve-out. Only `done → inbox` is a legal exit
    edge from done.)"""
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            task, actor = await _seed(session, repeat=True)
            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="inbox"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=actor),
            )
            assert updated.status == "inbox"
    finally:
        await engine.dispose()
