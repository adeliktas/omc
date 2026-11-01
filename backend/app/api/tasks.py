"""Task API routes for listing, streaming, and mutating board tasks."""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import asc, desc, or_, text
from sqlalchemy import select as sa_select
from sqlmodel import col, select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    ActorContext,
    get_board_for_actor_read,
    get_board_for_user_write,
    get_task_or_404,
    require_user_auth,
    require_user_or_agent,
)
from app.api.comment_filters import not_system_marker_sql
from app.core.logging import get_logger
from app.core.time import utcnow
from app.db import crud
from app.db.pagination import paginate
from app.db.session import async_session_maker, get_session
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.models.approval_task_links import ApprovalTaskLink
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.tag_assignments import TagAssignment
from app.models.task_custom_fields import (
    BoardTaskCustomField,
    TaskCustomFieldDefinition,
    TaskCustomFieldValue,
)
from app.models.task_dependencies import TaskDependency
from app.models.task_fingerprints import TaskFingerprint
from app.models.task_stage_timings import TaskStageTiming
from app.models.tasks import Task
from app.schemas.activity_events import ActivityEventRead
from app.schemas.common import OkResponse
from app.schemas.errors import BlockedTaskError
from app.schemas.pagination import DefaultLimitOffsetPage
from app.schemas.task_custom_fields import (
    TaskCustomFieldType,
    TaskCustomFieldValues,
    validate_custom_field_value,
)
from app.schemas.tasks import (
    TaskCommentCreate,
    TaskCommentRead,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.services.activity_log import record_activity
from app.services.approval_task_links import (
    load_task_ids_by_approval,
    pending_approval_conflicts_by_task,
)
from app.services.mentions import extract_mentions, matches_agent_mention
from app.services.openclaw.gateway_dispatch import GatewayDispatchService
from app.services.openclaw.internal.session_keys import (
    is_gateway_parseable_session_key,
)
from app.services.openclaw.gateway_rpc import GatewayConfig as GatewayClientConfig
from app.services.openclaw.gateway_rpc import OpenClawGatewayError
from app.services.openclaw.provisioning_db import AgentLifecycleService
from app.services.organizations import require_board_access
from app.services.tags import (
    TagState,
    load_tag_state,
    replace_tags,
    validate_tag_ids,
)
from app.services.task_dependencies import (
    blocked_by_dependency_ids,
    dependency_ids_by_task_id,
    dependency_status_by_id,
    dependent_task_ids,
    replace_task_dependencies,
    validate_dependency_update,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel.sql.expression import SelectOfScalar

    from app.core.auth import AuthContext
    from app.models.users import User

router = APIRouter(prefix="/boards/{board_id}/tasks", tags=["tasks"])

ALLOWED_STATUSES = {"inbox", "in_progress", "review", "done", "blocked", "cancelled", "paused"}
TASK_EVENT_TYPES = {
    "task.created",
    "task.updated",
    "task.status_changed",
    "task.comment",
}
SSE_SEEN_MAX = 2000
TASK_SNIPPET_MAX_LEN = 500
TASK_SNIPPET_TRUNCATED_LEN = 497
TASK_EVENT_ROW_LEN = 2
BOARD_READ_DEP = Depends(get_board_for_actor_read)
ACTOR_DEP = Depends(require_user_or_agent)
SINCE_QUERY = Query(default=None)
STATUS_QUERY = Query(default=None, alias="status")
BOARD_WRITE_DEP = Depends(get_board_for_user_write)
SESSION_DEP = Depends(get_session)
USER_AUTH_DEP = Depends(require_user_auth)
TASK_DEP = Depends(get_task_or_404)


@dataclass(frozen=True, slots=True)
class _BoardCustomFieldDefinition:
    id: UUID
    field_key: str
    field_type: TaskCustomFieldType
    validation_regex: str | None
    required: bool
    default_value: object | None


def _comment_validation_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Comment is required.",
    )


def _task_update_forbidden_error(*, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "message": message,
            "code": code,
        },
    )


def _blocked_task_error(blocked_by_task_ids: Sequence[UUID]) -> HTTPException:
    # NOTE: Keep this payload machine-readable; UI and automation rely on it.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "Task is blocked by incomplete dependencies.",
            "code": "task_blocked_cannot_transition",
            "blocked_by_task_ids": [str(value) for value in blocked_by_task_ids],
        },
    )


def _approval_required_for_done_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": (
                "Task can only be marked done when a linked approval has been approved. "
                "Create one with POST /api/v1/agent/boards/{board_id}/approvals "
                '{"task_id": ..., "action_type": "close", "confidence": 0-100, '
                '"payload": {"reason": ...}}; at or above WATCHDOG_AUTO_CLOSE_CONFIDENCE (default 80) the watchdog approves '
                "it and closes the task, otherwise the owner decides."
            ),
            "blocked_by_task_ids": [],
        },
    )


def _review_required_for_done_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": ("Task can only be marked done from review when the board rule is enabled."),
            "blocked_by_task_ids": [],
        },
    )


def _pending_approval_blocks_status_change_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": ("Task status cannot be changed while a linked approval is pending."),
            "blocked_by_task_ids": [],
        },
    )


async def _task_has_approved_linked_approval(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
    resolved_after: datetime | None = None,
) -> bool:
    linked_approval_ids = select(col(ApprovalTaskLink.approval_id)).where(
        col(ApprovalTaskLink.task_id) == task_id,
    )
    statement = (
        select(col(Approval.id))
        .where(col(Approval.board_id) == board_id)
        .where(col(Approval.status) == "approved")
        .where(
            or_(
                col(Approval.task_id) == task_id,
                col(Approval.id).in_(linked_approval_ids),
            ),
        )
        .limit(1)
    )
    if resolved_after is not None:
        statement = statement.where(col(Approval.resolved_at) >= resolved_after)
    return (await session.exec(statement)).first() is not None


async def _task_has_pending_linked_approval(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
) -> bool:
    conflicts = await pending_approval_conflicts_by_task(
        session,
        board_id=board_id,
        task_ids=[task_id],
    )
    return task_id in conflicts


async def _require_approved_linked_approval_for_done(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
    task_complexity: int | None = None,
    previous_status: str,
    target_status: str,
) -> None:
    if previous_status == "done" or target_status != "done":
        return
    # L1 trivial tasks: no approval needed — worker output IS the deliverable.
    if task_complexity == 1:
        return
    requires_approval = (
        await session.exec(
            select(col(Board.require_approval_for_done)).where(col(Board.id) == board_id),
        )
    ).first()
    if requires_approval is False:
        return
    if not await _task_has_approved_linked_approval(
        session,
        board_id=board_id,
        task_id=task_id,
    ):
        raise _approval_required_for_done_error()


async def _require_review_before_done_when_enabled(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_complexity: int | None = None,
    previous_status: str,
    target_status: str,
) -> None:
    if previous_status == "done" or target_status != "done":
        return
    # L1 tasks bypass the review-before-done rule — no review needed.
    if task_complexity == 1:
        return
    requires_review = (
        await session.exec(
            select(col(Board.require_review_before_done)).where(col(Board.id) == board_id),
        )
    ).first()
    if requires_review and previous_status != "review":
        raise _review_required_for_done_error()


async def _require_comment_for_review_when_enabled(
    session: AsyncSession,
    *,
    board_id: UUID,
) -> bool:
    requires_comment = (
        await session.exec(
            select(col(Board.comment_required_for_review)).where(col(Board.id) == board_id),
        )
    ).first()
    return bool(requires_comment)


async def _require_no_pending_approval_for_status_change_when_enabled(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
    previous_status: str,
    target_status: str,
    status_requested: bool,
) -> None:
    if not status_requested or previous_status == target_status or target_status in {"cancelled", "paused"}:
        return  # stopping never approves or dismisses the pending decision
    blocks_status_change = (
        await session.exec(
            select(col(Board.block_status_changes_with_pending_approval)).where(
                col(Board.id) == board_id,
            ),
        )
    ).first()
    if not blocks_status_change:
        return
    # T28 (2026-05-14): the board flag promises to block ALL status changes
    # while an approval is pending. Previously this only fired on →done/→review
    # which silently let agents flip tasks to in_progress, bypassing the
    # owner's gate. Now any non-noop status transition triggers the check.
    if await _task_has_pending_linked_approval(
        session,
        board_id=board_id,
        task_id=task_id,
    ):
        raise _pending_approval_blocks_status_change_error()


def _truncate_snippet(value: str) -> str:
    text = value.strip()
    if len(text) <= TASK_SNIPPET_MAX_LEN:
        return text
    return f"{text[:TASK_SNIPPET_TRUNCATED_LEN]}..."


async def has_valid_recent_comment(
    session: AsyncSession,
    task: Task,
    agent_id: UUID | None,
    since: datetime | None,
) -> bool:
    """Check whether the task has a recent non-empty comment by the agent."""
    if agent_id is None or since is None:
        return False
    statement = (
        select(ActivityEvent)
        .where(col(ActivityEvent.task_id) == task.id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .where(col(ActivityEvent.agent_id) == agent_id)
        .where(col(ActivityEvent.created_at) >= since)
        .order_by(desc(col(ActivityEvent.created_at)))
    )
    event = (await session.exec(statement)).first()
    if event is None or event.message is None:
        return False
    return bool(event.message.strip())


def _parse_since(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp into a naive UTC `datetime`.

    The API accepts either naive timestamps (treated as UTC) or timezone-aware values.
    Returning naive UTC simplifies SQLModel comparisons against stored naive UTC values.
    """

    if not value:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    # Allow common ISO-8601 `Z` suffix (UTC) even though `datetime.fromisoformat` expects `+00:00`.
    normalized = normalized.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)

    # No tzinfo: interpret as UTC for consistency with other API timestamps.
    return parsed


def _coerce_task_items(items: Sequence[object]) -> list[Task]:
    """Validate/convert paginated query results to a concrete `list[Task]`.

    SQLModel pagination helpers return `Sequence[object]`; we validate types early so the
    rest of the route logic can assume real `Task` instances.
    """

    tasks: list[Task] = []
    for item in items:
        if not isinstance(item, Task):
            msg = "Expected Task items from paginated query"
            raise TypeError(msg)
        tasks.append(item)
    return tasks


def _coerce_task_event_rows(
    items: Sequence[object],
) -> list[tuple[ActivityEvent, Task | None]]:
    """Normalize DB rows into `(ActivityEvent, Task | None)` tuples.

    Depending on the SQLAlchemy/SQLModel execution path, result rows may arrive as:
    - real Python tuples, or
    - row-like objects supporting `__len__` and `__getitem__`.

    This helper centralizes validation so SSE/event-stream logic can assume a stable shape.
    """

    rows: list[tuple[ActivityEvent, Task | None]] = []
    for item in items:
        first: object
        second: object

        if isinstance(item, tuple):
            if len(item) != TASK_EVENT_ROW_LEN:
                msg = "Expected (ActivityEvent, Task | None) rows"
                raise TypeError(msg)
            first, second = item
        else:
            try:
                row_len = len(item)  # type: ignore[arg-type]
                first = item[0]  # type: ignore[index]
                second = item[1]  # type: ignore[index]
            except (IndexError, KeyError, TypeError):
                msg = "Expected (ActivityEvent, Task | None) rows"
                raise TypeError(msg) from None
            if row_len != TASK_EVENT_ROW_LEN:
                msg = "Expected (ActivityEvent, Task | None) rows"
                raise TypeError(msg)

        if isinstance(first, ActivityEvent) and (isinstance(second, Task) or second is None):
            rows.append((first, second))
            continue

        msg = "Expected (ActivityEvent, Task | None) rows"
        raise TypeError(msg)
    return rows


async def _lead_was_mentioned(
    session: AsyncSession,
    task: Task,
    lead: Agent,
) -> bool:
    """Return `True` if the lead agent is mentioned in any comment on the task.

    This is used to avoid redundant lead pings (especially in auto-created tasks) while still
    ensuring escalation happens when explicitly requested.
    """

    statement = (
        select(ActivityEvent.message)
        .where(col(ActivityEvent.task_id) == task.id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .order_by(desc(col(ActivityEvent.created_at)))
    )
    for message in await session.exec(statement):
        if not message:
            continue
        mentions = extract_mentions(message)
        if matches_agent_mention(lead, mentions):
            return True
    return False


def _lead_created_task(task: Task, lead: Agent) -> bool:
    """Return `True` if `task` was auto-created by the lead agent."""

    if not task.auto_created or not task.auto_reason:
        return False
    return task.auto_reason == f"lead_agent:{lead.id}"


async def _reconcile_dependents_for_dependency_toggle(
    session: AsyncSession,
    *,
    board_id: UUID,
    dependency_task: Task,
    previous_status: str,
    actor_agent_id: UUID | None,
) -> None:
    """Apply dependency side-effects when a dependency task toggles done/undone.

    The UI models dependencies as a DAG: when a dependency is reopened, dependents that were
    previously marked done may need to be reopened or flagged. This helper keeps dependent state
    consistent with the dependency graph without duplicating logic across endpoints.
    """

    done_toggled = (previous_status == "done") != (dependency_task.status == "done")
    if not done_toggled:
        return

    dependent_ids = await dependent_task_ids(
        session,
        board_id=board_id,
        dependency_task_id=dependency_task.id,
    )
    if not dependent_ids:
        return

    dependents = list(
        await session.exec(
            select(Task)
            .where(col(Task.board_id) == board_id)
            .where(col(Task.id).in_(dependent_ids))
            .order_by(col(Task.id))
            .with_for_update()
            .execution_options(populate_existing=True),
        ),
    )
    reopened = previous_status == "done" and dependency_task.status != "done"

    for dependent in dependents:
        if dependent.status in {"done", "blocked", "cancelled", "paused"}:
            continue  # dependency edits cannot resume an explicit stop
        if reopened:
            should_reset = (
                dependent.status != "inbox"
                or dependent.assigned_agent_id is not None
                or dependent.in_progress_at is not None
            )
            if should_reset:
                dependent.status = "inbox"
                dependent.assigned_agent_id = None
                dependent.in_progress_at = None
                dependent.updated_at = utcnow()
                session.add(dependent)
                record_activity(
                    session,
                    event_type="task.status_changed",
                    task_id=dependent.id,
                    message=(
                        "Task returned to inbox: dependency reopened " f"({dependency_task.title})."
                    ),
                    agent_id=actor_agent_id,
                    board_id=dependent.board_id,
                )
            else:
                record_activity(
                    session,
                    event_type="task.updated",
                    task_id=dependent.id,
                    message=f"Dependency completion changed: {dependency_task.title}.",
                    agent_id=actor_agent_id,
                    board_id=dependent.board_id,
                )
        else:
            record_activity(
                session,
                event_type="task.updated",
                task_id=dependent.id,
                message=f"Dependency completion changed: {dependency_task.title}.",
                agent_id=actor_agent_id,
                board_id=dependent.board_id,
            )


async def _fetch_task_events(
    session: AsyncSession,
    board_id: UUID,
    since: datetime,
) -> list[tuple[ActivityEvent, Task | None]]:
    task_ids = list(
        await session.exec(select(Task.id).where(col(Task.board_id) == board_id)),
    )
    if not task_ids:
        return []
    statement = (
        select(ActivityEvent, Task)
        .outerjoin(Task, col(ActivityEvent.task_id) == col(Task.id))
        .where(col(ActivityEvent.task_id).in_(task_ids))
        .where(col(ActivityEvent.event_type).in_(TASK_EVENT_TYPES))
        .where(col(ActivityEvent.created_at) >= since)
        .order_by(asc(col(ActivityEvent.created_at)))
    )
    result = await session.execute(statement)
    return _coerce_task_event_rows(list(result.tuples().all()))


def _serialize_comment(event: ActivityEvent) -> dict[str, object]:
    return TaskCommentRead.model_validate(event).model_dump(mode="json")


async def _send_lead_task_message(
    *,
    dispatch: GatewayDispatchService,
    session_key: str,
    config: GatewayClientConfig,
    message: str,
) -> OpenClawGatewayError | None:
    return await dispatch.try_send_agent_message(
        session_key=session_key,
        config=config,
        agent_name="Lead Agent",
        message=message,
        deliver=True,
    )


async def _send_agent_task_message(
    *,
    dispatch: GatewayDispatchService,
    session_key: str,
    config: GatewayClientConfig,
    agent_name: str,
    message: str,
) -> OpenClawGatewayError | None:
    # T107 (2026-05-26): deliver=True so the gateway runs inference immediately
    # on assignment instead of waiting up to a full heartbeat cycle (30-60m) to
    # drain queued messages. Idle workers were heartbeating but never claiming
    # assigned inbox tasks because the wake call only QUEUED the message.
    # PM dispatch already uses deliver=True; this restores symmetry.
    return await dispatch.try_send_agent_message(
        session_key=session_key,
        config=config,
        agent_name=(
            f"{agent_name[:20]} · {session_key.rsplit(':', 1)[-1]}"
            if ":task-" in session_key else agent_name
        ),
        message=message,
        deliver=True,
    )


async def _send_pm_task_message(
    *,
    dispatch: GatewayDispatchService,
    session_key: str,
    config: GatewayClientConfig,
    message: str,
    pm_name: str,
) -> OpenClawGatewayError | None:
    return await dispatch.try_send_agent_message(
        session_key=session_key,
        config=config,
        agent_name=pm_name,
        message=message,
        deliver=True,
    )


def _assignment_notification_message(*, board: Board, task: Task, agent: Agent) -> str:
    description = _truncate_snippet(task.description or "")
    details = [
        f"Board: {board.name}",
        f"Task: {task.title}",
        f"Task ID: {task.id}",
        f"Status: {task.status}",
    ]
    if description:
        details.append(f"Description: {description}")
    if task.status == "review" and agent.is_board_lead:
        action = (
            "Take action: review the deliverables now. "
            "Approve by moving to done or return to inbox with clear feedback."
        )
        return "TASK READY FOR LEAD REVIEW\n" + "\n".join(details) + f"\n\n{action}"
    return (
        "TASK ASSIGNED\n"
        + "\n".join(details)
        + ("\n\nTake action: open the task and begin work. " "Post updates as task comments.")
    )


def _rework_notification_message(
    *,
    board: Board,
    task: Task,
    feedback: str | None,
) -> str:
    description = _truncate_snippet(task.description or "")
    details = [
        f"Board: {board.name}",
        f"Task: {task.title}",
        f"Task ID: {task.id}",
        f"Status: {task.status}",
    ]
    if description:
        details.append(f"Description: {description}")
    requested_changes = (
        _truncate_snippet(feedback)
        if feedback and feedback.strip()
        else "Lead requested changes. Review latest task comments for exact required updates."
    )
    return (
        "CHANGES REQUESTED\n"
        + "\n".join(details)
        + "\n\nRequested changes:\n"
        + requested_changes
        + "\n\nTake action: address the requested changes, then move the task back to review."
    )


async def _latest_task_comment_by_agent(
    session: AsyncSession,
    *,
    task_id: UUID,
    agent_id: UUID,
) -> str | None:
    statement = (
        select(col(ActivityEvent.message))
        .where(col(ActivityEvent.task_id) == task_id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .where(col(ActivityEvent.agent_id) == agent_id)
        .order_by(desc(col(ActivityEvent.created_at)))
        .limit(1)
    )
    return (await session.exec(statement)).first()


async def _notification_current(session: AsyncSession, task: Task, stamp: datetime) -> bool:
    """Keep original legacy notification authority locked until its next commit."""
    await session.refresh(task, with_for_update=True)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(UTC).replace(tzinfo=None)
    if task.updated_at.replace(tzinfo=None) != stamp or task.status in ("done", "cancelled", "paused", "trash") or _harness_task(task):
        await session.rollback()
        await session.refresh(task)  # Read-only response hydration; no notification authority.
        return False
    return True


async def _wake_agent_online_for_task(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
    agent: Agent,
    reason: str,
) -> bool:
    stamp = task.updated_at
    if not await _notification_current(session, task, stamp):
        return False
    if not agent.openclaw_session_id:
        return False
    service = AgentLifecycleService(session)
    try:
        await service.commit_heartbeat(agent=agent, status_value="online")
        event_type = "task.assignee_woken"
        message = f"Assignee heartbeat set online ({reason}): {agent.name}."
    except Exception as exc:  # pragma: no cover - best effort wake path
        event_type = "task.assignee_wake_failed"
        message = f"Assignee wake failed ({reason}): {agent.name}. Error: {exc!s}"
    # Heartbeat commits independently; never attach its result to changed work.
    if not await _notification_current(session, task, stamp):
        return False
    record_activity(session, event_type=event_type, message=message,
                    agent_id=agent.id, task_id=task.id, board_id=board.id)
    await session.commit()
    return event_type == "task.assignee_woken"


def _delivery_precondition_failure(agent: Agent) -> str | None:
    """Reason string when a notification cannot be sent to this agent, else
    None. A missing or gateway-unparseable session key used to silently
    early-return, hiding total delivery failure (2026-07-02 incident class);
    every skip must leave an observable event instead.
    """
    if not agent.openclaw_session_id:
        return "missing openclaw_session_id"
    if not is_gateway_parseable_session_key(agent.openclaw_session_id):
        return (
            f"malformed openclaw_session_id {agent.openclaw_session_id!r} "
            "(agent-form keys need >=3 colon-separated parts, "
            "e.g. agent:mc-<id>:main)"
        )
    return None


async def _record_delivery_skip(
    session: AsyncSession,
    *,
    event_type: str,
    reason: str,
    board: Board,
    task: Task,
    agent: Agent,
) -> None:
    """Record a skipped/blocked notification as an activity event (never
    silent) and log at warning level. Bounded: callers run on assignment
    and on the 600s stuck-inbox re-notify path, so worst case is one event
    per task per re-notify interval — no extra dedup required.
    """
    record_activity(
        session,
        event_type=event_type,
        message=f"Delivery skipped for {agent.name}: {reason}",
        agent_id=agent.id,
        task_id=task.id,
        board_id=board.id,
    )
    await session.commit()
    get_logger(__name__).warning(
        "task_notify.delivery_skipped event=%s task=%s agent=%s reason=%s",
        event_type,
        str(task.id)[:8],
        agent.name,
        reason,
    )


def _task_session_key(agent: Agent, task: Task) -> str:
    """Give each task its own agent conversation, not the agent's accumulated main chat."""
    if not agent.openclaw_session_id:
        raise ValueError("Agent has no gateway session")
    return f"{agent.openclaw_session_id.rsplit(':', 1)[0]}:task-{task.id}"


async def _notify_agent_on_task_assign(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
    agent: Agent,
    wake_assignee: bool = True,
) -> bool:
    stamp = task.updated_at
    if not await _notification_current(session, task, stamp):
        return False
    if not agent.openclaw_session_id:
        # Original code silently early-returned here (pre-wake, so wake
        # semantics are unchanged); the skip is now observable.
        await _record_delivery_skip(
            session,
            event_type="task.assignee_notify_failed",
            reason="missing openclaw_session_id",
            board=board,
            task=task,
            agent=agent,
        )
        return False
    if wake_assignee:
        await _wake_agent_online_for_task(
            session=session,
            board=board,
            task=task,
            agent=agent,
            reason="assignment",
        )
        # Wake commits independently: reacquire original authority before delivery.
        if not await _notification_current(session, task, stamp):
            return False
    # AFTER the wake (which is legitimate assignment bookkeeping either
    # way): a key that claims the agent form but is gateway-unparseable
    # would be refused with "Malformed agent session key" — record and
    # skip the pointless send.
    if not is_gateway_parseable_session_key(agent.openclaw_session_id):
        await _record_delivery_skip(
            session,
            event_type="task.assignee_notify_failed",
            reason=(
                f"malformed openclaw_session_id {agent.openclaw_session_id!r} "
                "(agent-form keys need >=3 colon-separated parts, "
                "e.g. agent:mc-<id>:main)"
            ),
            board=board,
            task=task,
            agent=agent,
        )
        return False
    dispatch = GatewayDispatchService(session)
    config = await dispatch.optional_gateway_config_for_board(board)
    if config is None:
        await _record_delivery_skip(
            session,
            event_type="task.assignee_notify_failed",
            reason="no gateway config resolvable for board",
            board=board,
            task=task,
            agent=agent,
        )
        return False
    message = _assignment_notification_message(board=board, task=task, agent=agent)
    error = await _send_agent_task_message(
        dispatch=dispatch,
        session_key=_task_session_key(agent, task),
        config=config,
        agent_name=agent.name,
        message=message,
    )
    if error is None:
        record_activity(
            session,
            event_type="task.assignee_notified",
            message=f"Agent notified for assignment: {agent.name}.",
            agent_id=agent.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()
    else:
        record_activity(
            session,
            event_type="task.assignee_notify_failed",
            message=f"Assignee notify failed: {error}",
            agent_id=agent.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()
    return error is None


async def _notify_agent_on_task_rework(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
    agent: Agent,
    lead: Agent,
) -> None:
    stamp = task.updated_at
    if not await _notification_current(session, task, stamp):
        return
    skip_reason = _delivery_precondition_failure(agent)
    if skip_reason is not None:
        await _record_delivery_skip(
            session,
            event_type="task.rework_notify_failed",
            reason=skip_reason,
            board=board,
            task=task,
            agent=agent,
        )
        return
    dispatch = GatewayDispatchService(session)
    config = await dispatch.optional_gateway_config_for_board(board)
    if config is None:
        await _record_delivery_skip(
            session,
            event_type="task.rework_notify_failed",
            reason="no gateway config resolvable for board",
            board=board,
            task=task,
            agent=agent,
        )
        return
    feedback = await _latest_task_comment_by_agent(
        session,
        task_id=task.id,
        agent_id=lead.id,
    )
    message = _rework_notification_message(
        board=board,
        task=task,
        feedback=feedback,
    )
    error = await _send_agent_task_message(
        dispatch=dispatch,
        session_key=_task_session_key(agent, task),
        config=config,
        agent_name=agent.name,
        message=message,
    )
    if error is None:
        record_activity(
            session,
            event_type="task.rework_notified",
            message=f"Assignee notified about requested changes: {agent.name}.",
            agent_id=agent.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()
    else:
        record_activity(
            session,
            event_type="task.rework_notify_failed",
            message=f"Rework notify failed: {error}",
            agent_id=agent.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()


async def notify_agent_on_task_assign(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
    agent: Agent,
) -> bool:
    """Return whether assignment delivery succeeded; refusal/failure is False."""
    return await _notify_agent_on_task_assign(
        session=session,
        board=board,
        task=task,
        agent=agent,
    )


async def _notify_lead_on_task_create(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
    stamp: datetime | None = None,
) -> None:
    """Wake and notify lead + PMs when a new task is created.

    Strategy:
    1. Wake only lead + PM agents (NOT all workers — waking idle workers
       causes them to loop on status updates they lack permission for).
    2. Send the lead an actionable message (deliver=True) to triage immediately.
    3. Send PM agents a condensed situational notice (do not wake).
    4. Workers are woken separately via _notify_agent_on_task_assign when
       the lead actually routes the task to them.
    """
    stamp = stamp or task.updated_at
    if not await _notification_current(session, task, stamp):
        return
    lead = (
        await Agent.objects.filter_by(board_id=board.id)
        .filter(col(Agent.is_board_lead).is_(True))
        .first(session)
    )

    all_agents = list(
        await Agent.objects.filter_by(board_id=board.id).all(session),
    )
    pm_agents = [
        a for a in all_agents
        if a.is_project_manager and not a.is_board_lead
    ]

    dispatch = GatewayDispatchService(session)
    config = await dispatch.optional_gateway_config_for_board(board)
    if config is None:
        return

    description = _truncate_snippet(task.description or "")
    details = [
        f"Board: {board.name}",
        f"Task: {task.title}",
        f"Task ID: {task.id}",
        f"Status: {task.status}",
    ]
    if description:
        details.append(f"Description: {description}")

    # ── Wake lead + PMs only (NOT all workers) ──
    targets = [a for a in all_agents if a.is_board_lead or a.is_project_manager]
    woken = 0
    for agent in targets:
        if not await _notification_current(session, task, stamp):
            return
        if not agent.openclaw_session_id:
            continue
        try:
            woken += int(await _wake_agent_online_for_task(
                session=session,
                board=board,
                task=task,
                agent=agent,
                reason="new task created",
            ))
        except Exception:
            pass
    if not await _notification_current(session, task, stamp):
        return
    if woken:
        record_activity(
            session,
            event_type="task.leads_pms_woken",
            message=f"Woke {woken} lead/PM agents on board for new task: {task.title}.",
            task_id=task.id,
            board_id=board.id,
        )

    # ── Lead: deliver actionable triage command ──
    if lead and lead.openclaw_session_id:
        assign_block = ""
        if task.assigned_agent_id:
            assignee = next(
                (a for a in all_agents if a.id == task.assigned_agent_id), None,
            )
            if assignee:
                assign_block = f"\nAssigned to: {assignee.name}"

        lead_message = (
            "NEW TASK — TRIAGE IMMEDIATELY\n"
            + "\n".join(details)
            + assign_block
            + "\n\nACTION REQUIRED: Review this task now. "
            "If unassigned, route it to the appropriate agent. "
            "If assigned, verify the assignment. "
            "Post a plan comment if scope is unclear."
            "\n\nCRITICAL: If you start working on this task, "
            "use update_status (or equivalent tool) to move it to "
            "\"in_progress\" BEFORE posting your response. "
            "Tasks left in inbox prevent downstream automation."
        )
        error = await _send_lead_task_message(
            dispatch=dispatch,
            session_key=lead.openclaw_session_id,
            config=config,
            message=lead_message,
        )
        if error is None:
            record_activity(
                session,
                event_type="task.lead_notified",
                message=f"Lead agent notified for task: {task.title}.",
                agent_id=lead.id,
                task_id=task.id,
                board_id=board.id,
            )
        else:
            record_activity(
                session,
                event_type="task.lead_notify_failed",
                message=f"Lead notify failed: {error}",
                agent_id=lead.id,
                task_id=task.id,
                board_id=board.id,
            )
    elif not lead:
        pm_message = (
            "NEW TASK ADDED (NO LEAD)\n"
            + "\n".join(details)
            + "\n\nACTION REQUIRED: No board lead exists. "
            "One of you must triage and assign this task immediately."
        )
        for pm in pm_agents:
            if not pm.openclaw_session_id:
                continue
            await _send_pm_task_message(
                dispatch=dispatch,
                session_key=pm.openclaw_session_id,
                config=config,
                message=pm_message,
                pm_name=pm.name,
            )

    # ── PM agents: situational awareness ──
    if lead:  # Only if lead is alive — avoid duplicate noise
        pm_message = (
            "NEW TASK (leads triaged)\n"
            + "\n".join(details)
            + "\n\nHeads-up: a new task was created. "
            "The lead has been asked to triage. "
            "Stand by for potential assignment."
        )
        for pm in pm_agents:
            if not pm.openclaw_session_id:
                continue
            await _send_pm_task_message(
                dispatch=dispatch,
                session_key=pm.openclaw_session_id,
                config=config,
                message=pm_message,
                pm_name=pm.name,
            )

    await session.commit()


async def _wake_lead_pms_for_subtask_creation(
    *,
    session: AsyncSession,
    board: Board,
    parent_task: Task,
    children: list[Task],
) -> int:
    """T90 (2026-05-16): event-driven wake for split parents.

    Previously subtasks were created with `status="inbox"` + no
    `assigned_agent_id`, so they only got routed on the next watchdog
    tick (180s wait). Deepseek-v4-pro audit flagged this as the real
    serial-processing bottleneck for L4/L5 tasks.

    This helper mirrors `_notify_lead_on_task_create` but is scoped to
    subtask-discovery: wake lead + PMs once after the split commits so
    they pull the new inbox children immediately. Each child still gets
    routed via the existing watchdog inbox-triage step; this just
    eliminates the latency between split and discovery.

    Returns the count of agents woken.
    """
    stamp = parent_task.updated_at
    if not children or not await _notification_current(session, parent_task, stamp):
        return 0
    all_agents = list(
        await Agent.objects.filter_by(board_id=board.id).all(session),
    )
    targets = [a for a in all_agents if a.is_board_lead or a.is_project_manager]
    woken = 0
    for agent in targets:
        if not await _notification_current(session, parent_task, stamp):
            return 0
        if not agent.openclaw_session_id:
            continue
        try:
            # Reuse the same wake primitive top-level tasks use. We wake
            # against the PARENT task so the activity event chain stays
            # linked; the lead can list children via API on receiving the
            # signal.
            woken += int(await _wake_agent_online_for_task(
                session=session,
                board=board,
                task=parent_task,
                agent=agent,
                reason=f"subtasks created ({len(children)} children)",
            ))
        except Exception:
            pass
    if not await _notification_current(session, parent_task, stamp):
        return 0
    if woken:
        record_activity(
            session,
            event_type="task.subtasks_woken",
            message=(
                f"Woke {woken} lead/PM agents for {len(children)} subtasks "
                f"of parent {parent_task.id}."
            ),
            task_id=parent_task.id,
            board_id=board.id,
        )
        await session.commit()
    return woken


async def _notify_lead_on_task_unassigned(
    *,
    session: AsyncSession,
    board: Board,
    task: Task,
) -> None:
    stamp = task.updated_at
    if not await _notification_current(session, task, stamp):
        return
    lead = (
        await Agent.objects.filter_by(board_id=board.id)
        .filter(col(Agent.is_board_lead).is_(True))
        .first(session)
    )
    if lead is None or not lead.openclaw_session_id:
        return
    dispatch = GatewayDispatchService(session)
    config = await dispatch.optional_gateway_config_for_board(board)
    if config is None:
        return
    description = _truncate_snippet(task.description or "")
    details = [
        f"Board: {board.name}",
        f"Task: {task.title}",
        f"Task ID: {task.id}",
        f"Status: {task.status}",
    ]
    if description:
        details.append(f"Description: {description}")
    message = (
        "TASK BACK IN INBOX\n"
        + "\n".join(details)
        + "\n\nTake action: assign a new owner or adjust the plan."
    )
    error = await _send_lead_task_message(
        dispatch=dispatch,
        session_key=lead.openclaw_session_id,
        config=config,
        message=message,
    )
    if error is None:
        record_activity(
            session,
            event_type="task.lead_unassigned_notified",
            message=f"Lead notified task returned to inbox: {task.title}.",
            agent_id=lead.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()
    else:
        record_activity(
            session,
            event_type="task.lead_unassigned_notify_failed",
            message=f"Lead notify failed: {error}",
            agent_id=lead.id,
            task_id=task.id,
            board_id=board.id,
        )
        await session.commit()


def _status_values(status_filter: str | None) -> list[str]:
    if not status_filter:
        return []
    values = [s.strip() for s in status_filter.split(",") if s.strip()]
    if any(value not in ALLOWED_STATUSES for value in values):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported task status filter.",
        )
    return values


async def _organization_custom_field_definitions_for_board(
    session: AsyncSession,
    *,
    board_id: UUID,
) -> dict[str, _BoardCustomFieldDefinition]:
    organization_id = (
        await session.exec(
            select(Board.organization_id).where(col(Board.id) == board_id),
        )
    ).first()
    if organization_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    definitions = list(
        await session.exec(
            select(TaskCustomFieldDefinition)
            .join(
                BoardTaskCustomField,
                col(BoardTaskCustomField.task_custom_field_definition_id)
                == col(TaskCustomFieldDefinition.id),
            )
            .where(
                col(BoardTaskCustomField.board_id) == board_id,
                col(TaskCustomFieldDefinition.organization_id) == organization_id,
            ),
        ),
    )
    return {
        definition.field_key: _BoardCustomFieldDefinition(
            id=definition.id,
            field_key=definition.field_key,
            field_type=cast(TaskCustomFieldType, definition.field_type),
            validation_regex=definition.validation_regex,
            required=definition.required,
            default_value=definition.default_value,
        )
        for definition in definitions
    }


def _reject_unknown_custom_field_keys(
    *,
    custom_field_values: TaskCustomFieldValues,
    definitions_by_key: dict[str, _BoardCustomFieldDefinition],
) -> None:
    unknown_field_keys = sorted(set(custom_field_values) - set(definitions_by_key))
    if not unknown_field_keys:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "Unknown custom field keys for this board.",
            "unknown_field_keys": unknown_field_keys,
        },
    )


def _reject_missing_required_custom_field_keys(
    *,
    effective_values: TaskCustomFieldValues,
    definitions_by_key: dict[str, _BoardCustomFieldDefinition],
) -> None:
    missing_field_keys = [
        definition.field_key
        for definition in definitions_by_key.values()
        if definition.required and effective_values.get(definition.field_key) is None
    ]
    if not missing_field_keys:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "Required custom fields must have values.",
            "missing_field_keys": sorted(missing_field_keys),
        },
    )


def _reject_invalid_custom_field_values(
    *,
    custom_field_values: TaskCustomFieldValues,
    definitions_by_key: dict[str, _BoardCustomFieldDefinition],
) -> None:
    for field_key, value in custom_field_values.items():
        definition = definitions_by_key[field_key]
        try:
            validate_custom_field_value(
                field_type=definition.field_type,
                value=value,
                validation_regex=definition.validation_regex,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": "Invalid custom field value.",
                    "field_key": field_key,
                    "field_type": definition.field_type,
                    "reason": str(err),
                },
            ) from err


async def _task_custom_field_rows_by_definition_id(
    session: AsyncSession,
    *,
    task_id: UUID,
    definition_ids: list[UUID],
) -> dict[UUID, TaskCustomFieldValue]:
    if not definition_ids:
        return {}
    rows = list(
        await session.exec(
            select(TaskCustomFieldValue).where(
                col(TaskCustomFieldValue.task_id) == task_id,
                col(TaskCustomFieldValue.task_custom_field_definition_id).in_(definition_ids),
            ),
        ),
    )
    return {row.task_custom_field_definition_id: row for row in rows}


async def _set_task_custom_field_values_for_create(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
    custom_field_values: TaskCustomFieldValues,
) -> None:
    definitions_by_key = await _organization_custom_field_definitions_for_board(
        session,
        board_id=board_id,
    )
    _reject_unknown_custom_field_keys(
        custom_field_values=custom_field_values,
        definitions_by_key=definitions_by_key,
    )
    _reject_invalid_custom_field_values(
        custom_field_values=custom_field_values,
        definitions_by_key=definitions_by_key,
    )

    effective_values: TaskCustomFieldValues = {}
    for field_key, definition in definitions_by_key.items():
        if field_key in custom_field_values:
            effective_values[field_key] = custom_field_values[field_key]
        else:
            effective_values[field_key] = definition.default_value

    _reject_missing_required_custom_field_keys(
        effective_values=effective_values,
        definitions_by_key=definitions_by_key,
    )

    for field_key, definition in definitions_by_key.items():
        value = effective_values.get(field_key)
        if value is None:
            continue
        session.add(
            TaskCustomFieldValue(
                task_id=task_id,
                task_custom_field_definition_id=definition.id,
                value=value,
            ),
        )


async def _set_task_custom_field_values_for_update(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
    custom_field_values: TaskCustomFieldValues,
) -> None:
    definitions_by_key = await _organization_custom_field_definitions_for_board(
        session,
        board_id=board_id,
    )
    _reject_unknown_custom_field_keys(
        custom_field_values=custom_field_values,
        definitions_by_key=definitions_by_key,
    )
    _reject_invalid_custom_field_values(
        custom_field_values=custom_field_values,
        definitions_by_key=definitions_by_key,
    )
    definitions_by_id = {definition.id: definition for definition in definitions_by_key.values()}
    rows_by_definition_id = await _task_custom_field_rows_by_definition_id(
        session,
        task_id=task_id,
        definition_ids=list(definitions_by_id),
    )

    effective_values: TaskCustomFieldValues = {}
    for field_key, definition in definitions_by_key.items():
        current_row = rows_by_definition_id.get(definition.id)
        if field_key in custom_field_values:
            effective_values[field_key] = custom_field_values[field_key]
        elif current_row is not None:
            effective_values[field_key] = current_row.value
        else:
            effective_values[field_key] = definition.default_value

    _reject_missing_required_custom_field_keys(
        effective_values=effective_values,
        definitions_by_key=definitions_by_key,
    )

    for field_key, value in custom_field_values.items():
        definition = definitions_by_key[field_key]
        row = rows_by_definition_id.get(definition.id)
        if value is None:
            if row is not None:
                await session.delete(row)
            continue
        if row is None:
            session.add(
                TaskCustomFieldValue(
                    task_id=task_id,
                    task_custom_field_definition_id=definition.id,
                    value=value,
                ),
            )
            continue
        row.value = value
        row.updated_at = utcnow()
        session.add(row)


async def _task_custom_field_values_by_task_id(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_ids: Sequence[UUID],
) -> dict[UUID, TaskCustomFieldValues]:
    unique_task_ids = list({*task_ids})
    if not unique_task_ids:
        return {}

    definitions_by_key = await _organization_custom_field_definitions_for_board(
        session,
        board_id=board_id,
    )
    if not definitions_by_key:
        return {task_id: {} for task_id in unique_task_ids}

    definitions_by_id = {definition.id: definition for definition in definitions_by_key.values()}
    default_values = {
        field_key: definition.default_value for field_key, definition in definitions_by_key.items()
    }
    values_by_task_id: dict[UUID, TaskCustomFieldValues] = {
        task_id: dict(default_values) for task_id in unique_task_ids
    }

    rows = (
        await session.exec(
            select(
                col(TaskCustomFieldValue.task_id),
                col(TaskCustomFieldValue.task_custom_field_definition_id),
                col(TaskCustomFieldValue.value),
            ).where(
                col(TaskCustomFieldValue.task_id).in_(unique_task_ids),
                col(TaskCustomFieldValue.task_custom_field_definition_id).in_(
                    list(definitions_by_id),
                ),
            ),
        )
    ).all()
    for task_id, definition_id, value in rows:
        definition = definitions_by_id.get(definition_id)
        if definition is None:
            continue
        values_by_task_id[task_id][definition.field_key] = value
    return values_by_task_id


def _task_list_statement(
    *,
    board_id: UUID,
    status_filter: str | None,
    assigned_agent_id: UUID | None,
    unassigned: bool | None,
) -> SelectOfScalar[Task]:
    statement = select(Task).where(Task.board_id == board_id)
    statuses = _status_values(status_filter)
    if statuses:
        statement = statement.where(col(Task.status).in_(statuses))
    else:
        # Terminal history is available through explicit status filters only.
        statement = statement.where(col(Task.status).notin_(["done", "cancelled", "trash"]))
    if assigned_agent_id is not None:
        statement = statement.where(col(Task.assigned_agent_id) == assigned_agent_id)
    if unassigned:
        statement = statement.where(col(Task.assigned_agent_id).is_(None))
    return statement.order_by(col(Task.created_at).desc())


async def _task_read_page(
    *,
    session: AsyncSession,
    board_id: UUID,
    tasks: Sequence[Task],
) -> list[TaskRead]:
    if not tasks:
        return []

    task_ids = [task.id for task in tasks]
    tag_state_by_task_id = await load_tag_state(
        session,
        task_ids=task_ids,
    )
    deps_map = await dependency_ids_by_task_id(
        session,
        board_id=board_id,
        task_ids=task_ids,
    )
    dep_ids: list[UUID] = []
    for value in deps_map.values():
        dep_ids.extend(value)
    dep_status = await dependency_status_by_id(
        session,
        board_id=board_id,
        dependency_ids=list({*dep_ids}),
    )
    custom_field_values_by_task_id = await _task_custom_field_values_by_task_id(
        session,
        board_id=board_id,
        task_ids=task_ids,
    )

    # Batch-load stage timings for all tasks in this page
    timing_rows = list(
        await TaskStageTiming.objects.filter(
            col(TaskStageTiming.task_id).in_(task_ids),
        )
        .order_by(col(TaskStageTiming.entered_at))
        .all(session),
    )
    timing_agent_ids = {r.agent_id for r in timing_rows if r.agent_id is not None}
    agent_names_map: dict[UUID, str] = {}
    if timing_agent_ids:
        timing_agents = list(
            await Agent.objects.filter(col(Agent.id).in_(timing_agent_ids)).all(session),
        )
        agent_names_map = {a.id: a.name for a in timing_agents}
    timings_by_task_id: dict[UUID, list[dict[str, object]]] = {}
    for row in timing_rows:
        timings_by_task_id.setdefault(row.task_id, []).append({
            "id": row.id,
            "status": row.status,
            "agent_id": row.agent_id,
            "agent_name": agent_names_map.get(row.agent_id) if row.agent_id else None,
            "entered_at": row.entered_at,
            "exited_at": row.exited_at,
            "duration_seconds": row.duration_seconds,
        })

    from app.services.task_execution import task_execution_state

    work_by_task_id = await task_execution_state(session, board_id=board_id, tasks=list(tasks))
    output: list[TaskRead] = []
    for task in tasks:
        tag_state = tag_state_by_task_id.get(task.id, TagState())
        dep_list = deps_map.get(task.id, [])
        blocked_by = blocked_by_dependency_ids(
            dependency_ids=dep_list,
            status_by_id=dep_status,
        )
        if task.status == "done":
            blocked_by = []
        output.append(
            TaskRead.model_validate(task, from_attributes=True).model_copy(
                update={
                    "depends_on_task_ids": dep_list,
                    "tag_ids": tag_state.tag_ids,
                    "tags": tag_state.tags,
                    "blocked_by_task_ids": blocked_by,
                    "is_blocked": bool(blocked_by),
                    "custom_field_values": custom_field_values_by_task_id.get(task.id, {}),
                    "stage_timings": timings_by_task_id.get(task.id, []),
                    **work_by_task_id[task.id],
                },
            ),
        )
    return output


async def _stream_task_state(
    session: AsyncSession,
    *,
    board_id: UUID,
    rows: list[tuple[ActivityEvent, Task | None]],
) -> tuple[
    dict[UUID, list[UUID]],
    dict[UUID, str],
    dict[UUID, TagState],
    dict[UUID, TaskCustomFieldValues],
]:
    task_ids = [
        task.id for event, task in rows if task is not None and event.event_type != "task.comment"
    ]
    if not task_ids:
        return {}, {}, {}, {}

    tag_state_by_task_id = await load_tag_state(
        session,
        task_ids=list({*task_ids}),
    )
    deps_map = await dependency_ids_by_task_id(
        session,
        board_id=board_id,
        task_ids=list({*task_ids}),
    )
    dep_ids: list[UUID] = []
    for value in deps_map.values():
        dep_ids.extend(value)
    custom_field_values_by_task_id = await _task_custom_field_values_by_task_id(
        session,
        board_id=board_id,
        task_ids=list({*task_ids}),
    )
    if not dep_ids:
        return deps_map, {}, tag_state_by_task_id, custom_field_values_by_task_id

    dep_status = await dependency_status_by_id(
        session,
        board_id=board_id,
        dependency_ids=list({*dep_ids}),
    )
    return deps_map, dep_status, tag_state_by_task_id, custom_field_values_by_task_id


def _task_event_payload(
    event: ActivityEvent,
    task: Task | None,
    *,
    deps_map: dict[UUID, list[UUID]],
    dep_status: dict[UUID, str],
    tag_state_by_task_id: dict[UUID, TagState],
    custom_field_values_by_task_id: dict[UUID, TaskCustomFieldValues] | None = None,
    work_by_task_id: dict[UUID, dict[str, object]] | None = None,
) -> dict[str, object]:
    resolved_custom_field_values_by_task_id = custom_field_values_by_task_id or {}
    payload: dict[str, object] = {
        "type": event.event_type,
        "activity": ActivityEventRead.model_validate(event).model_dump(
            mode="json",
            exclude={"board_id", "route_name", "route_params"},
        ),
    }
    if work_by_task_id:
        payload["execution_updates"] = {
            str(task_id): {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in work.items()
            } for task_id, work in work_by_task_id.items()
        }
    if event.event_type == "task.comment":
        payload["comment"] = _serialize_comment(event)
        return payload
    if task is None:
        payload["task"] = None
        return payload

    tag_state = tag_state_by_task_id.get(task.id, TagState())
    dep_list = deps_map.get(task.id, [])
    blocked_by = blocked_by_dependency_ids(
        dependency_ids=dep_list,
        status_by_id=dep_status,
    )
    if task.status == "done":
        blocked_by = []
    payload["task"] = (
        TaskRead.model_validate(task, from_attributes=True)
        .model_copy(
            update={
                "depends_on_task_ids": dep_list,
                "tag_ids": tag_state.tag_ids,
                "tags": tag_state.tags,
                "blocked_by_task_ids": blocked_by,
                "is_blocked": bool(blocked_by),
                "custom_field_values": resolved_custom_field_values_by_task_id.get(
                    task.id,
                    {},
                ),
                **(work_by_task_id or {}).get(task.id, {}),
            },
        )
        .model_dump(mode="json")
    )
    return payload


# T51 (2026-05-14): in-process event notifier replaces 2s blind polling.
# Code paths that mutate `tasks` call `notify_task_changed(board_id)` after
# commit; SSE generator awaits the per-board event with a 2s safety timeout.
# Wake-up latency drops from 2s avg → near-zero. The 2s remains as a floor
# for cross-process safety (e.g., watchdog tick in a different worker) and
# disconnect-detection.
from app.services.task_event_bus import wait_for_task_change


async def _task_event_generator(
    *,
    request: Request,
    board_id: UUID,
    since_dt: datetime,
) -> AsyncIterator[dict[str, str]]:
    last_seen = since_dt
    seen_ids: set[UUID] = set()
    seen_queue: deque[UUID] = deque()

    while True:
        if await request.is_disconnected():
            break

        async with async_session_maker() as session:
            rows = await _fetch_task_events(session, board_id, last_seen)
            deps_map, dep_status, tag_state_by_task_id, custom_field_values_by_task_id = (
                await _stream_task_state(
                    session,
                    board_id=board_id,
                    rows=rows,
                )
            )
            from app.services.task_execution import task_execution_state

            work_tasks = {task.id: task for _, task in rows if task is not None}
            parent_ids = {task.parent_task_id for task in work_tasks.values() if task.parent_task_id}
            if parent_ids:
                for parent in (await session.exec(select(Task).where(
                    Task.board_id == board_id, col(Task.id).in_(parent_ids),
                ))).all():
                    work_tasks[parent.id] = parent
            work_by_task_id = await task_execution_state(
                session, board_id=board_id, tasks=list(work_tasks.values()),
            )

        for event, task in rows:
            if event.id in seen_ids:
                continue
            seen_ids.add(event.id)
            seen_queue.append(event.id)
            if len(seen_queue) > SSE_SEEN_MAX:
                oldest = seen_queue.popleft()
                seen_ids.discard(oldest)
            last_seen = max(event.created_at, last_seen)

            payload = _task_event_payload(
                event,
                task,
                deps_map=deps_map,
                dep_status=dep_status,
                tag_state_by_task_id=tag_state_by_task_id,
                custom_field_values_by_task_id=custom_field_values_by_task_id,
                work_by_task_id=work_by_task_id,
            )
            yield {"event": "task", "data": json.dumps(payload)}
        # T51: wait for in-process notify OR 2s timeout (safety floor).
        await wait_for_task_change(board_id, timeout=2.0)


@router.get("/stream")
async def stream_tasks(
    request: Request,
    board: Board = BOARD_READ_DEP,
    _actor: ActorContext = ACTOR_DEP,
    since: str | None = SINCE_QUERY,
) -> EventSourceResponse:
    """Stream task and task-comment events as SSE payloads."""
    since_dt = _parse_since(since) or utcnow()
    return EventSourceResponse(
        _task_event_generator(
            request=request,
            board_id=board.id,
            since_dt=since_dt,
        ),
        ping=15,
    )


@router.get("", response_model=DefaultLimitOffsetPage[TaskRead])
async def list_tasks(
    status_filter: str | None = STATUS_QUERY,
    assigned_agent_id: UUID | None = None,
    unassigned: bool | None = None,
    board: Board = BOARD_READ_DEP,
    session: AsyncSession = SESSION_DEP,
    _actor: ActorContext = ACTOR_DEP,
) -> LimitOffsetPage[TaskRead]:
    """List board tasks with optional status and assignment filters."""
    statement = _task_list_statement(
        board_id=board.id,
        status_filter=status_filter,
        assigned_agent_id=assigned_agent_id,
        unassigned=unassigned,
    )

    async def _transform(items: Sequence[object]) -> Sequence[object]:
        tasks = _coerce_task_items(items)
        return await _task_read_page(
            session=session,
            board_id=board.id,
            tasks=tasks,
        )

    return await paginate(session, statement, transformer=_transform)


# Lane routing. The two lanes are not interchangeable: only the coding-harness
# lane can edit files, see images, or reach a vendor CLI, and measurement on
# 2026-09-29 put it at ~189k weighted tokens per delivered result against
# ~22M for the gateway-agent lane. So a task whose shape requires those
# capabilities is routed rather than left to chance, and an explicit choice by
# the caller always wins.
_HARNESS_SHAPE_HINTS = (
    "workspace_path",  # names a folder to work in
)
_LEGACY_HARNESS_PREFIXES = ("[agent]", "[claude]", "[codex]")


def _harness_task(task: Task) -> bool:
    return task.execution_mode == "team" or task.execution_lane == "harness" or (
        not task.execution_lane and (task.title or "").startswith(_LEGACY_HARNESS_PREFIXES)
    )


def _check_team_mode(mode: str | None, lane: str | None, workspace: str | None, assignee: object) -> None:
    """Team mode is harness-only, runs in the runner's own checkout, and has no gateway assignee."""
    if mode is None:
        raise HTTPException(status_code=422, detail="execution_mode cannot be null")
    if mode == "team" and (lane != "harness" or workspace or assignee is not None):
        raise HTTPException(
            status_code=422,
            detail="Team mode needs the harness lane, no workspace_path and no gateway assignee",
        )


def infer_execution_lane(data: dict) -> str:
    """Pick a lane for a task that did not name one.

    Ordinary work uses gateway agents; explicit choices and coding hints win.
    Persist the lane so executors and reviewers agree on ownership.
    """
    if data.get("execution_lane"):
        return data["execution_lane"]  # caller decided; never override
    if data.get("execution_mode") == "team":
        return "harness"
    if any(data.get(k) for k in _HARNESS_SHAPE_HINTS):
        return "harness"
    # A pinned engine only means anything to the harness lane.
    if data.get("harness_provider") or data.get("harness_model"):
        return "harness"
    if (data.get("title") or "").startswith(_LEGACY_HARNESS_PREFIXES):
        return "harness"
    return "agent"  # ordinary work belongs to the existing gateway execution/review path


@router.post("", response_model=TaskRead, responses={409: {"model": BlockedTaskError}})

async def create_task(
    payload: TaskCreate,
    board: Board = BOARD_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = USER_AUTH_DEP,
) -> TaskRead:
    """Create a task and initialize dependency rows."""
    board_id = board.id
    data = payload.model_dump(exclude={"depends_on_task_ids", "tag_ids", "custom_field_values"})
    inferred_lane = infer_execution_lane(data)
    data["execution_lane"] = inferred_lane
    if inferred_lane == "harness" and data.get("assigned_agent_id") is not None:
        raise HTTPException(status_code=422, detail="Harness tasks cannot have a gateway assignee")
    # ponytail: legacy cron clone loses lane/workspace; add cron support only
    # after cycle-safe cloning is implemented (S3).
    if inferred_lane == "harness" and data.get("repeat_enabled") and data.get("repeat_cron"):
        raise HTTPException(status_code=422, detail="Harness cron repeats are not supported yet")
    if ("execution_mode" not in payload.model_fields_set and inferred_lane == "harness"
            and not data.get("workspace_path")):
        data["execution_mode"] = "team"  # use the running executor; explicit legacy choices win
    _check_team_mode(
        data.get("execution_mode"), inferred_lane, data.get("workspace_path"), data.get("assigned_agent_id")
    )
    depends_on_task_ids = list(payload.depends_on_task_ids)
    tag_ids = list(payload.tag_ids)
    custom_field_values = dict(payload.custom_field_values)

    task = Task.model_validate(data)
    task.board_id = board.id
    if task.created_by_user_id is None and auth.user is not None:
        task.created_by_user_id = auth.user.id

    # Shared assessor: explicit levels win; with the LLM switch off this
    # records a deterministic keyword/history verdict without a model call.
    from app.services.complexity_assessor import populate_task_complexity
    await populate_task_complexity(session, task)

    # Auto-default repeat schedule: if repeat is enabled but no schedule is
    # set, default to daily (1440 min). Without this, tasks with
    # repeat_enabled=true go to done and NEVER repeat — confusing the user
    # who sees them "disappear" into the 600+ done-task pile.
    if task.repeat_enabled and task.repeat_cron is None and task.repeat_interval_minutes is None:
        task.repeat_interval_minutes = 1440  # daily

    # T104 (2026-05-23): per-complexity budget defaults so the existing
    # `_enforce_task_budgets` watchdog step has a fence to swing. Previously
    # tasks were created with NULL budgets → no automatic finalize → owner
    # saw L4 tasks burn 150K-token prompts for 47 min without progress.
    # Levels chosen to match observed `agent` workloads:
    #   L1 small Q/A: 600s, 20K tokens
    #   L2 short task: 1200s, 40K tokens
    #   L3 multi-step: 1800s, 80K tokens
    #   L4 multi-stage: 3600s, 200K tokens
    #   L5 cross-board: 7200s, 500K tokens
    if task.max_seconds_budget is None and task.max_tokens_budget is None:
        _budget_table = {
            1: (600, 20_000),
            2: (1200, 40_000),
            3: (1800, 80_000),
            4: (3600, 200_000),
            5: (7200, 500_000),
        }
        secs, toks = _budget_table.get(task.complexity or 3, (1800, 80_000))
        task.max_seconds_budget = secs
        task.max_tokens_budget = toks

    # Auto-split L4/L5 AND medium+ priority tasks into subtasks for parallel
    # multi-agent execution. Complex or urgent tasks benefit from decomposition:
    # split into 3 children (by_count) so multiple agents work simultaneously
    # across different providers. Parent stays in_progress until all children done.
    from app.services.task_splitter import should_auto_split
    _will_autosplit = should_auto_split(task)
    # T108 (2026-05-30): do NOT split single-artifact tool/RE tasks (a crackme
    # is ONE binary with ONE password — not parallelizable subwork). These go
    # to multi-agent CONSENSUS on the single task instead (≥2 analysts derive
    # the same password). Splitting would create N redundant/divergent checks.
    if _will_autosplit:
        try:
            from app.services.pev_loop import _task_requires_real_tools
            if _task_requires_real_tools(task.title or "", task.description or ""):
                _will_autosplit = False
        except Exception:
            pass

    # T39 (2026-05-14): when payload requests cross-board deps, collect the
    # caller's accessible boards (same organization) and pass to the validator.
    cross_board = bool(getattr(payload, "allow_cross_board_deps", False))
    accessible_board_ids: list[UUID] | None = None
    if cross_board:
        accessible_res = await session.exec(
            sa_select(Board.id).where(Board.organization_id == board.organization_id),
        )
        accessible_board_ids = [bid for bid in accessible_res]
    normalized_deps = await validate_dependency_update(
        session,
        board_id=board.id,
        task_id=task.id,
        depends_on_task_ids=depends_on_task_ids,
        allow_cross_board=cross_board,
        accessible_board_ids=accessible_board_ids,
    )
    normalized_tag_ids = await validate_tag_ids(
        session,
        organization_id=board.organization_id,
        tag_ids=tag_ids,
    )
    dep_status = await dependency_status_by_id(
        session,
        board_id=board.id,
        dependency_ids=normalized_deps,
    )
    blocked_by = blocked_by_dependency_ids(
        dependency_ids=normalized_deps,
        status_by_id=dep_status,
    )
    if blocked_by and (task.assigned_agent_id is not None or task.status != "inbox"):
        raise _blocked_task_error(blocked_by)
    dag_items = None
    if payload.subtasks is not None and len(payload.subtasks) > 0:
        from app.services.dag_materialize import (
            DagValidationError,
            parse_subtasks_payload,
            validate_subtasks,
        )

        try:
            dag_items = parse_subtasks_payload(json.dumps(payload.subtasks))
            validate_subtasks(dag_items)
        except DagValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "dag_validation_failed", "message": str(exc)},
            ) from exc
        # Same flip as the old post-commit block: an owner-driven DAG parent
        # goes straight to in_progress (children execute; parent tracks them).
        task.status = "in_progress"
        task.in_progress_at = utcnow()
    session.add(task)
    # Ensure the task exists in the DB before inserting dependency rows.
    await session.flush()
    await _set_task_custom_field_values_for_create(
        session,
        board_id=board.id,
        task_id=task.id,
        custom_field_values=custom_field_values,
    )
    for dep_id in normalized_deps:
        session.add(
            TaskDependency(
                board_id=board.id,
                task_id=task.id,
                depends_on_task_id=dep_id,
            ),
        )
    if dag_items is not None:
        from app.services.dag_materialize import materialize_dag

        summary = await materialize_dag(session, task, dag_items)
        record_activity(
            session,
            event_type="task.comment",
            task_id=task.id,
            board_id=board.id,
            message=(
                f"[watchdog-dag-materialized] children={summary['created']} "
                f"edges={summary['edges']} source=owner-api"
            ),
        )
    await replace_tags(
        session,
        task_id=task.id,
        tag_ids=normalized_tag_ids,
    )
    # Creation, its initial timing and audit record commit atomically.
    session.add(
        TaskStageTiming(
            task_id=task.id,
            status=task.status,
            agent_id=task.assigned_agent_id,
            entered_at=utcnow(),
        ),
    )
    record_activity(
        session,
        event_type="task.created",
        task_id=task.id,
        message=f"Task created: {task.title}.",
        board_id=board_id,
    )
    notification_stamp = task.updated_at
    gateway_owned = not _harness_task(task)
    await session.commit()
    await session.refresh(task)
    if gateway_owned and not await _notification_current(session, task, notification_stamp):
        return await _task_read_response(session, task=task, board_id=board_id)

    # T24a (E1 Phase 2, 2026-05-14): owner-driven DAG materialization.
    # If `subtasks` was supplied, materialize children + dep edges right now
    # and flip the parent to `in_progress`. Skips the PM marker round-trip.

    # Auto-assign unassigned tasks to the appropriate worker based on
    # complexity. L1-L3 (simple) → LAN-local worker (vllm / LMStudio —
    # always free, energy-only cost). L4-L5 (complex) → Senior variant
    # which may route to a higher-IQ cloud-capable model per the agent's
    # configured fallback chain.
    #
    # T85 (2026-05-16): "ollama" / "ollama-cloud" was historically used
    # in this code to mean "high-IQ cloud worker" — fixed in-place. The
    # real distinctions the system cares about:
    #   * local (LAN, free)  vs  cloud (network, may be paid)
    #   * free  vs  paid (T49 cost lockdown applies only to paid)
    # Per-provider routing is decided by the agent's openclaw.json
    # `model.primary` + `fallbacks[]` chain — NOT by hard-coding ollama
    # at the assignment site.
    #
    # Include OFFLINE agents in the search — task creation is the trigger
    # that should wake workers from standby. Agents that were force-set
    # offline by the stuck-session abort will be woken and reassigned.
    # A pipeline step waiting on unfinished steps is not assigned yet: the
    # watchdog's inbox router assigns it once its dependencies are done.
    if gateway_owned and not await _notification_current(session, task, notification_stamp):
        return await _task_read_response(session, task=task, board_id=board_id)
    if not task.assigned_agent_id and not _harness_task(task) and not blocked_by:
        from app.api.router import _role_keyword_for_task as _rkt

        is_complex = (task.complexity or 0) >= 4
        # T104 (2026-05-23): `_rkt` returns a FULL ROLE NAME ("Reverse Engineer"),
        # not a short keyword token. Previously caller looked it up in a
        # short-keyword map that always missed → every task fell through to
        # default "QA Engineer". Owner saw a crackme/IDA Pro RE task get
        # assigned to QA Engineer Senior. Now: use the returned role name
        # directly; default only if `_rkt` returns None.
        matched_role = _rkt(task.title, task.description or "")
        # T104 hierarchical-delegate: L4+ parent tasks go to the board lead
        # as PLANNER. Splitter then fans worker subtasks to spec roles.
        # Avoids the previous pattern where a single worker tried to solo
        # a multi-stage L4 problem and burned 150K-token prompts.
        will_split = _will_autosplit
        if will_split:
            base_role = "__lead__"  # sentinel resolved below
        else:
            base_role = matched_role if matched_role else "QA Engineer"
        # T104 guard: QA Engineer is for verify-only. Re-route if mismatch.
        if matched_role == "QA Engineer" and (
            "test" not in (task.title + " " + (task.description or "")).lower()
            and "qa" not in (task.title + " " + (task.description or "")).lower()
            and "verif" not in (task.title + " " + (task.description or "")).lower()
        ):
            base_role = "Software Architect"

        # Helper: find candidate preferring agents with active gateway sessions.
        async def _find_agent(name_pattern: str, exclude_senior: bool = False) -> Agent | None:
            base_q = Agent.objects.filter_by(board_id=board.id).filter(
                col(Agent.status).notin_(["retired", "provisioning"]),
                col(Agent.openclaw_session_id).isnot(None),
                col(Agent.openclaw_session_id) != "",
                col(Agent.last_provision_error).is_(None),
            )
            if exclude_senior:
                base_q = base_q.filter(col(Agent.name).notilike("%Senior%"))

            return await base_q.filter(
                col(Agent.name).ilike(f"%{name_pattern}%"),
            ).first(session)

        # T104 hierarchical-delegate: resolve __lead__ sentinel to the actual
        # board lead agent. Fan-out children carry spec role hints via
        # `auto_reason=split_from:{pid};role=<role>` (task_splitter T104).
        if base_role == "__lead__":
            lead = await Agent.objects.filter_by(board_id=board.id).filter(
                col(Agent.is_board_lead).is_(True),
                col(Agent.status).notin_(["retired"]),
            ).first(session)
            # T104 round-3 (2026-05-24): if lead has no live openclaw session
            # (e.g. session reconciler just dropped it because openclaw
            # said `timeout`/`failed`), assigning to lead is a silent
            # no-op — `_wake_agent_online_for_task` and `_notify_agent_on_task_assign`
            # both early-return on `not agent.openclaw_session_id`.
            # Fall back to spec worker so the task still progresses.
            if lead and not lead.openclaw_session_id:
                from app.core.logging import get_logger
                get_logger(__name__).info(
                    "auto_assign.lead_no_session falling back to spec worker — task=%s lead=%s",
                    str(task.id), lead.name,
                )
                lead = None
            if lead is not None:
                candidate = lead
            else:
                # Fall back: use keyword-matched role just like non-L4 path.
                fallback_role = matched_role if matched_role else "Software Architect"
                candidate = await _find_agent(f"{fallback_role} Senior")
                if not candidate:
                    candidate = await _find_agent(fallback_role)
        elif is_complex:
            candidate = await _find_agent(f"{base_role} Senior")
            if not candidate:
                candidate = await _find_agent(base_role)
        else:
            candidate = await _find_agent(base_role, exclude_senior=True)
            if not candidate:
                candidate = await _find_agent(base_role)

        if candidate is None:
            # Fresh boards have a lead but may not yet have role-specific workers.
            candidate = await Agent.objects.filter_by(board_id=board.id).filter(
                col(Agent.is_board_lead).is_(True),
                col(Agent.status).notin_(["retired", "provisioning"]),
                col(Agent.openclaw_session_id).isnot(None),
                col(Agent.openclaw_session_id) != "",
                col(Agent.last_provision_error).is_(None),
            ).first(session)
        if candidate:
            task.assigned_agent_id = candidate.id
            session.add(task)
            await session.commit()
            await session.refresh(task)
            if not await _notification_current(session, task, notification_stamp):
                return await _task_read_response(session, task=task, board_id=board_id)
            # T85: rename hint from "ollama"/"vLLM" to the architectural
            # distinction (cloud-tier vs LAN-local). The actual provider
            # is decided downstream by the agent's fallback chain.
            provider_hint = "cloud-tier" if is_complex else "lan-local"
            record_activity(
                session,
                event_type="task.auto_assigned",
                message=(
                    f"Task auto-assigned to {candidate.name} "
                    f"(complexity=L{task.complexity}, provider={provider_hint})."
                ),
                agent_id=candidate.id,
                task_id=task.id,
                board_id=board.id,
            )
            await session.commit()

            # Wake the assigned agent if it was offline — task creation is
            # the trigger that should bring workers out of standby.
            if candidate.status == "offline":
                candidate.status = "online"
                candidate.last_seen_at = utcnow()
                session.add(candidate)
                await session.commit()
                await _wake_agent_online_for_task(
                    session=session,
                    board=board,
                    task=task,
                    agent=candidate,
                    reason="task_auto_assign",
                )
        else:
            # No agent matched — fall back to PM assignment
            pm_candidates = list(
                await Agent.objects.filter_by(board_id=board.id)
                .filter(col(Agent.is_project_manager).is_(True),
                        col(Agent.status).notin_(["retired", "provisioning"]),
                        col(Agent.openclaw_session_id).isnot(None),
                        col(Agent.openclaw_session_id) != "",
                        col(Agent.last_provision_error).is_(None))
                .all(session),
            )
            if pm_candidates:
                task.assigned_agent_id = pm_candidates[0].id
                session.add(task)
                await session.commit()
                await session.refresh(task)
                if not await _notification_current(session, task, notification_stamp):
                    return await _task_read_response(session, task=task, board_id=board_id)
                record_activity(
                    session,
                    event_type="task.auto_assigned",
                    message=f"Task auto-assigned to PM: {pm_candidates[0].name} (no role matched).",
                    agent_id=pm_candidates[0].id,
                    task_id=task.id,
                    board_id=board.id,
                )
                await session.commit()

    if not _harness_task(task) and not task.assigned_agent_id and not blocked_by:
        record_activity(session, event_type="task.dispatch_unavailable", task_id=task.id,
                        board_id=board.id, message="No provisioned eligible worker or lead; complete or retry board onboarding.")
        await session.commit()

    if gateway_owned and not await _notification_current(session, task, notification_stamp):
        return await _task_read_response(session, task=task, board_id=board_id)
    # T93 (2026-05-17): fire-and-forget. `_notify_lead_on_task_create`
    # uses `deliver=True` which BLOCKS on the gateway ACK + lead-LLM
    # round-trip (30-40s observed). Owner sees "NetworkError"-feeling
    # waits on every task-create. Detach to a background task so the
    # HTTP response returns immediately.
    import asyncio as _asyncio

    from app.db.session import async_session_maker as _asm
    task_id = task.id
    async def _bg_notify(stamp=notification_stamp):
        try:
            async with _asm() as _bg_db:
                _bg_board = await Board.objects.filter_by(id=board_id).first(_bg_db)
                _bg_task = await Task.objects.filter_by(id=task_id).first(_bg_db)
                if _bg_board and _bg_task:
                    await _notify_lead_on_task_create(session=_bg_db, board=_bg_board, task=_bg_task, stamp=stamp)
        except Exception:
            pass
    if not _harness_task(task):
        _asyncio.create_task(_bg_notify())
    # T94: inline inbox-triage NOT needed for top-level tasks — the
    # auto-assign block above (role-keyword router) already runs at
    # create time. Unassigned-at-create paths are the owner's explicit
    # 5-min volunteer-grace window. Subtasks handled by T91 instead.

    if task.assigned_agent_id and not _harness_task(task):
        assigned_agent = await Agent.objects.by_id(task.assigned_agent_id).first(
            session,
        )
        if assigned_agent:
            await _notify_agent_on_task_assign(
                session=session,
                board=board,
                task=task,
                agent=assigned_agent,
            )

    if gateway_owned and not await _notification_current(session, task, notification_stamp):
        return await _task_read_response(session, task=task, board_id=board_id)
    # Auto-split L4/L5 tasks into subtasks for parallel multi-agent execution.
    # This happens AFTER assignment so the parent task has an assignee for
    # tracking. Children are auto-routed by watchdog inbox triage.
    if _will_autosplit:
        try:
            from app.services.task_splitter import split_by_count
            children = await split_by_count(
                session, task, 3,
                actor_id=auth.user.id if auth and auth.user else None,
            )
            notification_stamp = task.updated_at
            if not await _notification_current(session, task, notification_stamp):
                return await _task_read_response(session, task=task, board_id=board_id)
            record_activity(
                session,
                event_type="task.auto_split",
                message=(
                    f"Task auto-split into {len(children)} subtasks "
                    f"(complexity=L{task.complexity}). Children IDs: "
                    + ", ".join(str(c.id) for c in children[:3])
                ),
                task_id=task.id,
                board_id=board.id,
            )
            await session.commit()
            # T90 (2026-05-16): event-driven wake — eliminates 180s
            # watchdog-tick wait for subtask discovery. Deepseek audit
            # flagged this as the real serial-processing bottleneck.
            try:
                await _wake_lead_pms_for_subtask_creation(
                    session=session, board=board, parent_task=task, children=children,
                )
            except Exception:
                pass
            # T91 (2026-05-16): also run the watchdog inbox-triage now so
            # children get distributed across the role pool immediately,
            # not on next 180s tick. Combined with the grace-bypass for
            # subtasks above, this lands the children with workers in
            # seconds. NON-BLOCKING — fire as a background task so the
            # HTTP response returns fast even when the triage runs an
            # LLM evaluator call per child.
            try:
                import asyncio as _asyncio

                from app.db.session import async_session_maker as _asm
                from app.services.watchdog_loop import _route_manual_inbox_tasks
                async def _bg_triage():
                    try:
                        async with _asm() as _wd_db:
                            await _route_manual_inbox_tasks(_wd_db)
                    except Exception:
                        pass
                _asyncio.create_task(_bg_triage())
            except Exception:
                pass
        except Exception:
            pass  # best effort — don't block task creation on split failure

    if gateway_owned and not await _notification_current(session, task, notification_stamp):
        return await _task_read_response(session, task=task, board_id=board_id)
    # T105 Phase 5 (2026-05-25): auto-start PEV loop on tasks created in
    # boards where the owner has enabled autonomous mode. Background task
    # so HTTP response doesn't block on smart-tier LLM round-trip + the
    # pre-loop snapshot (which can take a few seconds).
    # T108 (2026-05-30): do NOT auto-start PEV on tasks that (a) need real
    # tools (PEV's text-only executor fabricates — those go to multi-agent
    # consensus solicitation instead) or (b) were just auto-split into
    # children (the children do the work; the parent is a coordinator).
    # This was the leak: 0a8b in the watchdog had the tool guard, but this
    # creation-time background start bypassed it.
    _pev_eligible = (
        getattr(board, "autonomous_enabled", False)
        and not _harness_task(task)
        and not task.parent_task_id
        and (task.split_count or 0) == 0
    )
    if _pev_eligible:
        try:
            from app.services.pev_loop import _task_requires_real_tools
            if _task_requires_real_tools(task.title or "", task.description or ""):
                _pev_eligible = False
        except Exception:
            pass
    if _pev_eligible:
        import asyncio as _asyncio

        from app.db.session import async_session_maker as _asm
        _task_id_for_pev = task.id

        async def _bg_pev_start() -> None:
            try:
                from app.services.pev_loop import start_pev
                async with _asm() as _pev_db:
                    current = await _pev_db.get(Task, _task_id_for_pev)
                    if current and await _notification_current(_pev_db, current, notification_stamp):
                        await start_pev(_pev_db, _task_id_for_pev)
            except Exception:
                pass

        _asyncio.create_task(_bg_pev_start())

    return await _task_read_response(
        session,
        task=task,
        board_id=board_id,
    )


@router.patch(
    "/{task_id}",
    response_model=TaskRead,
    responses={409: {"model": BlockedTaskError}},
)
async def update_task(
    payload: TaskUpdate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> TaskRead:
    """Update task status, assignment, comment, and dependency state."""
    if task.board_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Task board_id is required.",
        )
    board_id = task.board_id
    if actor.actor_type == "user" and actor.user is not None:
        await _require_task_user_write_access(
            session,
            board_id=board_id,
            user=actor.user,
        )
    # Lock before checking caller preconditions; two runner hosts must not both
    # claim the same inbox row after reading the same board snapshot.
    await session.refresh(task, with_for_update=True)
    if payload.expected_status is not None and task.status != payload.expected_status:
        raise HTTPException(status_code=409, detail="Task status changed since it was read")
    if payload.expected_updated_at is not None:
        expected = payload.expected_updated_at
        if expected.tzinfo is not None:
            expected = expected.astimezone(UTC).replace(tzinfo=None)
        if task.updated_at.replace(tzinfo=None) != expected:
            raise HTTPException(status_code=409, detail="Task changed since it was read")
    cap_state = None
    budget_fields = {"max_tokens_budget", "max_seconds_budget"}
    if budget_fields & payload.model_fields_set:
        if actor.actor_type != "user" or actor.user is None:
            raise HTTPException(403, "Only the organization owner can change a task token cap")
        board = await session.get(Board, board_id)
        if board is None:
            raise HTTPException(404, "Board not found")
        member = await require_board_access(session, user=actor.user, board=board, write=True)
        if member.role != "owner":
            raise HTTPException(403, "Only the organization owner can change a task token cap")
        if payload.expected_status is None or payload.expected_updated_at is None:
            raise HTTPException(422, "Token cap updates require status and updated_at preconditions")
        if payload.model_fields_set - {
            *budget_fields, "expected_status", "expected_updated_at", "comment"
        }:
            raise HTTPException(422, "Token cap updates must be separate from other task changes")
        if payload.max_tokens_budget is not None and payload.max_tokens_budget < task.tokens_used:
            raise HTTPException(409, "Token cap cannot be below already-used tokens")
        cap_state = task.model_dump(exclude={*budget_fields, "updated_at"})
    previous_status = task.status
    previous_assigned = task.assigned_agent_id
    updates = payload.model_dump(exclude_unset=True)
    updates.pop("expected_status", None)
    updates.pop("expected_updated_at", None)
    if actor.actor_type == "agent" and (
        previous_status in {"cancelled", "paused"} or updates.get("status") in {"cancelled", "paused"}
    ):
        raise HTTPException(403, "Only the owner can pause, cancel or resume a stopped task")
    if previous_status == "paused":
        resume_status = task.paused_from_status or "inbox"
        if updates.get("status", "paused") not in {"paused", "cancelled", resume_status}:
            raise HTTPException(409, f"Resume this task to {resume_status}; pause cannot bypass workflow gates")
        if updates.get("status") == resume_status:
            updates["paused_from_status"] = None
    if updates.get("status") == "paused" and previous_status != "paused":
        if previous_status == "done" and not task.repeat_enabled:
            raise HTTPException(409, "Only recurring Done tasks can be paused")
        updates["paused_from_status"] = (
            "blocked" if previous_status == "in_progress" else
            "inbox" if previous_status == "done" else previous_status
        )
    if updates.get("status", previous_status) == "paused":
        updates["assigned_agent_id"] = None
        task.previous_in_progress_at = task.in_progress_at or task.previous_in_progress_at
        task.in_progress_at = None
    if previous_status == "cancelled":
        await _require_valid_status_transition(
            session, board_id=board_id, previous_status=previous_status,
            target_status=updates.get("status", previous_status), task_id=task.id,
        )
    if updates.get("status", previous_status) == "cancelled":
        updates.update(repeat_enabled=False, assigned_agent_id=None)
        task.previous_in_progress_at = task.in_progress_at or task.previous_in_progress_at
        task.in_progress_at = None
    if _harness_task(task) and actor.actor_type == "agent":
        raise HTTPException(status_code=409, detail="Harness task is owned by the runner")
    if "execution_lane" in updates and updates["execution_lane"] != task.execution_lane:
        if task.status != "inbox" or task.assigned_agent_id is not None:
            raise HTTPException(status_code=409, detail="Stop and unassign task before changing lanes")
    if updates.get("execution_lane", task.execution_lane) == "harness" and updates.get(
        "assigned_agent_id", task.assigned_agent_id
    ) is not None:
        raise HTTPException(status_code=422, detail="Harness tasks cannot have a gateway assignee")
    if (updates.get("execution_lane", task.execution_lane) == "harness" or _harness_task(task)) and updates.get(
        "repeat_enabled", task.repeat_enabled
    ) and updates.get("repeat_cron", task.repeat_cron):
        raise HTTPException(status_code=422, detail="Harness cron repeats are not supported yet")
    if "execution_mode" in updates and updates["execution_mode"] != task.execution_mode:
        from app.services import team_cycle as _team

        review_handoff = (
            actor.actor_type == "user" and actor.user is not None
            and task.status == "review" and task.execution_mode == "single"
            and task.execution_lane == "harness" and updates["execution_mode"] == "team"
            and updates.get("status", "review") == "review"
            and payload.expected_status == "review" and payload.expected_updated_at is not None
        )
        if (task.status != "inbox" and not review_handoff) or await _team.active_cycle(session, task.id) is not None:
            raise HTTPException(status_code=409, detail="Stop the task before changing its execution mode")
        if review_handoff and (await session.exec(select(_team.TeamCycle).where(
            _team.TeamCycle.task_id == task.id).limit(1))).first() is not None:
            raise HTTPException(409, "Reconcile historical native work before mode conversion")
    if "execution_mode" in updates or task.execution_mode == "team":
        _check_team_mode(
            updates.get("execution_mode", task.execution_mode),
            updates.get("execution_lane", task.execution_lane),
            updates.get("workspace_path", task.workspace_path),
            updates.get("assigned_agent_id", task.assigned_agent_id),
        )
    if task.execution_mode == "team":
        from app.services import team_cycle as _team

        await _team.restamp_budget(session, task, updates)
    if task.execution_mode == "team" and task.status == "in_progress":
        from app.services import team_cycle as _team

        # A budget edit is no new instruction: the runner checks the cap at each cycle
        # boundary, so cancelling here only discarded paid work.
        context_change = any(key in updates and updates[key] != getattr(task, key)
                             for key in (*_team.TASK_INPUT_FIELDS, "status")
                             if key not in _team.BUDGET_FIELDS)
        if context_change:
            cycle = await _team.active_cycle(session, task.id)
            if cycle is not None:
                try:
                    await _team._lock_cycle(session, cycle)
                except _team.TeamFenced:
                    pass  # cancellation stays in this owner-PATCH transaction
                if cycle.state == "publishing":
                    raise HTTPException(409, "Publication is reserved; retry this edit after it finishes")
                await _team.cancel_cycle(session, cycle, "owner changed task state or instructions")
    comment = payload.comment if "comment" in payload.model_fields_set else None

    # ── QA gate: refuse done transition without QA Verdict ─────────────────
    from app.api.agent import _enforce_qa_gate as _qa_gate
    requested_status_for_gate = payload.status if "status" in payload.model_fields_set else None
    if requested_status_for_gate == "done" and previous_status != "done":
        await _qa_gate(task=task, session=session, payload=payload)

    # ── Done-is-final gate: block agents from reopening done tasks ────────
    # Only the owner (user actor via the UI) can manually reopen a done task.
    # This prevents autonomous agents from re-processing already-completed work
    # during restart/reconnect cascades or QA-gate confusion on legacy tasks.
    #
    # T67 (2026-05-15): EXCEPTION for repeat-enabled tasks. Those are
    # designed to cycle done → inbox → in_progress → done forever (until
    # owner disables repeat or deletes the task). Treating done as final
    # for them breaks the cycle: when the watchdog soft-restarts the task
    # back to inbox, the assigned agent's first status update would 409
    # because `previous_status` was momentarily `done` on the row the
    # gateway cached. For repeat tasks the cycle IS the contract.
    if (
        previous_status == "done"
        and actor.actor_type == "agent"
        and not task.repeat_enabled
    ):
        requested_status = payload.status if "status" in payload.model_fields_set else None
        if requested_status is not None and requested_status != "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Done is final: tasks in 'done' status cannot be reopened by agents. "
                    "Only the owner can manually reopen a done task from the UI."
                ),
            )

    depends_on_task_ids = (
        payload.depends_on_task_ids if "depends_on_task_ids" in payload.model_fields_set else None
    )
    tag_ids = payload.tag_ids if "tag_ids" in payload.model_fields_set else None
    custom_field_values = (
        payload.custom_field_values if "custom_field_values" in payload.model_fields_set else None
    )
    custom_field_values_set = "custom_field_values" in payload.model_fields_set
    updates.pop("comment", None)
    updates.pop("depends_on_task_ids", None)
    updates.pop("tag_ids", None)
    updates.pop("custom_field_values", None)
    requested_status = payload.status if "status" in payload.model_fields_set else None
    if (
        task.execution_mode == "team"
        and previous_status == "in_progress"
        and requested_status not in (None, "in_progress")
    ):
        # A human (or watchdog) took the task away: in-flight team workers lose
        # their claims and can no longer publish.
        from app.services import team_cycle as _team

        _cycle = await _team.active_cycle(session, task.id)
        if _cycle is not None:
            await _team.cancel_cycle(session, _cycle, f"task moved to {requested_status}")
    # Owner ends a recurring task: when a USER (not an agent/watchdog) moves a
    # repeat-enabled task to `done`, that is the explicit stop signal — disable
    # the recurrence so it stays done. Auto cycle-completion is done by the
    # watchdog (agent/system) and recycles to inbox instead, so only this
    # user-initiated path disables the repeat.
    if (
        actor.actor_type == "user"
        and requested_status == "done"
        and previous_status != "done"
        and getattr(task, "repeat_enabled", False)
        and "repeat_enabled" not in updates
    ):
        updates["repeat_enabled"] = False
    update = _TaskUpdateInput(
        task=task,
        actor=actor,
        board_id=board_id,
        previous_status=previous_status,
        previous_assigned=previous_assigned,
        previous_in_progress_at=task.in_progress_at,
        previous_max_tokens_budget=task.max_tokens_budget,
        previous_max_seconds_budget=task.max_seconds_budget,
        status_requested=(requested_status is not None and requested_status != previous_status),
        updates=updates,
        comment=comment,
        depends_on_task_ids=depends_on_task_ids,
        tag_ids=tag_ids,
        custom_field_values=custom_field_values or {},
        custom_field_values_set=custom_field_values_set,
    )
    if actor.actor_type == "agent" and actor.agent and actor.agent.is_board_lead:
        return await _apply_lead_task_update(session, update=update)

    if actor.actor_type == "agent":
        await _apply_non_lead_agent_task_rules(session, update=update)
    else:
        await _apply_admin_task_rules(session, update=update)
    if cap_state is not None and any(
        update.updates.get(key, getattr(task, key)) != value for key, value in cap_state.items()
    ):
        raise HTTPException(409, "Reconcile lifecycle changes separately before editing the token cap")
    return await _finalize_updated_task(
        session,
        update=update,
    )


async def delete_task_and_related_records(
    session: AsyncSession,
    *,
    task: Task,
) -> None:
    """Delete a task and associated relational records, then commit.

    Subtask protection: if the task was split (split_count > 0), delete
    the children first so they don't become orphans. The FK has no CASCADE.

    T57 (2026-05-15): removed the "repeat_enabled tasks become 'done'
    instead of deleted" silent-rewrite. It lied to users — they pressed
    DELETE, the API returned `{"ok":true}`, and the watchdog interval-
    repeat path immediately cycled the row back to inbox every N minutes.
    DELETE now means delete. To stop a recurring schedule without losing
    history, the user should toggle `repeat_enabled=false` first via
    edit, then DELETE if they also want the row gone — both intents are
    distinct actions.
    """
    # Clean up children before deleting parent
    if task.split_count > 0:
        children = list(
            await Task.objects.filter(
                col(Task.parent_task_id) == task.id,
            ).all(session),
        )
        for child in children:
            await session.delete(child)
        await session.flush()

    await crud.delete_where(
        session,
        ActivityEvent,
        col(ActivityEvent.task_id) == task.id,
        commit=False,
    )
    await crud.delete_where(
        session,
        TaskFingerprint,
        col(TaskFingerprint.task_id) == task.id,
        commit=False,
    )

    primary_approvals = list(
        await Approval.objects.filter(col(Approval.task_id) == task.id).all(session),
    )
    await crud.delete_where(
        session,
        ApprovalTaskLink,
        col(ApprovalTaskLink.task_id) == task.id,
        commit=False,
    )
    if primary_approvals:
        primary_ids = [approval.id for approval in primary_approvals]
        remaining_by_approval = await load_task_ids_by_approval(session, approval_ids=primary_ids)
        for approval in primary_approvals:
            remaining_task_ids = remaining_by_approval.get(approval.id, [])
            if remaining_task_ids:
                approval.task_id = remaining_task_ids[0]
                session.add(approval)
                continue
            await session.delete(approval)
    await crud.delete_where(
        session,
        TaskDependency,
        or_(
            col(TaskDependency.task_id) == task.id,
            col(TaskDependency.depends_on_task_id) == task.id,
        ),
        commit=False,
    )
    await crud.delete_where(
        session,
        TagAssignment,
        col(TagAssignment.task_id) == task.id,
        commit=False,
    )
    await crud.delete_where(
        session,
        TaskCustomFieldValue,
        col(TaskCustomFieldValue.task_id) == task.id,
        commit=False,
    )
    await crud.delete_where(
        session,
        TaskStageTiming,
        col(TaskStageTiming.task_id) == task.id,
        commit=False,
    )
    await session.delete(task)
    await session.commit()


@router.delete("/{task_id}", response_model=OkResponse)
async def delete_task(
    session: AsyncSession = SESSION_DEP,
    task: Task = TASK_DEP,
    auth: AuthContext = USER_AUTH_DEP,
) -> OkResponse:
    """Delete a task and related records."""
    if task.board_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    board = await Board.objects.by_id(task.board_id).first(session)
    if board is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_board_access(session, user=auth.user, board=board, write=True)
    await delete_task_and_related_records(session, task=task)
    return OkResponse()


@router.get(
    "/{task_id}/comments",
    response_model=DefaultLimitOffsetPage[TaskCommentRead],
)
async def list_task_comments(
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    markers: bool = Query(False, description="Also return [slug] system markers."),
) -> LimitOffsetPage[TaskCommentRead]:
    """List comments for a task in chronological order.

    Excludes system/management markers ([watchdog-…], [cycle-complete],
    [owner-directive…], [repeat-…] etc. — always posted with a NULL agent_id and
    a `[`/`**[` prefix). Those are operational noise and belong in the activity
    (details) feed, not the human comment thread. The thread keeps genuine agent
    result summaries and owner comments.

    `markers=true` keeps them: the fleet pool counts its own [auto-recovery],
    [escalated] and [meta-resumed] markers. Hidden, they never counted, and one
    task looped through four meta-debug tasks (2026-10-22).
    """
    statement = (
        select(ActivityEvent)
        .where(col(ActivityEvent.task_id) == task.id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        # Newest first, like every other activity feed here. A long-lived
        # recurring task accumulates thousands of comments: ascending order
        # meant page one showed the oldest, and the current cycle was
        # unreachable without paging through everything. Observed on a task
        # with 1059 comments whose newest visible entry was two months old.
        .order_by(desc(col(ActivityEvent.created_at)))
    )
    if not markers:
        # Drop bracketed markers ([watchdog-…], [cycle-complete], [ready-to-close],
        # [agent-gateway]…) whoever posted them — they are system/process noise
        # for the activity (details) feed. The thread keeps prose result
        # summaries and owner comments.
        statement = statement.where(text(not_system_marker_sql("message")))
    return await paginate(session, statement)


# T103 v40 (2026-05-19): unified activity log = activity_events for the task
# + LLM traffic flows for the assigned agent (since task creation). Owner saw
# tasks with traffic happening but empty Comments panel — no transparency. This
# endpoint backs the "Live Activity" tab in the task drawer.
@router.get(
    "/{task_id}/activity-log",
    summary="Unified activity log (events + LLM flows) for a task",
)
async def get_task_activity_log(
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, object]:
    import sqlite3
    from pathlib import Path

    rows: list[dict[str, object]] = []

    # 1) activity_events for this task
    ev_stmt = (
        select(ActivityEvent)
        .where(col(ActivityEvent.task_id) == task.id)
        .order_by(desc(col(ActivityEvent.created_at)))
        .limit(limit)
    )
    ev_result = await session.exec(ev_stmt)
    for ev in ev_result.all():
        rows.append({
            "kind": "event",
            "id": str(ev.id),
            "ts": ev.created_at.isoformat(),
            "event_type": ev.event_type,
            "message": ev.message,
            "agent_id": str(ev.agent_id) if ev.agent_id else None,
        })

    # 2) LLM flows for the assigned agent since task creation
    assigned_name: str | None = None
    if task.assigned_agent_id is not None:
        agent = await Agent.objects.by_id(task.assigned_agent_id).first(session)
        if agent is not None:
            assigned_name = agent.name

    if assigned_name:
        traffic_db = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
        if traffic_db.exists():
            since_iso = task.created_at.isoformat() if task.created_at else None
            try:
                conn = sqlite3.connect(f"file:{traffic_db}?mode=ro", uri=True, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                q = (
                    "SELECT id, ts, model, status_code, latency_ms, finish_reason, "
                    "prompt_tokens, completion_tokens, total_tokens, has_tool_calls, "
                    "tool_calls_count, last_user_message, content_preview, reasoning_preview "
                    "FROM flows WHERE agent_name = ?"
                )
                params: list[object] = [assigned_name]
                if since_iso:
                    q += " AND ts >= ?"
                    params.append(since_iso)
                q += " ORDER BY ts DESC LIMIT ?"
                params.append(limit)
                for r in conn.execute(q, params):
                    rows.append({
                        "kind": "llm_call",
                        "id": f"flow-{r['id']}",
                        "ts": r["ts"],
                        "model": r["model"],
                        "status_code": r["status_code"],
                        "latency_ms": r["latency_ms"],
                        "finish_reason": r["finish_reason"],
                        "prompt_tokens": r["prompt_tokens"],
                        "completion_tokens": r["completion_tokens"],
                        "total_tokens": r["total_tokens"],
                        "has_tool_calls": bool(r["has_tool_calls"]),
                        "tool_calls_count": r["tool_calls_count"],
                        "last_user_message": (r["last_user_message"] or "")[:400],
                        "content_preview": (r["content_preview"] or "")[:400],
                        "reasoning_preview": (r["reasoning_preview"] or "")[:200],
                    })
                conn.close()
            except sqlite3.Error:
                pass

    rows.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    rows = rows[:limit]

    summary = {
        "event_count": sum(1 for r in rows if r["kind"] == "event"),
        "llm_call_count": sum(1 for r in rows if r["kind"] == "llm_call"),
        "assigned_agent": assigned_name,
    }
    return {"items": rows, "summary": summary}


async def _validate_task_comment_access(
    session: AsyncSession,
    *,
    task: Task,
    actor: ActorContext,
) -> None:
    if task.board_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    if actor.actor_type == "user" and actor.user is not None:
        board = await Board.objects.by_id(task.board_id).first(session)
        if board is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        await require_board_access(session, user=actor.user, board=board, write=True)

    if (
        actor.actor_type == "agent"
        and actor.agent
        and actor.agent.is_board_lead
        and task.status != "review"
        and not await _lead_was_mentioned(session, task, actor.agent)
        and not _lead_created_task(task, actor.agent)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Board leads can only comment during review, when mentioned, "
                "or on tasks they created."
            ),
        )


def _comment_actor_id(actor: ActorContext) -> UUID | None:
    if actor.actor_type == "agent" and actor.agent:
        return actor.agent.id
    return None


def _comment_actor_name(actor: ActorContext) -> str:
    if actor.actor_type == "agent" and actor.agent:
        return actor.agent.name
    return "User"


async def _comment_targets(
    session: AsyncSession,
    *,
    task: Task,
    message: str,
    actor: ActorContext,
) -> tuple[dict[UUID, Agent], set[str]]:
    mention_names = extract_mentions(message)
    targets: dict[UUID, Agent] = {}
    if mention_names and task.board_id:
        for agent in await Agent.objects.filter_by(board_id=task.board_id).all(session):
            if matches_agent_mention(agent, mention_names):
                targets[agent.id] = agent
    if not mention_names and task.assigned_agent_id:
        assigned_agent = await Agent.objects.by_id(task.assigned_agent_id).first(
            session,
        )
        if assigned_agent:
            targets[assigned_agent.id] = assigned_agent

    if actor.actor_type == "agent" and actor.agent:
        targets.pop(actor.agent.id, None)
    return targets, mention_names


@dataclass(frozen=True, slots=True)
class _TaskCommentNotifyRequest:
    task: Task
    actor: ActorContext
    message: str
    targets: dict[UUID, Agent]
    mention_names: set[str]


async def _notify_task_comment_targets(
    session: AsyncSession,
    *,
    request: _TaskCommentNotifyRequest,
) -> None:
    if not request.targets:
        return
    # The operator's hourly script already assembled and persisted this report.
    # Keep it visible on the board without starting another paid agent turn.
    if request.actor.actor_type == "user" and request.message.startswith(
        "**Automated state report**"
    ):
        return
    board = (
        await Board.objects.by_id(request.task.board_id).first(session)
        if request.task.board_id
        else None
    )
    if board is None:
        return
    dispatch = GatewayDispatchService(session)
    config = await dispatch.optional_gateway_config_for_board(board)
    if not config:
        return

    snippet = _truncate_snippet(request.message)
    actor_name = _comment_actor_name(request.actor)
    for agent in request.targets.values():
        if not agent.openclaw_session_id:
            continue
        mentioned = matches_agent_mention(agent, request.mention_names)
        header = "TASK MENTION" if mentioned else "NEW TASK COMMENT"
        action_line = (
            "You were mentioned in this comment."
            if mentioned
            else "A new comment was posted on your task."
        )
        notification = (
            f"{header}\n"
            f"Board: {board.name}\n"
            f"Task: {request.task.title}\n"
            f"Task ID: {request.task.id}\n"
            f"From: {actor_name}\n\n"
            f"{action_line}\n\n"
            f"Comment:\n{snippet}\n\n"
            "If you are mentioned but not assigned, reply in the task "
            "thread but do not change task status."
        )
        await _send_agent_task_message(
            dispatch=dispatch,
            session_key=agent.openclaw_session_id,
            config=config,
            agent_name=agent.name,
            message=notification,
        )


@dataclass(slots=True)
class _TaskUpdateInput:
    task: Task
    actor: ActorContext
    board_id: UUID
    previous_status: str
    previous_assigned: UUID | None
    status_requested: bool
    updates: dict[str, object]
    comment: str | None
    depends_on_task_ids: list[UUID] | None
    tag_ids: list[UUID] | None
    custom_field_values: TaskCustomFieldValues
    custom_field_values_set: bool
    previous_in_progress_at: datetime | None = None
    normalized_tag_ids: list[UUID] | None = None
    notification_stamp: datetime | None = None
    previous_max_tokens_budget: int | None = None
    previous_max_seconds_budget: int | None = None


def _required_status_value(value: object) -> str:
    if isinstance(value, str):
        return value
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def _optional_assigned_agent_id(value: object) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


async def _board_organization_id(
    session: AsyncSession,
    *,
    board_id: UUID,
) -> UUID:
    organization_id = (
        await session.exec(
            select(Board.organization_id).where(col(Board.id) == board_id),
        )
    ).first()
    if organization_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return organization_id


async def _task_dep_ids(
    session: AsyncSession,
    *,
    board_id: UUID,
    task_id: UUID,
) -> list[UUID]:
    deps_map = await dependency_ids_by_task_id(
        session,
        board_id=board_id,
        task_ids=[task_id],
    )
    return deps_map.get(task_id, [])


async def _task_blocked_ids(
    session: AsyncSession,
    *,
    board_id: UUID,
    dep_ids: Sequence[UUID],
) -> list[UUID]:
    if not dep_ids:
        return []
    dep_status = await dependency_status_by_id(
        session,
        board_id=board_id,
        dependency_ids=list(dep_ids),
    )
    return blocked_by_dependency_ids(
        dependency_ids=list(dep_ids),
        status_by_id=dep_status,
    )


async def _task_read_response(
    session: AsyncSession,
    *,
    task: Task,
    board_id: UUID,
) -> TaskRead:
    return (await _task_read_page(session=session, board_id=board_id, tasks=[task]))[0]


async def _require_task_user_write_access(
    session: AsyncSession,
    *,
    board_id: UUID,
    user: User | None,
) -> None:
    board = await Board.objects.by_id(board_id).first(session)
    if board is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_board_access(session, user=user, board=board, write=True)


def _lead_requested_fields(update: _TaskUpdateInput) -> set[str]:
    requested_fields = set(update.updates)
    if update.comment is not None:
        requested_fields.add("comment")
    if update.depends_on_task_ids is not None:
        requested_fields.add("depends_on_task_ids")
    if update.tag_ids is not None:
        requested_fields.add("tag_ids")
    if update.custom_field_values_set:
        requested_fields.add("custom_field_values")
    return requested_fields


def _validate_lead_update_request(update: _TaskUpdateInput) -> None:
    allowed_fields = {
        "assigned_agent_id",
        "status",
        "depends_on_task_ids",
        "tag_ids",
        "custom_field_values",
    }
    requested_fields = _lead_requested_fields(update)
    if update.comment is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Lead comment gate failed: board leads cannot include `comment` in task PATCH. "
                "Use the task comments endpoint instead."
            ),
        )
    disallowed_fields = requested_fields - allowed_fields
    if disallowed_fields:
        disallowed = ", ".join(sorted(disallowed_fields))
        allowed = ", ".join(sorted(allowed_fields))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Lead field gate failed: unsupported fields for board leads: "
                f"{disallowed}. Allowed fields: {allowed}."
            ),
        )


async def _lead_effective_dependencies(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> tuple[list[UUID], list[UUID]]:
    # Use newly normalized dependency updates when supplied; otherwise fall back
    # to the task's current dependencies for blocked-by evaluation.
    normalized_deps: list[UUID] | None = None
    if update.depends_on_task_ids is not None:
        if update.task.status == "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("Cannot change task dependencies after a task is done."),
            )
        normalized_deps = await replace_task_dependencies(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
            depends_on_task_ids=update.depends_on_task_ids,
        )
    effective_deps = (
        normalized_deps
        if normalized_deps is not None
        else await _task_dep_ids(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
        )
    )
    blocked_by = await _task_blocked_ids(
        session,
        board_id=update.board_id,
        dep_ids=effective_deps,
    )
    return effective_deps, blocked_by


async def _normalized_update_tag_ids(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> list[UUID] | None:
    if update.tag_ids is None:
        return None
    organization_id = await _board_organization_id(
        session,
        board_id=update.board_id,
    )
    return await validate_tag_ids(
        session,
        organization_id=organization_id,
        tag_ids=update.tag_ids,
    )


async def _lead_apply_assignment(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    if "assigned_agent_id" not in update.updates:
        return
    assigned_id = _optional_assigned_agent_id(update.updates["assigned_agent_id"])
    if not assigned_id:
        update.task.assigned_agent_id = None
        return
    agent = await Agent.objects.by_id(assigned_id).first(session)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if agent.is_board_lead:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Board leads cannot assign tasks to themselves.",
        )
    if agent.board_id and update.task.board_id and agent.board_id != update.task.board_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    update.task.assigned_agent_id = agent.id


async def _last_worker_who_moved_task_to_review(
    session: AsyncSession,
    *,
    task_id: UUID,
    board_id: UUID,
    lead_agent_id: UUID,
) -> UUID | None:
    statement = (
        select(col(ActivityEvent.agent_id))
        .where(col(ActivityEvent.task_id) == task_id)
        .where(col(ActivityEvent.event_type) == "task.status_changed")
        .where(col(ActivityEvent.message).like("Task moved to review:%"))
        .where(col(ActivityEvent.agent_id).is_not(None))
        .order_by(desc(col(ActivityEvent.created_at)))
    )
    candidate_ids = list(await session.exec(statement))
    for candidate_id in candidate_ids:
        if candidate_id is None or candidate_id == lead_agent_id:
            continue
        candidate = await Agent.objects.by_id(candidate_id).first(session)
        if candidate is None:
            continue
        if candidate.board_id != board_id or candidate.is_board_lead:
            continue
        return candidate.id
    return None


async def _lead_apply_status(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    if update.actor.actor_type != "agent" or update.actor.agent is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    lead_agent = update.actor.agent
    if "status" not in update.updates:
        return
    target_status = _required_status_value(update.updates["status"])
    # Leads may set `in_progress` when simultaneously assigning an agent to an
    # inbox task (assignment-and-start shortcut).
    if update.task.status != "review":
        assigning_agent = "assigned_agent_id" in update.updates and bool(
            _optional_assigned_agent_id(update.updates["assigned_agent_id"])
        )
        if update.task.status == "inbox" and target_status == "in_progress" and assigning_agent:
            update.task.status = target_status
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Lead status gate failed: board leads can only change status when the current "
                f"task status is `review` (current: `{update.task.status}`)."
            ),
        )
    if target_status not in {"done", "inbox"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Lead status target gate failed: review tasks can only move to `done` or "
                f"`inbox` (requested: `{target_status}`)."
            ),
        )
    if target_status == "inbox":
        update.task.assigned_agent_id = await _last_worker_who_moved_task_to_review(
            session,
            task_id=update.task.id,
            board_id=update.board_id,
            lead_agent_id=lead_agent.id,
        )
        update.task.in_progress_at = None
    update.task.status = target_status


def _task_event_details(task: Task, previous_status: str) -> tuple[str, str]:
    if task.status != previous_status:
        return "task.status_changed", f"Task moved to {task.status}: {task.title}."
    return "task.updated", f"Task updated: {task.title}."


async def _lead_notify_new_assignee(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    if not await _notification_current(session, update.task, update.notification_stamp or update.task.updated_at):
        return
    if (
        not update.task.assigned_agent_id
        or update.task.assigned_agent_id == update.previous_assigned
    ):
        return
    assigned_agent = await Agent.objects.by_id(update.task.assigned_agent_id).first(
        session,
    )
    if assigned_agent is None:
        return
    board = (
        await Board.objects.by_id(update.task.board_id).first(session)
        if update.task.board_id
        else None
    )
    if board:
        if (
            update.previous_status == "review"
            and update.task.status == "inbox"
            and update.actor.actor_type == "agent"
            and update.actor.agent
            and update.actor.agent.is_board_lead
        ):
            await _notify_agent_on_task_rework(
                session=session,
                board=board,
                task=update.task,
                agent=assigned_agent,
                lead=update.actor.agent,
            )
            return
        await _notify_agent_on_task_assign(
            session=session,
            board=board,
            task=update.task,
            agent=assigned_agent,
        )


async def _apply_lead_task_update(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> TaskRead:
    if update.actor.actor_type != "agent" or update.actor.agent is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    _validate_lead_update_request(update)
    _effective_deps, blocked_by = await _lead_effective_dependencies(
        session,
        update=update,
    )
    normalized_tag_ids = await _normalized_update_tag_ids(
        session,
        update=update,
    )

    # Blocked tasks should not be silently rewritten into a "blocked-safe" state.
    # Instead, reject assignment/status transitions with an explicit 409 payload.
    if blocked_by:
        attempted_fields: set[str] = set(update.updates.keys())
        attempted_transition = (
            "assigned_agent_id" in attempted_fields or "status" in attempted_fields
        )
        if attempted_transition:
            raise _blocked_task_error(blocked_by)

    await _lead_apply_assignment(session, update=update)
    await _lead_apply_status(session, update=update)
    await _require_valid_status_transition(
        session,
        board_id=update.board_id,
        previous_status=update.previous_status,
        target_status=update.task.status,
        task_id=update.task.id,
    )
    await _require_no_pending_approval_for_status_change_when_enabled(
        session,
        board_id=update.board_id,
        task_id=update.task.id,
        previous_status=update.previous_status,
        target_status=update.task.status,
        status_requested=update.status_requested,
    )
    await _require_review_before_done_when_enabled(
        session,
        board_id=update.board_id,
        task_complexity=update.task.complexity,
        previous_status=update.previous_status,
        target_status=update.task.status,
    )
    await _require_approved_linked_approval_for_done(
        session,
        board_id=update.board_id,
        task_id=update.task.id,
        task_complexity=update.task.complexity,
        previous_status=update.previous_status,
        target_status=update.task.status,
    )

    if normalized_tag_ids is not None:
        await replace_tags(
            session,
            task_id=update.task.id,
            tag_ids=normalized_tag_ids,
        )
    if update.custom_field_values_set:
        await _set_task_custom_field_values_for_update(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
            custom_field_values=update.custom_field_values,
        )

    update.task.updated_at = utcnow()
    update.notification_stamp = update.task.updated_at
    session.add(update.task)
    event_type, message = _task_event_details(update.task, update.previous_status)
    record_activity(
        session,
        event_type=event_type,
        task_id=update.task.id,
        message=message,
        agent_id=update.actor.agent.id,
        board_id=update.board_id,
    )
    await _reconcile_dependents_for_dependency_toggle(
        session,
        board_id=update.board_id,
        dependency_task=update.task,
        previous_status=update.previous_status,
        actor_agent_id=update.actor.agent.id,
    )
    await session.commit()
    await session.refresh(update.task)
    await _lead_notify_new_assignee(session, update=update)
    return await _task_read_response(
        session,
        task=update.task,
        board_id=update.board_id,
    )


async def _apply_non_lead_agent_task_rules(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    if update.actor.actor_type != "agent":
        return
    if (
        update.actor.agent
        and update.actor.agent.board_id
        and update.task.board_id
        and update.actor.agent.board_id != update.task.board_id
    ):
        raise _task_update_forbidden_error(
            code="task_board_mismatch",
            message="Agent can only update tasks for their assigned board.",
        )
    # Allow agents to claim unassigned tasks by updating status (when permitted by board rules).
    if (
        update.actor.agent
        and update.task.assigned_agent_id is not None
        and update.task.assigned_agent_id != update.actor.agent.id
        and "status" in update.updates
    ):
        raise _task_update_forbidden_error(
            code="task_assignee_mismatch",
            message="Agents can only change status on tasks assigned to them.",
        )
    # Agents are limited to status/comment updates, and non-inbox status moves
    # must pass dependency checks before they can proceed.
    allowed_fields = {"status", "comment", "custom_field_values"}
    if (
        update.depends_on_task_ids is not None
        or update.tag_ids is not None
        or not set(update.updates).issubset(
            allowed_fields,
        )
    ):
        raise _task_update_forbidden_error(
            code="task_update_field_forbidden",
            message="Agents may only update status, comment, and custom field values.",
        )
    if "status" in update.updates:
        only_lead_can_change_status = (
            await session.exec(
                select(col(Board.only_lead_can_change_status)).where(
                    col(Board.id) == update.board_id,
                ),
            )
        ).first()
        if only_lead_can_change_status:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only board leads can change task status.",
            )
        status_value = _required_status_value(update.updates["status"])
        # T28 (2026-05-14): run the more-specific board-rule gates BEFORE the
        # generic transition check. Otherwise `in_progress → done` raises the
        # generic "Invalid status transition" error before the review-gate
        # has a chance to surface its dict-shape, actionable message.
        await _require_review_before_done_when_enabled(
            session,
            board_id=update.board_id,
            task_complexity=update.task.complexity,
            previous_status=update.previous_status,
            target_status=status_value,
        )
        await _require_no_pending_approval_for_status_change_when_enabled(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
            previous_status=update.previous_status,
            target_status=status_value,
            status_requested=update.status_requested,
        )
        await _require_valid_status_transition(
            session,
            board_id=update.board_id,
            previous_status=update.previous_status,
            target_status=status_value,
            task_id=update.task.id,
        )
        if status_value != "inbox":
            dep_ids = await _task_dep_ids(
                session,
                board_id=update.board_id,
                task_id=update.task.id,
            )
            blocked_ids = await _task_blocked_ids(
                session,
                board_id=update.board_id,
                dep_ids=dep_ids,
            )
            if blocked_ids:
                raise _blocked_task_error(blocked_ids)
        if status_value == "inbox":
            update.task.assigned_agent_id = None
            update.task.previous_in_progress_at = update.task.in_progress_at
            update.task.in_progress_at = None
        elif status_value == "review":
            update.task.previous_in_progress_at = update.task.in_progress_at
            update.task.assigned_agent_id = None
            update.task.in_progress_at = None
        else:
            update.task.assigned_agent_id = update.actor.agent.id if update.actor.agent else None
            if status_value == "in_progress":
                update.task.in_progress_at = utcnow()
        # T28 (2026-05-14): apply the status value to the task model so callers
        # see the new status. Mirror what the lead-agent path already does at
        # L2718. Without this, non-lead status updates would silently no-op.
        update.task.status = status_value


# T28 (2026-05-14): `in_progress` no longer includes `done` by default.
# The canonical state machine is `inbox → in_progress → review → done` per
# audit-2026-05-13 + tests/test_tasks_status_transitions.py. L1 (trivial)
# tasks are explicitly allowed to skip review via an L1-bypass below.
# `blocked` is a REVERSIBLE holding state, not a sink. The watchdog sets it via
# raw SQL (owner-triage/stuck), but an assignee that wakes and does the work
# MUST be able to record progress out of it — otherwise the autonomous loop
# dead-ends (agent gets 409 `blocked → in_progress` and the task rots forever).
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "inbox": {"in_progress", "inbox", "blocked", "cancelled", "paused"},
    "in_progress": {"review", "inbox", "in_progress", "blocked", "cancelled", "paused"},
    "review": {"done", "inbox", "review", "blocked", "cancelled", "paused"},
    "done": {"inbox", "done", "paused"},
    "blocked": {"in_progress", "inbox", "review", "done", "blocked", "cancelled", "paused"},
    "cancelled": {"cancelled", "blocked", "inbox"},
    "paused": {"paused", "inbox", "review", "blocked", "cancelled"},
}


async def _require_valid_status_transition(
    session: AsyncSession,
    *,
    board_id: UUID,
    previous_status: str,
    target_status: str,
    task_id: UUID | None = None,
) -> None:
    """Enforce board workflow rules for status transitions."""
    if previous_status == target_status:
        return

    allowed = _VALID_TRANSITIONS.get(previous_status, set())
    # L1 trivial task bypass: in_progress → done is allowed if the task is
    # complexity 1. Caller must pass task_id so we can look up complexity.
    if (
        previous_status == "in_progress"
        and target_status == "done"
        and task_id is not None
    ):
        cplx_row = (
            await session.exec(
                sa_select(Task.complexity).where(Task.id == task_id),
            )
        ).first()
        if cplx_row == 1:
            return  # L1 may close directly without review.
    if target_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Invalid status transition: `{previous_status}` → `{target_status}`. "
                f"Allowed from `{previous_status}`: {', '.join(sorted(allowed))}."
            ),
        )

    # If board has require_review_before_done=true, done can only come from review
    # UNLESS task is L1 — trivial tasks skip review by design.
    if target_status == "done":
        requires_review = (
            await session.exec(
                select(col(Board.require_review_before_done)).where(col(Board.id) == board_id),
            )
        ).first()
        if requires_review and previous_status != "review":
            # Check if task is L1 — allow L1 to bypass review gate
            if task_id:
                cplx = (
                    await session.exec(
                        sa_select(Task.complexity).where(Task.id == task_id)
                    )
                ).first()
                if cplx == 1:
                    return  # L1 tasks skip review even on strict boards
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Board rule requires_review_before_done: task must pass through `review` before `done`.",
            )


async def _apply_admin_task_rules(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    admin_normalized_deps: list[UUID] | None = None
    update.normalized_tag_ids = await _normalized_update_tag_ids(
        session,
        update=update,
    )
    if update.depends_on_task_ids is not None:
        if update.task.status == "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("Cannot change task dependencies after a task is done."),
            )
        admin_normalized_deps = await replace_task_dependencies(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
            depends_on_task_ids=update.depends_on_task_ids,
        )

    effective_deps = (
        admin_normalized_deps
        if admin_normalized_deps is not None
        else await _task_dep_ids(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
        )
    )
    blocked_ids = await _task_blocked_ids(
        session,
        board_id=update.board_id,
        dep_ids=effective_deps,
    )
    target_status = _required_status_value(
        update.updates.get("status", update.task.status),
    )
    # Resume is not authority to bypass dependencies or silently restart work.
    if update.previous_status == "paused" and blocked_ids and target_status not in {"paused", "cancelled", "blocked"}:
        raise _blocked_task_error(blocked_ids)
    # Unmet dependencies must never undo an explicit stop or resume a stopped task.
    if (
        blocked_ids
        and target_status not in {"blocked", "cancelled", "paused"}
        and not (update.task.status == "done" and target_status == "done")
    ):
        update.task.status = "inbox"
        update.task.assigned_agent_id = None
        update.task.in_progress_at = None
        update.updates["status"] = "inbox"
        update.updates["assigned_agent_id"] = None

    if "status" in update.updates:
        status_value = _required_status_value(update.updates["status"])
        # Enforce valid transitions for admin users too
        await _require_valid_status_transition(
            session,
            board_id=update.board_id,
            previous_status=update.previous_status,
            target_status=status_value,
            task_id=update.task.id,
        )
        if status_value == "inbox":
            update.task.previous_in_progress_at = update.task.in_progress_at
            update.task.assigned_agent_id = None
            update.task.in_progress_at = None
        elif status_value == "review":
            update.task.previous_in_progress_at = update.task.in_progress_at
            update.task.assigned_agent_id = None
            update.task.in_progress_at = None
        elif status_value == "in_progress":
            update.task.in_progress_at = utcnow()

    assigned_agent_id = _optional_assigned_agent_id(
        update.updates.get("assigned_agent_id"),
    )
    if assigned_agent_id:
        agent = await Agent.objects.by_id(assigned_agent_id).first(session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if agent.board_id and update.task.board_id and agent.board_id != update.task.board_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        update.task.assigned_agent_id = assigned_agent_id


async def _record_task_comment_from_update(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
    commit: bool = True,
) -> None:
    if update.comment is None or not update.comment.strip():
        return
    event = ActivityEvent(
        event_type="task.comment",
        message=update.comment,
        task_id=update.task.id,
        board_id=update.task.board_id,
        agent_id=(
            update.actor.agent.id
            if update.actor.actor_type == "agent" and update.actor.agent
            else None
        ),
    )
    session.add(event)
    if commit:
        await session.commit()


async def _record_task_update_activity(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
    commit: bool = True,
) -> None:
    event_type, message = _task_event_details(update.task, update.previous_status)
    actor_agent_id = (
        update.actor.agent.id if update.actor.actor_type == "agent" and update.actor.agent else None
    )
    # Record the task transition first, then reconcile dependents so any
    # cascaded dependency effects are logged after the source change.
    if {"max_tokens_budget", "max_seconds_budget"} & update.updates.keys():
        # Budgets and their audit row share one commit; wake consumers only afterwards.
        assert update.actor.user is not None
        session.add(
            ActivityEvent(
                event_type=event_type,
                task_id=update.task.id,
                board_id=update.board_id,
                message=f"Task token cap changed: {update.previous_max_tokens_budget} -> "
                        f"{update.task.max_tokens_budget}; time budget: "
                        f"{update.previous_max_seconds_budget} -> {update.task.max_seconds_budget} "
                        f"(null = disabled; by user {update.actor.user.id}).",
            )
        )
    else:
        record_activity(
            session,
            event_type=event_type,
            task_id=update.task.id,
            message=message,
            agent_id=actor_agent_id,
            board_id=update.board_id,
        )
    await _reconcile_dependents_for_dependency_toggle(
        session,
        board_id=update.board_id,
        dependency_task=update.task,
        previous_status=update.previous_status,
        actor_agent_id=actor_agent_id,
    )
    if commit:
        await session.commit()


async def _assign_review_task_to_lead(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    if update.task.status != "review" or update.previous_status == "review" or _harness_task(update.task):
        return
    lead = (
        await Agent.objects.filter_by(board_id=update.board_id)
        .filter(col(Agent.is_board_lead).is_(True))
        .first(session)
    )
    if lead is None:
        return
    update.task.assigned_agent_id = lead.id


async def _notify_task_update_assignment_changes(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
) -> None:
    stamp = update.notification_stamp or update.task.updated_at
    if not await _notification_current(session, update.task, stamp):
        return
    board: Board | None = None

    async def _board() -> Board | None:
        nonlocal board
        if board is None and update.task.board_id:
            board = await Board.objects.by_id(update.task.board_id).first(session)
        return board

    if (
        update.task.status == "inbox"
        and update.task.assigned_agent_id is None
        and (update.previous_status != "inbox" or update.previous_assigned is not None)
    ):
        current_board = await _board()
        if current_board:
            await _notify_lead_on_task_unassigned(
                session=session,
                board=current_board,
                task=update.task,
            )

    if not await _notification_current(session, update.task, stamp):
        return
    if not update.task.assigned_agent_id:
        return

    assigned_agent = await Agent.objects.by_id(update.task.assigned_agent_id).first(
        session,
    )
    if assigned_agent is None:
        return

    assignment_changed = update.task.assigned_agent_id != update.previous_assigned
    entered_in_progress = (
        update.task.status == "in_progress" and update.previous_status != "in_progress"
    )

    if entered_in_progress and not assignment_changed:
        current_board = await _board()
        if current_board:
            await _wake_agent_online_for_task(
                session=session,
                board=current_board,
                task=update.task,
                agent=assigned_agent,
                reason="status_in_progress",
            )

    if not await _notification_current(session, update.task, stamp):
        return
    if not assignment_changed:
        return

    if (
        update.previous_status == "review"
        and update.task.status == "inbox"
        and update.actor.actor_type == "agent"
        and update.actor.agent
        and update.actor.agent.is_board_lead
    ):
        current_board = await _board()
        if current_board:
            await _notify_agent_on_task_rework(
                session=session,
                board=current_board,
                task=update.task,
                agent=assigned_agent,
                lead=update.actor.agent,
            )
        return

    if (
        update.actor.actor_type == "agent"
        and update.actor.agent
        and update.task.assigned_agent_id == update.actor.agent.id
    ):
        return

    current_board = await _board()
    if current_board:
        await _notify_agent_on_task_assign(
            session=session,
            board=current_board,
            task=update.task,
            agent=assigned_agent,
            wake_assignee=True,
        )


async def _record_stage_transition(
    session: AsyncSession,
    *,
    task: Task,
    previous_status: str,
    new_status: str,
    agent_id: UUID | None,
) -> None:
    """Close the open timing row for the previous stage and open a new one."""
    if new_status == previous_status:
        return

    now = utcnow()
    # Lock the open row to prevent concurrent status transitions from both
    # closing the same row (race condition: two goroutines both do UPDATE → done
    # simultaneously would produce duplicate closed rows).
    # T28 (2026-05-14): `FOR UPDATE` is Postgres-only; SQLite test fixtures
    # don't support it. Conditional on dialect.
    # T56 (2026-05-15): switched from `text(SELECT *).scalars()` which returned
    # the id column as UUID (caller then tried `row.exited_at = now` and hit
    # `UUID objects are immutable`). Use sqlmodel select + ORM rows so the
    # `FOR UPDATE` row lock still applies and attribute writes work.
    open_stmt = (
        select(TaskStageTiming)
        .where(col(TaskStageTiming.task_id) == task.id)
        .where(col(TaskStageTiming.exited_at).is_(None))
    )
    dialect_name = session.bind.dialect.name if session.bind else ""
    if dialect_name == "postgresql":
        open_stmt = open_stmt.with_for_update()
    open_rows = list((await session.exec(open_stmt)).all())
    for row in open_rows:
        row.exited_at = now
        delta = now - row.entered_at
        row.duration_seconds = max(0, int(delta.total_seconds()))
        session.add(row)
    if open_rows:
        await session.flush()

    # Open a new row for the current stage (caller commits)
    session.add(
        TaskStageTiming(
            task_id=task.id,
            status=new_status,
            agent_id=agent_id,
            entered_at=now,
        ),
    )


def _reset_attempt_budget(task: Task, previous_status: str, target_status: str | None) -> None:
    """Legacy retries may reset attempt budgets; native usage is durable telemetry."""
    if task.execution_mode == "team":
        return
    if previous_status != "paused" and target_status in {"inbox", "in_progress"} and target_status != previous_status:
        task.tokens_used = 0
        task.budget_exhausted_at = None


async def _finalize_updated_task(
    session: AsyncSession,
    *,
    update: _TaskUpdateInput,
    notify: bool = True,
) -> TaskRead:
    for key, value in update.updates.items():
        setattr(update.task, key, value)
    await _require_no_pending_approval_for_status_change_when_enabled(
        session,
        board_id=update.board_id,
        task_id=update.task.id,
        previous_status=update.previous_status,
        target_status=update.task.status,
        status_requested=update.status_requested,
    )
    await _require_review_before_done_when_enabled(
        session,
        board_id=update.board_id,
        task_complexity=update.task.complexity,
        previous_status=update.previous_status,
        target_status=update.task.status,
    )
    await _require_approved_linked_approval_for_done(
        session,
        board_id=update.board_id,
        task_id=update.task.id,
        task_complexity=update.task.complexity,
        previous_status=update.previous_status,
        target_status=update.task.status,
    )
    update.task.updated_at = utcnow()
    update.notification_stamp = update.task.updated_at

    status_raw = update.updates.get("status")
    _reset_attempt_budget(update.task, update.previous_status, status_raw)
    # Entering review can require a new comment or valid recent context when
    # the board-level rule is enabled.
    if status_raw == "review" and update.previous_status != "paused" and await _require_comment_for_review_when_enabled(
        session,
        board_id=update.board_id,
    ):
        comment_text = (update.comment or "").strip()
        review_comment_author = update.task.assigned_agent_id or update.previous_assigned
        review_comment_since = (
            update.task.previous_in_progress_at
            if update.task.previous_in_progress_at is not None
            else update.previous_in_progress_at
        )
        if not comment_text and not await has_valid_recent_comment(
            session,
            update.task,
            review_comment_author,
            review_comment_since,
        ):
            raise _comment_validation_error()
    await _assign_review_task_to_lead(session, update=update)

    if update.tag_ids is not None:
        normalized = (
            update.normalized_tag_ids
            if update.normalized_tag_ids is not None
            else await _normalized_update_tag_ids(
                session,
                update=update,
            )
        )
        await replace_tags(
            session,
            task_id=update.task.id,
            tag_ids=normalized or [],
        )

    if update.custom_field_values_set:
        await _set_task_custom_field_values_for_update(
            session,
            board_id=update.board_id,
            task_id=update.task.id,
            custom_field_values=update.custom_field_values,
        )

    atomic_cap = bool({"max_tokens_budget", "max_seconds_budget"} & update.updates.keys())
    session.add(update.task)
    if not atomic_cap:
        await session.commit()
        await session.refresh(update.task)
    # Record stage timing: close previous stage, open new one
    actual_status = update.task.status
    actor_agent_id = (
        update.actor.agent.id if update.actor.actor_type == "agent" and update.actor.agent else None
    )
    await _record_stage_transition(
        session,
        task=update.task,
        previous_status=update.previous_status,
        new_status=actual_status,
        agent_id=actor_agent_id,
    )
    if atomic_cap:
        await _record_task_comment_from_update(session, update=update, commit=False)
        await _record_task_update_activity(session, update=update, commit=False)
        await session.commit()
        await session.refresh(update.task)
        from app.services.task_event_bus import notify_task_changed

        notify_task_changed(update.board_id)
    else:
        await _record_task_comment_from_update(session, update=update)
        await _record_task_update_activity(session, update=update)
    if notify:
        await _notify_task_update_assignment_changes(session, update=update)

    return await _task_read_response(
        session,
        task=update.task,
        board_id=update.board_id,
    )


@router.post("/{task_id}/comments", response_model=TaskCommentRead)
async def create_task_comment(
    payload: TaskCommentCreate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> ActivityEvent:
    """Create a task comment and notify relevant agents."""
    await _validate_task_comment_access(session, task=task, actor=actor)
    event = ActivityEvent(
        event_type="task.comment",
        message=payload.message,
        task_id=task.id,
        board_id=task.board_id,
        agent_id=_comment_actor_id(actor),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    targets, mention_names = await _comment_targets(
        session,
        task=task,
        message=payload.message,
        actor=actor,
    )
    await _notify_task_comment_targets(
        session,
        request=_TaskCommentNotifyRequest(
            task=task,
            actor=actor,
            message=payload.message,
            targets=targets,
            mention_names=mention_names,
        ),
    )
    return event


# ── Task splitting ──────────────────────────────────────────────────────


class SplitRequest(BaseModel):
    method: str = "by_count"  # "by_count" or "by_sections"
    count: int = 2            # only for "by_count"


@router.post("/{task_id}/split", summary="Split task into parallel subtasks")
async def split_task(
    task_id: UUID,
    payload: SplitRequest,
    session: AsyncSession = Depends(get_session),
    actor: AuthContext = Depends(require_user_or_agent),
    board_id: UUID = None,  # from router prefix — resolved by Depends
):
    from app.services.task_splitter import split_by_count, split_by_sections

    stmt = sa_select(Task).where(Task.id == task_id)
    result = await session.execute(stmt)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.board_id != board_id:
        raise HTTPException(status_code=400, detail="Task does not belong to this board")

    if payload.method == "by_sections":
        children = await split_by_sections(session, task, actor_id=actor.user.id if actor else None)
    else:
        children = await split_by_count(session, task, payload.count, actor_id=actor.user.id if actor else None)

    # T90 (2026-05-16): event-driven wake on subtask creation. Without
    # this, subtasks wait up to 180s for the next watchdog tick before
    # any agent sees them. With it, lead+PMs wake within seconds and
    # the existing inbox-triage path routes children immediately.
    board = await Board.objects.filter_by(id=task.board_id).first(session)
    if board is not None:
        try:
            await _wake_lead_pms_for_subtask_creation(
                session=session, board=board, parent_task=task, children=children,
            )
        except Exception:
            pass
    # T91 (2026-05-16): immediate inbox-triage for the new subtasks
    # (fire-and-forget so the HTTP response doesn't block on per-child
    # LLM evaluator calls).
    try:
        import asyncio as _asyncio

        from app.db.session import async_session_maker as _asm
        from app.services.watchdog_loop import _route_manual_inbox_tasks
        async def _bg_triage():
            try:
                async with _asm() as _wd_db:
                    await _route_manual_inbox_tasks(_wd_db)
            except Exception:
                pass
        _asyncio.create_task(_bg_triage())
    except Exception:
        pass

    return [await _task_read_response(session, task=c, board_id=board_id) for c in children]


@router.get("/{task_id}/children", summary="List child subtasks")
async def list_children(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    board_id: UUID = None,
):
    stmt = sa_select(Task).where(
        Task.parent_task_id == task_id
    ).order_by(Task.split_position)
    result = await session.execute(stmt)
    children = result.scalars().all()
    return [await _task_read_response(session, task=c, board_id=board_id) for c in children]


@router.get("/{task_id}/parent", summary="Get parent task")
async def get_parent(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    board_id: UUID = None,
):
    stmt = sa_select(Task).where(Task.id == task_id)
    result = await session.execute(stmt)
    task = result.scalar_one_or_none()
    if not task or not task.parent_task_id:
        return None
    pstmt = sa_select(Task).where(Task.id == task.parent_task_id)
    presult = await session.execute(pstmt)
    parent = presult.scalar_one_or_none()
    if not parent:
        return None
    return await _task_read_response(session, task=parent, board_id=board_id)
