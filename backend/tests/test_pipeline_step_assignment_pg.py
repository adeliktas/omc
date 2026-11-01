"""A pipeline step is not handed to an agent while its upstream step is unfinished.

Production 2026-10-04: step 3/3 of a pipeline test was auto-assigned and its agent
notified at creation, before step 2 existed as output. Run against a disposable
migrated database only:
MC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55438/mc_audit
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.core.auth import AuthContext
from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.schemas.tasks import TaskCreate


@pytest.mark.asyncio
async def test_dependent_step_waits_unassigned():
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    if not url.endswith("/mc_audit"):
        pytest.fail("Refusing non-disposable database: expected /mc_audit")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))
    org, gw, bid = uuid4(), uuid4(), uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Organization(id=org, name="pipeline"))
        await db.flush()
        db.add(Gateway(id=gw, organization_id=org, name="g", url="https://fake.invalid",
                       workspace_root="/tmp/pipeline"))
        await db.flush()
        board = Board(id=bid, organization_id=org, gateway_id=gw, name="b", slug=f"b-{bid.hex}")
        db.add(board)
        await db.flush()
        db.add(Agent(name="QA Engineer", board_id=bid, gateway_id=gw, status="online"))
        await db.commit()
        auth = AuthContext(actor_type="user")
        first = await tasks_api.create_task(
            TaskCreate(title="test step 1", description="verify", execution_lane="agent"),
            board=board, session=db, auth=auth,
        )
        second = await tasks_api.create_task(
            TaskCreate(title="test step 2", description="verify", execution_lane="agent",
                       depends_on_task_ids=[first.id]),
            board=board, session=db, auth=auth,
        )
    await engine.dispose()
    assert first.assigned_agent_id is not None
    assert second.assigned_agent_id is None
