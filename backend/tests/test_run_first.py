"""Run-first contracts: real create route, lane review ownership and board readiness."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import importlib.util
import pytest
from fastapi import HTTPException

from app.api import tasks as tasks_api, review as review_api, board_onboarding
from app.db import session as db_session
from app.models.agents import Agent
from app.models.board_onboarding import BoardOnboardingSession
from app.models.tasks import Task
from app.schemas.tasks import TaskCreate
from app.schemas.board_onboarding import BoardOnboardingConfirm
from tests.test_team_task_mode import world  # noqa: F401
from tests.test_team_pool import ROOT


async def test_normal_creation_routes_and_lead_fallback(world, monkeypatch):
    session, board, _, actor = world

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(db_session, "async_session_maker", maker)
    monkeypatch.setattr(tasks_api, "_notify_lead_on_task_create", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(tasks_api, "_notify_agent_on_task_assign", notify)
    lead = Agent(
        board_id=board.id,
        gateway_id=board.gateway_id,
        name="Lead",
        is_board_lead=True,
        status="online",
        openclaw_session_id="agent:lead:main",
    )
    session.add(lead)
    await session.commit()
    auth = SimpleNamespace(user=actor.user)
    normal = await tasks_api.create_task(
        TaskCreate(title="Write an operations note", complexity=1),
        board=board,
        session=session,
        auth=auth,
    )
    assert normal.execution_lane == "agent" and normal.assigned_agent_id == lead.id
    assert notify.await_count == 1
    code = await tasks_api.create_task(
        TaskCreate(title="Fix code", complexity=1, execution_lane="harness"),
        board=board,
        session=session,
        auth=auth,
    )
    assert code.execution_mode == "team" and code.assigned_agent_id is None
    legacy = await tasks_api.create_task(
        TaskCreate(
            title="Explicit legacy", complexity=1, execution_lane="harness", execution_mode="single"
        ),
        board=board,
        session=session,
        auth=auth,
    )
    assert legacy.execution_mode == "single"
    (await session.get(Task, normal.id)).status = "review"
    (await session.get(Task, code.id)).status = "review"
    await session.commit()
    candidates = await review_api._candidate_tasks(session, board.organization_id)
    assert normal.id not in {t.id for t in candidates} and code.id in {t.id for t in candidates}


async def test_board_not_confirmed_after_provisioning_failure_then_retry(world, monkeypatch):
    session, board, _, actor = world
    onboarding = BoardOnboardingSession(
        board_id=board.id, session_key="board-onboarding", status="active", draft_goal={}
    )
    session.add(onboarding)
    await session.commit()
    gateway = SimpleNamespace(id=board.gateway_id)
    monkeypatch.setattr(
        board_onboarding.GatewayDispatchService,
        "require_gateway_config_for_board",
        AsyncMock(return_value=(gateway, SimpleNamespace())),
    )
    provision = AsyncMock(side_effect=HTTPException(502, "gateway unavailable"))
    monkeypatch.setattr(
        board_onboarding.OpenClawProvisioningService, "ensure_board_lead_agent", provision
    )
    payload = BoardOnboardingConfirm(
        board_type="general", objective="Finish ordinary tasks", success_metrics={}
    )
    with pytest.raises(HTTPException):
        await board_onboarding.confirm_onboarding(
            payload, board=board, session=session, auth=SimpleNamespace(user=actor.user)
        )
    await session.refresh(board)
    await session.refresh(onboarding)
    assert not board.goal_confirmed and onboarding.status == "provisioning"
    provision.side_effect = None
    provision.return_value = (SimpleNamespace(), True)
    await board_onboarding.confirm_onboarding(
        payload, board=board, session=session, auth=SimpleNamespace(user=actor.user)
    )
    assert board.goal_confirmed and onboarding.status == "confirmed"


@pytest.mark.parametrize("retired", [False, True])
async def test_partial_lead_provisioning_retries_same_identity_but_retired_stays_off(
    world, monkeypatch, retired
):
    from app.models.gateways import Gateway
    from app.services.openclaw import provisioning_db as svc

    session, board, _, actor = world
    lead = Agent(
        board_id=board.id,
        gateway_id=board.gateway_id,
        name="Lead Agent",
        is_board_lead=True,
        status="retired" if retired else "provisioning",
        last_provision_error="gateway unavailable",
    )
    session.add(lead)
    await session.commit()
    run = AsyncMock(return_value=lead)
    monkeypatch.setattr(svc.AgentLifecycleOrchestrator, "run_lifecycle", run)
    request = svc.LeadAgentRequest(
        board=board,
        gateway=await session.get(Gateway, board.gateway_id),
        config=SimpleNamespace(),
        user=actor.user,
        options=svc.LeadAgentOptions(action="provision"),
    )
    if retired:
        with pytest.raises(HTTPException, match="retired"):
            await svc.OpenClawProvisioningService(session).ensure_board_lead_agent(request=request)
        assert run.await_count == 0 and lead.status == "retired"
    else:
        result, created = await svc.OpenClawProvisioningService(session).ensure_board_lead_agent(
            request=request
        )
        assert result.id == lead.id and not created
        assert run.await_args.kwargs["agent_id"] == lead.id
        assert run.await_args.kwargs["reset_session"] is False


def test_all_ready_boards_keep_task_board_binding(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "run_first_pool", ROOT / "scripts/mc-team-pool.py"
    )
    pool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pool)
    monkeypatch.setattr(pool, "BOARD", "*")

    def rows(path):
        if path == "/api/v1/boards":
            return [
                {"id": "ready", "goal_confirmed": True},
                {"id": "not-ready", "goal_confirmed": False},
            ]
        assert path == "/api/v1/boards/ready/tasks"
        return [{"id": "owned", "board_id": "ready"}, {"id": "foreign", "board_id": "other"}]

    monkeypatch.setattr(pool, "fetch_rows", rows)
    assert pool.fetch_tasks() == [{"id": "owned", "board_id": "ready"}]
    calls = []
    monkeypatch.setattr(pool.subprocess, "Popen", lambda args, **kw: calls.append(kw["env"]))
    monkeypatch.setattr(pool, "log", lambda *_: None)
    pool.spawn("owned", "ready")
    assert calls[0]["RUNNER_BOARD"] == "ready"
