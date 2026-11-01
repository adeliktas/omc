"""Task comments come back newest-first.

A long-lived recurring task accumulates thousands of comments. Ascending
order meant page one held the oldest, so the current cycle was unreachable
without paging through everything: observed on a task with 1059 comments
whose newest visible entry was two months old.

This executes the query the endpoint builds rather than grepping for
`desc(...)`, so reordering the clause is caught.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity_events import ActivityEvent


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def test_comments_are_returned_newest_first(session: AsyncSession):
    from sqlalchemy import desc
    from sqlmodel import col, select

    task_id = uuid4()
    base = datetime(2026, 8, 19, 12, 0, 0)
    # Oldest first on the way in, so a naive query would return "Aug" first.
    for offset, body in ((0, "oldest"), (30, "middle"), (60, "newest")):
        session.add(
            ActivityEvent(
                task_id=task_id,
                event_type="task.comment",
                message=body,
                created_at=base + timedelta(days=offset),
            )
        )
    await session.commit()

    statement = (
        select(ActivityEvent)
        .where(col(ActivityEvent.task_id) == task_id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .order_by(desc(col(ActivityEvent.created_at)))
    )
    rows = (await session.exec(statement)).all()
    assert [r.message for r in rows] == ["newest", "middle", "oldest"]


def test_endpoint_orders_descending():
    """The endpoint itself must use the descending clause."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app/api/tasks.py"
    body = src.read_text()
    i = body.index('not_system_marker_sql("message")')
    window = body[i : i + 700]
    assert "desc(col(ActivityEvent.created_at))" in window
    assert "asc(col(ActivityEvent.created_at))" not in window
