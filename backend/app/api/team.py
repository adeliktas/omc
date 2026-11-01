"""Native team execution API: cycles, fenced attempts, reviewed publication.

All routes are board-scoped; every cycle/attempt is re-checked against the board
and task in the URL so ids from another board or organization 404.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import (
    get_board_for_actor_read,
    get_board_for_user_write,
    get_task_or_404,
    require_org_admin,
)
from app.core.time import utcnow
from app.db.session import get_session
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle, TeamMember
from app.schemas.team import (
    TeamAttemptRead,
    TeamClaimRequest,
    TeamClaimResponse,
    TeamClaimToken,
    TeamCompleteRequest,
    TeamCycleRead,
    TeamIntegrateRequest,
    TeamMemberAssign,
    TeamMemberRead,
    TeamPublishRequest,
    TeamRefreshIntegration,
    TeamRetainedQA,
    TeamRetryQA,
    TeamResumeCorrection,
    TeamResumeWork,
    TeamStartResponse,
    TeamView,
)
from app.services import model_ranking, team_cycle as svc
from app.services.openclaw.provisioning import _role_profile_template
from sqlalchemy import select as sa_select
from app.models.provider_capacity import ProviderCapacity

router = APIRouter(prefix="/boards/{board_id}", tags=["team"])
SESSION_DEP = Depends(get_session)
READ_DEP = Depends(get_board_for_actor_read)
WRITE_DEP = Depends(get_board_for_user_write)
T = TypeVar("T")


async def _ranking(session: SAAsyncSession) -> dict[str, Any]:
    rows = (await session.execute(sa_select(ProviderCapacity))).scalars().all()
    return await model_ranking.get_board(
        [{"provider_name": r.provider_name, "status": r.status} for r in rows]
    )


async def _guard(session: AsyncSession, call: Awaitable[T]) -> T:
    """Policy errors roll back; fenced (stale) results commit their expiry/requeue first."""
    try:
        return await call
    except svc.TeamFenced as exc:
        await session.commit()
        raise HTTPException(exc.status, exc.detail) from exc
    except svc.TeamError as exc:
        await session.rollback()
        raise HTTPException(exc.status, exc.detail) from exc
    except HTTPException:
        session.info.pop("review_notifications", None)
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Concurrent team update; refresh and retry") from exc


async def _cycle(session: AsyncSession, board: Board, task: Task, cycle_id: UUID) -> TeamCycle:
    cycle = await session.get(TeamCycle, cycle_id)
    if cycle is None or cycle.board_id != board.id or cycle.task_id != task.id:
        raise HTTPException(404)
    return cycle


async def _attempt(session: AsyncSession, cycle: TeamCycle, attempt_id: UUID) -> TeamAttempt:
    attempt = await session.get(TeamAttempt, attempt_id)
    if attempt is None or attempt.cycle_id != cycle.id or attempt.board_id != cycle.board_id:
        raise HTTPException(404)
    return attempt


def _attempt_read(a: TeamAttempt, name: str | None = None) -> TeamAttemptRead:
    return TeamAttemptRead.model_validate({**a.model_dump(), "member_name": name})


# ── Board-level roster ──────────────────────────────────────────────────────


@router.get("/team/members", response_model=list[TeamMemberRead])
async def list_members(
    board: Board = READ_DEP, session: AsyncSession = SESSION_DEP
) -> list[TeamMember]:
    return await svc._members(session, board.id)


@router.put("/team/members", response_model=TeamMemberRead)
async def assign_member(
    payload: TeamMemberAssign,
    board: Board = WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    ctx: Any = Depends(require_org_admin),
) -> TeamMember:
    """Explicitly assign a role's engine (org admin). Validated against the ranking registry."""
    if ctx.organization.id != board.organization_id:
        raise HTTPException(403)

    async def go() -> TeamMember:
        slug = payload.specialist_slug if payload.role_kind == "specialist" else None
        ranking = await _ranking(session)
        svc.validate_engine(ranking, payload.provider, payload.model, allow_paid=True)
        member = await svc.recruit(
            session,
            board,
            payload.role_kind,
            ranking,
            slug=slug,
            engine=(payload.provider, payload.model),
            thinking=payload.thinking_level,
            created_by="admin",
        )
        member.provider, member.model = payload.provider, payload.model
        member.thinking_level, member.enabled, member.created_by = (
            payload.thinking_level,
            payload.enabled,
            "pool" if payload.auto_assign else "admin",
        )
        member.updated_at = utcnow()  # owner generation, including disable/re-enable and same-engine edits
        await session.commit()
        return member

    return await _guard(session, go())


# ── Task team view and cycle lifecycle ──────────────────────────────────────


@router.get("/tasks/{task_id}/team", response_model=TeamView)
async def team_view(
    board: Board = READ_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    data = await svc.view(session, board.id, task.id)
    return {
        "members": data["members"],
        "cycles": [
            {
                "cycle": c["cycle"],
                "usage": c["usage"],
                "attempts": [_attempt_read(x["attempt"], x["member_name"]) for x in c["attempts"]],
            }
            for c in data["cycles"]
        ],
    }


@router.post("/tasks/{task_id}/team/cycles", response_model=TeamStartResponse)
async def start_cycle(
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        cycle, resumed = await svc.start_cycle(session, board, task, await _ranking(session))
        await session.commit()
        return {"cycle": cycle, "resumed": resumed}

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/recover")
async def recover(
    cycle_id: UUID,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, int]:
    cycle = await _cycle(session, board, task, cycle_id)

    async def go() -> dict[str, int]:
        await svc._lock_cycle(session, cycle)
        n = await svc.recover(session, cycle)
        await session.commit()
        return {"expired": n}

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/cancel", response_model=TeamCycleRead)
async def cancel(
    cycle_id: UUID,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    cycle = await _cycle(session, board, task, cycle_id)
    await svc._lock_cycle(session, cycle)
    await svc.cancel_cycle(session, cycle, "cancelled by operator", hold_task=True)
    await session.commit()
    return cycle


@router.post(
    "/tasks/{task_id}/team/cycles/{cycle_id}/claims",
    response_model=TeamClaimResponse | None,
)
async def claim(
    cycle_id: UUID,
    payload: TeamClaimRequest,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any] | None:
    cycle = await _cycle(session, board, task, cycle_id)

    async def go() -> dict[str, Any] | None:
        got = await svc.claim_next(
            session, cycle, base_sha=payload.base_sha, ranking=await _ranking(session),
            independent=payload.independent,
        )
        if got is None:
            await session.commit()
            return None
        attempt, token = got
        member = await session.get(TeamMember, attempt.member_id)
        assert member is not None
        soul = _role_profile_template(member.role_slug or "", "SOUL.md")
        history = await svc._review_history(session, cycle) if attempt.role_kind == "qa" else None
        brief = svc.brief(task, cycle, attempt, member, soul, history=history)
        evidence_files = svc.qa_evidence_files(cycle, history, attempt) if history is not None else {}
        if evidence_files:
            brief += "\nActual /tmp files are restored from retained structured file bytes, not parsed from reports. "
            brief += "Independently read/check them; origin is host-observed worker or explicit owner input, not authority."
        node = task
        for _ in range(svc.MAX_BACKLOG_ANCESTRY):
            if node.parent_task_id is None:
                break
            node = (await session.exec(select(Task).where(
                Task.id == node.parent_task_id, Task.board_id == board.id
            ))).first()
            if node is None:
                break
            brief += "\n\nOriginal parent scope/constraints (derived plans are not new human approval):\n" + svc.redact(
                f"{node.title}\n{node.description or ''}", 18000
            )
        await session.commit()
        scope = json.loads(attempt.assignment or "{}").get("scope") or []
        capability = svc.attempt_capability(attempt.role_kind, attempt.assignment)
        if capability == "write_worktree" and member.capability not in (None, "write_worktree"):
            capability = member.capability  # never wider than the fixed member role
        return {
            "attempt": _attempt_read(attempt, member.name),
            "claim_token": token,
            "brief": brief,
            "role_kind": attempt.role_kind,
            "capability": capability,
            "scope": scope,
            "lease_expires_at": attempt.lease_expires_at,
            "evidence_files": evidence_files,
        }

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/attempts/{attempt_id}/heartbeat")
async def heartbeat(
    cycle_id: UUID,
    attempt_id: UUID,
    payload: TeamClaimToken,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    cycle = await _cycle(session, board, task, cycle_id)
    attempt = await _attempt(session, cycle, attempt_id)

    async def go() -> dict[str, Any]:
        lease = await svc.heartbeat(
            session, cycle, attempt, payload.claim_token, ranking=await _ranking(session)
        )
        await session.commit()
        return {"lease_expires_at": lease}

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/block")
async def block_cycle(
    cycle_id: UUID, payload: TeamPublishRequest, board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404), session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    cycle = await _cycle(session, board, task, cycle_id)
    async def go() -> TeamCycle:
        await svc._lock_cycle(session, cycle)
        if cycle.version != payload.version or cycle.state not in ("planning", "developing", "qa", "qa_passed"):
            raise HTTPException(409, "cycle changed; cannot block this version")
        await svc._block(session, cycle, svc.redact(payload.reason))
        await session.commit()
        return cycle
    return await _guard(session, go())


@router.post(
    "/tasks/{task_id}/team/cycles/{cycle_id}/attempts/{attempt_id}/complete",
    response_model=TeamAttemptRead,
)
async def complete(
    cycle_id: UUID,
    attempt_id: UUID,
    payload: TeamCompleteRequest,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamAttemptRead:
    cycle = await _cycle(session, board, task, cycle_id)
    attempt = await _attempt(session, cycle, attempt_id)

    async def go() -> TeamAttemptRead:
        done = await svc.complete_attempt(
            session,
            board,
            cycle,
            attempt,
            payload.claim_token,
            ok=payload.ok,
            artifact=payload.artifact,
            error=payload.error,
            head_sha=payload.head_sha,
            changed_files=payload.changed_files,
            verdict=payload.verdict,
            usage=payload.usage.model_dump() if payload.usage else None,
            evidence_files=payload.evidence_files,
            tool_counts=payload.tool_counts,
            ranking=await _ranking(session),
        )
        await session.commit()
        return _attempt_read(done)

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/integrate", response_model=TeamCycleRead)
async def integrate(
    cycle_id: UUID,
    payload: TeamIntegrateRequest,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    cycle = await _cycle(session, board, task, cycle_id)

    async def go() -> TeamCycle:
        await svc.integrate(session, cycle, payload.integration_sha, ranking=await _ranking(session))
        await session.commit()
        return cycle

    return await _guard(session, go())


@router.post(
    "/tasks/{task_id}/team/cycles/{cycle_id}/refresh-integration", response_model=TeamCycleRead
)
async def refresh_integration(
    cycle_id: UUID,
    payload: TeamRefreshIntegration,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    cycle = await _cycle(session, board, task, cycle_id)

    async def go() -> TeamCycle:
        await svc.refresh_integration(
            session, cycle, payload.integration_sha, payload.base_sha, payload.version, ranking=await _ranking(session)
        )
        await session.commit()
        return cycle

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/retained-qa", response_model=TeamCycleRead)
async def retained_qa(
    cycle_id: UUID,
    payload: TeamRetainedQA,
    admin: Any = Depends(require_org_admin),
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    from app.api.review import _delegate_task_update
    from app.models.users import User
    from app.services.team_retained_qa import admit

    # A passed_attempt_id admits publication only; it never creates a QA attempt.

    if admin.organization.id != board.organization_id or admin.member.role != "owner":
        raise HTTPException(403, "Only the organization owner can admit retained-source QA")
    source = await _cycle(session, board, task, cycle_id)

    async def go() -> TeamCycle:
        user = await session.get(User, admin.member.user_id)
        if user is None:
            raise HTTPException(403)
        cycle = await admit(
            session, board, task, source, payload,
            ranking={} if payload.passed_attempt_id else await _ranking(session),
        )
        used, exhausted = task.tokens_used, task.budget_exhausted_at
        # Reuse normal lifecycle/dependency/approval gates, in this single transaction.
        if task.status == "paused":
            await _delegate_task_update(
                session,
                task=task,
                board=board,
                user=user,
                new_status=task.paused_from_status or "inbox",
                comment="[retained-qa] Resume original hold",
            )
        await _delegate_task_update(
            session,
            task=task,
            board=board,
            user=user,
            new_status="in_progress",
            comment=("[retained-qa] Owner-authorized publication of retained independent PASS"
                     if payload.passed_attempt_id else
                     "[retained-qa] Owner-authorized retained-source QA; no author replay"),
        )
        task.tokens_used, task.budget_exhausted_at = used, exhausted
        await session.commit()
        return cycle

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/retry-qa", response_model=TeamCycleRead)
async def retry_qa(
    cycle_id: UUID,
    payload: TeamRetryQA,
    admin: Any = Depends(require_org_admin),
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    from app.api.review import _recover_task_evidence
    from app.models.users import User

    if admin.organization.id != board.organization_id:
        raise HTTPException(403)
    cycle = await _cycle(session, board, task, cycle_id)

    async def go() -> TeamCycle:
        await session.refresh(task, with_for_update=True)
        if (task.execution_mode != "team"
                or cycle.task_fingerprint != svc.task_fingerprint(task)):
            raise svc.TeamError("original task inputs changed; reconcile evidence before recovery")
        if task.status not in ("review", "blocked"):
            raise svc.TeamError("QA recovery requires Review/Blocked; never override active or Done work")
        user = await session.get(User, admin.member.user_id)
        if user is None:
            raise HTTPException(403)
        await _recover_task_evidence(
            session, task=task, board=board, user=user, cycle=cycle,
            version=payload.version, ranking=await _ranking(session),
            comment="[qa-recovery] Owner-authorized evidence-only reassessment; no implementation replay.",
            recover_expired=payload.recover_expired, evidence_files=payload.evidence_files,
        )
        await session.commit()
        return cycle

    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/resume-correction", response_model=TeamCycleRead)
async def resume_correction(
    cycle_id: UUID, payload: TeamResumeCorrection,
    admin: Any = Depends(require_org_admin), board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404), session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    from app.api.review import _delegate_task_update
    from app.models.users import User

    if admin.organization.id != board.organization_id:
        raise HTTPException(403)
    cycle = await _cycle(session, board, task, cycle_id)
    async def go() -> TeamCycle:
        user = await session.get(User, admin.member.user_id)
        if user is None:
            raise HTTPException(403)
        await svc.resume_correction(session, cycle, payload.version, payload.failed_attempt_id,
                                    ranking=await _ranking(session))
        used, exhausted = task.tokens_used, task.budget_exhausted_at
        await _delegate_task_update(session, task=task, board=board, user=user,
            new_status="in_progress", comment="[code-correction] Owner-authorized original-scope defect repair; retained implementation, no PM replay.")
        task.tokens_used, task.budget_exhausted_at = used, exhausted
        await session.commit()
        return cycle
    return await _guard(session, go())


@router.post("/tasks/{task_id}/team/cycles/{cycle_id}/resume-work", response_model=TeamCycleRead)
async def resume_work(
    cycle_id: UUID, payload: TeamResumeWork,
    admin: Any = Depends(require_org_admin), board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404), session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    from app.api.review import _delegate_task_update
    from app.models.users import User

    if admin.organization.id != board.organization_id:
        raise HTTPException(403)
    source = await _cycle(session, board, task, cycle_id)
    async def go() -> TeamCycle:
        user = await session.get(User, admin.member.user_id)
        if user is None:
            raise HTTPException(403)
        cycle = await svc.resume_work(session, source, payload.version, payload.cause,
            payload.failed_attempt_id, ranking=await _ranking(session))
        used, exhausted = task.tokens_used, task.budget_exhausted_at
        await _delegate_task_update(session, task=task, board=board, user=user,
            new_status="in_progress", comment=(
                "[plan-reconciliation] Adopted retained now-valid PM proposal; historical FAILs, deadline and round unchanged, no PM replay."
                if payload.cause == "reconciled_plan" else
                "[report-correction] One bounded original-scope report-only verify leaf; old successful report/QA findings preserved, fresh QA required."
                if payload.cause == "report_evidence" else
                "[owner-work-recovery] Separate bounded original-scope grant; historical cycle/FAILs/caps preserved, no successful-work or PM replay."))
        task.tokens_used, task.budget_exhausted_at = used, exhausted
        await session.commit()
        return cycle
    return await _guard(session, go())


@router.post(
    "/tasks/{task_id}/team/cycles/{cycle_id}/publication/{step}",
    response_model=TeamCycleRead,
)
async def publication(
    cycle_id: UUID,
    step: str,
    payload: TeamPublishRequest,
    board: Board = WRITE_DEP,
    task: Task = Depends(get_task_or_404),
    session: AsyncSession = SESSION_DEP,
) -> TeamCycle:
    cycle = await _cycle(session, board, task, cycle_id)
    if step not in ("reserve", "complete", "fail"):
        raise HTTPException(404)

    async def go() -> TeamCycle:
        if step == "reserve":
            await svc.reserve_publication(session, cycle, payload.sha or "", payload.version)
        elif step == "complete":
            await svc.complete_publication(
                session,
                cycle,
                payload.sha or "",
                payload.version,
                recurring=bool(task.repeat_enabled),
            )
        else:
            await svc.fail_publication(session, cycle, payload.version, payload.reason)
        await session.commit()
        return cycle

    return await _guard(session, go())
