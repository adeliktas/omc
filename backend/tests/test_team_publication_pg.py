"""Cross-board publication reservation: one repo, one durable publisher."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.time import utcnow
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.team import TeamCycle
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING


async def test_two_hosts_cannot_reserve_publication_even_on_different_boards():
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    assert url.endswith("/mc_audit"), "Refusing non-disposable database"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))
    ids = []
    async with AsyncSession(engine, expire_on_commit=False) as s:
        org = Organization(id=uuid4(), name="pg")
        s.add(org)
        await s.flush()
        gateway = Gateway(
            id=uuid4(),
            organization_id=org.id,
            name="g",
            url="https://g.invalid",
            workspace_root="/tmp/w",
        )
        s.add(gateway)
        await s.flush()
        for i in range(2):
            board = Board(
                id=uuid4(),
                organization_id=org.id,
                name="b",
                slug=f"b-{uuid4().hex}",
                gateway_id=gateway.id,
            )
            s.add(board)
            await s.flush()
            task = Task(
                id=uuid4(),
                board_id=board.id,
                title="t",
                status="in_progress",
                execution_lane="harness",
                execution_mode="team",
            )
            s.add(task)
            await s.flush()
            cycle, _ = await svc.start_cycle(s, board, task, RANKING)
            cycle.state, cycle.reviewed_sha = "qa_passed", BASE
            ids.append(cycle.id)
        await s.commit()

    async def reserve(cid):
        async with AsyncSession(engine, expire_on_commit=False) as s:
            cycle = await s.get(TeamCycle, cid)
            try:
                await svc.reserve_publication(s, cycle, BASE, cycle.version)
                await s.commit()
                return True
            except (IntegrityError, svc.TeamError):
                await s.rollback()
                return False

    results = await asyncio.gather(*(reserve(cid) for cid in ids))
    assert sorted(results) == [False, True]
    await engine.dispose()
