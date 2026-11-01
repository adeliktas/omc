"""Template sync survives one agent's gateway failure and never re-provisions retired agents.

Production 2026-10-02: a retired agent's legacy workspace made the gateway refuse
its update; the orchestrator rolled the session back, the error path then read
an expired ORM attribute (MissingGreenlet) and the whole sync returned HTTP 500,
so no agent was re-keyed. Run against a disposable migrated database only:
MC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55438/mc_audit
"""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.users import User
import app.services.openclaw.provisioning as provisioning
import app.services.openclaw.provisioning_db as provisioning_db
from app.services.openclaw.gateway_rpc import OpenClawGatewayError


@pytest.mark.asyncio
async def test_sync_continues_after_gateway_refusal_and_skips_retired(monkeypatch):
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    if not url.endswith("/mc_audit"):
        pytest.fail("Refusing non-disposable database: expected /mc_audit")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))

    attempted: list[str] = []

    async def fake_apply(self, *, agent, **_kwargs):
        attempted.append(agent.name)
        if agent.name == "Bad":
            raise OpenClawGatewayError("Gateway refused: legacy workspace needs maintenance")

    monkeypatch.setattr(provisioning.OpenClawGatewayProvisioner, "apply_agent_lifecycle", fake_apply)
    monkeypatch.setattr(provisioning_db.OpenClawGatewayControlPlane, "health", AsyncMock(return_value={}))
    monkeypatch.setattr(provisioning_db, "_get_existing_auth_token", AsyncMock(return_value="tok"))

    org, gw, bid = uuid4(), uuid4(), uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = User(clerk_user_id=f"u-{uuid4().hex}", email="o@x.invalid", name="Owner")
        db.add(user)
        db.add(Organization(id=org, name="sync"))
        await db.flush()
        gateway = Gateway(id=gw, organization_id=org, name="g", url="ws://fake.invalid/ws",
                          workspace_root="/tmp/sync")
        db.add(gateway)
        await db.flush()
        db.add(Board(id=bid, organization_id=org, gateway_id=gw, name="b", slug=f"b-{bid.hex}"))
        await db.flush()
        for name, status in (("Bad", "online"), ("Good", "online"), ("Old", "retired")):
            db.add(Agent(name=name, board_id=bid, gateway_id=gw, status=status))
        await db.commit()

        result = await provisioning_db.OpenClawProvisioningService(db).sync_gateway_templates(
            gateway,
            options=provisioning_db.GatewayTemplateSyncOptions(user=user, include_main=False),
        )

    await engine.dispose()
    assert "Old" not in attempted  # retired agents are not re-provisioned
    assert attempted.count("Good") == 1  # the sync carried on past the failure
    assert result.agents_updated == 1
    assert [e.agent_name for e in result.errors] == ["Bad"]
