"""Synthetic controller effects only; no Gateway, model or credential access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from app.models.agents import Agent
from app.services.openclaw import db_agent_state as state
from app.services.openclaw import gateway_rpc as rpc
from app.services.openclaw import lifecycle_orchestrator as lifecycle
from app.services.openclaw import provisioning as provision
from app.services.openclaw import provisioning_db
from app.schemas.agents import AgentUpdate

BOARD = UUID("00000000-0000-4000-8000-00000000b0a7")
LEAD = f"lead-{BOARD}"
KEY = f"agent:{LEAD}:main"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,params",
    [
        ("chat.send", {"sessionKey": KEY, "message": "synthetic private fixture"}),
        ("agent", {"agentId": LEAD, "message": "synthetic private fixture"}),
        ("agents.create", {"name": LEAD}),
        ("sessions.compact", {"key": KEY}),
        ("chat.history", {"sessionKey": KEY}),
        ("sessions.preview", {"keys": ["agent:other:main", KEY]}),
        ("sessions.preview", {"keys": ("agent:other:main", KEY)}),
        ("agents.files.get", {"agentId": LEAD, "name": "MEMORY.md"}),
        ("chat.send", {"sessionKey": f"  AGENT:{LEAD.upper()}:MAIN  "}),
    ],
)
async def test_current_lead_refused_before_transport(monkeypatch, method, params):
    transport = AsyncMock(return_value={"synthetic": True})
    monkeypatch.setattr(rpc, "_openclaw_call_once", transport)
    monkeypatch.setattr(
        rpc, "_build_gateway_url", lambda _: pytest.fail("credential path reached")
    )
    with pytest.raises(rpc.OpenClawGatewayError, match="Private-board hold"):
        await rpc.openclaw_call(
            method, params, config=rpc.GatewayConfig(url="synthetic-only")
        )
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_non_private_prompt_prose_and_stop_maintenance_unchanged(monkeypatch):
    transport = AsyncMock(return_value={"synthetic": True})
    monkeypatch.setattr(rpc, "_openclaw_call_once", transport)
    config = rpc.GatewayConfig(url="synthetic-only")
    for method, params in [
        ("chat.send", {"sessionKey": "agent:other:main", "message": KEY}),
        ("chat.abort", {"sessionKey": KEY}),
        ("config.get", None),
    ]:
        assert await rpc.openclaw_call(method, params, config=config) == {
            "synthetic": True
        }
    assert transport.await_count == 3


@pytest.mark.parametrize("board_id,session_key", [(BOARD, None), (None, KEY)])
def test_token_rotation_refused_before_generation_or_state_change(
    monkeypatch, board_id, session_key
):
    agent = Agent(
        name="synthetic",
        board_id=board_id,
        openclaw_session_id=session_key,
        agent_token_hash="existing-fixture-hash",
    )
    before = agent.model_dump()
    monkeypatch.setattr(
        state, "generate_agent_token", lambda: pytest.fail("token generated")
    )
    with pytest.raises(HTTPException) as refused:
        state.mint_agent_token(agent)
    assert refused.value.status_code == 409
    assert agent.model_dump() == before


def test_unrelated_token_generation_unchanged(monkeypatch):
    agent = Agent(name="synthetic", board_id=uuid4())
    monkeypatch.setattr(state, "generate_agent_token", lambda: "synthetic-token")
    monkeypatch.setattr(state, "hash_agent_token", lambda _: "synthetic-hash")
    assert state.mint_agent_token(agent) == "synthetic-token"
    assert agent.agent_token_hash == "synthetic-hash"


@pytest.mark.asyncio
async def test_explicit_token_lifecycle_refused_before_db_effects(monkeypatch):
    agent = Agent(name="synthetic", board_id=BOARD)
    before = agent.model_dump()

    def effect(*_, **__):
        pytest.fail("lifecycle state or DB effect reached")

    session = SimpleNamespace(add=effect, flush=effect)
    service = lifecycle.AgentLifecycleOrchestrator(session)
    monkeypatch.setattr(service, "_lock_agent", AsyncMock(return_value=agent))
    with pytest.raises(HTTPException) as refused:
        await service.run_lifecycle(
            gateway=SimpleNamespace(url="synthetic-only"),
            agent_id=agent.id,
            board=SimpleNamespace(id=BOARD),
            user=None,
            action="update",
            auth_token="synthetic-existing-token",
        )
    assert refused.value.status_code == 409
    assert agent.model_dump() == before


@pytest.mark.asyncio
async def test_direct_provision_refused_before_gateway(monkeypatch):
    agent = Agent(name="synthetic", board_id=BOARD, openclaw_session_id=KEY)
    monkeypatch.setattr(
        provision,
        "_control_plane_for_gateway",
        lambda _: pytest.fail("Gateway provision reached"),
    )
    with pytest.raises(HTTPException) as refused:
        await provision.OpenClawGatewayProvisioner().apply_agent_lifecycle(
            agent=agent,
            gateway=SimpleNamespace(url="synthetic-only"),
            board=SimpleNamespace(id=BOARD),
            auth_token="synthetic-token",
            user=None,
        )
    assert refused.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("board_id", [BOARD, None])
async def test_generic_heartbeat_sync_does_not_overwrite_held_agent(
    monkeypatch, board_id
):
    stopped = Agent(
        name="synthetic stopped", board_id=board_id, openclaw_session_id=KEY
    )
    other = Agent(
        name="synthetic other", board_id=uuid4(), openclaw_session_id="agent:other:main"
    )
    patch = AsyncMock()
    monkeypatch.setattr(provision, "_patch_gateway_agent_heartbeats", patch)
    await provision.OpenClawGatewayProvisioner().sync_gateway_agent_heartbeats(
        SimpleNamespace(workspace_root="/tmp/synthetic-only"), [stopped, other]
    )
    assert patch.await_count == 1
    kwargs = patch.await_args.kwargs
    assert [row[0] for row in kwargs["entries"]] == ["other"]
    assert set(kwargs["tool_by_id"]) == {"other"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_board,changes,force",
    [
        (BOARD, {"name": "synthetic renamed"}, False),
        (BOARD, {}, True),
        (uuid4(), {"board_id": BOARD}, False),
        (BOARD, {"board_id": uuid4()}, False),
    ],
)
async def test_agent_update_refused_before_mutation(
    monkeypatch, current_board, changes, force
):
    agent = Agent(
        name="synthetic", board_id=current_board, agent_token_hash="fixture-existing"
    )
    before = agent.model_dump()
    manager = SimpleNamespace(
        by_id=lambda _: SimpleNamespace(first=AsyncMock(return_value=agent))
    )
    monkeypatch.setattr(Agent, "objects", manager)
    service = provisioning_db.AgentLifecycleService(SimpleNamespace())
    monkeypatch.setattr(service, "require_agent_access", AsyncMock())
    monkeypatch.setattr(service, "validate_agent_update_inputs", AsyncMock())
    mutate = AsyncMock(side_effect=AssertionError("mutation reached"))
    monkeypatch.setattr(service, "apply_agent_update_mutations", mutate)
    options = SimpleNamespace(force=force, context=SimpleNamespace(), user=None)
    with pytest.raises(HTTPException) as refused:
        await service.update_agent(
            agent_id=str(agent.id), payload=AgentUpdate(**changes), options=options
        )
    assert refused.value.status_code == 409
    assert agent.model_dump() == before
    mutate.assert_not_called()


@pytest.mark.asyncio
async def test_lead_ensure_refuses_before_query_or_repair():
    def query(*_, **__):
        pytest.fail("lead metadata query/repair reached")

    service = provisioning_db.OpenClawProvisioningService(SimpleNamespace(exec=query))
    with pytest.raises(HTTPException) as refused:
        await service.ensure_board_lead_agent(
            request=SimpleNamespace(board=SimpleNamespace(id=BOARD))
        )
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_direct_heartbeat_writer_refuses_before_config_read(monkeypatch):
    query = AsyncMock(side_effect=AssertionError("Gateway config reached"))
    monkeypatch.setattr(provision, "_gateway_config_agent_list", query)
    plane = provision.OpenClawGatewayControlPlane(
        rpc.GatewayConfig(url="synthetic-only")
    )
    with pytest.raises(rpc.OpenClawGatewayError, match="Private-board hold"):
        await plane.patch_agent_heartbeats(
            [(LEAD, "/tmp/synthetic-only", {"every": "24h"})]
        )
    query.assert_not_called()
