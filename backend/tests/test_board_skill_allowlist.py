"""Tests for board_skill_allowlist runtime helper (T34 + T37, 2026-05-14)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel


@pytest.fixture
async def db():
    import app.models  # noqa: F401 (register tables)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_allow_when_no_entry(db: AsyncSession) -> None:
    from app.api.board_skill_allowlist import is_skill_allowed
    board_id = uuid4()
    skill_id = uuid4()
    assert await is_skill_allowed(db, board_id=board_id, skill_id=skill_id) is True


@pytest.mark.asyncio
async def test_explicit_deny_blocks(db: AsyncSession) -> None:
    from app.api.board_skill_allowlist import is_skill_allowed
    from app.models.board_skill_allowlist import BoardSkillAllowlist
    board_id = uuid4()
    skill_id = uuid4()
    db.add(BoardSkillAllowlist(board_id=board_id, skill_id=skill_id, allowed=False))
    await db.commit()
    assert await is_skill_allowed(db, board_id=board_id, skill_id=skill_id) is False


@pytest.mark.asyncio
async def test_explicit_allow_passes(db: AsyncSession) -> None:
    from app.api.board_skill_allowlist import is_skill_allowed
    from app.models.board_skill_allowlist import BoardSkillAllowlist
    board_id = uuid4()
    skill_id = uuid4()
    db.add(BoardSkillAllowlist(board_id=board_id, skill_id=skill_id, allowed=True))
    await db.commit()
    assert await is_skill_allowed(db, board_id=board_id, skill_id=skill_id) is True


@pytest.mark.asyncio
async def test_deny_mode_blocks_unknown(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import board_skill_allowlist as mod
    monkeypatch.setattr(mod, "_DEFAULT_MODE", "deny")
    board_id = uuid4()
    skill_id = uuid4()
    # No row → blocked.
    assert await mod.is_skill_allowed(db, board_id=board_id, skill_id=skill_id) is False
