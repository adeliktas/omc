"""Owner review queue — agents post suggestions, owner approves/rejects/revisions/defers/escalates.

T56 (2026-05-15): backed by Postgres `owner_reviews` table via
`app.services.owner_reviews` (T18 migration). Earlier file-backed storage
(`/app/review-queue/queue.json`) was the wrong source of truth — watchdog
escalations went to Postgres while the UI read from a non-existent file
and rendered an empty review tab.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import or_, text
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import col, select

from app.api.deps import (
    ORG_MEMBER_DEP,
    ActorContext,
    OrganizationContext,
    require_org_admin,
    require_user_or_agent,
)
from app.core.auth import SESSION_DEP
from app.models.approval_task_links import ApprovalTaskLink
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.owner_reviews import OwnerReview
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from app.models.users import User
from app.services.owner_reviews import (
    AUTOMATION_POLICY,
    MAX_AUTO_CORRECTIONS,
    automation_enabled,
    automation_models_status,
    is_reviewer_engine,
    managed_boards,
    unstarted_recovery_enabled,
    set_automation_enabled,
)

router = APIRouter(prefix="/review", tags=["review"])
_review_lock = asyncio.Lock()

ReviewStatus = Literal["pending", "approved", "rejected", "revision_requested", "resolved"]
class CreateReviewItem(BaseModel):
    """Payload for creating a new Owner Review item."""
    title: str
    type: str
    summary: str
    reason: str
    proposed_action: str
    submitted_by: str
    # A task-linked item supersedes older pending items for that task; /review then
    # offers task actions (retry, cancel) instead of generic approve/reject.
    task_id: _uuid.UUID | None = None

class ProposedOption(BaseModel):
    """T105 (2026-05-25): one selectable action parsed from
    `proposed_action` text. Surfaces in /review as a button so the owner
    can act on the submitter's literal suggestions instead of only the
    generic approve/reject controls."""
    label: str
    action: str  # one of: approve | reject | request_revision | escalate_to_lead | defer
    note: str | None = None  # populated as reviewer_note on submit


class ReviewItem(CreateReviewItem):
    """Full review item returned by API."""
    id: str = ''
    submitted_at: str = ''
    status: str = 'pending'
    reviewed_at: str | None = None
    reviewer_note: str | None = None
    snooze_until: str | None = None
    escalated_to_board_id: str | None = None
    reject_options: list[str] | None = None
    proposed_options: list[ProposedOption] | None = None  # T105

class PatchReviewItem(BaseModel):
    """Payload for patching a review item."""
    action: str
    status: str | None = None
    reviewer_note: str | None = None
    snooze_hours: int | None = None
    escalate_board_id: str | None = None
    # Optional optimistic fencing (2026-10-05): when provided, the patch is
    # rejected with 409 if the item moved since the caller read it.
    expected_status: str | None = None
    expected_updated_at: datetime | None = None


_TABLE = "owner_reviews"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_item(row: Any) -> dict[str, Any]:
    m = row._mapping if hasattr(row, "_mapping") else row
    return {
        "id": _uuid_str(m["id"]),
        "title": m["title"],
        "type": m["type"],
        "summary": m.get("summary") or "",
        "reason": m.get("reason") or "",
        "proposed_action": m.get("proposed_action") or "",
        "submitted_by": m["submitted_by"],
        "submitted_at": _iso(m["submitted_at"]),
        "status": m["status"],
        "reviewed_at": _iso(m.get("reviewed_at")),
        "reviewer_note": m.get("reviewer_note"),
        "snooze_until": _iso(m.get("snooze_until")),
        "escalated_to_board_id": str(m["escalated_to_board_id"]) if m.get("escalated_to_board_id") else None,
        "task_id": _uuid_str(m.get("task_id")),
        "task_board_id": _uuid_str(m.get("task_board_id")),
        "task_status": m.get("task_status"),
        "task_resume_status": m.get("task_resume_status"),
        "task_tokens_used": m.get("task_tokens_used"),
        "updated_at": _iso(m.get("updated_at")),
        "reject_options": None,
        "proposed_options": None,  # T105 — set on read by _enrich_item
    }


# T105 (2026-05-25): parse `(a)/(b)/(c)`, `1./2./3.`, `- ` bullet lists
# from proposed_action into selectable button options. Each parsed item
# is mapped to an action verb (approve/reject/request_revision/etc.)
# based on the keywords in the line.

_OPTION_RES = [
    # "(a) reassign manually" / "(b) close as won't-fix" / "(c) accept ..."
    re.compile(r"\(([a-z]|\d+)\)\s*([^,;\.\n()]+(?:,[^()\.\n]+)?)", re.IGNORECASE),
    # "1. step" / "2. step"
    re.compile(r"(?:^|\n)\s*(\d+)\.\s+([^\n]+)", re.MULTILINE),
    # "- step"
    re.compile(r"(?:^|\n)\s*[-*]\s+([^\n]+)", re.MULTILINE),
]


def _label_to_action(label: str) -> str:
    """Heuristic — pick the best generic action for a parsed option."""
    s = label.lower()
    if any(k in s for k in (
        "accept", "approve", "resume", "continue", "proceed", "ok",
        "looks good", "merge", "ship",
    )):
        return "approve"
    if any(k in s for k in (
        "won't-fix", "wont-fix", "won't fix", "wont fix", "drop",
        "close as", "abandon", "discard", "delete", "no-op", "ignore",
        "reject", "cancel",
    )):
        return "reject"
    if any(k in s for k in (
        "reassign", "rotate", "retry", "redo", "fix", "revise", "rework",
        "more evidence", "clarif", "details", "explain",
    )):
        return "request_revision"
    if any(k in s for k in ("escalate", "lead", "expert")):
        return "escalate_to_lead"
    if any(k in s for k in ("defer", "snooze", "later", "park", "postpone")):
        return "defer"
    return "request_revision"


def _parse_proposed_options(text: str) -> list[dict[str, str]]:
    if not text or not text.strip():
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for pat in _OPTION_RES:
        for m in pat.finditer(text):
            label = (m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)).strip().rstrip(".,;: ")
            if not label or len(label) < 3:
                continue
            key = label.lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "label": label[:200],
                "action": _label_to_action(label),
                "note": label,  # the owner's choice is carried out from this text: keep it whole
            })
            if len(out) >= 6:
                return out
        if out:
            # First regex that matched anything wins — don't double-count
            return out
    return out


def _generate_reject_options(payload: CreateReviewItem) -> list[str]:
    """Generate 3 context-aware alternative rejection suggestions based on item type and proposed action."""
    ptype = payload.type
    title = payload.title.lower()
    action_text = payload.proposed_action.lower()
    submitted_by = (payload.submitted_by or "").lower()
    summary = (getattr(payload, "summary", "") or "").lower()
    reason_text = (getattr(payload, "reason", "") or "").lower()

    # T105 (2026-05-25): submitter-specific buckets. The heuristic
    # keyword match below misses watchdog stale-task + PEV escalations
    # — they had no domain keywords and fell to the irrelevant "other"
    # bucket ("Pilot with one agent", "Create RFC", etc).
    if "pev loop" in submitted_by or "pev " in submitted_by or title.startswith("pev"):
        return [
            "Reduce scope: split the step into smaller atomic substeps + re-run PEV",
            "Switch executor model: routing went wrong tier — re-run with planner-as-executor",
            "Mark task blocked: insufficient executor capability for this step; owner picks up manually",
        ]
    if "pipeline watchdog" in submitted_by or "watchdog" in submitted_by:
        if "stale" in title or "stale" in summary or "stuck" in summary:
            return [
                "Reassign to next pool agent: stale task — let routing rotate to a fresh worker",
                "Close as won't-fix: task was abandoned or superseded; remove from board",
                "Reset budget + resume: assignee still capable; give them another cycle",
            ]
        if "infra" in (ptype or "") or "infra" in title or "provider" in summary:
            return [
                "Wait for provider recovery: external outage — defer 1h, re-check",
                "Failover to backup provider: switch routing while primary recovers",
                "Acknowledge known-broken: log incident, owner monitors directly",
            ]

    patterns: list[tuple[str, re.Pattern[str], tuple[str, ...]]] = [
        (
            "security",
            re.compile(r"security|auth|token|secret|vuln|exploit|permission|access", re.I),
            (
                "Scope down: run as read-only audit with findings report only, no write access",
                "Defer to external pentest: flag for quarterly external review instead of internal fix",
                "Accept with compensating controls: approve but require anomaly monitoring + 48h incident SLA",
            ),
        ),
        (
            "infrastructure",
            re.compile(r"infra|deploy|build|docker|ci|cd|pipeline|server|host|network", re.I),
            (
                "Roll out canary: deploy to 5% of agents first, monitor 24h before full rollout",
                "Create backup/revert plan first: doc rollback steps before any config change",
                "Test in staging only: validate in sandbox env for 7 days, report results, no prod touch",
            ),
        ),
        (
            "frontend-feature",
            re.compile(r"ui|frontend|component|css|style|button|page|modal|view", re.I),
            (
                "Split into smaller PRs: break into 3 incremental merges with separate QA per chunk",
                "Add feature flag: gate behind config toggle, enable for 10% users, measure before GA",
                "A/B test first: ship variant A vs B in parallel, pick winner based on 7-day metrics",
            ),
        ),
        (
            "backend-change",
            re.compile(r"api|db|query|migration|schema|model|endpoint|route", re.I),
            (
                "Add non-blocking migration: use online schema change, zero-downtime deployment",
                "Gate behind read-only mode: expose as GET-only for 48h, validate data integrity",
                "Extract to separate service: decouple into dedicated microservice with its own deploy cycle",
            ),
        ),
        (
            "llm-engine",
            re.compile(r"llm|model|prompt|token|inference|generation|embedding", re.I),
            (
                "Shadow mode eval: log new behavior without surfacing to users, compare quality 7 days",
                "Cap token budget: approve but limit to 10% of daily tokens, review burn rate before scaling",
                "A/B test prompt variants: run 3 prompt candidates in parallel, pick best via human eval",
            ),
        ),
        (
            "other",
            re.compile(r"", re.I),
            (
                "Pilot with one agent: assign as experiment to single agent, report outcomes in 7 days",
                "Create RFC first: doc the proposal as lightweight RFC, circulate to 2 peers, iterate",
                f"Defer to next sprint: park item, escalate if blocked >14 days or cited by 2+ agents",
            ),
        ),
        (
            "capacity",
            re.compile(r"rate|limit|throttle|budget|quota|capacity|scale|burst", re.I),
            (
                "Increase with kill-switch: raise limit 2× but add auto-revert if error rate >5%",
                "Staggered rollout: bump 20% per day over 5 days, monitor saturation at each step",
                "Defer to load-test: require 1h soak test at 3× current load before any limit change",
            ),
        ),
    ]

    # Match against type, title, and action
    combined = f"{ptype} {title} {action_text}"

    for _cat, pat, opts in patterns:
        if pat.search(combined):
            return list(opts)

    # Fallback
    return [
        f"Defer: park item for 7 days, escalate if still unresolved",
        f"Counter-propose: suggest alternative approach based on existing tooling",
        f"Reject with request: need more evidence (metrics, user impact, cost estimate)",
    ]


@router.get(
    "/items",
    summary="List owner review items",
    dependencies=[Depends(require_user_or_agent)],
)
async def list_items(
    status: ReviewStatus | None = None,
    include_snoozed: bool = False,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {}
    if status:
        where.append("r.status = :status")
        params["status"] = status
    if not include_snoozed:
        where.append("(r.snooze_until IS NULL OR r.snooze_until <= NOW())")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    res = await session.execute(
        text(
            f"SELECT r.*, t.board_id AS task_board_id, t.status AS task_status, "
            f"COALESCE(t.paused_from_status, 'inbox') AS task_resume_status, "
            f"t.tokens_used AS task_tokens_used "
            f"FROM {_TABLE} r LEFT JOIN tasks t ON t.id = r.task_id "
            f"{where_sql} ORDER BY r.submitted_at DESC"
        ),
        params,
    )
    items = []
    for row in res.fetchall():
        item = _row_to_item(row)
        record = await session.get(OwnerReview, _uuid.UUID(item["id"]))
        try:
            await _require_item_org(session, record, ctx)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        items.append(item)
    # Recompute reject_options + proposed_options on read — not persisted,
    # depends on title/type/proposed_action which are immutable post-insert.
    for it in items:
        payload = CreateReviewItem(
            title=it["title"], type=it["type"], summary=it["summary"],
            reason=it["reason"], proposed_action=it["proposed_action"],
            submitted_by=it["submitted_by"],
        )
        it["reject_options"] = _generate_reject_options(payload)
        it["proposed_options"] = _parse_proposed_options(it["proposed_action"])
    return items


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    summary="Submit item for owner review",
    dependencies=[Depends(require_user_or_agent)],
)
async def create_item(
    payload: CreateReviewItem,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    await _require_item_org(session, OwnerReview(), ctx)
    new_id = str(_uuid.uuid4())
    now = datetime.now(timezone.utc)
    task_id = _tid(payload.task_id) if payload.task_id else None
    if task_id:
        # One open decision per task (2026-10-25: one paused task carried three items).
        await session.execute(
            text(
                f"UPDATE {_TABLE} SET status = 'resolved', reviewed_at = :ts, updated_at = :ts, "
                "reviewer_note = 'Superseded by a newer item for the same task.' "
                "WHERE task_id = :tid AND status = 'pending'"
            ),
            {"tid": task_id, "ts": now},
        )
    await session.execute(
        text(
            f"INSERT INTO {_TABLE} "
            "(id, title, type, summary, reason, proposed_action, submitted_by, "
            " submitted_at, status, created_at, updated_at, task_id) "
            "VALUES (:id, :title, :type, :summary, :reason, :action, :by, "
            " :ts, 'pending', :ts, :ts, :tid)"
        ),
        {
            "id": _tid(new_id), "title": payload.title, "type": payload.type,
            "summary": payload.summary, "reason": payload.reason,
            "action": payload.proposed_action, "by": payload.submitted_by,
            "ts": now, "tid": task_id,
        },
    )
    await session.commit()
    res = await session.execute(
        text(f"SELECT * FROM {_TABLE} WHERE id = :id"), {"id": _tid(new_id)},
    )
    item = _row_to_item(res.first())
    item["reject_options"] = _generate_reject_options(payload)
    item["proposed_options"] = _parse_proposed_options(item["proposed_action"])
    return item


async def _fetch_item(session: AsyncSession, item_id: str) -> dict[str, Any] | None:
    try:
        _uuid.UUID(item_id)
    except (ValueError, TypeError):
        return None
    res = await session.execute(
        text(f"SELECT * FROM {_TABLE} WHERE id = :id"), {"id": _tid(item_id)},
    )
    row = res.first()
    return _row_to_item(row) if row else None


# ── Owner-authorized autonomous acceptance review (2026-10-05) ─────────────
#
# The owner delegates ROUTINE acceptance (Review → Done) and routine/confirmed-
# obsolete owner-queue resolution to an independent read-only reviewer model.
# The reviewer model never gets the MC admin token and never executes code,
# actions or SQL: a privileged coordinator (scripts/mc-review-agent.py) runs it
# in an isolated read-only sandbox and applies ONLY the validated structured
# JSON verdict through POST /review/decisions, which re-checks every gate
# server-side. Major decisions always stay human-visible at /review.

# Marker for delegated-decision audit notes on tasks.
_AUTO_REVIEW_TAG = "[auto-review]"
# Submitter identity for delegated escalations in the owner queue.
_ESCALATION_SUBMITTER = "Autonomous Review (delegated)"


class AutomationPutBody(BaseModel):
    enabled: StrictBool
    managed_board_ids: list[_uuid.UUID] | None = Field(default=None, max_length=50)
    recover_unstarted: StrictBool | None = None


class ReviewDecision(BaseModel):
    """One structured delegated-review outcome.

    Applied atomically under row locks; `fingerprint` and `expected_status`
    bind the decision to the exact candidate snapshot the reviewer saw.
    """

    kind: Literal["task", "item"]
    id: str
    fingerprint: str
    expected_status: str
    action: Literal["accept", "revise", "escalate", "resolve"]
    impact: Literal["routine", "major"]
    reviewer_provider: str
    reviewer_model: str
    reviewer_session_id: _uuid.UUID | None = None
    review_lease_id: _uuid.UUID | None = None
    evidence: str
    feedback: str | None = None
    correction_kind: Literal["evidence", "code"] = "evidence"
    published_sha: str | None = None


def _fingerprint(payload: dict[str, Any]) -> str:
    """Stable snapshot fingerprint (sha256 over canonical JSON)."""
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _redact(text_value: str | None, limit: int = 4000) -> str:
    """Redact secrets + bound length for evidence shipped to a model."""
    from app.services.team_cycle import redact

    return redact(text_value, limit)


def _tid(task_id: Any) -> str:
    """Canonical hex task id for raw-SQL binds (Postgres uuid casts accept
    hex; SQLite CHAR(32) columns store exactly that)."""
    try:
        return _uuid.UUID(str(task_id)).hex
    except (ValueError, TypeError, AttributeError):
        return str(task_id)


def _uuid_str(value: Any) -> str | None:
    """Canonical dashed UUID string for API output."""
    if value is None:
        return None
    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _board_rules(board: Board) -> dict[str, Any]:
    return {
        "require_approval_for_done": bool(board.require_approval_for_done),
        "require_review_before_done": bool(board.require_review_before_done),
        "comment_required_for_review": bool(board.comment_required_for_review),
        "block_status_changes_with_pending_approval": bool(
            board.block_status_changes_with_pending_approval
        ),
        "only_lead_can_change_status": bool(board.only_lead_can_change_status),
    }


async def _latest_cycle(session: AsyncSession, task_id: Any) -> TeamCycle | None:
    return (
        await session.exec(
            select(TeamCycle)
            .where(col(TeamCycle.task_id) == task_id)
            .order_by(col(TeamCycle.created_at).desc())
            .limit(1)
        )
    ).first()


async def _cycle_attempts(session: AsyncSession, cycle_id: Any) -> list[TeamAttempt]:
    return list(
        (
            await session.exec(
                select(TeamAttempt)
                .where(col(TeamAttempt.cycle_id) == cycle_id)
                .order_by(col(TeamAttempt.created_at).asc())
            )
        ).all()
    )


async def _latest_qa_verdict_comment(
    session: AsyncSession, task_id: Any
) -> str | None:
    res = await session.execute(
        text(
            "SELECT message FROM activity_events "
            "WHERE task_id = :tid AND event_type = 'task.comment' "
            "AND (LOWER(message) LIKE '%qa verdict: pass%' "
            "     OR LOWER(message) LIKE '%qa-verdict: pass%') "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"tid": _tid(task_id)},
    )
    row = res.first()
    return (row[0] or "")[:200] if row else None


async def _task_dependencies_snapshot(
    session: AsyncSession, board_id: Any, task_id: Any
) -> list[dict[str, Any]]:
    from app.models.task_dependencies import TaskDependency

    rows = (
        await session.exec(
            select(TaskDependency, Task)
            .join(Task, col(Task.id) == col(TaskDependency.depends_on_task_id))
            .where(
                col(TaskDependency.board_id) == board_id,
                col(TaskDependency.task_id) == task_id,
            )
        )
    ).all()
    return [
        {
            "task_id": str(dep.depends_on_task_id),
            "status": task.status,
            "title": task.title,
        }
        for dep, task in rows
    ]


async def _linked_owner_approvals(
    session: AsyncSession, task_id: Any
) -> list[dict[str, Any]]:
    """Approved decisions already linked to this task (owner or delegated).

    Gives the reviewer the task's already-owner-approved major-decision
    evidence without inventing it; payloads are redacted before shipping.
    """
    linked = select(col(ApprovalTaskLink.approval_id)).where(
        col(ApprovalTaskLink.task_id) == task_id,
    )
    rows = (
        await session.exec(
            select(Approval).where(
                col(Approval.status) == "approved",
                or_(
                    col(Approval.task_id) == task_id,
                    col(Approval.id).in_(linked),
                ),
            )
        )
    ).all()
    out: list[dict[str, Any]] = []
    for a in rows:
        payload = a.payload if isinstance(a.payload, dict) else {}
        out.append(
            {
                "action_type": a.action_type,
                "created_at": _iso(a.created_at),
                "resolved_at": _iso(a.resolved_at),
                "source": payload.get("source"),
                "snapshot_fingerprint": payload.get("snapshot_fingerprint"),
                "evidence": _redact(str(payload.get("evidence") or ""), 1000),
            }
        )
    return out


async def _task_comments_redacted(
    session: AsyncSession, task_id: Any, limit: int = 20
) -> list[str]:
    """Redacted user-facing comments only — never private transcripts,
    auth stores or claim tokens/hashes."""
    res = await session.execute(
        text(
            "SELECT message FROM activity_events "
            "WHERE task_id = :tid AND event_type = 'task.comment' "
            "ORDER BY created_at DESC LIMIT :lim"
        ),
        {"tid": _tid(task_id), "lim": limit},
    )
    return [_redact((r[0] or ""), 800) for r in res.fetchall()]


async def _task_snapshot(
    session: AsyncSession, task: Task, board: Board
) -> dict[str, Any]:
    """Everything the task fingerprint binds + the reviewer evidence bundle."""
    cycle = await _latest_cycle(session, task.id)
    from app.services.team_cycle import _retained, _review_history

    attempts = await _cycle_attempts(session, cycle.id) if cycle else []
    if cycle and _retained(cycle).get("publication_source_id"):
        attempts = await _review_history(session, cycle)
    qa = [a for a in attempts if a.role_kind == "qa"]
    deps = await _task_dependencies_snapshot(session, task.board_id, task.id)
    qa_verdict_comment = await _latest_qa_verdict_comment(session, task.id)
    cycle_info: dict[str, Any] | None = None
    if cycle is not None:
        cycle_info = {
            "state": cycle.state,
            "round": cycle.round,
            "base_sha": cycle.base_sha,
            "integration_sha": cycle.integration_sha,
            "reviewed_sha": cycle.reviewed_sha,
            "published_sha": cycle.published_sha,
            "block_reason": cycle.block_reason,
            "plan": _redact(cycle.plan, 16000),
            "qa_feedback": _redact(cycle.qa_feedback, 1000),
        }
    from app.core.evidence_redaction import redact_evidence
    from app.services.team_cycle import MAX_QA_ARTIFACT_BYTES, TeamError, qa_evidence_files

    files, files_problem = {}, None
    current_qa = [a for a in qa if a.round == cycle.round] if cycle else []
    if current_qa:
        try:
            files = qa_evidence_files(cycle, attempts, max(current_qa, key=lambda a: a.attempt_no))
        except (TeamError, ValueError, TypeError) as exc:
            files_problem = str(exc)  # unavailable whole proof, never a partial/reconstructed packet

    # Summaries below are previews, not complete proof. Hand off whole safe reports
    # separately, bounded in aggregate; never silently clip required evidence.
    reports, unavailable = [], []
    budget = MAX_QA_ARTIFACT_BYTES * 2
    for a in reversed(attempts):
        if not (a.artifact or "").strip():
            continue
        data = (a.artifact or "").encode("utf-8")
        if len(data) > budget or redact_evidence(a.artifact) != a.artifact:
            unavailable.append(str(a.id))
            continue
        try:
            certified = json.loads(a.assignment or "{}").get("artifact_complete") is True
        except (ValueError, TypeError, AttributeError):
            certified = False
        reports.append({"attempt_id": str(a.id), "artifact": a.artifact,
                        "artifact_complete": certified})
        budget -= len(data)
    from app.services.team_cycle import engine_identity
    workers = [
        {
            "id": str(a.id),
            "member_id": str(a.member_id),
            "stage_key": a.stage_key,
            "role_kind": a.role_kind,
            "provider": a.provider,
            "model": a.model,
            "engine_identity": engine_identity(a.provider, a.model),
            "started_at": _iso(a.started_at),
            "finished_at": _iso(a.finished_at),
            "assignment": _redact(a.assignment, 4000),
            "artifact": _redact(a.artifact, 4000),
            "status": a.status,
            "head_sha": a.head_sha,
            "changed_files": _parse_changed_files(a.changed_files),
            "verdict": a.verdict,
        }
        for a in attempts
        if a.role_kind in ("pm", "developer", "specialist", "qa")
    ]
    return {
        "kind": "task",
        "id": str(task.id),
        "board_id": str(task.board_id),
        "title": task.title,
        "description": _redact(task.description, 64 * 1024),
        "goal_complete": _redact(task.description, 64 * 1024) == (task.description or ""),
        "status": task.status,
        "complexity": task.complexity,
        "execution_mode": task.execution_mode,
        "execution_lane": task.execution_lane,
        "split_count": task.split_count,
        "updated_at": _iso(task.updated_at),
        "board": {"id": str(board.id), "name": board.name, "rules": _board_rules(board)},
        "cycle": cycle_info,
        "qa_attempts": [
            {"verdict": a.verdict, "head_sha": a.head_sha, "status": a.status} for a in qa
        ],
        "qa_verdict_comment": qa_verdict_comment,
        "workers": workers,
        "stored_reports": reports,
        "unavailable_reports": unavailable,
        "evidence_files": files,
        "evidence_files_problem": files_problem,
        "comments": await _task_comments_redacted(session, task.id),
        "human_evidence": await _human_evidence_fingerprint(session, task),
        "goal_fingerprint": _goal_fingerprint(task),
        "dependencies": deps,
        "owner_approvals": await _linked_owner_approvals(session, task.id),
    }


def _parse_changed_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


_CORRECTION_MARKER = "\n\n[auto-review corrections — original scope only]\n"


def _goal_fingerprint(task: Task) -> str:
    return _fingerprint({"title": task.title, "description": task.description or ""})


async def _human_evidence_fingerprint(
    session: AsyncSession, task: Task, exclude_approval: Any = None
) -> str:
    # Hash complete evidence, not truncated/redacted text or spoofable audit tags.
    # Applied fingerprints normalize our changes without ignoring human comments.
    from app.models.activity_events import ActivityEvent

    comments = (
        await session.exec(
            select(ActivityEvent)
            .where(
                col(ActivityEvent.task_id) == task.id,
                col(ActivityEvent.event_type) == "task.comment",
            )
            .order_by(col(ActivityEvent.created_at), col(ActivityEvent.id))
        )
    ).all()
    linked = select(col(ApprovalTaskLink.approval_id)).where(
        col(ApprovalTaskLink.task_id) == task.id
    )
    approvals = (
        await session.exec(
            select(Approval)
            .where(or_(col(Approval.task_id) == task.id, col(Approval.id).in_(linked)))
            .order_by(col(Approval.id))
        )
    ).all()
    return _fingerprint(
        {
            "comments": [(str(c.id), c.message) for c in comments],
            "approvals": [
                (str(a.id), a.status, a.payload, _iso(a.resolved_at))
                for a in approvals
                if a.id != exclude_approval
            ],
            "goal": _goal_fingerprint(task),
        }
    )


def _task_fingerprint(snapshot: dict[str, Any]) -> str:
    """Bind status/update/description, published/integration/review SHA,
    durable QA results, dependencies and board completion rules."""
    payload = {
        "kind": "task",
        "id": snapshot["id"],
        "status": snapshot["status"],
        "updated_at": snapshot["updated_at"],
        "description": snapshot["description"],
        "cycle": (
            {
                "state": snapshot["cycle"]["state"],
                "published_sha": snapshot["cycle"]["published_sha"],
                "integration_sha": snapshot["cycle"]["integration_sha"],
                "reviewed_sha": snapshot["cycle"]["reviewed_sha"],
            }
            if snapshot["cycle"]
            else None
        ),
        "qa_attempts": snapshot["qa_attempts"],
        "qa_verdict_comment": snapshot["qa_verdict_comment"],
        "dependencies": snapshot["dependencies"],
        "board_rules": snapshot["board"]["rules"],
        "human_evidence": snapshot["human_evidence"],
    }
    if snapshot.get("stored_reports") or snapshot.get("unavailable_reports"):
        payload["report_evidence"] = _fingerprint({
            "reports": snapshot.get("stored_reports"),
            "unavailable": snapshot.get("unavailable_reports"),
        })
    return _fingerprint(payload)


async def _item_snapshot(
    session: AsyncSession, item: OwnerReview
) -> dict[str, Any]:
    """Owner-queue item snapshot: actual fields/status/update plus linked task state."""
    linked_task: dict[str, Any] | None = None
    if item.task_id is not None:
        t = await session.get(Task, item.task_id)
        if t is not None:
            linked_task = {
                "id": str(t.id),
                "board_id": str(t.board_id) if t.board_id else None,
                "title": t.title,
                "status": t.status,
                "updated_at": _iso(t.updated_at),
            }
    return {
        "kind": "item",
        "id": str(item.id),
        "title": item.title,
        "type": item.type,
        "summary": _redact(item.summary, 2000),
        "reason": _redact(item.reason, 1000),
        "proposed_action": _redact(item.proposed_action, 1000),
        "submitted_by": item.submitted_by,
        "submitted_at": _iso(item.submitted_at),
        "status": item.status,
        "updated_at": _iso(item.updated_at),
        "linked_task": linked_task,
    }


def _item_fingerprint(snapshot: dict[str, Any]) -> str:
    return _fingerprint(
        {
            "kind": "item",
            "id": snapshot["id"],
            "status": snapshot["status"],
            "updated_at": snapshot["updated_at"],
            "title": snapshot["title"],
            "summary": snapshot["summary"],
            "reason": snapshot["reason"],
            "proposed_action": snapshot["proposed_action"],
            "submitted_by": snapshot["submitted_by"],
            "submitted_at": snapshot["submitted_at"],
            "linked_task": snapshot["linked_task"],
        }
    )


@router.get(
    "/automation",
    summary="Autonomous acceptance review: enabled state, ordered reviewer models, policy",
)
async def get_automation(
    _ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> dict[str, Any]:
    """Public UI contract: {enabled, models:[{provider, model, eligible,
    block_reason}|...], policy}. Default disabled; models are the owner-ordered
    reviewer trio in strict priority order."""
    return {
        "enabled": automation_enabled(_ctx.organization.id),
        "managed_board_ids": managed_boards(_ctx.organization.id),
        "recover_unstarted": unstarted_recovery_enabled(_ctx.organization.id),
        "models": automation_models_status(),
        "policy": AUTOMATION_POLICY,
    }


@router.put(
    "/automation",
    summary="Enable/disable autonomous acceptance review (org admin only)",
)
async def put_automation(
    payload: AutomationPutBody,
    _ctx: OrganizationContext = Depends(require_org_admin),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    """Explicit operator control. Persisted atomically in the mc_sidecar
    model-meta namespace (no `level` key, so it never appears on the ranking
    board). Enabling never bypasses manual provider disables or holds."""
    if payload.managed_board_ids is not None:
        for board_id in payload.managed_board_ids:
            board = await session.get(Board, board_id)
            if board is None or board.organization_id != _ctx.organization.id:
                raise HTTPException(404, "Managed board not found in this organization")
    set_automation_enabled(payload.enabled, _ctx.organization.id,
        managed_board_ids=(list(dict.fromkeys(map(str, payload.managed_board_ids)))
                           if payload.managed_board_ids is not None else None),
        recover_unstarted=payload.recover_unstarted)
    return {
        "enabled": automation_enabled(_ctx.organization.id),
        "managed_board_ids": managed_boards(_ctx.organization.id),
        "recover_unstarted": unstarted_recovery_enabled(_ctx.organization.id),
        "models": automation_models_status(),
        "policy": AUTOMATION_POLICY,
    }


async def _candidate_tasks(
    session: AsyncSession, organization_id: Any
) -> list[Task]:
    """ALL review-status tasks of the org's boards, paged to the end —
    never silently stopped after the first page."""
    tasks: list[Task] = []
    page = 0
    while page < 25:  # hard cap: 25 × 200 = 5000 candidates
        rows = (
            await session.exec(
                select(Task)
                .join(Board, col(Board.id) == col(Task.board_id))
                .where(
                    col(Board.organization_id) == organization_id,
                    col(Task.status) == "review",
                )
                .order_by(col(Task.updated_at).asc())
                .offset(page * 200)
                .limit(200)
            )
        ).all()
        tasks.extend(rows)
        if len(rows) < 200:
            break
        page += 1
    return tasks


async def _candidate_items(
    session: AsyncSession, organization_id: Any
) -> list[OwnerReview]:
    """Relevant owner-queue rows: watchdog-originated pending or
    revision_requested items that are org-scoped (linked task in this org,
    or linked task missing entirely — obsolete escalation), plus board-less
    watchdog infra rows."""
    rows = (
        await session.exec(
            select(OwnerReview)
            .where(
                col(OwnerReview.status).in_(("pending", "revision_requested")),
                or_(
                    col(OwnerReview.submitted_by).like("Pipeline Watchdog%"),
                    col(OwnerReview.submitted_by) == "PEV Loop",
                ),
            )
            .order_by(col(OwnerReview.submitted_at).desc())
        )
    ).all()
    out: list[OwnerReview] = []
    for item in rows:
        if item.task_id is None:
            out.append(item)
            continue
        t = await session.get(Task, item.task_id)
        if t is None:
            out.append(item)  # task gone: obsolete escalation candidate
            continue
        if t.board_id is None:
            continue
        board = await session.get(Board, t.board_id)
        if board is not None and board.organization_id == organization_id:
            out.append(item)
    return out


@router.get(
    "/candidates",
    summary="Autonomous acceptance candidates (org admin only, org-scoped)",
)
async def list_candidates(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: OrganizationContext = Depends(require_org_admin),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    """Candidate work for the delegated reviewer: org board tasks in Review
    plus relevant owner-queue rows. Each candidate carries a stable snapshot
    fingerprint (binds status/update/description, published/integration/
    review SHA, durable QA results, dependencies and board completion rules —
    or, for items, the actual fields plus linked task state) and a redacted
    evidence bundle. Comments are redacted user-facing comments only."""
    org_id = ctx.organization.id
    merged: list[dict[str, Any]] = []
    for task in await _candidate_tasks(session, org_id):
        board = await session.get(Board, task.board_id)
        if board is None:
            continue
        snapshot = await _task_snapshot(session, task, board)
        merged.append(
            {
                "kind": "task",
                "id": str(task.id),
                "board_id": str(task.board_id),
                "title": task.title,
                "status": task.status,
                "fingerprint": _task_fingerprint(snapshot),
                "evidence": snapshot,
            }
        )
    for item in await _candidate_items(session, org_id):
        try:
            await _require_item_org(session, item, ctx)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        snapshot = await _item_snapshot(session, item)
        merged.append(
            {
                "kind": "item",
                "id": str(item.id),
                "board_id": None,
                "title": item.title,
                "status": item.status,
                "fingerprint": _item_fingerprint(snapshot),
                "evidence": snapshot,
            }
        )
    return {
        "items": merged[offset : offset + limit],
        "total": len(merged),
        "limit": limit,
        "offset": offset,
    }


async def _qa_pass_evidence(
    session: AsyncSession, task: Task, snapshot: dict[str, Any]
) -> bool:
    """Succeeded QA PASS on the exact published SHA of the latest cycle.
    A comment alone, a failed attempt or an unpublished cycle is not evidence.
    """
    from app.services.team_cycle import (
        _retained, _review_history, qa_is_independent, task_fingerprint,
    )

    stored = await _latest_cycle(session, task.id)
    if (stored is None or stored.board_id != task.board_id
            or stored.task_fingerprint != task_fingerprint(task)):
        return False
    from app.services.team_retained_qa import publication_basis

    history = await _review_history(session, stored)
    basis = stored
    if _retained(stored).get("publication_source_id"):
        basis, _ = await publication_basis(session, stored)
    current_qa = [a for a in history if a.role_kind == "qa" and a.round == stored.round]
    provenance = await _review_history(session, stored)
    if not current_qa or not qa_is_independent(current_qa[-1], provenance, basis):
        return False
    actual = current_qa[-1]
    try:
        complete = json.loads(actual.assignment or "{}").get("artifact_complete") is True
    except (ValueError, AttributeError, TypeError):
        complete = False
    if not complete or not (actual.artifact or "").strip():
        return False  # absent/uncertain required QA evidence is not a certificate
    if not is_reviewer_engine(actual.provider, actual.model):
        return False
    cycle = snapshot.get("cycle") or {}
    sha = cycle.get("published_sha")
    attempts = snapshot.get("qa_attempts") or []
    qa = attempts[-1] if attempts else {}
    return bool(
        stored.state == "published" and stored.published_sha == stored.reviewed_sha == stored.integration_sha == sha
        and actual.status == "succeeded" and actual.verdict == "pass" and actual.head_sha == sha
        and sha
        and re.fullmatch(r"[0-9a-f]{40}", sha)
        and cycle.get("state") == "published"
        and cycle.get("reviewed_sha") == sha
        and qa.get("verdict") == "pass"
        and qa.get("status") == "succeeded"
        and qa.get("head_sha") == sha
    )


async def _owner_approved_for_snapshot(
    session: AsyncSession, task: Task, snapshot: dict[str, Any]
) -> bool:
    """Verify human authorization of this exact decision or this settled goal.

    Delegated routine acceptance never authorizes a new major goal.
    """
    fingerprint_value = _task_fingerprint(snapshot)
    linked = select(col(ApprovalTaskLink.approval_id)).where(
        col(ApprovalTaskLink.task_id) == task.id,
    )
    rows = (
        await session.exec(
            select(Approval).where(
                col(Approval.status) == "approved",
                or_(
                    col(Approval.task_id) == task.id,
                    col(Approval.id).in_(linked),
                ),
            )
        )
    ).all()
    for approval in rows:
        payload = approval.payload if isinstance(approval.payload, dict) else {}
        if approval.board_id != task.board_id or approval.agent_id is not None:
            continue
        if payload.get("source") not in ("owner", "owner_review") or payload.get("delegated"):
            continue
        if payload.get("source") == "owner_review":
            try:
                item_id = _uuid.UUID(str(payload.get("review_item_id")))
            except ValueError:
                continue
            item = await session.get(OwnerReview, item_id)
            if item is None or item.status != "approved" or item.task_id != task.id:
                continue
        if payload.get("goal_fingerprint") == _goal_fingerprint(task):
            return True
        bound_snapshot = payload.get("snapshot_fingerprint")
        if bound_snapshot and (
            bound_snapshot == fingerprint_value
            or bound_snapshot
            == _task_fingerprint(
                {
                    **snapshot,
                    "human_evidence": await _human_evidence_fingerprint(session, task, approval.id),
                }
            )
        ):
            return True
        # Legacy explicit owner decisions without a binding are usable only
        # while the task has not changed since that human authorization.
        if (
            approval.action_type == "owner_decision"
            and payload.get("source") == "owner"
            and not payload.get("snapshot_fingerprint")
            and not payload.get("goal_fingerprint")
            and task.updated_at.replace(tzinfo=None) <= approval.created_at.replace(tzinfo=None)
        ):
            return True
    return False


async def _count_delegated_revisions(session: AsyncSession, task_id: Any) -> int:
    res = await session.execute(
        text(
            "SELECT message FROM activity_events "
            "WHERE task_id = :tid AND event_type = 'task.comment' "
            "AND message LIKE :pat"
        ),
        {"tid": _tid(task_id), "pat": f"{_AUTO_REVIEW_TAG} action=revise%"},
    )
    # Only the server-generated header classifies a revision; feedback is untrusted.
    return sum("correction_kind=evidence" not in (row[0] or "").split("\n", 1)[0].split()
               for row in res.fetchall())


class _LifecycleTransaction:
    """Reuse lifecycle helpers without their intermediate commits."""

    def __init__(self, session: AsyncSession):
        self.session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    async def commit(self) -> None:
        await self.session.flush()


async def _delegate_task_update(
    session: AsyncSession,
    *,
    task: Task,
    board: Board,
    user: User,
    new_status: str,
    comment: str,
) -> None:
    """Apply a status change through the EXISTING task update lifecycle
    helpers, so dependency/pending-approval/required-custom-field/review/
    time gates and activity tracking stay enforced. No raw UPDATE, no
    fallback bypass."""
    from app.api.tasks import (
        _apply_admin_task_rules,
        _finalize_updated_task,
        _TaskUpdateInput,
    )

    actor = ActorContext(actor_type="user", user=user)
    update = _TaskUpdateInput(
        task=task,
        actor=actor,
        board_id=board.id,
        previous_status=task.status,
        previous_assigned=task.assigned_agent_id,
        previous_in_progress_at=task.in_progress_at,
        status_requested=(new_status != task.status),
        updates={"status": new_status},
        comment=comment,
        depends_on_task_ids=None,
        tag_ids=None,
        custom_field_values={},
        custom_field_values_set=False,
    )
    transaction = cast(AsyncSession, _LifecycleTransaction(session))
    await _apply_admin_task_rules(transaction, update=update)
    if update.updates.get("status") != new_status:
        raise HTTPException(status_code=409, detail="Lifecycle blocked the requested status.")
    await _finalize_updated_task(transaction, update=update, notify=False)
    if task.status != new_status:
        raise HTTPException(status_code=409, detail="Lifecycle blocked the requested status.")
    session.info.setdefault("review_notifications", []).append(update)


async def _recover_task_evidence(
    session: AsyncSession, *, task: Task, board: Board, user: User,
    cycle: TeamCycle, version: int, ranking: dict, comment: str,
    recover_expired: bool = False, evidence_files: dict[str, str] | None = None,
) -> None:
    from app.services import team_cycle

    await team_cycle.retry_qa(session, cycle, version, ranking=ranking,
                              recover_expired=recover_expired, evidence_files=evidence_files)
    tokens_used, exhausted_at = task.tokens_used, task.budget_exhausted_at
    if task.status == "review":
        # Review → In Progress is not a legal lifecycle edge. The Inbox hop is
        # atomic with the existing QA cycle, so it cannot admit implementation.
        await _delegate_task_update(session, task=task, board=board, user=user,
                                    new_status="inbox", comment=comment)
    await _delegate_task_update(session, task=task, board=board, user=user,
                                new_status="in_progress", comment=comment)
    # Only the cycle gets a new QA window; task usage/exhaustion are historical facts.
    task.tokens_used, task.budget_exhausted_at = tokens_used, exhausted_at


async def _upsert_delegated_escalation(
    session: AsyncSession,
    *,
    task: Task,
    evidence: str,
    feedback: str | None,
    reviewer: str,
) -> OwnerReview:
    """Create (or update a deduplicated) pending owner_reviews item so a
    human decides. The proposed change is never executed here."""
    existing = (
        await session.exec(
            select(OwnerReview).where(
                col(OwnerReview.task_id) == task.id,
                col(OwnerReview.status).in_(("pending", "revision_requested")),
                col(OwnerReview.submitted_by) == _ESCALATION_SUBMITTER,
            ).with_for_update()
        )
    ).first()
    note = (
        f"Delegated reviewer {reviewer} escalated: major/unknown decision or "
        f"failed acceptance. Evidence: {evidence[:1500]}"
        + (f" Feedback: {feedback[:500]}" if feedback else "")
    )
    if existing is not None:
        held = await _fetch_item(session, str(existing.id))
        if (existing.status == "revision_requested" or existing.reviewed_at is not None
                or (held and held.get("snooze_until") is not None)):
            return existing  # An owner decision/deferral is a hold, not a fresh escalation.
        existing.summary = _redact(evidence, 4000)
        existing.reason = "Delegated reviewer requires an owner decision."
        existing.proposed_action = (
            "Owner to decide; the proposed change was NOT executed.\n"
            f"Goal fingerprint: {_goal_fingerprint(task)}"
        )
        existing.reviewer_note = note
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        await session.flush()
        return existing
    item = OwnerReview(
        title=f"ESCALATED: {task.title[:120]}",
        type="other",
        summary=_redact(evidence, 4000),
        reason="Delegated reviewer requires an owner decision.",
        proposed_action=(
            "Owner to decide; the proposed change was NOT executed.\n"
            f"Goal fingerprint: {_goal_fingerprint(task)}"
        ),
        submitted_by=_ESCALATION_SUBMITTER,
        task_id=task.id,
        status="pending",
        reviewer_note=note,
    )
    session.add(item)
    await session.flush()
    return item


def _decision_response(
    *,
    kind: str,
    candidate_id: str,
    action: str,
    task_id: str | None,
    updated_at: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "kind": kind,
        "id": candidate_id,
        "action": action,
        "task_id": task_id,
        "updated_at": updated_at,
    }
    if extra:
        out.update(extra)
    return out


@router.get("/work-recoveries", summary="List opted-in, expired and unstarted native work")
async def work_recoveries(
    ctx: OrganizationContext = Depends(require_org_admin), session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    from app.services import team_cycle as svc
    from app.models.activity_events import ActivityEvent

    boards = [_uuid.UUID(b) for b in managed_boards(ctx.organization.id)]
    if not boards or not unstarted_recovery_enabled(ctx.organization.id):
        return {"items": []}
    tasks = list(await session.exec(select(Task).join(Board, Task.board_id == Board.id).where(
        Board.organization_id == ctx.organization.id, col(Task.board_id).in_(boards),
        Task.status == "blocked", Task.execution_mode == "team", Task.split_count == 0,
        col(Task.budget_exhausted_at).is_(None))))
    items = []
    for task in tasks:
        cycle = await _latest_cycle(session, task.id)
        if (cycle is None or cycle.state != "blocked" or cycle.deadline_at.replace(tzinfo=None) > svc.utcnow()
                or cycle.integration_sha or cycle.reviewed_sha or cycle.published_sha
                or cycle.task_fingerprint != svc.task_fingerprint(task)
                or not re.fullmatch(r"cycle deadline exceeded|stage dev-[\w:-]+ failed after [1-4] attempts "
                                    r"\(no time left in the cycle\)", cycle.block_reason or "")):
            continue
        last = (await session.exec(select(ActivityEvent).where(
            ActivityEvent.task_id == task.id, ActivityEvent.event_type == "task.comment")
            .order_by(col(ActivityEvent.created_at).desc(), col(ActivityEvent.id).desc()).limit(1))).first()
        if last and not (last.message or "").startswith(svc.COORDINATOR + ":"):
            continue  # later owner/worker feedback is not an expiry recovery grant
        history = list(await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id)))
        try:
            assignments = [json.loads(a.assignment or "{}") for a in history]
            plans = [json.loads(c.plan or "{}") for c in await session.exec(
                select(TeamCycle).where(TeamCycle.task_id == task.id))]
            if any(not isinstance(p, dict) or p.get("acceptance_evidence_collection") for p in plans):
                continue
            workers = svc.parse_plan(cycle.plan or "{}")["workers"]
        except (ValueError, TypeError, svc.TeamError):
            continue
        if (any(not isinstance(a, dict) or "owner_work_recovery" in a for a in assignments)
                or len(workers) != 1 or workers[0]["role"] != "developer"
                or any(a.status in ("queued", "running") or a.head_sha or a.verdict
                       or (a.role_kind != "pm" and a.started_at is not None
                           and a.error != "no time left in the cycle") for a in history)):
            continue
        # Never re-open the frozen research tree via an old, broad writing scope.
        if workers[0]["mode"] != "verify" and any(
            svc._overlap(p, "docs/research/ai-repos-20261005/") for p in workers[0]["scope"]
        ):
            continue
        items.append({"task_id": str(task.id), "board_id": str(task.board_id),
                      "cycle_id": str(cycle.id), "version": cycle.version, "cause": "expired_unstarted"})
    return {"items": items}


class EvidenceHandoff(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/tasks/{task_id}/evidence", summary="Retired acceptance evidence collection")
async def handoff_evidence(
    task_id: _uuid.UUID, payload: EvidenceHandoff,
    ctx: OrganizationContext = Depends(require_org_admin),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    task = await session.get(Task, task_id)
    board = await session.get(Board, task.board_id) if task and task.board_id else None
    if board is None or board.organization_id != ctx.organization.id:
        raise HTTPException(404, "Task not found")
    raise HTTPException(410, "Acceptance evidence collection was removed; feedback never queues work.")


class ReviewLeaseClaim(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_provider: str = Field(min_length=1, max_length=100)
    reviewer_model: str = Field(min_length=1, max_length=200)
    reviewer_session_id: _uuid.UUID
    timeout_seconds: int = Field(default=900, ge=60, le=900, strict=True)


class ReviewLeaseUpdate(BaseModel):
    lease_id: _uuid.UUID
    action: Literal["heartbeat", "finish", "release"]


def _reviewer_available(organization_id, provider: str, model: str) -> bool:
    return automation_enabled(organization_id) and any(
        entry["eligible"] and (entry["provider"], entry["model"]) == (provider, model)
        for entry in automation_models_status()
    )


async def _review_task_locked(session, task_id, organization_id):
    task = (await session.exec(select(Task).where(Task.id == task_id)
            .with_for_update().execution_options(populate_existing=True))).first()
    board = await session.get(Board, task.board_id, populate_existing=True) if task and task.board_id else None
    if board is None or board.organization_id != organization_id:
        raise HTTPException(404, "Task not found")
    return task, board


async def _review_watch(session, task: Task, board: Board):
    from app.models.activity_events import ActivityEvent
    from app.services.task_execution import task_stamp
    from app.services.team_cycle import _parent_problem, task_fingerprint

    problem = await _parent_problem(session, task)
    if problem:
        raise HTTPException(409, f"Review parent authority changed; no work was cancelled ({problem})")
    parents = []
    node = task
    while node.parent_task_id is not None:  # ancestry bound and locks checked by _parent_problem
        node = await session.get(Task, node.parent_task_id)
        parents.append([str(node.id), task_stamp(node), task_fingerprint(node)])
    comment = (await session.exec(select(ActivityEvent.id).where(
        ActivityEvent.task_id == task.id, ActivityEvent.event_type == "task.comment",
    ).order_by(col(ActivityEvent.created_at).desc(), col(ActivityEvent.id).desc()).limit(1))).first()
    cycle = await _latest_cycle(session, task.id)
    return {
        "task_stamp": task_stamp(task), "comment_head": str(comment) if comment else None,
        "board_rules": _board_rules(board),
        "cycle": [str(cycle.id), cycle.version] if cycle else None,
        "parents": parents,
    }


async def _require_review_lease(session, task: Task, board: Board, data):
    from app.core.time import utcnow

    now = utcnow()
    if (task.status != "review" or any(data.get(key) != value
            for key, value in (await _review_watch(session, task, board)).items())
            or datetime.fromisoformat(data["expires_at"]) <= now
            or datetime.fromisoformat(data["deadline"]) <= now):
        raise HTTPException(409, "Review authority changed or expired; result must be discarded")
    if not _reviewer_available(board.organization_id, data["provider"], data["model"]):
        raise HTTPException(403, "Reviewer disabled or held")


@router.post("/tasks/{task_id}/review-lease", summary="Claim bounded read-only acceptance review")
async def claim_review_lease(
    task_id: _uuid.UUID, payload: ReviewLeaseClaim,
    ctx: OrganizationContext = Depends(require_org_admin), session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    from app.core.time import utcnow
    from app.models.activity_events import ActivityEvent
    from app.services import mc_sidecar
    from app.services.task_execution import REVIEW_LEASE_EVENT, REVIEW_LEASE_SECONDS, live_review_leases
    from app.services.team_cycle import MAX_RUNNING_WORKERS, _dispatch_lock, _fleet_attempts

    if not _reviewer_available(ctx.organization.id, payload.reviewer_provider, payload.reviewer_model):
        raise HTTPException(403, "Reviewer disabled or held")
    await _dispatch_lock(session)
    task, board = await _review_task_locked(session, task_id, ctx.organization.id)
    if task.status != "review":
        raise HTTPException(409, "Only queued Review work may be reviewed")
    watch = await _review_watch(session, task, board)
    snapshot = await _task_snapshot(session, task, board)
    if _task_fingerprint(snapshot) != payload.fingerprint:
        raise HTTPException(409, "Candidate changed since the review")
    try:
        leases = await live_review_leases(session, include_finished=True)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "Review lease unavailable; no dispatch") from None
    if any(event.task_id == task_id for event, _ in leases):
        raise HTTPException(409, "A fresh acceptance review already owns this task")
    leases = [(event, data) for event, data in leases if "finished_at" not in data]
    now = utcnow()
    running = [a for a in await _fleet_attempts(session)
               if a.status == "running" and a.lease_expires_at and a.lease_expires_at > now]
    meta = mc_sidecar.get_model_meta(payload.reviewer_provider, payload.reviewer_model)
    capacity = meta.get("max_concurrency", 1)
    if type(capacity) is not int or not 1 <= capacity <= MAX_RUNNING_WORKERS:
        raise HTTPException(409, "Reviewer capacity unavailable; no dispatch")
    occupied = sum(a.provider == payload.reviewer_provider for a in running) + sum(
        data["provider"] == payload.reviewer_provider for _, data in leases)
    if occupied >= capacity or len(running) + len(leases) >= MAX_RUNNING_WORKERS:
        raise HTTPException(409, "Reviewer capacity occupied; wait without cancelling work")
    deadline = now + timedelta(seconds=payload.timeout_seconds)
    event = ActivityEvent(
        event_type=REVIEW_LEASE_EVENT, task_id=task.id, board_id=board.id,
        message=json.dumps({
            **watch,
            "fingerprint": payload.fingerprint, "provider": payload.reviewer_provider,
            "model": payload.reviewer_model, "session_id": str(payload.reviewer_session_id),
            "deadline": deadline.isoformat(),
            "expires_at": min(deadline, now + timedelta(seconds=REVIEW_LEASE_SECONDS)).isoformat(),
        }),
    )
    session.add(event)
    session.add(ActivityEvent(event_type="task.updated", task_id=task.id, board_id=board.id,
        message=f"Acceptance review started: {payload.reviewer_provider}/{payload.reviewer_model}; session={payload.reviewer_session_id}"))
    if not _reviewer_available(ctx.organization.id, payload.reviewer_provider, payload.reviewer_model):
        raise HTTPException(403, "Reviewer disabled before admission")
    await session.commit()
    return {"lease_id": str(event.id), "deadline": deadline.isoformat()}


@router.post("/tasks/{task_id}/review-lease/update", summary="Heartbeat or release only this review lease")
async def update_review_lease(
    task_id: _uuid.UUID, payload: ReviewLeaseUpdate,
    ctx: OrganizationContext = Depends(require_org_admin), session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    from app.core.time import utcnow
    from app.models.activity_events import ActivityEvent
    from app.services.task_execution import REVIEW_LEASE_EVENT, REVIEW_LEASE_SECONDS, lease_data

    task, board = await _review_task_locked(session, task_id, ctx.organization.id)
    event = await session.get(ActivityEvent, payload.lease_id, populate_existing=True, with_for_update=True)
    if event is None or event.task_id != task.id or event.board_id != board.id:
        raise HTTPException(409, "Wrong review lease; no work was cancelled")
    if event.event_type != REVIEW_LEASE_EVENT:
        if payload.action == "release" and event.event_type == "review.lease_closed":
            return {"ok": True, "already_released": True}
        raise HTTPException(409, "Stale review lease; no work was cancelled")
    try:
        data = lease_data(event)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "Review lease unavailable") from None
    now = utcnow()
    if payload.action != "release":
        await _require_review_lease(session, task, board, data)
        if "finished_at" in data:
            if payload.action == "finish":
                return {"ok": True, "already_finished": True}
            raise HTTPException(409, "Reviewer finished; no activity renewal")
        if payload.action == "heartbeat":
            data["expires_at"] = min(datetime.fromisoformat(data["deadline"]),
                                     now + timedelta(seconds=REVIEW_LEASE_SECONDS)).isoformat()
        else:
            data["finished_at"] = now.isoformat()  # end activity; final decision keeps the unrenewed bound
    else:
        event.event_type = "review.lease_closed"
        data["finished_at"] = now.isoformat()
    event.message = json.dumps(data)
    session.add(event)
    session.add(ActivityEvent(event_type="task.updated", task_id=task.id, board_id=board.id,
        message=f"Acceptance review {payload.action}: {data['provider']}/{data['model']}; session={data['session_id']}"))
    await session.commit()
    return {"ok": True}


@router.post(
    "/decisions",
    summary="Apply one delegated acceptance-review decision (org admin only)",
)
async def post_decision(
    payload: ReviewDecision,
    ctx: OrganizationContext = Depends(require_org_admin),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    """Apply ONE structured delegated outcome under row locks.

    Requires: autonomy enabled, an approved reviewer provider/model tuple,
    concrete evidence, the exact candidate fingerprint and the current
    expected status (stale verdict → 409; no human/owner decision is ever
    overridden). Arbitrary action/code/SQL execution is not possible — only
    accept/revise/escalate/resolve with impact routine|major, each routed
    through the existing gated lifecycle paths.
    """
    if not automation_enabled(ctx.organization.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Autonomous acceptance review is disabled.",
        )
    if not is_reviewer_engine(payload.reviewer_provider, payload.reviewer_model):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Reviewer engine "
                f"{payload.reviewer_provider}/{payload.reviewer_model} is not "
                "one of the approved reviewer models."
            ),
        )
    evidence = (payload.evidence or "").strip()
    if not evidence:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A delegated decision requires concrete evidence.",
        )
    reviewer = f"{payload.reviewer_provider}/{payload.reviewer_model}"

    def require_eligible() -> None:
        if not automation_enabled(ctx.organization.id) or not any(
            m["eligible"]
            and (m["provider"], m["model"]) == (payload.reviewer_provider, payload.reviewer_model)
            for m in automation_models_status()
        ):
            raise HTTPException(status_code=403, detail="Reviewer disabled or held.")

    require_eligible()
    try:
        if payload.kind == "task":
            result = await _decide_task(payload, ctx, session, evidence, reviewer)
        else:
            result = await _decide_item(payload, ctx, session, evidence, reviewer)
        require_eligible()
        await session.commit()
    except BaseException:
        session.info.pop("review_notifications", None)
        await session.rollback()
        raise
    from app.api.tasks import _notify_task_update_assignment_changes

    for update in session.info.pop("review_notifications", []):
        try:
            await _notify_task_update_assignment_changes(session, update=update)
        except Exception:
            # Committed acceptance must not be reported as a failed/rolled-back decision.
            result["notification_delivered"] = False
    return result


async def _decide_task(
    payload: ReviewDecision,
    ctx: OrganizationContext,
    session: AsyncSession,
    evidence: str,
    reviewer: str,
) -> dict[str, Any]:
    if payload.action == "resolve":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Tasks are accepted, revised or escalated — not resolved.",
        )
    try:
        task_uuid = _uuid.UUID(payload.id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Task not found")
    # Row lock before any check: two coordinators must not decide twice.
    task, board = await _review_task_locked(session, task_uuid, ctx.organization.id)

    if task.execution_lane == "agent" and not (
        payload.action == "escalate" or (payload.action == "revise" and payload.correction_kind == "evidence")
    ):
        raise HTTPException(409, "Gateway tasks use board-lead review, not native Git acceptance.")
    from app.models.activity_events import ActivityEvent
    from app.services.task_execution import REVIEW_LEASE_EVENT, live_review_leases

    # Once leased, expired/closed sessions cannot opt out by omitting the ID.
    prior_lease = (await session.exec(select(ActivityEvent.id).where(
        ActivityEvent.task_id == task.id,
        col(ActivityEvent.event_type).in_([REVIEW_LEASE_EVENT, "review.lease_closed"]),
    ).limit(1))).first()
    try:
        leases = await live_review_leases(session, [task.id], include_finished=True)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "Review lease unavailable") from None
    if payload.review_lease_id is not None or prior_lease is not None:
        matching = [data for event, data in leases if event.id == payload.review_lease_id
                    and data["provider"] == payload.reviewer_provider
                    and data["model"] == payload.reviewer_model
                    and data["session_id"] == str(payload.reviewer_session_id)
                    and data["fingerprint"] == payload.fingerprint]
        if len(matching) != 1:
            raise HTTPException(409, "Stale or wrong review lease; no work was cancelled")
        await _require_review_lease(session, task, board, matching[0])
    else:
        await _review_watch(session, task, board)  # legacy review still respects stopped parents
    snapshot = await _task_snapshot(session, task, board)
    fingerprint_value = _task_fingerprint(snapshot)
    if fingerprint_value != payload.fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Candidate changed since the review; re-review the new snapshot.",
        )
    if task.status != payload.expected_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task status changed since the review.",
        )

    user = await session.get(User, ctx.member.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    audit_note = (
        f"{_AUTO_REVIEW_TAG} action={payload.action} reviewer={reviewer} "
        f"impact={payload.impact} sha={(payload.published_sha or '')[:12]}"
    )

    if payload.action == "accept":
        if not snapshot.get("goal_complete"):
            raise HTTPException(422, "Whole current goal unavailable; no partial acceptance")
        if task.status != "review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task ACCEPT is only allowed from Review.",
            )
        if payload.impact == "major" and not await _owner_approved_for_snapshot(
            session, task, snapshot
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Major-impact acceptance requires an owner approval "
                    "of this exact decision or unchanged original goal; escalate instead."
                ),
            )
        if not await _qa_pass_evidence(session, task, snapshot):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="No durable QA PASS evidence; missing tests are not success.",
            )
        from app.services.team_cycle import engine_identity
        same_model_author = any(
            w["role_kind"] in ("developer", "specialist") and w["status"] == "succeeded"
            and engine_identity(w["provider"], w["model"]) == engine_identity(payload.reviewer_provider, payload.reviewer_model)
            for w in snapshot.get("workers", [])
        )
        if same_model_author and (payload.reviewer_session_id is None or
                str(payload.reviewer_session_id) in {str(w.get("id")) for w in snapshot.get("workers", [])}):
            raise HTTPException(status_code=422, detail="Acceptance reviewer engine authored this work; a fresh separate reviewer session is required.")
        cycle = snapshot.get("cycle") or {}
        published_sha = cycle.get("published_sha")
        if not published_sha or payload.published_sha != published_sha:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Decision is not bound to the exact published SHA.",
            )
        # Authenticated delegated acceptance approval, created atomically in
        # the same transaction as the gated lifecycle update below. If the
        # update fails, the whole transaction (including this approval)
        # rolls back.
        approval = Approval(
            board_id=board.id,
            task_id=task.id,
            action_type="delegated_review_accept",
            payload={
                "source": "autonomous_review",
                "delegated": True,
                "reviewer_provider": payload.reviewer_provider,
                "reviewer_model": payload.reviewer_model,
                "reviewer_session_id": str(payload.reviewer_session_id) if payload.reviewer_session_id else None,
                "review_basis": "separate_session" if same_model_author else "distinct_engine",
                "impact": payload.impact,
                "evidence": evidence[:4000],
                "snapshot_fingerprint": fingerprint_value,
                "published_sha": published_sha,
            },
            confidence=100.0,
            status="approved",
            resolved_at=datetime.now(timezone.utc),
        )
        session.add(approval)
        await session.flush()
        await _delegate_task_update(
            session,
            task=task,
            board=board,
            user=user,
            new_status="done",
            comment=(
                f"{audit_note}\nDelegated acceptance by the owner-authorized "
                f"autonomous reviewer. Evidence: {evidence[:1500]}"
            ),
        )
        return _decision_response(
            kind="task",
            candidate_id=str(task.id),
            action="accept",
            task_id=str(task.id),
            updated_at=_iso(task.updated_at),
        )

    if payload.action == "revise":
        if task.status != "review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task revision is only queued from Review.",
            )
        feedback = (payload.feedback or "").strip()
        if not feedback:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A revision requires concrete feedback.",
            )
        prior = await _count_delegated_revisions(session, task.id)
        if payload.impact == "major" or (
            payload.correction_kind == "code" and prior >= MAX_AUTO_CORRECTIONS
        ):
            # Bounded corrections: escalate instead of queueing another round.
            item = await _upsert_delegated_escalation(
                session,
                task=task,
                evidence=evidence,
                feedback=feedback,
                reviewer=reviewer,
            )
            from app.models.activity_events import ActivityEvent

            # Already in Review: leave it unclaimable, even with blocked dependencies.
            session.add(
                ActivityEvent(
                    event_type="task.comment",
                    task_id=task.id,
                    board_id=board.id,
                    message=(
                        f"{_AUTO_REVIEW_TAG} action=escalate reviewer={reviewer} "
                        f"(major revision or bound reached; owner required)\n{feedback[:2000]}"
                    ),
                )
            )
            await session.flush()
            return _decision_response(
                kind="task",
                candidate_id=str(task.id),
                action="escalate",
                task_id=str(task.id),
                updated_at=_iso(task.updated_at),
                extra={
                    "escalation_item_id": str(item.id),
                    "fingerprint": _task_fingerprint(await _task_snapshot(session, task, board)),
                },
            )
        if payload.correction_kind == "evidence":
            from app.models.activity_events import ActivityEvent

            session.add(ActivityEvent(
                event_type="task.comment", task_id=task.id, board_id=board.id,
                message=f"{audit_note} correction_kind=evidence (feedback only; no work queued)\n{feedback[:2000]}",
            ))
            await session.flush()
            return _decision_response(
                kind="task", candidate_id=str(task.id), action="revise",
                task_id=str(task.id), updated_at=_iso(task.updated_at),
                extra={"correction_kind": "evidence", "fingerprint": _task_fingerprint(await _task_snapshot(session, task, board))},
            )
        # Native PM reads description. Preserve the goal, append bounded correction
        # context, and normalize ONLY this server-generated edit in bound approvals.
        old_goal = _goal_fingerprint(task)
        task.description = (task.description or "") + (
            _CORRECTION_MARKER
            + f"Correction {prior + 1}/{MAX_AUTO_CORRECTIONS}:\n{feedback[:2000]}"
        )
        linked = select(col(ApprovalTaskLink.approval_id)).where(
            col(ApprovalTaskLink.task_id) == task.id
        )
        approvals = (
            await session.exec(
                select(Approval).where(
                    or_(col(Approval.task_id) == task.id, col(Approval.id).in_(linked)),
                    col(Approval.status) == "approved",
                )
            )
        ).all()
        for approval in approvals:
            data = approval.payload or {}
            if (
                data.get("source") in ("owner", "owner_review")
                and data.get("goal_fingerprint") == old_goal
            ):
                approval.payload = {**data, "goal_fingerprint": _goal_fingerprint(task)}
        # Correction under the ORIGINAL scope: bounded context, never a new goal. Review
        # requeues through the valid transition (review → inbox), the same
        # path a blocked team cycle uses — never a raw status override.
        await _delegate_task_update(
            session,
            task=task,
            board=board,
            user=user,
            new_status="inbox",
            comment=(
                f"{_AUTO_REVIEW_TAG} action=revise reviewer={reviewer} "
                f"impact={payload.impact} correction_kind=code (correction {prior + 1}/"
                f"{MAX_AUTO_CORRECTIONS})\n{feedback[:2000]}"
            ),
        )
        return _decision_response(
            kind="task",
            candidate_id=str(task.id),
            action="revise",
            task_id=str(task.id),
            updated_at=_iso(task.updated_at),
        )

    # escalate: deduplicated linked owner-queue item; nothing is executed.
    item = await _upsert_delegated_escalation(
        session, task=task, evidence=evidence, feedback=payload.feedback, reviewer=reviewer
    )
    await session.flush()
    return _decision_response(
        kind="task",
        candidate_id=str(task.id),
        action="escalate",
        task_id=str(task.id),
        updated_at=_iso(task.updated_at),
        extra={
            "escalation_item_id": str(item.id),
            "fingerprint": _task_fingerprint(await _task_snapshot(session, task, board)),
        },
    )


async def _decide_item(
    payload: ReviewDecision,
    ctx: OrganizationContext,
    session: AsyncSession,
    evidence: str,
    reviewer: str,
) -> dict[str, Any]:
    if payload.action == "accept":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Owner-queue items are revised, escalated or resolved — not accepted.",
        )
    try:
        item_uuid = _uuid.UUID(payload.id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Item not found")
    item = (
        await session.exec(
            select(OwnerReview)
            .where(col(OwnerReview.id) == item_uuid)
            .with_for_update()
        )
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await _require_item_org(session, item, ctx)
    # Org scoping: watchdog-originated items only, and only when their linked
    # task belongs to this org or is missing entirely.
    if not ((item.submitted_by or "").startswith("Pipeline Watchdog") or item.submitted_by == "PEV Loop"):
        raise HTTPException(status_code=404, detail="Item not found")
    linked_task = None
    if item.task_id is not None:
        linked_task = await session.get(Task, item.task_id)
        if linked_task is not None and linked_task.board_id is not None:
            board = await session.get(Board, linked_task.board_id)
            if board is None or board.organization_id != ctx.organization.id:
                raise HTTPException(status_code=404, detail="Item not found")

    snapshot = await _item_snapshot(session, item)
    if _item_fingerprint(snapshot) != payload.fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Candidate changed since the review; re-review the new snapshot.",
        )
    if item.status != payload.expected_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Item status changed since the review.",
        )

    now = datetime.now(timezone.utc)
    if payload.action == "resolve":
        if payload.impact == "major":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Major/unknown meaningful decisions stay human-visible; "
                    "escalate instead of resolving."
                ),
            )
        confirmed_obsolete = linked_task is None or linked_task.status in (
            "done",
            "closed",
            "cancelled",
        )
        if not (confirmed_obsolete or payload.impact == "routine"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Item is neither confirmed obsolete nor routine.",
            )
        item.status = "resolved"
        item.reviewed_at = now
        item.updated_at = now
        item.reviewer_note = (
            f"Delegated resolution by {reviewer} (owner-authorized, "
            f"{'confirmed obsolete' if confirmed_obsolete else 'routine'}): "
            f"{evidence[:1500]}"
        )
        session.add(item)
        await session.flush()
        return _decision_response(
            kind="item",
            candidate_id=str(item.id),
            action="resolve",
            task_id=str(item.task_id) if item.task_id else None,
            updated_at=_iso(item.updated_at),
        )

    # escalate (and revise on an item): keep it human-visible, attach the
    # delegated reviewer's evidence so the owner can act on it.
    item.reviewer_note = (
        f"Delegated reviewer {reviewer} kept this for an owner decision. "
        f"Evidence: {evidence[:1500]}"
        + (f" Feedback: {(payload.feedback or '')[:500]}" if payload.feedback else "")
    )
    # This note is our own evidence, not a new human decision/timestamp.
    if item.status == "revision_requested":
        # A revision_requested item with fresh delegated feedback goes back to
        # pending so it is not lost in a filtered view.
        item.status = "pending"
    session.add(item)
    await session.flush()
    return _decision_response(
        kind="item",
        candidate_id=str(item.id),
        action="escalate",
        task_id=str(item.task_id) if item.task_id else None,
        updated_at=_iso(item.updated_at),
        extra={"fingerprint": _item_fingerprint(await _item_snapshot(session, item))},
    )


async def _require_item_org(
    session: AsyncSession, item: OwnerReview, ctx: OrganizationContext
) -> None:
    scoped = False
    if item.task_id:
        task = await session.get(Task, item.task_id)
        if task is not None:
            board = await session.get(Board, task.board_id) if task.board_id else None
            if board is None or board.organization_id != ctx.organization.id:
                raise HTTPException(status_code=404, detail="Item not found")
            scoped = True
    if item.escalated_to_board_id:
        board = await session.get(Board, item.escalated_to_board_id)
        if board is None or board.organization_id != ctx.organization.id:
            raise HTTPException(status_code=404, detail="Item not found")
        scoped = True
    if not scoped:
        from app.models.organizations import Organization

        user = await session.get(User, ctx.member.user_id)
        # ponytail: legacy rows have no tenant FK; deny ambiguous multi-tenant
        # ownership until owner_reviews gets an organization_id migration.
        orgs = (await session.exec(select(Organization.id).limit(2))).all()
        if not (user and user.is_super_admin) and orgs != [ctx.organization.id]:
            raise HTTPException(status_code=404, detail="Item not found")


async def _try_snooze_update(
    session: AsyncSession, item_id: str, value: datetime | None
) -> None:
    """Best-effort snooze write. `snooze_until` exists only in the migrated
    production schema (not in the SQLModel metadata), so environments whose
    tables come from metadata.create_all simply skip it. Column presence is
    probed read-only, so a missing column never rolls back pending changes."""
    global _SNOOZE_COLUMN
    if _SNOOZE_COLUMN is None:
        def _check(conn: Any) -> bool:
            from sqlalchemy import inspect as sa_inspect

            return "snooze_until" in {
                c["name"] for c in sa_inspect(conn).get_columns(_TABLE)
            }

        _SNOOZE_COLUMN = await session.run_sync(
            lambda sess: _check(sess.connection())
        )
    if not _SNOOZE_COLUMN:
        return
    if value is None:
        await session.execute(
            text(f"UPDATE {_TABLE} SET snooze_until = NULL WHERE id = :id"),
            {"id": _tid(item_id)},
        )
    else:
        await session.execute(
            text(f"UPDATE {_TABLE} SET snooze_until = :sn WHERE id = :id"),
            {"id": _tid(item_id), "sn": value},
        )


_SNOOZE_COLUMN: bool | None = None


@router.patch(
    "/items/{item_id}",
    summary="Take action on a review item (approve, reject, request_revision, "
    "escalate_to_lead, defer, resolve) — org admin only",
)
async def patch_item(
    item_id: str,
    payload: PatchReviewItem,
    session: AsyncSession = SESSION_DEP,
    _ctx: OrganizationContext = Depends(require_org_admin),
) -> dict[str, Any]:
    """Manual owner action on a review item.

    2026-10-05 hardening: org-admin only, row-locked optimistic fencing
    (optional `expected_status`), and only the canonical status per action —
    an unconstrained status override is no longer accepted. The new `resolve`
    action marks an item RESOLVED without creating a task approval (unlike
    `approve`, which for watchdog-linked tasks records the linked approval).
    """
    async with _review_lock:
        try:
            item_uuid = _uuid.UUID(item_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="Item not found")
        item = (
            await session.exec(
                select(OwnerReview)
                .where(col(OwnerReview.id) == item_uuid)
                .with_for_update()
            )
        ).first()
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        await _require_item_org(session, item, _ctx)
        if payload.expected_updated_at is not None and (
            item.updated_at.replace(tzinfo=None)
            != (
                payload.expected_updated_at.astimezone(timezone.utc).replace(tzinfo=None)
                if payload.expected_updated_at.tzinfo
                else payload.expected_updated_at
            )
        ):
            raise HTTPException(status_code=409, detail="Item changed since it was read")
        if (
            payload.expected_status is not None
            and item.status != payload.expected_status
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Item changed since it was read",
            )

        action = payload.action
        note = payload.reviewer_note or ""
        now = datetime.now(timezone.utc)

        # Canonical status per action; `payload.status` may only confirm it.
        _ACTION_STATUS = {
            "approve": "approved",
            "reject": "rejected",
            "request_revision": "revision_requested",
            "escalate_to_lead": "pending",
            "defer": item.status,
            "resolve": "resolved",
        }
        if action not in _ACTION_STATUS:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
        new_status = _ACTION_STATUS[action]
        if payload.status is not None and payload.status != new_status:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Action '{action}' resolves to status '{new_status}'; "
                    "arbitrary status overrides are not allowed."
                ),
            )

        new_reviewer_note: str | None = item.reviewer_note
        new_reviewed_at: datetime | None = None
        new_snooze: datetime | None = None
        clear_snooze = True

        if action == "approve":
            new_reviewer_note = note or "Approved by owner."
            new_reviewed_at = now
            # T56 (2026-05-15): when a watchdog stale-task review item is
            # approved, auto-insert an approved approval row for the linked
            # task. This unblocks the `require_approval_for_done` board gate
            # so the owner can transition review→done after acknowledging
            # the stale-task escalation.
            linked_task_id = item.task_id
            if linked_task_id and item.submitted_by == _ESCALATION_SUBMITTER:
                if item.status not in ("pending", "revision_requested"):
                    raise HTTPException(status_code=409, detail="Escalation already decided")
                task = await session.get(Task, linked_task_id)
                if task is None or task.board_id is None:
                    raise HTTPException(status_code=409, detail="Escalation task missing")
                if not (item.proposed_action or "").endswith(
                    f"Goal fingerprint: {_goal_fingerprint(task)}"
                ):
                    raise HTTPException(
                        status_code=409, detail="Escalated goal changed; re-review it"
                    )
                session.add(
                    Approval(
                        board_id=task.board_id,
                        task_id=task.id,
                        action_type="owner_decision",
                        status="approved",
                        confidence=100.0,
                        resolved_at=now,
                        payload={
                            "source": "owner_review",
                            "review_item_id": str(item.id),
                            "goal_fingerprint": _goal_fingerprint(task),
                            "evidence": note or "Owner authorized the displayed escalation.",
                        },
                    )
                )
            elif linked_task_id and (item.submitted_by or "").startswith("Pipeline Watchdog"):
                tid_hex = _tid(linked_task_id)
                trow = (await session.execute(
                    text("SELECT board_id, status FROM tasks WHERE id = :tid"),
                    {"tid": tid_hex},
                )).first()
                if trow and trow._mapping["status"] != "done":
                    board_id = trow._mapping["board_id"]
                    already = (await session.execute(
                        text(
                            "SELECT 1 FROM approvals "
                            "WHERE task_id = :tid AND status = 'approved' LIMIT 1"
                        ),
                        {"tid": tid_hex},
                    )).first()
                    if not already:
                        await session.execute(
                            text(
                                "INSERT INTO approvals "
                                "(id, board_id, task_id, action_type, payload, "
                                " confidence, status, created_at, resolved_at) "
                                "VALUES (:id, :bid, :tid, 'owner_review_approve', :payload, "
                                " 100, 'approved', CURRENT_TIMESTAMP, "
                                " CURRENT_TIMESTAMP)"
                            ),
                            {
                                "id": str(_uuid.uuid4()),
                                "bid": _tid(board_id),
                                "tid": tid_hex,
                                "payload": '{"source":"owner_review","review_item_id":"' + item_id + '"}',
                            },
                        )
        elif action == "reject":
            new_reviewer_note = note or "Rejected. Revisions needed before resubmission."
            new_reviewed_at = now
        elif action == "request_revision":
            new_reviewer_note = note
            new_reviewed_at = now
        elif action == "resolve":
            # Resolved WITHOUT creating a task approval — the audit note says
            # why; nothing is approved by age or proxy.
            new_reviewer_note = note or "Resolved without approval."
            new_reviewed_at = now
        elif action == "escalate_to_lead":
            new_reviewer_note = note or "Escalated to board lead for technical assessment."
            if payload.escalate_board_id:
                try:
                    target_board_id = _uuid.UUID(payload.escalate_board_id)
                    target_board = await session.get(Board, target_board_id)
                    if target_board is None or target_board.organization_id != _ctx.organization.id:
                        raise HTTPException(status_code=404, detail="Board not found")
                    item.escalated_to_board_id = target_board_id
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail="Invalid board id")
            new_reviewed_at = now
        elif action == "defer":
            hours = payload.snooze_hours or 24
            new_snooze = now + timedelta(hours=hours)
            new_reviewer_note = note or f"Deferred for {hours}h."
            clear_snooze = False

        if item.task_id and item.submitted_by == _ESCALATION_SUBMITTER:
            from app.models.activity_events import ActivityEvent

            session.add(
                ActivityEvent(
                    event_type="task.comment",
                    task_id=item.task_id,
                    message=f"Owner escalation decision: {action}. {note}"[:4000],
                )
            )
        item.status = new_status
        item.reviewer_note = new_reviewer_note
        item.reviewed_at = new_reviewed_at if new_reviewed_at is not None else item.reviewed_at
        item.updated_at = now
        session.add(item)
        if clear_snooze:
            await _try_snooze_update(session, item_id, None)
        if new_snooze is not None:
            await _try_snooze_update(session, item_id, new_snooze)
        await session.commit()

        # Escalate-to-lead: spawn a board task referencing this review item.
        if action == "escalate_to_lead" and item.escalated_to_board_id:
            import httpx

            from app.core.logging import get_logger
            _log = get_logger(__name__)
            local_auth = os.environ.get("LOCAL_AUTH_TOKEN", "")
            headers = {"Content-Type": "application/json"}
            if local_auth:
                headers["Authorization"] = f"Bearer {local_auth}"
            task_payload = {
                "title": f"[Owner Review] {item.title}",
                "description": (
                    f"Escalated from Owner Review by {note or 'owner'}.\n\n"
                    f"**Summary:** {item.summary or ''}\n"
                    f"**Reason:** {item.reason or ''}\n"
                    f"**Proposed Action:** {item.proposed_action or ''}\n"
                    f"**Submitted by:** {item.submitted_by}\n"
                    f"**Review Item ID:** `{item_id}`"
                ),
                "status": "inbox",
                "priority": "medium",
            }
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"http://localhost:8000/api/v1/boards/{item.escalated_to_board_id}/tasks",
                        json=task_payload, headers=headers, timeout=10.0,
                    )
                    if resp.status_code >= 400:
                        _log.warning(
                            "review.escalate_task_failed item=%s status=%s body=%s",
                            item_id, resp.status_code, resp.text[:200],
                        )
            except Exception:
                _log.warning(
                    "review.escalate_task_error item=%s", item_id, exc_info=True,
                )

        out = await _fetch_item(session, item_id)
        assert out is not None
        return out


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a review item (org admin only)",
)
async def delete_item(
    item_id: str,
    session: AsyncSession = SESSION_DEP,
    _ctx: OrganizationContext = Depends(require_org_admin),
) -> None:
    try:
        item_uuid = _uuid.UUID(item_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Item not found")
    item = (
        await session.exec(
            select(OwnerReview).where(col(OwnerReview.id) == item_uuid).with_for_update()
        )
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await _require_item_org(session, item, _ctx)
    await session.delete(item)
    await session.commit()
