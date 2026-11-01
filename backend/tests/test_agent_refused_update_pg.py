"""A transition the API refuses must not be saved by the refusal's own audit note.

Production 2026-10-04: the lead asked to close a review task, the approval gate
answered 409, and the handler committed its "[agent-gateway] update blocked"
comment in the same session as the already-applied status — the task ended up
done with no approval. Run against a disposable migrated database only:
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

from app.core import agent_auth
from app.core.agent_tokens import generate_agent_token, hash_agent_token
from app.db.session import get_session
from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task


@pytest.mark.asyncio
async def test_refused_done_stays_in_review(monkeypatch):
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
    org, gw, bid, tid = uuid4(), uuid4(), uuid4(), uuid4()
    token = generate_agent_token()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Organization(id=org, name="gate"))
        await db.flush()
        db.add(Gateway(id=gw, organization_id=org, name="g", url="https://fake.invalid",
                       workspace_root="/tmp/gate"))
        await db.flush()
        db.add(Board(id=bid, organization_id=org, gateway_id=gw, name="b", slug=f"b-{bid.hex}",
                     require_approval_for_done=True))
        await db.flush()
        db.add(Agent(name="Atlas", board_id=bid, gateway_id=gw, is_board_lead=True, status="online",
                     agent_token_hash=hash_agent_token(token)))
        db.add(Task(id=tid, board_id=bid, title="step", status="review", execution_lane="agent"))
        await db.commit()

    async def _session():
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    base = f"/api/v1/agent/boards/{bid}/tasks/{tid}"
    auth = {"Authorization": f"Bearer {token}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(base + "/comments", headers=auth, json={"message": "Lead QA Verdict: PASS"})
            refused = await c.patch(base, headers=auth, json={"status": "done"})
            assert refused.status_code == 409
    finally:
        app.dependency_overrides.clear()
    async with AsyncSession(engine) as db:
        status = (await db.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})).scalar()
        note = (await db.execute(text(
            "SELECT count(*) FROM activity_events WHERE task_id = :t AND message LIKE '[agent-gateway] update blocked%'"
        ), {"t": tid})).scalar()
    await engine.dispose()
    assert status == "review"
    assert note == 1
