from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.api.deps import ActorContext
from app.models.agents import Agent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.schemas.tasks import TaskUpdate


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


async def _seed_board_task_and_agent(
    session: AsyncSession,
    *,
    task_status: str = "in_progress",
    require_review_before_done: bool = False,
    only_lead_can_change_status: bool = False,
) -> tuple[Board, Task, Agent]:
    organization_id = uuid4()
    gateway = Gateway(
        id=uuid4(),
        organization_id=organization_id,
        name="gateway",
        url="https://gateway.local",
        workspace_root="/tmp/workspace",
    )
    board = Board(
        id=uuid4(),
        organization_id=organization_id,
        gateway_id=gateway.id,
        name="board",
        slug=f"board-{uuid4()}",
        require_review_before_done=require_review_before_done,
        only_lead_can_change_status=only_lead_can_change_status,
    )
    agent = Agent(
        id=uuid4(),
        organization_id=organization_id,
        gateway_id=gateway.id,
        board_id=board.id,
        name="agent",
        status="online",
        is_board_lead=True,
        is_gateway_main=False,
    )
    task = Task(
        id=uuid4(),
        organization_id=organization_id,
        board_id=board.id,
        title="task",
        status=task_status,
    )
    session.add(gateway)
    session.add(board)
    session.add(agent)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return board, task, agent


def test_new_attempt_clears_stale_budget_exhaustion() -> None:
    task = Task(
        id=uuid4(),
        organization_id=uuid4(),
        title="retry",
        status="inbox",
        tokens_used=99_999,
        budget_exhausted_at=datetime.now(timezone.utc),
    )

    tasks_api._reset_attempt_budget(task, "blocked", "inbox")

    assert task.tokens_used == 0
    assert task.budget_exhausted_at is None


class TestStatusTransitionEnforcement:
    """Tests for the _require_valid_status_transition runtime gate."""

    @pytest.mark.asyncio
    async def test_valid_inbox_to_in_progress(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="inbox")
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="inbox",
                target_status="in_progress",
            )

    @pytest.mark.asyncio
    async def test_valid_in_progress_to_review(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="in_progress")
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="in_progress",
                target_status="review",
            )

    @pytest.mark.asyncio
    async def test_valid_review_to_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="review")
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="review",
                target_status="done",
            )

    @pytest.mark.asyncio
    async def test_valid_done_to_inbox(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="done")
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="done",
                target_status="inbox",
            )

    @pytest.mark.asyncio
    async def test_invalid_inbox_to_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="inbox")
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="inbox",
                    target_status="done",
                )

    @pytest.mark.asyncio
    async def test_invalid_inbox_to_review(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="inbox")
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="inbox",
                    target_status="review",
                )

    @pytest.mark.asyncio
    async def test_invalid_in_progress_to_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="in_progress")
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="in_progress",
                    target_status="done",
                )

    @pytest.mark.asyncio
    async def test_invalid_done_to_review(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="done")
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="done",
                    target_status="review",
                )

    @pytest.mark.asyncio
    async def test_invalid_done_to_in_progress(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="done")
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="done",
                    target_status="in_progress",
                )

    @pytest.mark.asyncio
    async def test_review_before_done_enforced_when_board_rule_set(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(
                session, task_status="review", require_review_before_done=True
            )
            # review -> done is allowed when require_review_before_done=True AND previous is review
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="review",
                target_status="done",
            )

    @pytest.mark.asyncio
    async def test_review_before_done_blocks_inbox_direct_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            # This is actually caught by the generic transition check first,
            # but we test the rule in isolation by bypassing transition checks.
            board, task, _ = await _seed_board_task_and_agent(
                session, task_status="inbox", require_review_before_done=True
            )
            # The generic _VALID_TRANSITIONS blocks inbox->done regardless
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="inbox",
                    target_status="done",
                )

    @pytest.mark.asyncio
    async def test_review_before_done_blocks_in_progress_to_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            # Test the board-rule-specific enforcement
            board, task, _ = await _seed_board_task_and_agent(
                session, task_status="in_progress", require_review_before_done=True
            )
            # The generic _VALID_TRANSITIONS already blocks in_progress->done
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._require_valid_status_transition(
                    session,
                    board_id=board.id,
                    previous_status="in_progress",
                    target_status="done",
                )

    @pytest.mark.asyncio
    async def test_same_status_noop(self) -> None:
        """No error when previous_status == target_status."""
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, _ = await _seed_board_task_and_agent(session, task_status="in_progress")
            await tasks_api._require_valid_status_transition(
                session,
                board_id=board.id,
                previous_status="in_progress",
                target_status="in_progress",
            )


class TestIntegrationWithStatusPatch:
    """Integration tests ensuring task PATCH rejects blocked transitions."""

    @pytest.mark.asyncio
    async def test_agent_cannot_skip_to_done(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, agent = await _seed_board_task_and_agent(
                session, task_status="in_progress", only_lead_can_change_status=False
            )
            update = tasks_api._TaskUpdateInput(
                task=task,
                actor=ActorContext(actor_type="agent", agent=agent),
                board_id=board.id,
                previous_status="in_progress",
                previous_assigned=None,
                status_requested=True,
                updates={"status": "done"},
                comment=None,
                depends_on_task_ids=None,
                tag_ids=None,
                custom_field_values={},
                custom_field_values_set=False,
            )
            with pytest.raises(HTTPException, match="Invalid status transition"):
                await tasks_api._apply_non_lead_agent_task_rules(session, update=update)

    @pytest.mark.asyncio
    async def test_agent_must_go_through_review(self) -> None:
        engine = await _make_engine()
        async with _make_session(engine) as session:
            board, task, agent = await _seed_board_task_and_agent(
                session, task_status="in_progress", only_lead_can_change_status=False
            )
            # Agent can move in_progress -> review (valid)
            update = tasks_api._TaskUpdateInput(
                task=task,
                actor=ActorContext(actor_type="agent", agent=agent),
                board_id=board.id,
                previous_status="in_progress",
                previous_assigned=None,
                status_requested=True,
                updates={"status": "review"},
                comment=None,
                depends_on_task_ids=None,
                tag_ids=None,
                custom_field_values={},
                custom_field_values_set=False,
            )
            await tasks_api._apply_non_lead_agent_task_rules(session, update=update)
            assert task.status == "review"
