"""Live work is distinct from workflow state (especially delegated parents)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlmodel import col, select

from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle

REVIEW_LEASE_EVENT = "review.lease"
REVIEW_LEASE_SECONDS = 20


def task_stamp(task: Task) -> str:
    return task.updated_at.replace(tzinfo=None).isoformat()


def lease_data(event: ActivityEvent) -> dict[str, Any]:
    """Only the authenticated coordinator writes these bounded control records."""
    data = json.loads(event.message or "{}")
    if not isinstance(data, dict):
        raise ValueError("review lease unavailable")
    for key in ("deadline", "expires_at", *(["finished_at"] if "finished_at" in data else [])):
        value = datetime.fromisoformat(data[key])
        if value.tzinfo is not None:
            raise ValueError("invalid review lease clock")
    if not isinstance(data.get("session_id"), str):
        raise ValueError("review lease unavailable")
    UUID(data["session_id"])
    if not all(isinstance(data.get(key), str) and data[key] for key in (
        "provider", "model", "fingerprint", "task_stamp",
    )):
        raise ValueError("review lease unavailable")
    return data


async def live_review_leases(session, task_ids: list[UUID] | None = None, *, include_finished=False):
    # ponytail: scan only live control records; a dedicated lease table if volume requires it.
    statement = select(ActivityEvent, Task).join(Task, Task.id == ActivityEvent.task_id).where(
        ActivityEvent.event_type == REVIEW_LEASE_EVENT,
        Task.status == "review",
        Task.board_id == ActivityEvent.board_id,
    )
    if task_ids is not None:
        statement = statement.where(col(Task.id).in_(task_ids))
    rows = (await session.exec(statement.execution_options(populate_existing=True))).all()
    now = utcnow()
    result = []
    for event, task in rows:
        data = lease_data(event)  # corrupt authority must not silently free a capacity slot
        if ((include_finished or "finished_at" not in data)
                and data["task_stamp"] == task_stamp(task)
                and datetime.fromisoformat(data["expires_at"]) > now
                and datetime.fromisoformat(data["deadline"]) > now):
            result.append((event, data))
    return result


async def review_load(session) -> dict[tuple[str, str], int]:
    """Live review leases per engine; max_concurrency is a per-model limit (2026-10-24)."""
    load: dict[tuple[str, str], int] = {}
    for _, data in await live_review_leases(session):
        key = (data["provider"], data["model"])
        load[key] = load.get(key, 0) + 1
    return load


def _single_harness_inbox_detail(task: Task, ranking: dict[str, Any] | None) -> str | None:
    """Negative diagnostic only: configuration is never work/approval evidence.

    Native team admission and the legacy writing executor are separate. In
    particular, a trusted read-only endpoint must not become a writing team or
    acquire disclosure authority merely to get a task out of Inbox.
    """
    if ranking is None:
        return (
            "Execution admission UNKNOWN: ranking/accounting availability could not be checked. "
            "Coordinator must verify the pinned engine and qualified runtime; no fallback."
        )
    from app.services.team_cycle import _entries, write_engine_allowed

    entry = next(
        (
            e
            for e in _entries(ranking)
            if (e.get("provider"), e.get("model")) == (task.harness_provider, task.harness_model)
        ),
        None,
    )
    if entry is None:
        return (
            "Execution refused: pinned engine is not registered in ranking. "
            "Coordinator must verify registration/accounting and a qualified executor; "
            "pins and mode unchanged, no fallback."
        )
    reasons = []
    if entry.get("execution") != "agent_harness":
        reasons.append("unsupported single executor (model endpoint, not a legacy writing harness)")
    if not write_engine_allowed(ranking, task.harness_provider or "", task.harness_model or ""):
        reasons.append("read-only engine: legacy single execution requires writing")
    if entry.get("provider_status") == "unregistered":
        reasons.append(
            "provider_unregistered: provider availability is unregistered; "
            "accounting needs separate verification"
        )
    elif entry.get("disabled") or not entry.get("eligible"):
        reasons.append(
            "pinned engine disabled or unavailable; coordinator must verify availability"
        )
    if int(task.complexity or 3) > int(entry.get("max_complexity", 5)):
        reasons.append("task exceeds pinned engine complexity ceiling")
    if not reasons:
        return None  # Not a positive admission, claim, runtime check or approval.
    return (
        "Execution refused: " + "; ".join(reasons) + ". "
        "Read-only research needs a separately verified qualified runtime and genuine "
        "access/disclosure approvals; fully_trusted grants neither writes nor disclosure. "
        "Pins and mode unchanged; no fallback."
    )


async def _single_harness_inbox_details(session, tasks: list[Task]) -> dict[UUID, str | None]:
    candidates = [
        task
        for task in tasks
        if (
            task.status == "inbox"
            and task.execution_mode == "single"
            and (
                task.execution_lane == "harness"
                or (
                    not task.execution_lane
                    and (task.title or "").startswith(("[agent]", "[claude]", "[codex]"))
                )
            )
            and task.harness_provider
            and task.harness_model
        )
    ]
    if not candidates:
        return {}
    from app.models.provider_capacity import ProviderCapacity
    from app.services import model_ranking

    try:
        providers = (await session.exec(select(ProviderCapacity))).all()
        ranking = await model_ranking.get_board(
            [{"provider_name": row.provider_name, "status": row.status} for row in providers]
        )
        return {task.id: _single_harness_inbox_detail(task, ranking) for task in candidates}
    except Exception:
        # A diagnostic must not break task reads or misreport unobserved admission.
        # Never expose exception text (which may include private runtime details).
        return {task.id: _single_harness_inbox_detail(task, None) for task in candidates}


async def task_execution_state(session, *, board_id: UUID, tasks: list[Task]):
    """Batched GET/SSE projection; never change lifecycle or renew work authority."""
    result = {
        task.id: {
            "dag_done_count": 0 if task.split_count else None,
            "work_state": "waiting" if task.status == "in_progress" else None,
            "work_started_at": None,
            "work_expires_at": None,
            "work_detail": (
                "Waiting for children" if task.split_count
                else "Waiting for engine" if task.execution_mode == "team"
                else "Worker activity UNKNOWN"
            ) if task.status == "in_progress" else None,
        }
        for task in tasks
    }
    if not tasks:
        return result
    for task_id, detail in (await _single_harness_inbox_details(session, tasks)).items():
        result[task_id]["work_detail"] = detail
    from app.services.team_cycle import PUBLISH_LEASE_SECONDS

    now = utcnow()
    ids = list(result)
    parents = [task.id for task in tasks if task.split_count]
    if parents:
        counts = (await session.exec(
            select(Task.parent_task_id, func.count()).where(
                Task.board_id == board_id, col(Task.parent_task_id).in_(parents),
                Task.status == "done",
            ).group_by(Task.parent_task_id)
        )).all()
        for parent, count in counts:
            result[parent]["dag_done_count"] = count
    running = (await session.exec(
        select(TeamCycle.task_id, TeamAttempt.role_kind, TeamAttempt.provider, TeamAttempt.model,
               TeamAttempt.started_at, TeamAttempt.lease_expires_at, TeamCycle.deadline_at)
        .join(TeamAttempt, TeamAttempt.cycle_id == TeamCycle.id)
        .join(Task, Task.id == TeamCycle.task_id).where(
            Task.board_id == board_id, col(Task.id).in_(ids), Task.status == "in_progress",
            TeamCycle.board_id == board_id,
            TeamAttempt.task_id == TeamCycle.task_id, TeamAttempt.board_id == TeamCycle.board_id,
            col(TeamCycle.state).in_(["planning", "developing", "qa"]),
            TeamAttempt.status == "running", TeamAttempt.lease_expires_at > now,
            TeamCycle.deadline_at > now,
        )
    )).all()
    for task_id, role, provider, model, started_at, expires_at, deadline_at in running:
        result[task_id].update(work_state="running", work_detail=f"{role} · {provider}/{model}",
                               work_started_at=started_at,
                               work_expires_at=min(expires_at, deadline_at))
    publishing = (await session.exec(
        select(TeamCycle.task_id, TeamCycle.updated_at, TeamCycle.deadline_at)
        .join(Task, Task.id == TeamCycle.task_id).where(
            Task.board_id == board_id, col(Task.id).in_(ids), Task.status == "in_progress",
            TeamCycle.board_id == board_id,
            TeamCycle.state == "publishing", TeamCycle.deadline_at > now,
            TeamCycle.updated_at > now - timedelta(seconds=PUBLISH_LEASE_SECONDS),
        )
    )).all()
    for task_id, updated_at, deadline_at in publishing:
        expires_at = min(deadline_at.replace(tzinfo=None),
                         updated_at.replace(tzinfo=None) + timedelta(seconds=PUBLISH_LEASE_SECONDS))
        result[task_id].update(work_state="running", work_detail="Publishing reviewed head",
                               work_expires_at=expires_at)
    try:
        leases = await live_review_leases(session, ids)
    except (KeyError, TypeError, ValueError):
        leases = []  # visibility is UNKNOWN, never asserted running on malformed evidence
    for event, data in leases:
        result[event.task_id].update(
            work_state="reviewing", work_detail=f"Reviewing · {data['provider']}/{data['model']}",
            work_started_at=event.created_at,
            work_expires_at=min(datetime.fromisoformat(data['expires_at']),
                                datetime.fromisoformat(data['deadline'])),
        )
    return result
