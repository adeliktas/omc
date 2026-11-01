"""Historical Inbox fixtures only; no protected source, consent or real lifecycle.

These IDs no longer describe the current owner-authorized native parents. All
stores and titles below are synthetic. Reads must explain refusal, not convert
or claim either historical single-Qwen configuration.
"""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi_pagination import add_pagination
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from app.api import tasks as tasks_api
from app.core import private_access
from app.core.auth import AuthContext
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.approvals import Approval
from app.models.provider_capacity import ProviderCapacity
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from app.schemas.tasks import TaskCreate, TaskUpdate
from app.services import mc_sidecar, model_ranking, task_splitter, team_cycle
from app.services.board_snapshot import build_board_snapshot
from app.services.task_execution import task_execution_state
from tests.test_team_task_mode import world  # noqa: F401

FIXTURE_IDS = (
    UUID("00000000-0000-4000-8000-0000000000c1"),
    UUID("00000000-0000-4000-8000-0000000000c2"),
)
PIN = ("omniroute-api", "vllm/Qwen3.8-27B-FP8")
PROFILE = {
    "level": 3,
    "rank": 0,
    "max_complexity": 3,
    "max_concurrency": 1,
    "fully_trusted": True,
    "write_enabled": False,
    "disabled": False,
    "cost_class": "local",
}


@pytest.fixture(autouse=True)
def isolated_collaborators(monkeypatch):
    # Exercise real ranking logic but never read production telemetry/sidecar,
    # invoke access approval, call a model or dispatch a worker.
    data = {"models": {PIN[0]: {PIN[1]: copy.deepcopy(PROFILE)}}}
    monkeypatch.setattr(mc_sidecar, "load_sidecar", lambda: copy.deepcopy(data))
    monkeypatch.setattr(model_ranking, "compute_usage", lambda *_a, **_kw: [])
    monkeypatch.setattr(task_splitter, "should_auto_split", lambda _task: False)

    def no_access(*_a, **_kw):
        pytest.fail("an admission diagnostic must never inspect protected access/approval state")

    monkeypatch.setattr(private_access, "require_jira_access", no_access)
    return data


def historical_task(task_id, board_id):
    return Task(
        id=task_id,
        board_id=board_id,
        title="Synthetic historical routing fixture",
        description=None,
        status="inbox",
        complexity=3,
        execution_lane="harness",
        execution_mode="single",
        harness_provider=PIN[0],
        harness_model=PIN[1],
        workspace_path=None,
        assigned_agent_id=None,
        in_progress_at=None,
        max_seconds_budget=1800,
        max_tokens_budget=None,
    )


async def test_both_historical_fixtures_get_same_visible_refusal_in_all_read_consumers(
    world,
    monkeypatch,
    isolated_collaborators,
):
    session, board, _unused, actor = world
    tasks = [historical_task(task_id, board.id) for task_id in FIXTURE_IDS]
    session.add_all(tasks)
    await session.commit()
    before = {task.id: task.model_dump() for task in tasks}
    before_profile = copy.deepcopy(isolated_collaborators)
    events = [
        ActivityEvent(
            board_id=board.id,
            task_id=task.id,
            event_type="task.updated",
            message="Synthetic technical metadata",
        )
        for task in tasks
    ]
    session.add_all(events)
    await session.commit()
    page = await tasks_api._task_read_page(session=session, board_id=board.id, tasks=tasks)
    snapshot = await build_board_snapshot(session, board)
    cards = {card.id: card for card in snapshot.tasks}
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/v1")
    add_pagination(app)

    async def synthetic_db():
        yield session

    app.dependency_overrides[tasks_api.get_session] = synthetic_db
    app.dependency_overrides[tasks_api.get_board_for_actor_read] = lambda: board
    app.dependency_overrides[tasks_api.require_user_or_agent] = lambda: actor
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        response = await client.get(f"/api/v1/boards/{board.id}/tasks")
    assert response.status_code == 200, response.text
    api_cards = {card["id"]: card for card in response.json()["items"]}
    details = [
        await tasks_api._task_read_response(session, task=task, board_id=board.id) for task in tasks
    ]

    @asynccontextmanager
    async def synthetic_session():
        yield session

    async def synthetic_events(*_a, **_kw):
        return list(zip(events, tasks))

    class Connected:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(tasks_api, "async_session_maker", synthetic_session)
    monkeypatch.setattr(tasks_api, "_fetch_task_events", synthetic_events)
    stream = tasks_api._task_event_generator(
        request=Connected(),
        board_id=board.id,
        since_dt=utcnow() - timedelta(seconds=1),
    )
    try:
        for task, listed, detailed in zip(tasks, page, details):
            payload = json.loads((await anext(stream))["data"])
            streamed = payload["task"]
            reason = listed.work_detail
            assert api_cards[str(task.id)]["work_detail"] == reason
            assert api_cards[str(task.id)]["work_state"] is None
            assert api_cards[str(task.id)]["status"] == "inbox"
            assert (
                reason
                == detailed.work_detail
                == cards[task.id].work_detail
                == streamed["work_detail"]
            )
            assert payload["execution_updates"][str(task.id)]["work_detail"] == reason
            for text in (
                "unsupported single executor",
                "read-only",
                "provider_unregistered",
                "accounting",
                "qualified runtime",
                "access/disclosure approvals",
                "fully_trusted grants neither writes nor disclosure",
                "no fallback",
            ):
                assert text in reason
            assert (
                listed.status
                == detailed.status
                == cards[task.id].status
                == streamed["status"]
                == "inbox"
            )
            assert listed.work_state is detailed.work_state is cards[task.id].work_state is None
            assert streamed["work_state"] is None
            assert listed.work_started_at is listed.work_expires_at is None
            assert (
                listed.blocked_by_task_ids
                == detailed.blocked_by_task_ids
                == streamed["blocked_by_task_ids"]
                == []
            )
            assert not listed.is_blocked  # diagnostic refusal is not a dependency or lifecycle edit
            for read in (listed, detailed, cards[task.id]):
                for key, value in before[task.id].items():
                    if hasattr(read, key):
                        assert getattr(read, key) == value, key
            assert PROFILE["fully_trusted"] is True and PROFILE["write_enabled"] is False
            assert not team_cycle.is_backlog_task(task)
            # Existing native admission cannot reinterpret the single fixture.
            with pytest.raises(team_cycle.TeamError, match="not in team mode"):
                await team_cycle.start_cycle(
                    session, board, task, await model_ranking.get_board([])
                )
    finally:
        await stream.aclose()
    for task in tasks:
        await session.refresh(task)
        assert task.model_dump() == before[task.id]
    assert isolated_collaborators == before_profile
    for model in (Approval, TeamAttempt, TeamCycle, ProviderCapacity):
        assert (await session.exec(select(model))).all() == []


async def test_historical_profile_ranking_is_not_writer_reviewer_or_native_engine():
    ranking = await model_ranking.get_board([])
    entry = team_cycle._entries(ranking)[0]
    for key, expected in PROFILE.items():
        assert entry[key] == expected
    assert (entry["provider"], entry["model"]) == PIN  # no splitting/normalizing the model ID
    assert entry["execution"] == "model_endpoint"
    assert entry["eligible"] is False and entry["block_reason"] == "provider_unregistered"
    assert entry["usage"] == []
    assert not team_cycle.write_engine_allowed(ranking, *PIN)
    assert not team_cycle.engine_supported(*PIN)
    from app.services.owner_reviews import is_reviewer_engine

    assert not is_reviewer_engine(*PIN)
    with pytest.raises(team_cycle.TeamError, match="not a supported harness engine"):
        team_cycle.validate_engine(ranking, *PIN)


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, "team"),
        ({"execution_mode": "single"}, "single"),
        ({"execution_mode": "team"}, "team"),
        ({"workspace_path": "/synthetic/workspace"}, "single"),
    ],
)
async def test_create_omitted_vs_explicit_mode_and_edit_preserve_choice(world, fields, expected):
    session, board, _unused, actor = world
    payload = TaskCreate(
        title="Synthetic create parity",
        complexity=3,
        harness_provider=PIN[0],
        harness_model=PIN[1],
        max_seconds_budget=1800,
        **fields,
    )
    created = await tasks_api.create_task(
        payload, board=board, session=session, auth=AuthContext(actor_type="user", user=actor.user)
    )
    assert created.execution_lane == "harness" and created.execution_mode == expected
    assert created.status == "inbox" and created.work_state is None
    task = await session.get(Task, created.id)
    patched = await tasks_api.update_task(
        TaskUpdate(title="Synthetic title edit"), task=task, session=session, actor=actor
    )
    assert patched.execution_mode == expected
    assert patched.harness_provider == PIN[0] and patched.harness_model == PIN[1]
    assert patched.work_detail == created.work_detail
    assert patched.workspace_path == fields.get("workspace_path")
    assert await team_cycle.active_cycle(session, task.id) is None
    assert patched.max_seconds_budget == 1800 and patched.max_tokens_budget is None


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_lane": "agent"},
        {"workspace_path": "/synthetic/workspace"},
    ],
)
async def test_create_edit_team_validation_parity(world, changes):
    session, board, task, actor = world
    with pytest.raises(HTTPException) as create_error:
        await tasks_api.create_task(
            TaskCreate(
                **{
                    "title": "Synthetic invalid team",
                    "execution_mode": "team",
                    "execution_lane": "harness",
                    **changes,
                }
            ),
            board=board,
            session=session,
            auth=AuthContext(actor_type="user"),
        )
    with pytest.raises(HTTPException) as edit_error:
        await tasks_api.update_task(
            TaskUpdate(execution_mode="team", **changes), task=task, session=session, actor=actor
        )
    assert create_error.value.status_code == edit_error.value.status_code == 422
    assert create_error.value.detail == edit_error.value.detail


async def test_valid_native_harness_team_no_workspace_stays_inbox_until_real_claim(world):
    session, board, task, actor = world
    created = await tasks_api.create_task(
        TaskCreate(
            title="Synthetic native configuration",
            complexity=3,
            execution_lane="harness",
            execution_mode="team",
            harness_provider="omniroute-codex",
            harness_model="gpt-6.1-sol",
        ),
        board=board,
        session=session,
        auth=AuthContext(actor_type="user", user=actor.user),
    )
    assert created.execution_mode == "team" and created.execution_lane == "harness"
    assert created.workspace_path is None and created.assigned_agent_id is None
    assert created.status == "inbox" and created.work_detail is created.work_state is None
    edited = await tasks_api.update_task(
        TaskUpdate(execution_mode="team"), task=task, session=session, actor=actor
    )
    assert edited.execution_mode == "team" and edited.workspace_path is None
    stored = await session.get(Task, created.id)
    await session.refresh(board)
    with pytest.raises(team_cycle.TeamError, match="only for claimed"):
        await team_cycle.start_cycle(session, board, stored, {})
    assert await team_cycle.active_cycle(session, stored.id) is None


@pytest.mark.parametrize(
    "provider,model,execution",
    [
        ("omniroute-codex", "wrong-model", "agent_harness"),
        (*PIN, "model_endpoint"),
    ],
)
def test_existing_native_validator_refuses_wrong_model_or_unsupported_executor(
    provider, model, execution
):
    ranking = {
        "levels": [
            {
                "entries": [
                    {
                        "provider": provider,
                        "model": model,
                        "execution": execution,
                        "eligible": True,
                        "write_enabled": False,
                    }
                ]
            }
        ]
    }
    with pytest.raises(team_cycle.TeamError, match="not a supported harness engine"):
        team_cycle.validate_engine(ranking, provider, model)


async def test_restored_provider_registration_does_not_authorize_single_or_disclosure(world):
    session, board, _task, _actor = world
    task = historical_task(FIXTURE_IDS[0], board.id)
    session.add(ProviderCapacity(provider_name=PIN[0], status="online"))
    await session.commit()
    before = task.model_dump()
    state = (await task_execution_state(session, board_id=board.id, tasks=[task]))[task.id]
    assert "unsupported single executor" in state["work_detail"]
    assert "read-only engine" in state["work_detail"]
    assert "provider_unregistered" not in state["work_detail"]
    assert state["work_state"] is None and task.model_dump() == before


@pytest.mark.parametrize("failure", ["unavailable", "unregistered-model"])
async def test_missing_accounting_readback_or_model_remains_actionable_unknown(
    world, monkeypatch, failure
):
    session, board, _task, _actor = world
    task = historical_task(FIXTURE_IDS[0], board.id)
    before = task.model_dump()
    if failure == "unavailable":

        async def unavailable(_rows):
            raise OSError("private exception text must not be projected")

        monkeypatch.setattr(model_ranking, "get_board", unavailable)
    else:
        task.harness_model = "vllm/wrong-model"
        before = task.model_dump()
    state = (await task_execution_state(session, board_id=board.id, tasks=[task]))[task.id]
    assert state["work_state"] is state["work_started_at"] is state["work_expires_at"] is None
    assert "private exception" not in state["work_detail"]
    assert "no fallback" in state["work_detail"]
    assert ("UNKNOWN" if failure == "unavailable" else "not registered in ranking") in state[
        "work_detail"
    ]
    assert task.model_dump() == before


@pytest.mark.parametrize(
    "mode,lane,status,pinned",
    [
        ("team", "harness", "inbox", True),
        ("single", "agent", "inbox", True),
        ("single", "harness", "review", True),
        ("single", "harness", "in_progress", True),
        ("single", "harness", "inbox", False),
    ],
)
async def test_unrelated_work_projection_does_not_require_ranking(
    world, monkeypatch, mode, lane, status, pinned
):
    session, board, _task, _actor = world
    task = historical_task(FIXTURE_IDS[0], board.id)
    task.execution_mode, task.execution_lane, task.status = mode, lane, status
    if not pinned:
        task.harness_provider = task.harness_model = None

    async def forbidden(_rows):
        pytest.fail("unrelated work must not acquire an Inbox diagnostic or read ranking")

    monkeypatch.setattr(model_ranking, "get_board", forbidden)
    state = (await task_execution_state(session, board_id=board.id, tasks=[task]))[task.id]
    assert state["work_state"] == ("waiting" if status == "in_progress" else None)
    assert state["work_detail"] == ("Worker activity UNKNOWN" if status == "in_progress" else None)


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"disabled": True}, "disabled or unavailable"),
        ({"max_complexity": 2}, "complexity ceiling"),
    ],
)
async def test_other_pinned_refusals_are_visible_without_policy_changes(
    world, isolated_collaborators, change, expected
):
    session, board, _task, _actor = world
    isolated_collaborators["models"][PIN[0]][PIN[1]].update(change)
    task = historical_task(FIXTURE_IDS[0], board.id)
    # Technical provider registration is not authority for the read-only model.
    session.add(ProviderCapacity(provider_name=PIN[0], status="online"))
    await session.commit()
    before = task.model_dump()
    state = (await task_execution_state(session, board_id=board.id, tasks=[task]))[task.id]
    assert expected in state["work_detail"]
    assert state["work_state"] is None and task.model_dump() == before


async def test_supported_single_configuration_is_not_a_positive_claim_or_readonly_refusal(
    world, isolated_collaborators
):
    session, board, task, _actor = world
    isolated_collaborators["models"] = {
        "claude-code": {"sonnet": {"level": 1, "write_enabled": True}}
    }
    task.harness_provider, task.harness_model = "claude-code", "sonnet"
    before = task.model_dump()
    state = (await task_execution_state(session, board_id=board.id, tasks=[task]))[task.id]
    assert state["work_detail"] is state["work_state"] is None
    assert state["work_started_at"] is state["work_expires_at"] is None
    assert task.model_dump() == before
