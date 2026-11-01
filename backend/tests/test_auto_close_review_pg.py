"""A review auto-closes only on an approval from its own work round.

Production 2026-10-04: the recurring `Poem scraper` was closed to done every cycle
by one approval from its first cycle (2026-07-06), so no run's result was reviewed.
Run against a disposable migrated database only:
MC_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55438/mc_audit
"""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.time import utcnow
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.services import watchdog_loop as wl


@pytest.mark.asyncio
async def test_stale_approval_does_not_close_a_new_round():
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable migrated MC_TEST_POSTGRES_URL")
    if not url.endswith("/mc_audit"):
        pytest.fail("Refusing non-disposable database: expected /mc_audit")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE organizations CASCADE"))

    org, gw, bid, tid = uuid4(), uuid4(), uuid4(), uuid4()
    now = utcnow()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Organization(id=org, name="close"))
        await db.flush()
        db.add(Gateway(id=gw, organization_id=org, name="g", url="ws://fake.invalid/ws",
                       workspace_root="/tmp/close"))
        await db.flush()
        db.add(Board(id=bid, organization_id=org, gateway_id=gw, name="b", slug=f"b-{bid.hex}"))
        await db.flush()
        db.add(Task(id=tid, board_id=bid, title="recurring", status="review", complexity=1,
                    previous_in_progress_at=now - timedelta(minutes=5)))
        await db.flush()
        db.add(Approval(board_id=bid, task_id=tid, action_type="close", confidence=100,
                        status="approved", created_at=now - timedelta(days=90)))
        await db.commit()

        assert await wl._auto_close_review_tasks(db) == 0

        db.add(Approval(board_id=bid, task_id=tid, action_type="close", confidence=100,
                        status="approved", created_at=now))
        await db.commit()
        assert await wl._auto_close_review_tasks(db) == 1
        status = (await db.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})).scalar()

    await engine.dispose()
    assert status == "done"
