"""Utilities for recording normalized activity events."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.organization_members import OrganizationMember

from app.models.activity_events import ActivityEvent
from app.models.users import User

T = TypeVar("T")


def record_activity(
    session: AsyncSession,
    *,
    event_type: str,
    message: str,
    agent_id: UUID | None = None,
    task_id: UUID | None = None,
    board_id: UUID | None = None,
) -> ActivityEvent:
    """Create and attach an activity event row to the current DB session.

    T51 (2026-05-14): also pulses the in-process task-event bus so SSE
    consumers wake immediately instead of polling. Safe to fire pre-commit:
    consumers re-query the DB and will only see committed rows. If commit
    later rolls back, the extra wake-up just produces an empty fetch.
    """
    event = ActivityEvent(
        event_type=event_type,
        message=message,
        agent_id=agent_id,
        task_id=task_id,
        board_id=board_id,
    )
    session.add(event)
    try:
        from app.services.task_event_bus import notify_task_changed
        notify_task_changed(board_id)
    except Exception:
        # Bus failures must never break the audit log write path.
        pass
    return event


class AdminChangeFailed(Exception):
    """Applying an audited admin change failed; its FAILED row names the state."""


async def audited_admin_change(
    session: AsyncSession,
    member: OrganizationMember,
    message: str,
    apply: Callable[[], T],
    state: Callable[[], str],
) -> T:
    """Commit the audit row for an admin runtime change, then apply it.

    2026-10-06: a watchdog re-enable left no actor. The row naming the account and
    the old -> new values is committed before anything changes, so no change can
    exist without it; a failed audit changes nothing. Callers read the old value
    strictly (an unreadable store aborts before the row). If applying then fails
    (it may have partly written), a second row records the error and the state
    read afterwards — or that it could not be read — and AdminChangeFailed is
    raised. A crash in between leaves a row for a change that may not have
    happened: over-reported, never hidden. Values only, never credentials
    (settings and ranking metadata hold none).
    """
    user = await session.get(User, member.user_id)
    who = (user and (user.preferred_name or user.name)) or f"user {member.user_id}"
    record_activity(session, event_type="admin.change", message=f"{message} (by {who})")
    await session.commit()
    try:
        return apply()
    except Exception as exc:
        try:
            now = state()
        except Exception as read_exc:
            now = f"unreadable ({type(read_exc).__name__})"
        try:
            record_activity(
                session,
                event_type="admin.change",
                message=f"FAILED ({type(exc).__name__}), now {now}: {message} (by {who})",
            )
            await session.commit()
        except Exception:
            pass  # the first row already records the attempt; keep the real error
        raise AdminChangeFailed(str(exc)) from exc
