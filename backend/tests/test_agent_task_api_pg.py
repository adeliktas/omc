"""A worker agent can read, start, report on and hand back its task over HTTP.

Regression for task 0000a003 (2026-10-02): the agent did the work but GET on its
task was 405 and its stale token got 401, so the board saw no reply at all.
Run against a disposable migrated database only:
MC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55438/mc_audit
"""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.core import agent_auth
from app.core.agent_tokens import generate_agent_token, hash_agent_token
from app.core.auth import AuthContext
from app.db.session import get_session
from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.schemas.tasks import TaskCreate


@pytest.mark.asyncio
async def test_worker_agent_task_round_trip(monkeypatch):
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    if not url.endswith("/mc_audit"):
        pytest.fail("Refusing non-disposable database: expected /mc_audit")
    from app.main import app

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))
    monkeypatch.setattr(agent_auth.agent_auth_limiter, "is_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(tasks_api, "_notify_agent_on_task_assign", AsyncMock())
    org, gw, bid, worker = (uuid4() for _ in range(4))
    token = generate_agent_token()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Organization(id=org, name="agent-api"))
        await db.flush()
        db.add(Gateway(id=gw, organization_id=org, name="g", url="https://fake.invalid",
                       workspace_root="/tmp/agent-api"))
        await db.flush()
        board = Board(id=bid, organization_id=org, gateway_id=gw, name="b", slug=f"b-{bid.hex}")
        db.add(board)
        await db.flush()
        db.add(Agent(id=uuid4(), name="Atlas", board_id=bid, gateway_id=gw, is_board_lead=True,
                     status="online"))
        db.add(Agent(id=worker, name="QA Engineer", board_id=bid, gateway_id=gw, status="online",
                     openclaw_session_id="agent:worker:main", agent_token_hash=hash_agent_token(token)))
        await db.commit()
        created = await tasks_api.create_task(
            TaskCreate(title="fetch page", description="fetch and reply", execution_lane="agent"),
            board=board, session=db, auth=AuthContext(actor_type="user"),
        )
        assert created.assigned_agent_id == worker

    async def _session():
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    base = f"/api/v1/agent/boards/{bid}/tasks/{created.id}"
    good = {"Authorization": f"Bearer {token}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get(base, headers=good)).json()["status"] == "inbox"
            assert (await c.patch(base, headers=good, json={"status": "in_progress"})).status_code == 200
            reply = await c.post(base + "/comments", headers=good, json={"message": "<pre>1.2.3.4</pre>"})
            assert reply.status_code == 200
            assert (await c.patch(base, headers=good, json={"status": "review"})).status_code == 200
            stale = {"Authorization": "Bearer " + "x" * len(token)}
            assert (await c.post(base + "/comments", headers=stale, json={"message": "x"})).status_code == 401
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
