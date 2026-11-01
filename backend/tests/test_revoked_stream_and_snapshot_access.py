# ruff: noqa: INP001, S101
"""T-027 / T-086: revoked board access vs SSE streams and board snapshots.

ORIGINAL-TODO-IDs: T-027, T-086 (Epic 3 + Epic 11).

Part 1 — T-027 (VERIFY-ONLY, do NOT fix before measuring):
    Hold an SSE connection open against
    `app.api.tasks._task_event_generator` and
    `app.api.activity.stream_task_comment_feed`, revoke the user's board
    access mid-stream (by removing the `OrganizationBoardAccess` grant that
    `app.services.organizations.has_board_access` consults), then measure
    whether the already-open generator keeps emitting events created after the
    revocation.

    MEASURED OUTCOME (documented here; verify-only, no fix applied):
    BOTH generators keep emitting board-scoped events after the user's board
    access has been revoked. Neither generator re-checks board access inside
    its loop:
      - `_task_event_generator` only checks `request.is_disconnected()` and
        fetches `board_id`-filtered events each iteration.
      - `stream_task_comment_feed` resolves `list_accessible_board_ids` ONCE
        before the generator starts and keeps filtering with the stale
        snapshot (`allowed_ids`) / keeps fetching the originally-authorized
        `board_id`.
    A control test below shows NEW connections are denied after revocation,
    so the leak is specific to already-open streams.

    This is a P1 follow-up proposal, NOT fixed here: the remediation policy
    (terminate stream vs periodic re-check) is T-028, an OPTIONAL-HUMAN-GATE
    that requires an owner security/product decision. If T-028 is ever
    implemented, the two leak-documentation tests below must be updated to
    assert the chosen denial behavior instead.

Part 2 — T-086:
    Prove a user revoked between snapshot requests is denied a new snapshot
    with HTTP 403 via `app.api.boards.get_board_snapshot`
    (-> `get_board_for_actor_read` -> `require_board_access`
    -> `app.services.organizations.has_board_access`), including an
    end-to-end check through the boards router where the first snapshot
    request succeeds (200) and the post-revocation refresh fails (403).

Constraint (read-only):
    `frontend/src/app/boards/[boardId]/page.tsx` SSE abort/backoff consumers
    must remain unchanged; the final test only asserts the existing
    abort/backoff wiring is still present (no frontend edits in this batch).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import activity, boards, tasks
from app.api.boards import router as boards_router
from app.api.deps import ActorContext, get_board_for_actor_read
from app.core.auth import LOCAL_AUTH_EMAIL, LOCAL_AUTH_NAME, LOCAL_AUTH_USER_ID
from app.core.auth_mode import AuthMode
from app.core.config import settings
from app.core.time import utcnow
from app.db import crud
from app.db.session import get_session
from app.models.activity_events import ActivityEvent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organization_board_access import OrganizationBoardAccess
from app.models.organization_members import OrganizationMember
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.users import User
from app.services import organizations
from app.services.organizations import OrganizationContext
from app.services.task_event_bus import notify_task_changed

FRONTEND_BOARD_PAGE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "app"
    / "boards"
    / "[boardId]"
    / "page.tsx"
)


class _FakeRequest:
    """Minimal Request stand-in: the stream generators only call is_disconnected()."""

    async def is_disconnected(self) -> bool:
        return False


@dataclass
class _World:
    engine: AsyncEngine
    session_maker: async_sessionmaker
    org: Organization
    board: Board
    task: Task
    user: User
    member: OrganizationMember


async def _make_world(tmp_path: Path, *, local_auth_user: bool = False) -> _World:
    """Seed one org/board/task plus a scoped (non-admin) member with read access."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/revoked.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as s:
        org = Organization(id=uuid4(), name="org")
        s.add(org)
        await s.flush()
        gateway = Gateway(
            id=uuid4(),
            organization_id=org.id,
            name="gateway",
            url="https://gateway.invalid",
            workspace_root="/tmp/workspace",
        )
        s.add(gateway)
        await s.flush()
        board = Board(
            id=uuid4(), organization_id=org.id, name="Board", slug="board", gateway_id=gateway.id
        )
        s.add(board)
        await s.flush()
        task = Task(id=uuid4(), board_id=board.id, title="task", status="in_progress")
        s.add(task)
        await s.flush()
        if local_auth_user:
            # Reuse the fixed local-auth identity so the real local-token
            # auth dependency resolves to this seeded user (no token literal
            # is embedded here; the runtime value comes from settings).
            user = User(
                id=uuid4(),
                clerk_user_id=LOCAL_AUTH_USER_ID,
                email=LOCAL_AUTH_EMAIL,
                name=LOCAL_AUTH_NAME,
                active_organization_id=org.id,
            )
        else:
            user = User(
                id=uuid4(),
                clerk_user_id=f"stream-user-{uuid4().hex}",
                email="stream-user@example.local",
                active_organization_id=org.id,
            )
        s.add(user)
        await s.flush()
        member = OrganizationMember(
            id=uuid4(),
            organization_id=org.id,
            user_id=user.id,
            role="member",
            all_boards_read=False,
            all_boards_write=False,
        )
        s.add(member)
        await s.flush()
        s.add(
            OrganizationBoardAccess(
                id=uuid4(),
                organization_member_id=member.id,
                board_id=board.id,
                can_read=True,
                can_write=False,
            )
        )
        await s.commit()
    return _World(
        engine=engine,
        session_maker=session_maker,
        org=org,
        board=board,
        task=task,
        user=user,
        member=member,
    )


async def _add_event(world: _World, *, event_type: str, message: str) -> UUID:
    async with world.session_maker() as s:
        event = ActivityEvent(
            event_type=event_type,
            task_id=world.task.id,
            board_id=world.board.id,
            message=message,
        )
        s.add(event)
        await s.commit()
        return event.id


async def _revoke_board_access(world: _World) -> None:
    """Remove the member's explicit board grant (what admin access edits do)."""
    async with world.session_maker() as s:
        deleted = await crud.delete_where(
            s,
            OrganizationBoardAccess,
            col(OrganizationBoardAccess.organization_member_id) == world.member.id,
            commit=True,
        )
        assert deleted >= 1


# ---------------------------------------------------------------------------
# Part 1 — T-027, VERIFY-ONLY: mid-stream revocation vs open SSE generators.
# ---------------------------------------------------------------------------


async def test_task_stream_keeps_emitting_after_revocation_t027(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-027 verify-only: `_task_event_generator` leaks post-revocation events.

    Documented behavior (no fix in this batch): the task SSE generator keeps
    emitting board events after the consuming user's board access is revoked
    mid-stream. Remediation policy is T-028 (OPTIONAL-HUMAN-GATE).
    """
    world = await _make_world(tmp_path)
    monkeypatch.setattr(tasks, "async_session_maker", world.session_maker)

    await _add_event(world, event_type="task.updated", message="event before revocation")
    stream = tasks._task_event_generator(
        request=_FakeRequest(),
        board_id=world.board.id,
        since_dt=utcnow() - timedelta(minutes=5),
    )
    first = await asyncio.wait_for(stream.__anext__(), 5)
    assert "event before revocation" in first["data"]

    # Mid-stream revocation: the access grant backing has_board_access is gone.
    await _revoke_board_access(world)
    async with world.session_maker() as s:
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=False
            )
            is False
        )
    post_event_id = await _add_event(
        world, event_type="task.updated", message="event after revocation"
    )

    pull = asyncio.ensure_future(stream.__anext__())
    # Let the generator park in wait_for_task_change, then wake it.
    await asyncio.sleep(0.3)
    notify_task_changed(world.board.id)
    second = await asyncio.wait_for(pull, 5)

    # MEASURED OUTCOME: the still-open generator emitted an event created
    # after revocation. This documents the T-027 leak; it is intentionally
    # asserted as current behavior, not fixed here.
    payload = json.loads(second["data"])
    assert payload["activity"]["id"] == str(post_event_id)
    assert "event after revocation" in second["data"]
    await stream.aclose()
    await world.engine.dispose()


async def test_task_comment_stream_keeps_emitting_after_revocation_t027(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-027 verify-only: `stream_task_comment_feed` leaks post-revocation events.

    Documented behavior (no fix in this batch): the task-comment SSE feed keeps
    emitting comments for a board whose access the consumer lost mid-stream
    because `list_accessible_board_ids` is resolved once, before the generator
    starts, and never re-checked. Remediation policy is T-028
    (OPTIONAL-HUMAN-GATE).
    """
    world = await _make_world(tmp_path)
    monkeypatch.setattr(activity, "async_session_maker", world.session_maker)
    monkeypatch.setattr(activity, "STREAM_POLL_SECONDS", 0.05)

    await _add_event(world, event_type="task.comment", message="comment before revocation")
    async with world.session_maker() as s:
        response = await activity.stream_task_comment_feed(
            request=_FakeRequest(),
            board_id=world.board.id,
            since=(utcnow() - timedelta(minutes=5)).isoformat(),
            db_session=s,
            ctx=OrganizationContext(organization=world.org, member=world.member),
        )
    stream = response.body_iterator
    first = await asyncio.wait_for(stream.__anext__(), 5)
    assert "comment before revocation" in first["data"]

    # Mid-stream revocation: the member no longer has any accessible board.
    await _revoke_board_access(world)
    async with world.session_maker() as s:
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=False
            )
            is False
        )
        assert (
            await organizations.list_accessible_board_ids(s, member=world.member, write=False) == []
        )
    post_event_id = await _add_event(
        world, event_type="task.comment", message="comment after revocation"
    )

    second = await asyncio.wait_for(stream.__anext__(), 5)

    # MEASURED OUTCOME: the still-open generator emitted a comment created
    # after revocation. This documents the T-027 leak; intentionally asserted
    # as current behavior, not fixed here.
    payload = json.loads(second["data"])
    assert payload["comment"]["id"] == str(post_event_id)
    assert "comment after revocation" in second["data"]
    await stream.aclose()
    await world.engine.dispose()


async def test_new_stream_connection_is_denied_after_revocation_t027(tmp_path: Path) -> None:
    """T-027 control: NEW connections are denied after revocation.

    The documented leak above only concerns already-open connections: a fresh
    request resolves `get_board_for_actor_read` -> `require_board_access` and
    receives HTTP 403 once the grant is removed.
    """
    world = await _make_world(tmp_path)
    actor = ActorContext(actor_type="user", user=world.user)
    async with world.session_maker() as s:
        board = await get_board_for_actor_read(world.board.id, s, actor)
        assert board.id == world.board.id

    await _revoke_board_access(world)
    async with world.session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await get_board_for_actor_read(world.board.id, s, actor)
    assert exc.value.status_code == 403
    await world.engine.dispose()


# ---------------------------------------------------------------------------
# Part 2 — T-086: revoked user is denied a new snapshot (403).
# ---------------------------------------------------------------------------


async def test_has_board_access_reflects_revocation_t086(tmp_path: Path) -> None:
    """T-086 service layer: `has_board_access` flips on grant removal."""
    world = await _make_world(tmp_path)
    async with world.session_maker() as s:
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=False
            )
            is True
        )
        # Grant is read-only, so write access was never granted.
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=True
            )
            is False
        )

    await _revoke_board_access(world)
    async with world.session_maker() as s:
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=False
            )
            is False
        )
        assert (
            await organizations.has_board_access(
                s, member=world.member, board=world.board, write=True
            )
            is False
        )
    await world.engine.dispose()


async def test_snapshot_dependency_denies_revoked_user_t086(tmp_path: Path) -> None:
    """T-086 dependency layer: `get_board_for_actor_read` raises 403 post-revocation."""
    world = await _make_world(tmp_path)
    actor = ActorContext(actor_type="user", user=world.user)
    async with world.session_maker() as s:
        board = await get_board_for_actor_read(world.board.id, s, actor)
        assert board.id == world.board.id

    await _revoke_board_access(world)
    async with world.session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await get_board_for_actor_read(world.board.id, s, actor)
    assert exc.value.status_code == 403
    await world.engine.dispose()


async def test_revoked_user_gets_403_on_snapshot_refresh_t086(tmp_path: Path) -> None:
    """T-086 end-to-end: snapshot refresh after revocation returns 403.

    First `GET /api/v1/boards/{board_id}/snapshot` succeeds (200) while the
    scoped member holds the read grant; after the grant is removed the next
    snapshot request is denied with 403 by the real boards router +
    `get_board_snapshot` dependency chain.
    """
    if settings.auth_mode != AuthMode.LOCAL:
        pytest.skip(
            "end-to-end snapshot denial requires the local auth mode used by the test suite"
        )
    world = await _make_world(tmp_path, local_auth_user=True)

    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(boards_router)
    app.include_router(api_v1)

    async def _override_get_session() -> AsyncSession:
        async with world.session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session

    url = f"/api/v1/boards/{world.board.id}/snapshot"
    headers = {"Authorization": f"Bearer {settings.local_auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(url, headers=headers)
        assert first.status_code == 200, first.text

        await _revoke_board_access(world)

        second = await client.get(url, headers=headers)
        assert second.status_code == 403, second.text
    await world.engine.dispose()


def test_board_page_sse_abort_and_backoff_wiring_unchanged() -> None:
    """Read-only guard: board page SSE consumers keep abort/backoff cleanup.

    Constraint for this batch (T-027/T-086): the SSE abort/backoff wiring in
    `frontend/src/app/boards/[boardId]/page.tsx` must remain unchanged. This
    only reads the file and asserts the existing wiring markers are present.
    """
    assert FRONTEND_BOARD_PAGE.is_file(), FRONTEND_BOARD_PAGE
    text = FRONTEND_BOARD_PAGE.read_text(encoding="utf-8")
    assert text.count("const abortController = new AbortController();") >= 4
    assert text.count("const backoff = createExponentialBackoff(SSE_RECONNECT_BACKOFF);") >= 4
    assert "abortController.abort();" in text
    assert "window.clearTimeout(reconnectTimeout);" in text
