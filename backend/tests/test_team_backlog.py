"""[backlog] board-backlog manager: bounded native PM -> board DAG children.

Covers the '[backlog]' title-prefix opt-in contract: only the top-level PM
attempt of an opted-in native team task may return a bounded subtasks plan;
the host completes that attempt and atomically materializes team/harness
board children + dependency edges; the parent cycle enters the truthful
terminal 'delegated' state and is not runnable; native split parents never
complete by majority and reach Review only when every child (count-checked)
is Done. Disposable SQLite only; no provider/credential/production writes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import ActorContext, get_board_for_user_write, require_user_auth
from app.api.tasks import router as tasks_router
from app.db.session import get_session
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.task_dependencies import TaskDependency
from app.models.tasks import Task
from app.services import team_cycle as svc
from app.services import watchdog_loop as wl

BASE = "a" * 40


def entry(provider, model, level=1, **kw):
    return {
        "provider": provider,
        "model": model,
        "level": level,
        "rank": 0,
        "provider_priority": 0,
        "max_complexity": 5,
        "execution": "agent_harness",
        "eligible": True,
        "disabled": False,
        "cost_class": "free",
        **kw,
    }


RANKING = {
    "max_level_for_complexity": {"1": 5, "2": 4, "3": 3, "4": 2, "5": 1},
    "levels": [{"level": 1, "entries": [entry("claude-code", "sonnet")]}],
}

BACKLOG_PLAN = {
    "summary": "split the epic",
    "subtasks": [
        {
            "title": "Implement API half",
            "description": "Cover TODO T-001/T-003 in backend/app/api; acceptance: targeted pytest passes",
            "deps": [],
        },
        {
            "title": "Implement UI half",
            "description": "Cover TODO T-002 in frontend/src; acceptance: vitest passes",
            "deps": [0],
        },
    ],
}


def backlog_artifact(plan: dict | None = None) -> str:
    return "```json\n" + json.dumps(plan or BACKLOG_PLAN) + "\n```"


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(id=uuid4(), name="o")
        session.add(org)
        gw = Gateway(
            id=uuid4(),
            organization_id=org.id,
            name="g",
            url="https://g.local",
            workspace_root="/tmp/w",
        )
        session.add(gw)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id)
        session.add(board)
        task = Task(
            id=uuid4(),
            board_id=board.id,
            title="[backlog] ship the epic",
            status="in_progress",
            execution_lane="harness",
            execution_mode="team",
            complexity=3,
            thinking_level="low",
            harness_provider="claude-code",
            harness_model="opus-5",
            max_seconds_budget=1800,
            repeat_enabled=False,
        )
        session.add(task)
        await session.commit()
        yield session, board, task
    await engine.dispose()


async def run_backlog_pm(session, board, task, artifact=backlog_artifact()):
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    got = await svc.claim_next(session, cycle, base_sha=BASE)
    assert got
    pm, token = got
    await session.commit()
    await svc.complete_attempt(
        session, board, cycle, pm, token, ok=True, artifact=artifact, ranking=RANKING
    )
    await session.commit()
    return cycle, pm, token


def test_backlog_pm_brief_has_no_worker_plan_bounds():
    board = uuid4()
    task = Task(id=uuid4(), board_id=board, title="[backlog] open issues", execution_mode="team")
    cycle = svc.TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = svc.TeamMember(id=uuid4(), board_id=board, name="pm", role_kind="pm",
                            provider="omniroute-codex", model="gpt-6.1-sol")
    attempt = svc.TeamAttempt(id=uuid4(), board_id=board, task_id=task.id,
                              cycle_id=cycle.id, member_id=member.id,
                              role_kind="pm", stage_key="pm")

    brief = svc.brief(task, cycle, attempt, member, None)
    assert '"subtasks"' in brief
    assert "Worker-plan bounds" not in brief
    assert "no overlapping owners" not in brief


# ── happy path: manager plan -> persisted team/harness DAG children ────────


async def test_backlog_plan_materializes_team_children_and_delegates(env):
    session, board, task = env
    cycle, pm, _ = await run_backlog_pm(session, board, task)

    assert cycle.state == "delegated"
    assert cycle.state not in svc.ACTIVE_CYCLE_STATES  # truthful terminal state
    assert pm.status == "succeeded"
    assert task.status == "in_progress"  # parent tracks children, never fabricated Done
    assert task.split_count == 2 and task.split_type == "dag"

    children = list(
        await session.exec(
            select(Task).where(Task.parent_task_id == task.id).order_by(Task.split_position)
        )
    )
    assert [c.title for c in children] == ["Implement API half", "Implement UI half"]
    for c in children:
        assert c.execution_mode == "team" and c.execution_lane == "harness"
        assert c.harness_provider == "claude-code" and c.harness_model == "opus-5"
        assert c.thinking_level == "low"
        assert c.max_seconds_budget == 1800
        assert c.status == "inbox" and c.assigned_agent_id is None
        assert c.description and "TODO" in c.description  # actionable, not optional filler
    edges = list(
        await session.exec(select(TaskDependency).where(TaskDependency.board_id == board.id))
    )
    assert {(e.task_id, e.depends_on_task_id) for e in edges} == {(children[1].id, children[0].id)}

    # No worker/QA stage was fabricated on the parent.
    assert all(
        a.role_kind == "pm" and a.stage_key == "pm" for a in await svc._attempts(session, cycle.id)
    )


async def test_empty_backlog_plan_settles_in_review_without_children(env):
    # 2026-10-24: the manager truthfully found only human-held issues; the
    # rejected empty list re-ran the same plan on 14 leads.
    session, board, task = env
    artifact = backlog_artifact({"summary": "all issues need live infrastructure", "subtasks": []})
    cycle, pm, _ = await run_backlog_pm(session, board, task, artifact=artifact)

    assert pm.status == "succeeded"
    assert cycle.state == "delegated"
    assert task.status == "review"  # not schedulable again, unlike an unsplit in_progress task
    assert task.split_count == 0
    assert not list(await session.exec(select(Task).where(Task.parent_task_id == task.id)))


async def test_delegated_parent_is_not_runnable_and_cannot_duplicate(env):
    session, board, task = env
    cycle, pm, token = await run_backlog_pm(session, board, task)

    # No further claims on the delegated cycle.
    assert await svc.claim_next(session, cycle, base_sha=BASE) is None
    # A new manager pass on the split parent is refused.
    with pytest.raises(svc.TeamError, match="split team parents are not runnable"):
        await svc.start_cycle(session, board, task, RANKING)
    # Replay of the completed attempt is fenced (stale claim).
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(
            session, board, cycle, pm, token, ok=True, artifact=backlog_artifact()
        )
    # Double materialization is refused at the shared boundary.
    from app.services.dag_materialize import DagValidationError, materialize_dag, Subtask

    with pytest.raises(DagValidationError, match="already has split children"):
        await materialize_dag(session, task, [Subtask("late", [])])


async def test_split_parent_is_not_pool_eligible():
    spec = importlib.util.spec_from_file_location(
        "mc_team_pool_backlog", str(Path(__file__).resolve().parents[2] / "scripts/mc-team-pool.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def team_task(**over):
        t = {
            "id": "x",
            "title": "[backlog] root",
            "status": "in_progress",
            "execution_mode": "team",
            "execution_lane": "harness",
            "is_blocked": False,
            "blocked_by_task_ids": [],
            "repeat_enabled": False,
            "repeat_interval_minutes": None,
            "updated_at": "2026-01-01T00:00:00",
        }
        t.update(over)
        return t

    assert mod.eligible(team_task(split_count=2, split_type="dag")) is False
    assert mod.eligible(team_task()) is True  # unsplit team task still runs


# ── shape guards: only the opted-in top-level PM may use the subtasks shape ─


async def test_normal_pm_cannot_select_the_subtasks_shape(env):
    session, board, task = env
    task.title = "normal team task"  # no [backlog] prefix
    await session.commit()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, token = await svc.claim_next(session, cycle, base_sha=BASE)
    await session.commit()
    await svc.complete_attempt(
        session, board, cycle, pm, token, ok=True, artifact=backlog_artifact(), ranking=RANKING
    )
    await session.commit()
    assert pm.status == "failed"
    assert task.split_count == 0  # nothing materialized from the wrong shape
    assert cycle.state == "planning"  # retried, not delegated


async def test_backlog_pm_cannot_return_a_workers_plan(env):
    session, board, task = env
    plan = {
        "summary": "s",
        "qa_checks": [],
        "workers": [
            {
                "key": "api",
                "role": "developer",
                "title": "API",
                "scope": ["backend/app"],
                "instructions": "do",
            },
        ],
    }
    cycle, pm, token = await run_backlog_pm(
        session, board, task, artifact="```json\n" + json.dumps(plan) + "\n```"
    )
    assert pm.status == "failed"
    assert cycle.state == "planning"  # retried, no delegation
    assert task.split_count == 0


# ── bounded plan validation ────────────────────────────────────────────────


def test_parse_backlog_plan_accepts_the_bounded_shape():
    plan = svc.parse_backlog_plan(backlog_artifact())
    assert plan["summary"] == "split the epic"
    assert [s.title for s in plan["items"]] == ["Implement API half", "Implement UI half"]


@pytest.mark.parametrize(
    "mutate,match",
    [
        ({"workers": []}, "must not mix workers"),
        ({"summary": " ", "subtasks": []}, "subtasks list"),  # empty needs a reason
        (
            {"subtasks": [{"title": "a", "description": "d", "deps": []}, {"title": "b"}]},
            "description is required",
        ),
        (
            {"subtasks": [{"title": "a", "description": "d", "deps": [], "scope": ["backend"]}]},
            "unsupported keys",
        ),
        (
            {"subtasks": [{"title": "a", "description": "d", "deps": [], "complexity": True}]},
            "complexity",
        ),
        ({"subtasks": [{"title": "a", "description": "d", "deps": [True]}]}, "deps"),
        ({"subtasks": [{"title": "a", "description": "d", "deps": [5]}]}, "out of range"),
        (
            {
                "subtasks": [
                    {"title": "a", "description": "d", "deps": [1]},
                    {"title": "b", "description": "d", "deps": [0]},
                ]
            },
            "cycle",
        ),
        (
            {"subtasks": [{"title": f"c{i}", "description": "d", "deps": []} for i in range(21)]},
            "child limit",
        ),
        (
            {
                "subtasks": [
                    {"title": f"c{i}", "description": "d", "deps": [] if i == 0 else [i - 1]}
                    for i in range(4)
                ]
            },
            "depth",
        ),
    ],
)
def test_parse_backlog_plan_rejects_malformed_or_unbounded(mutate, match):
    payload = {**BACKLOG_PLAN, **mutate}
    with pytest.raises(svc.TeamError, match=match):
        svc.parse_backlog_plan(backlog_artifact(payload))


def test_parse_plan_rejects_the_subtasks_key_for_normal_plans():
    mixed = {
        "workers": [
            {
                "key": "a",
                "role": "developer",
                "title": "A",
                "scope": ["backend"],
                "instructions": "do",
            }
        ],
        **BACKLOG_PLAN,
    }
    with pytest.raises(svc.TeamError, match="reserved for \\[backlog\\]"):
        svc.parse_plan("```json\n" + json.dumps(mixed) + "\n```")
    # subtasks-only (no workers) is equally refused for normal plans
    with pytest.raises(svc.TeamError, match="workers list"):
        svc.parse_plan(backlog_artifact())


async def test_backlog_ancestry_is_bounded(env):
    session, board, task = env
    # Chain MAX_BACKLOG_ANCESTRY [backlog] ancestors above this task.
    ancestor = None
    for i in range(svc.MAX_BACKLOG_ANCESTRY):
        row = Task(
            id=uuid4(),
            board_id=board.id,
            title=f"[backlog] ancestor {i}",
            status="in_progress",
            execution_lane="harness",
            execution_mode="team",
            parent_task_id=ancestor.id if ancestor else None,
        )
        session.add(row)
        await session.flush()
        ancestor = row
    task.parent_task_id = ancestor.id
    await session.commit()
    with pytest.raises(svc.TeamError, match="bounded"):
        await run_backlog_pm(session, board, task)
    assert task.split_count == 0


# ── watchdog truthfulness for native split parents ─────────────────────────


@pytest.fixture
async def wenv():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(id=uuid4(), name="o")
        session.add(org)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=uuid4())
        session.add(board)
        await session.commit()
        yield session, board
    await engine.dispose()


def native_parent(board, *, split_count=2, status="in_progress", parent_task_id=None):
    return Task(
        board_id=board.id,
        title="[backlog] epic",
        status=status,
        execution_lane="harness",
        execution_mode="team",
        split_count=split_count,
        split_type="dag",
        parent_task_id=parent_task_id,
    )


def child(board, parent, status, title="c"):
    return Task(
        board_id=board.id,
        title=title,
        status=status,
        execution_mode="team",
        execution_lane="harness",
        parent_task_id=parent.id,
        split_position=0,
        split_type="dag",
    )


@pytest.fixture
def no_comments(monkeypatch):
    async def _quiet(db, task_id, board_id, content):
        return True

    monkeypatch.setattr(wl, "_post_comment", _quiet)


async def test_native_parent_never_completes_by_majority(wenv, no_comments):
    session, board = wenv
    parent = native_parent(board)
    session.add(parent)
    await session.flush()
    session.add(child(board, parent, "done"))
    lag = child(board, parent, "in_progress", title="laggard")
    lag.updated_at = lag.updated_at  # stuck: oldest_pending far in the past
    from datetime import timedelta
    from app.core.time import utcnow

    lag.updated_at = utcnow() - timedelta(hours=12)
    session.add(lag)
    await session.commit()

    assert await wl._detect_split_parent_majority_completion(session) == 0
    await session.refresh(parent)
    assert parent.status == "in_progress"  # 1/2 done is NOT a majority promotion
    assert await wl._detect_split_parent_completion(session) == 0
    await session.refresh(parent)
    assert parent.status == "in_progress"


async def test_legacy_split_parent_keeps_majority_behavior(wenv, no_comments):
    session, board = wenv
    parent = Task(
        board_id=board.id,
        title="legacy autosplit",
        status="in_progress",
        split_count=3,
        split_type="by_count",
    )
    session.add(parent)
    await session.flush()
    from datetime import timedelta
    from app.core.time import utcnow

    for st in ("done", "done", "in_progress"):
        c = Task(board_id=board.id, title=st, status=st, parent_task_id=parent.id)
        c.updated_at = utcnow() - timedelta(hours=12)
        session.add(c)
    await session.commit()

    assert await wl._detect_split_parent_majority_completion(session) == 1
    await session.refresh(parent)
    assert parent.status == "done"  # unchanged historic behavior for non-native parents


async def test_native_parent_review_requires_all_children_done_and_count_match(wenv, no_comments):
    session, board = wenv
    parent = native_parent(board, split_count=3)
    session.add(parent)
    await session.flush()
    for st in ("done", "done"):
        session.add(child(board, parent, st))
    await session.commit()
    # split_count says 3 but only 2 children exist: never silently complete.
    assert await wl._detect_split_parent_completion(session) == 0
    await session.refresh(parent)
    assert parent.status == "in_progress"

    # Add the missing third child, still pending: still no promotion.
    pending = child(board, parent, "blocked", title="c3")
    session.add(pending)
    await session.commit()
    assert await wl._detect_split_parent_completion(session) == 0
    await session.refresh(parent)
    assert parent.status == "in_progress"

    # All three done and counted: promote to review — never done.
    pending.status = "done"
    session.add(pending)
    await session.commit()
    assert await wl._detect_split_parent_completion(session) == 1
    await session.refresh(parent)
    assert parent.status == "review"


async def test_nested_native_parent_is_also_promoted_to_review(wenv, no_comments):
    session, board = wenv
    root = native_parent(board)
    session.add(root)
    await session.flush()
    nested = native_parent(board, parent_task_id=root.id)
    session.add(nested)
    await session.flush()
    session.add(child(board, nested, "done", title="n1"))
    session.add(child(board, nested, "done", title="n2"))
    # root's other children are still pending: root stays, nested promotes.
    session.add(child(board, root, "in_progress", title="r1"))
    await session.commit()

    assert await wl._detect_split_parent_completion(session) == 1
    await session.refresh(nested)
    assert nested.status == "review"
    await session.refresh(root)
    assert root.status == "in_progress"


# ── owner API: malformed subtasks leave no runnable orphan ─────────────────


async def _api(session, board):
    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(tasks_router)
    app.include_router(api_v1)

    async def _session():
        yield session

    async def _board(board_id, session=Depends(get_session)):
        return board

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_board_for_user_write] = _board
    app.dependency_overrides[require_user_auth] = lambda: ActorContext(actor_type="user", user=None)
    return app


@pytest.fixture
async def api_env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(id=uuid4(), name="o")
        session.add(org)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=uuid4())
        session.add(board)
        await session.commit()
        app = await _api(session, board)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, session, board
    await engine.dispose()


async def test_malformed_owner_subtasks_create_no_task(api_env):
    client, session, board = api_env
    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks",
        json={
            "title": "owner dag",
            "complexity": 3,
            "execution_lane": "harness",
            "subtasks": [{"title": "a", "deps": [True]}],  # bool is not an int
        },
    )
    assert resp.status_code == 422, resp.text
    assert "dag_validation_failed" in resp.text
    assert list(await session.exec(select(Task))) == []  # no orphan left behind


async def test_valid_owner_subtasks_materialize_atomically(api_env):
    client, session, board = api_env
    resp = await client.post(
        f"/api/v1/boards/{board.id}/tasks",
        json={
            "title": "owner dag",
            "complexity": 3,
            "execution_lane": "harness",
            "subtasks": [
                {"title": "a", "description": "first", "deps": []},
                {"title": "b", "description": "second", "deps": [0]},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_progress" and body["split_count"] == 2
    children = list(await session.exec(select(Task).where(Task.parent_task_id == UUID(body["id"]))))
    assert len(children) == 2
    assert all(c.execution_lane == "harness" for c in children)
