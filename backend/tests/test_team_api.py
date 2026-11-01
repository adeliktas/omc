"""Team API boundaries: board scoping, admin-only validated assignment, migration chain."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import team as team_api
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.schemas.team import TeamMemberAssign
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING


@pytest.fixture
async def world(monkeypatch):
    async def ranking(_s):
        return RANKING

    monkeypatch.setattr(team_api, "_ranking", ranking)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        org = Organization(id=uuid4(), name="o"); s.add(org)
        gw = Gateway(id=uuid4(), organization_id=org.id, name="g", url="https://g.local", workspace_root="/tmp/w"); s.add(gw)
        boards = []
        for n in ("a", "b"):
            b = Board(id=uuid4(), organization_id=org.id, name=n, slug=n, gateway_id=gw.id); s.add(b); boards.append(b)
        task = Task(id=uuid4(), board_id=boards[0].id, title="t", status="in_progress", execution_lane="harness", execution_mode="team")
        s.add(task); await s.commit()
        yield s, org, boards, task
    await engine.dispose()


async def test_cycle_and_attempt_ids_do_not_cross_boards_or_tasks(world):
    s, _org, (a, b), task = world
    cycle, _ = await svc.start_cycle(s, a, task, RANKING)
    await s.commit()
    with pytest.raises(HTTPException) as exc:
        await team_api._cycle(s, b, task, cycle.id)
    assert exc.value.status_code == 404
    other = Task(id=uuid4(), board_id=a.id, title="o", status="in_progress", execution_lane="harness", execution_mode="team")
    s.add(other); await s.commit()
    with pytest.raises(HTTPException):
        await team_api._cycle(s, a, other, cycle.id)
    got, _tok = await svc.claim_next(s, cycle, base_sha=BASE)  # type: ignore[misc]
    stranger, _ = await svc.start_cycle(s, a, other, RANKING)
    with pytest.raises(HTTPException):
        await team_api._attempt(s, stranger, got.id)


async def test_start_for_a_task_on_another_board_is_refused(world):
    s, _org, (a, b), task = world
    with pytest.raises(svc.TeamError):
        await svc.start_cycle(s, b, task, RANKING)


async def test_admin_assignment_is_validated_against_registry_and_org_scoped(world):
    s, org, (a, _b), _task = world
    ctx = SimpleNamespace(organization=org)
    ok = await team_api.assign_member(
        TeamMemberAssign(role_kind="developer", provider="deepseek-sub", model="deepseek-flash"), board=a, session=s, ctx=ctx)
    assert (ok.provider, ok.model, ok.capability, ok.created_by) == ("deepseek-sub", "deepseek-flash", "write_worktree", "admin")
    for bad in (("claude-code", "opus-4-8"), ("openai-codex-1", "gpt-5.6-sol"), ("claude-code", "made-up")):
        with pytest.raises(HTTPException) as exc:
            await team_api.assign_member(TeamMemberAssign(role_kind="qa", provider=bad[0], model=bad[1]), board=a, session=s, ctx=ctx)
        assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await team_api.assign_member(
            TeamMemberAssign(role_kind="qa", provider="claude-code", model="sonnet"), board=a, session=s,
            ctx=SimpleNamespace(organization=SimpleNamespace(id=uuid4())))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:  # off-list specialist profile
        await team_api.assign_member(
            TeamMemberAssign(role_kind="specialist", specialist_slug="red-team-operator", provider="claude-code", model="sonnet"),
            board=a, session=s, ctx=ctx)
    assert exc.value.status_code == 422


async def test_admin_assigned_engine_disabled_blocks_but_a_temporary_hold_does_not(world):
    # Owner 2026-10-24: a quota/auth hold must not stop work; attempts draft locally and the
    # pin stays. Only an owner-disabled pinned engine still refuses to start a cycle.
    s, org, (a, _b), task = world
    await team_api.assign_member(
        TeamMemberAssign(role_kind="developer", provider="deepseek-sub", model="deepseek-flash"), board=a, session=s,
        ctx=SimpleNamespace(organization=org))
    disabled = {**RANKING, "levels": [{"level": 1, "entries": [
        {**e, "eligible": False, "disabled": True} if e["model"] == "deepseek-flash" else e
        for e in RANKING["levels"][0]["entries"]]}]}
    with pytest.raises(svc.TeamError) as exc:
        await svc.start_cycle(s, a, task, disabled)
    assert "admin-assigned" in exc.value.detail
    held = {**RANKING, "levels": [{"level": 1, "entries": [
        {**e, "eligible": False} if e["model"] == "deepseek-flash" else e for e in RANKING["levels"][0]["entries"]]}]}
    cycle, _ = await svc.start_cycle(s, a, task, held)
    assert cycle.state == "planning"


def test_migration_chain_has_one_head_and_team_revision_precedes_pause():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_heads() == ["x8b9c0d1e2f3"]
    assert scripts.get_revision("x8b9c0d1e2f3").down_revision == "w7a8b9c0d1e2"
    assert scripts.get_revision("w7a8b9c0d1e2").down_revision == "v6f7a8b9c0d1"


async def test_concurrent_claims_never_hand_out_one_attempt_twice_postgres():
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    if not url.endswith("/mc_audit"):
        pytest.fail("Refusing non-disposable database: expected /mc_audit")
    import asyncio

    from sqlalchemy import text

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))
    async with AsyncSession(engine, expire_on_commit=False) as s:
        org = Organization(id=uuid4(), name="pg"); s.add(org); await s.flush()
        gw = Gateway(id=uuid4(), organization_id=org.id, name="g", url="https://g.invalid", workspace_root="/tmp/w"); s.add(gw); await s.flush()
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug=f"b-{uuid4().hex}", gateway_id=gw.id); s.add(board); await s.flush()
        task = Task(id=uuid4(), board_id=board.id, title="t", status="in_progress", execution_lane="harness", execution_mode="team")
        s.add(task); await s.commit()
        cycle, _ = await svc.start_cycle(s, board, task, RANKING)
        await s.commit()

    async def racer():
        async with AsyncSession(engine, expire_on_commit=False) as rs:
            c = await rs.get(TeamCycle, cycle.id)
            got = await svc.claim_next(rs, c, base_sha=BASE)
            await rs.commit()
            return got[0].id if got else None

    from app.models.team import TeamCycle

    results = await asyncio.gather(*[racer() for _ in range(6)])
    assert len([r for r in results if r]) == 1  # one queued PM attempt, one winner
    await engine.dispose()
