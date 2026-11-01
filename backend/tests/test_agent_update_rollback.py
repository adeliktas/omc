"""Gateway provisioning failure must not revoke a live agent's token."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.services.openclaw.gateway_rpc import OpenClawGatewayError
from app.services.openclaw.lifecycle_orchestrator import AgentLifecycleOrchestrator
from app.services.openclaw.provisioning import OpenClawGatewayProvisioner


@pytest.mark.asyncio
async def test_failed_update_rolls_back_token_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    org_id = uuid4()
    gateway = Gateway(id=uuid4(), organization_id=org_id, name="test", url="ws://example.test", workspace_root="/tmp")
    board = Board(id=uuid4(), organization_id=org_id, gateway_id=gateway.id, name="test", slug="test")
    agent = Agent(id=uuid4(), organization_id=org_id, board_id=board.id, gateway_id=gateway.id, name="PM", status="online", agent_token_hash="original", identity_profile={"role": "Generalist"})

    async def reject(self: OpenClawGatewayProvisioner, **kwargs: object) -> None:
        raise OpenClawGatewayError("missing scope: operator.admin")

    monkeypatch.setattr(OpenClawGatewayProvisioner, "apply_agent_lifecycle", reject)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add_all([gateway, board, agent])
            await session.commit()
            agent.agent_token_hash = "unpublished-new-token"
            agent.identity_profile = {"role": "Project Manager"}
            with pytest.raises(HTTPException, match="Gateway update failed"):
                await AgentLifecycleOrchestrator(session).run_lifecycle(
                    gateway=gateway, agent_id=agent.id, board=board,
                    user=None, action="update", auth_token="raw-new-token",
                    wake=False, raise_gateway_errors=True,
                )
            await session.refresh(agent)
            assert agent.agent_token_hash == "original"
            assert agent.identity_profile == {"role": "Generalist"}
            assert agent.status == "online"
            assert agent.provision_action is None
    finally:
        await engine.dispose()
