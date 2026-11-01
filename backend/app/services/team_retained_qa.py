"""One owner-authorized retained-source QA grant; never replay or clone author attempts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta, timezone
from typing import Any
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.evidence_redaction import redact_evidence
from app.core.time import utcnow
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import ACTIVE_CYCLE_STATES, TeamAttempt, TeamCycle
from app.schemas.team import TeamRetainedQA
from app.services import team_cycle as svc
from app.services.owner_reviews import is_reviewer_engine


def contributor(cycle: TeamCycle, attempt: TeamAttempt) -> bool:
    """Only server-created lineage may cite real old successful authors."""
    if (
        attempt.board_id != cycle.board_id
        or attempt.task_id != cycle.task_id
        or attempt.role_kind not in ("developer", "specialist")
    ):
        return False
    if attempt.cycle_id == cycle.id:
        return attempt.round <= cycle.round
    retained = svc._retained(cycle)
    if retained.get("publication_source_id"):
        return str(attempt.id) in retained.get("publication_author_ids", [])
    return (
        attempt.status == "succeeded"
        and str(attempt.id) in retained.get("author_ids", [])
        and str(attempt.cycle_id) == retained.get("source_cycle_id")
    )


async def history(session: AsyncSession, cycle: TeamCycle) -> list[TeamAttempt]:
    """Return original rows as provenance, never copies or additional usage events."""
    own = await svc._attempts(session, cycle.id)
    retained = svc._retained(cycle)
    if retained.get("publication_source_id"):
        _, provenance = await publication_basis(session, cycle)
        return own + provenance
    ids = retained.get("author_ids") or []
    if not ids:
        return own
    if retained.get("correction_source_id"):
        parent = await session.get(TeamCycle, UUID(retained["correction_source_id"]))
        if (
            parent is None
            or parent.task_id != cycle.task_id
            or parent.board_id != cycle.board_id
            or parent.state != "cancelled"
            or parent.version != retained["correction_source_version"]
            or parent.integration_sha != retained["correction_source_integration_sha"]
        ):
            raise svc.TeamError("retained correction provenance changed")
    source = await session.get(TeamCycle, UUID(retained["source_cycle_id"]))
    if (
        source is None
        or source.board_id != cycle.board_id
        or source.task_id != cycle.task_id
        or source.version != retained["source_version"]
        or source.integration_sha != retained["integration_sha"]
    ):
        raise svc.TeamError("retained source provenance changed")
    authors = await svc._attempts(session, source.id)
    selected = [a for a in authors if str(a.id) in ids and contributor(cycle, a)]
    if len(selected) != len(ids):
        raise svc.TeamError("retained source authors unavailable")
    return own + selected


def _digest(row: Any) -> str:
    return hashlib.sha256(
        json.dumps(row.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


async def publication_basis(
    session: AsyncSession, cycle: TeamCycle
) -> tuple[TeamCycle, list[TeamAttempt]]:
    """Revalidate immutable original rows; references are not new work or usage."""
    lineage = svc._retained(cycle)
    source = await session.get(TeamCycle, UUID(lineage["publication_source_id"]))
    if source is None or _digest(source) != lineage.get("publication_source_sha256"):
        raise svc.TeamError("retained publication source changed")
    if source.task_id != cycle.task_id or source.board_id != cycle.board_id:
        raise svc.TeamError("retained publication source scope changed")
    if svc._retained(source).get("publication_source_id"):
        raise svc.TeamError("publication authority cannot recursively certify publication")
    provenance = await history(session, source)
    if {str(a.id): _digest(a) for a in provenance} != lineage.get("publication_attempt_sha256"):
        raise svc.TeamError("retained publication attempt provenance changed")
    if source.reviewed_sha != cycle.reviewed_sha or source.integration_sha != cycle.integration_sha:
        raise svc.TeamError("retained publication exact source changed")
    return source, provenance


async def _publication(
    session: AsyncSession, board: Board, task: Task, source: TeamCycle, payload: TeamRetainedQA
) -> TeamCycle:
    """New finite publication authorization, not revival or a fabricated successful QA."""
    if (
        payload.failed_attempt_id is not None
        or payload.authorized_token_cap is not None
        or task.status not in ("paused", "blocked")
        or task.execution_mode != "team"
        or task.execution_lane not in (None, "harness")
        or task.split_count
        or task.budget_exhausted_at is not None
    ):
        raise svc.TeamError("retained publication task or budget is not eligible")
    problem = await svc._parent_problem(session, task)
    if problem:
        raise svc.TeamError(problem)
    cycles = list(await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))
    # These are cumulative snapshots, not additive usage events. Preserve the high-water mark.
    charges = [payload.external_input_output_charge] + [
        svc._retained(c).get("external_input_output_charge", 0) for c in cycles
    ]
    if any(type(charge) is not int or charge < 0 for charge in charges):
        raise svc.TeamError("invalid recorded external usage")
    external_charge = max(charges)
    if (
        task.max_tokens_budget is not None
        and task.tokens_used + external_charge > task.max_tokens_budget
    ):
        raise svc.TeamError("shared recorded allowance exhausted including external review charges")
    if any(c.state in ACTIVE_CYCLE_STATES for c in cycles):
        raise svc.TeamError("conflicting active work")
    attempts = list(await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id)))
    if any(a.status in ("queued", "running") for a in attempts):
        raise svc.TeamError("conflicting outstanding attempts")
    latest = max(cycles, key=lambda c: c.created_at)
    if latest.id != source.id and not (
        latest.state == "cancelled"
        and svc._retained(latest).get("publication_source_id") == str(source.id)
    ):
        raise svc.TeamError("latest retained publication source required")
    if any(
        c.published_sha and svc._retained(c).get("publication_source_id") == str(source.id)
        for c in cycles
    ):
        raise svc.TeamError("retained source publication already completed")
    old_inputs = {key: getattr(task, key) for key in svc.TASK_INPUT_FIELDS}
    old_inputs.update(
        max_tokens_budget=payload.original_token_cap,
        max_seconds_budget=payload.original_seconds_budget,
    )
    original_fingerprint = hashlib.sha256(
        json.dumps(old_inputs, sort_keys=True).encode()
    ).hexdigest()
    if (
        source.state not in ("cancelled", "blocked")
        or source.version != payload.version
        or source.published_sha
        or svc._retained(source).get("publication_source_id")
        or source.reviewed_sha != payload.candidate_sha
        or source.integration_sha != payload.candidate_sha
        or payload.retained_integration_sha != payload.candidate_sha
        or source.task_fingerprint != original_fingerprint
        or source.round != 1
    ):
        raise svc.TeamError("mismatched retained PASS source or original task inputs")
    provenance = await history(session, source)
    qas = [
        a
        for a in provenance
        if a.cycle_id == source.id and a.role_kind == "qa" and a.round == source.round
    ]
    qa = max(qas, key=lambda a: a.attempt_no) if qas else None
    authors = [a for a in provenance if contributor(source, a) and a.status == "succeeded"]
    if (
        qa is None
        or qa.id != payload.passed_attempt_id
        or qa.status != "succeeded"
        or qa.verdict != "pass"
        or qa.head_sha != payload.candidate_sha
        or not is_reviewer_engine(qa.provider, qa.model)
        or not svc.qa_is_independent(qa, provenance, source)
        or not (qa.artifact or "").strip()
        or redact_evidence(qa.artifact or "") != qa.artifact
        or json.loads(qa.assignment or "{}").get("artifact_complete") is not True
        or not authors
        or any(
            json.loads(a.assignment or "{}").get("artifact_complete") is not True
            or not a.head_sha
            or not (a.artifact or "").strip()
            for a in authors
        )
    ):
        raise svc.TeamError("actual complete qualified independent exact-source PASS required")
    svc.qa_evidence_files(source, provenance, qa)
    plan = svc.parse_plan(source.plan or "{}")
    lineage = {
        **svc._retained(source),
        "publication_source_id": str(source.id),
        "publication_source_sha256": _digest(source),
        "publication_attempt_sha256": {str(a.id): _digest(a) for a in provenance},
        "publication_author_ids": [str(a.id) for a in authors],
        "publication_qa_id": str(qa.id),
        "external_input_output_charge": external_charge,
    }
    cycle = TeamCycle(
        board_id=board.id,
        task_id=task.id,
        state="qa_passed",
        max_rounds=1,
        task_fingerprint=svc.task_fingerprint(task),
        base_sha=source.base_sha,
        integration_sha=source.integration_sha,
        reviewed_sha=source.reviewed_sha,
        plan=json.dumps({**plan, "retained_qa": lineage}),
        deadline_at=utcnow() + timedelta(seconds=1200),
    )
    session.add(cycle)
    await session.flush()
    svc._say(
        session,
        cycle,
        f"{svc.COORDINATOR}: owner admitted publication-only authorization "
        f"for exact {source.reviewed_sha}, immutable source {source.id}/QA {qa.id}; "
        "no new attempts, historical renewal, usage event or acceptance.",
    )
    return cycle


async def admit(
    session: AsyncSession,
    board: Board,
    task: Task,
    source: TeamCycle,
    payload: TeamRetainedQA,
    *,
    ranking: dict[str, Any],
) -> TeamCycle:
    await session.flush()
    await session.refresh(task, with_for_update=True)
    await session.refresh(source, with_for_update=True)
    stamp = payload.expected_updated_at
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    if (
        task.board_id != board.id
        or source.board_id != board.id
        or source.task_id != task.id
        or task.updated_at.replace(tzinfo=None) != stamp
        or svc.task_fingerprint(task) != payload.expected_fingerprint
    ):
        raise svc.TeamError("stale retained-QA task inputs")
    if payload.passed_attempt_id is not None:
        return await _publication(session, board, task, source, payload)
    if (
        task.status not in ("paused", "blocked")
        or task.execution_mode != "team"
        or task.execution_lane not in (None, "harness")
        or task.split_count
        or task.budget_exhausted_at is not None
        or task.max_seconds_budget != 1200
        or task.max_tokens_budget is None
        or task.tokens_used >= task.max_tokens_budget
    ):
        raise svc.TeamError("retained-QA task is not eligible within recorded allowance")
    problem = await svc._parent_problem(session, task)
    if problem:
        raise svc.TeamError(problem)
    cycles = list(await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))
    if any(c.state in ACTIVE_CYCLE_STATES for c in cycles):
        raise svc.TeamError("conflicting active work")
    if payload.failed_attempt_id is None:
        if payload.authorized_token_cap is not None or payload.external_input_output_charge:
            raise svc.TeamError("correction accounting requires its actual failed attempt")
        if any(svc._retained(c) for c in cycles):
            raise svc.TeamError("retained-QA grant already spent; no renewal")
    elif any(svc._retained(c).get("correction_source_id") for c in cycles):
        raise svc.TeamError("retained correction grant already spent; no renewal")
    attempts = list(await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id)))
    if any(a.status in ("queued", "running") for a in attempts):
        raise svc.TeamError("conflicting outstanding attempts")
    old_inputs = {key: getattr(task, key) for key in svc.TASK_INPUT_FIELDS}
    old_inputs["max_tokens_budget"] = payload.original_token_cap
    original_fingerprint = hashlib.sha256(
        json.dumps(old_inputs, sort_keys=True).encode()
    ).hexdigest()
    if (
        source.state != ("cancelled" if payload.failed_attempt_id else "blocked")
        or source.version != payload.version
        or source.reviewed_sha
        or source.published_sha
        or (
            payload.failed_attempt_id is None
            and source.integration_sha != payload.retained_integration_sha
        )
        or (
            payload.failed_attempt_id is not None
            and svc._retained(source).get("candidate_sha") != payload.retained_integration_sha
        )
        or source.task_fingerprint != original_fingerprint
    ):
        raise svc.TeamError("mismatched retained source or original inputs")
    if payload.failed_attempt_id is not None:
        return await _correction(session, board, task, source, payload, cycles, ranking)
    plan = svc.parse_plan(source.plan or "{}")
    if len(plan["workers"]) != 1 or plan["workers"][0]["role"] != "developer":
        raise svc.TeamError("retained-QA requires one completed implementation leaf")
    if plan["workers"][0]["mode"] != "implement":
        raise svc.TeamError("retained-QA must not fabricate a verify leaf")
    source_history = await svc._attempts(session, source.id)
    authors = [
        a
        for a in source_history
        if a.round == source.round and a.role_kind == "developer" and a.status == "succeeded"
    ]
    if (
        len(authors) != 1
        or not authors[0].head_sha
        or not authors[0].artifact
        or not authors[0].changed_files
        or json.loads(authors[0].assignment or "{}").get("artifact_complete") is not True
    ):
        raise svc.TeamError("complete retained author evidence required")
    _, problem = svc.worker_evidence(source, source_history)
    if problem:
        raise svc.TeamError(problem)
    if any(
        a.role_kind == "qa" and (a.verdict is not None or a.status == "succeeded") for a in attempts
    ):
        raise svc.TeamError("prior substantive QA requires cause-specific correction")
    lineage = {
        "source_cycle_id": str(source.id),
        "source_version": source.version,
        "integration_sha": source.integration_sha,
        "candidate_sha": payload.candidate_sha,
        "author_ids": [str(a.id) for a in authors],
    }
    # Candidate is explicit owner input, not a backend Git attestation. The existing
    # host exact-source/evidence gate and independent QA must verify its actual tree.
    cycle = TeamCycle(
        board_id=board.id,
        task_id=task.id,
        state="qa",
        max_rounds=2,
        task_fingerprint=svc.task_fingerprint(task),
        base_sha=source.base_sha,
        integration_sha=payload.candidate_sha,
        plan=json.dumps({**plan, "retained_qa": lineage}),
        deadline_at=utcnow() + timedelta(seconds=1200),
    )
    session.add(cycle)
    await session.flush()
    member = await svc._qa_member(session, cycle, authors, ranking)
    qa = await svc._new_attempt(
        session, cycle, member, "qa", ranking=ranking, assignment={"retained_qa": lineage}
    )
    if not svc.qa_is_independent(qa, authors, cycle):
        raise svc.TeamError("qualified independent retained-source QA required")
    svc.qa_evidence_files(cycle, authors, qa)
    svc._say(
        session,
        cycle,
        f"{svc.COORDINATOR}: owner retained-source QA-only admission on "
        f"{payload.candidate_sha}; source cycle {source.id} unchanged. ONE1200second window; "
        "one QA, at most one necessary code correction and re-review; no PM replay, "
        "historical deadline extension, usage reset or acceptance.",
    )
    return cycle


async def _correction(
    session: AsyncSession,
    board: Board,
    task: Task,
    source: TeamCycle,
    payload: TeamRetainedQA,
    cycles: list[TeamCycle],
    ranking: dict[str, Any],
) -> TeamCycle:
    """One NEW child grant for a cancelled first-QA CODE failure; never revive its parent."""
    lineage = svc._retained(source)
    cap = payload.authorized_token_cap
    if (
        cap is None
        or "external_input_output_charge" not in payload.model_fields_set
        or not lineage
        or lineage.get("correction_source_id")
        or max(cycles, key=lambda c: c.created_at).id != source.id
        or source.round != 2
        or source.max_rounds != 2
        or source.integration_sha not in (None, payload.candidate_sha)
        or payload.retained_integration_sha != payload.candidate_sha
        or lineage.get("candidate_sha") != payload.candidate_sha
        or cap != task.max_tokens_budget
    ):
        raise svc.TeamError(
            "cancelled retained first-QA source and current authorized cap required"
        )
    if task.tokens_used + payload.external_input_output_charge >= cap:
        raise svc.TeamError("shared recorded allowance exhausted including external review charges")
    own = await svc._attempts(session, source.id)
    qas = [a for a in own if a.role_kind == "qa"]
    failure = qas[0] if len(qas) == 1 else None
    provenance = await history(session, source)
    if (
        failure is None
        or failure.id != payload.failed_attempt_id
        or failure.round != 1
        or failure.status != "succeeded"
        or failure.verdict != "fail"
        or not is_reviewer_engine(failure.provider, failure.model)
        or failure.head_sha != payload.candidate_sha
        or json.loads(failure.assignment or "{}").get("artifact_complete") is not True
        or [
            k.upper()
            for k in re.findall(
                r"^CORRECTION_KIND:\s*(CODE|EVIDENCE)\s*$", failure.artifact or "", re.M | re.I
            )
        ]
        != ["CODE"]
        or redact_evidence(failure.artifact or "") != failure.artifact
        or not svc.qa_is_independent(failure, provenance, source, review_round=1)
        or any(
            a is not failure
            and (
                a.role_kind != "developer"
                or a.status != "cancelled"
                or a.started_at is not None
                or a.head_sha is not None
            )
            for a in own
        )
    ):
        raise svc.TeamError(
            "actual complete independent first-QA CODE FAIL and unstarted correction required"
        )
    plan = svc.parse_plan(source.plan or "{}")
    if (
        len(plan["workers"]) != 1
        or plan["workers"][0]["role"] != "developer"
        or plan["workers"][0]["mode"] != "implement"
    ):
        raise svc.TeamError("one retained original implementation leaf required")
    try:
        async with session.begin_nested():
            for author in provenance:
                if contributor(source, author) and author.status == "succeeded":
                    await svc._check_member(session, source, author)
    except svc.TeamFenced as exc:
        # Native authority refusal must not cancel or mutate this historical parent.
        raise svc.TeamError(exc.detail) from exc
    child_lineage = {
        **lineage,
        "correction_source_id": str(source.id),
        "correction_source_version": source.version,
        "correction_source_integration_sha": source.integration_sha,
        "failed_attempt_id": str(failure.id),
        "external_input_output_charge": payload.external_input_output_charge,
    }
    cycle = TeamCycle(
        board_id=board.id,
        task_id=task.id,
        state="developing",
        max_rounds=1,
        task_fingerprint=svc.task_fingerprint(task),
        base_sha=source.base_sha,
        plan=json.dumps({**plan, "retained_qa": child_lineage}),
        qa_feedback=failure.artifact,
        deadline_at=utcnow() + timedelta(seconds=1200),
    )
    session.add(cycle)
    await session.flush()
    await svc._queue_devs(session, cycle, board, ranking, base_sha=payload.candidate_sha)
    svc._say(
        session,
        cycle,
        f"{svc.COORDINATOR}: owner admitted NEW retained CODE correction "
        f"from cancelled {source.id}, actual QA FAIL {failure.id}; ONE correction/re-review, "
        "1200 seconds from admission. Historical rows/usage untouched; no PM replay or renewal.",
    )
    return cycle
