"""Owner Review service — Postgres-backed replacement for OWNER_REVIEW.json
(T18, 2026-05-14).

Sync helpers used by watchdog file-replacement paths; async helpers used by
the FastAPI surface. Both share the same `owner_reviews` table.

Public surface:
    list_pending_sync()             -> list[dict]
    insert_sync(payload)            -> bool
    update_status_sync(item_id, ...) -> bool
    auto_close_resolved_sync(...)   -> int      # noop helper for callers
    has_open_infra_for(reason)      -> bool     # quick check
    task_recently_escalated(task_id, cooldown_sec) -> bool  # open item + cooldown
    file_infra_incident(reason, detail) -> bool
    close_infra_incident(reason)    -> int

All sync helpers run inside a synchronous SQLAlchemy session via
`app.db.session.sync_session_maker()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import sync_session_maker

logger = get_logger(__name__)

_TABLE = "owner_reviews"

# ── Owner-authorized autonomous acceptance review ───────────────────────────
#
# The owner explicitly delegates ROUTINE acceptance (Review → Done) and
# routine owner-queue resolution to an independent read-only reviewer agent.
# Big new system / architecture / security-authority / destructive-data /
# goal changes still require a human at /review. The policy flag and the
# ordered reviewer trio live in the mc_sidecar model-meta namespace WITHOUT
# a `level` key, so the ranking board (which only renders entries that have
# one) omits them. Default is DISABLED; the coordinator enables it explicitly
# after review/deployment via PUT /api/v1/review/automation.

REVIEWER_MODELS: tuple[tuple[str, str], ...] = (
    # Strict owner order. gpt-6.1-sol first, then claude opus-5-5 (canonical
    # CLI name claude-opus-5-5), then the FULL pi-neuralwatt glm-5.3 — never
    # the flash variant and never an older/smaller silent substitute.
    ("openai-codex-2", "gpt-6.1-sol"),
    ("omniroute-codex", "gpt-6.1-sol"),  # Same Sol engine, eligible account-pool transport.
    ("openai-codex-canonical", "gpt-6.1-sol"),  # Explicit Pi-owner/account pin; not model diversity.
    ("claude-code", "opus-5-5"),
    ("pi-neuralwatt", "glm-5.3"),
)

AUTOMATION_PROVIDER = "_mc_automation"
AUTOMATION_MODEL = "owner_review"

AUTOMATION_POLICY = (
    "Owner-authorized autonomous acceptance: an independent read-only reviewer "
    "model may accept routine Review tasks (evidence + QA PASS + exact published "
    "SHA required, via the normal task lifecycle gates) and resolve routine or "
    "confirmed-obsolete watchdog queue items. New system/architecture/security/"
    "destructive/goal changes escalate to a human at /review. Nothing is ever "
    "approved by age or confidence alone; manual provider disables, genuine "
    "quota/auth holds and LG ownership stay untouched."
)

# Bounded corrections: after this many delegated revisions on one task the
# decision escalates to the human instead of queueing another correction.
MAX_AUTO_CORRECTIONS = 3


def automation_enabled(organization_id: Any = None) -> bool:
    """True when the owner has enabled autonomous acceptance review."""
    from app.services import mc_sidecar

    if organization_id is None:
        return any(bool(m.get("enabled")) for m in mc_sidecar.get_provider_meta(AUTOMATION_PROVIDER).values())
    key = f"{AUTOMATION_MODEL}:{UUID(str(organization_id))}"
    meta = mc_sidecar.get_model_meta(AUTOMATION_PROVIDER, key)
    if "enabled" not in meta:
        meta = mc_sidecar.get_model_meta(AUTOMATION_PROVIDER, AUTOMATION_MODEL)
    return bool(meta.get("enabled"))


def managed_boards(organization_id: Any) -> list[str]:
    """Explicit owner scope for automatic, read-only acceptance evidence handoffs."""
    from app.services import mc_sidecar

    meta = mc_sidecar.get_model_meta(AUTOMATION_PROVIDER,
        f"{AUTOMATION_MODEL}:{UUID(str(organization_id))}")
    value = meta.get("managed_board_ids")
    if not automation_enabled(organization_id) or not isinstance(value, list):
        return []
    try:
        return [str(UUID(str(board_id))) for board_id in value]
    except (ValueError, TypeError, AttributeError):
        return []


def unstarted_recovery_enabled(organization_id: Any) -> bool:
    from app.services import mc_sidecar

    meta = mc_sidecar.get_model_meta(AUTOMATION_PROVIDER,
        f"{AUTOMATION_MODEL}:{UUID(str(organization_id))}")
    return automation_enabled(organization_id) and meta.get("recover_unstarted") is True


def set_managed_boards(organization_id: Any, board_ids: list[str], *, recover_unstarted: bool | None = None) -> None:
    set_automation_enabled(automation_enabled(organization_id), organization_id,
                           managed_board_ids=board_ids, recover_unstarted=recover_unstarted)


def set_automation_enabled(enabled: bool, organization_id: Any = None, *,
                           managed_board_ids: list[str] | None = None,
                           recover_unstarted: bool | None = None) -> None:
    """Persist the autonomy flag via the atomic sidecar update helper."""
    from datetime import datetime, timezone

    from app.services import mc_sidecar

    mc_sidecar.set_model_meta(
        AUTOMATION_PROVIDER,
        AUTOMATION_MODEL if organization_id is None else f"{AUTOMATION_MODEL}:{UUID(str(organization_id))}",
        enabled=bool(enabled),
        updated_at=datetime.now(timezone.utc).isoformat(),
        **({"managed_board_ids": managed_board_ids} if managed_board_ids is not None else {}),
        **({"recover_unstarted": recover_unstarted} if recover_unstarted is not None else {}),
    )


def automation_models_status() -> list[dict[str, Any]]:
    """Ordered reviewer trio with live eligibility from sidecar metadata.

    Eligibility reflects manual `disabled` flags and genuine availability
    holds (quota/auth `unavailable_until` windows). It is never a spending or
    token-budget gate.
    """
    from app.services import mc_sidecar
    from app.services.model_ranking import _availability

    from app.services.team_cycle import engine_identity
    out: list[dict[str, Any]] = []
    for provider, model in REVIEWER_MODELS:
        meta = mc_sidecar.get_model_meta(provider, model)
        disabled = bool(meta.get("disabled"))
        available, availability_reason = _availability(meta, provider)
        eligible = (not disabled) and available
        block_reason = None
        if disabled:
            block_reason = meta.get("disabled_reason") or "disabled"
        elif not available:
            block_reason = availability_reason
        out.append(
            {
                "provider": provider,
                "model": model,
                "engine_identity": engine_identity(provider, model),
                "eligible": eligible,
                "block_reason": block_reason,
            }
        )
    return out


def is_reviewer_engine(provider: str, model: str) -> bool:
    """True when (provider, model) is one of the approved reviewer engines."""
    return (provider, model) in REVIEWER_MODELS


def _iso_or_str(value: Any) -> str | None:
    """Datetime → ISO string. Already-string passes through (sqlite TEXT)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _uuid_str(value: Any) -> str | None:
    """Canonical dashed UUID string (raw-SQL rows may carry hex on sqlite)."""
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy row mapping into the dict shape consumers expect
    (matches the legacy OWNER_REVIEW.json structure for backward compat).
    """
    m = row._mapping if hasattr(row, "_mapping") else row
    return {
        "id": str(m["id"]),
        "title": m["title"],
        "type": m["type"],
        "summary": m["summary"],
        "reason": m["reason"],
        "proposed_action": m["proposed_action"],
        "submitted_by": m["submitted_by"],
        "submitted_at": _iso_or_str(m["submitted_at"]),
        "status": m["status"],
        "reviewed_at": _iso_or_str(m["reviewed_at"]),
        "reviewer_note": m["reviewer_note"],
        "escalated_to_board_id": str(m["escalated_to_board_id"]) if m["escalated_to_board_id"] else None,
        "task_id": _uuid_str(m["task_id"]),
    }


def list_all_sync(limit: int = 500) -> list[dict[str, Any]]:
    """Return all owner_reviews rows newest first (legacy compat)."""
    with sync_session_maker() as db:
        res = db.execute(text(
            f"SELECT * FROM {_TABLE} ORDER BY submitted_at DESC LIMIT :lim"
        ), {"lim": limit})
        return [_row_to_dict(r) for r in res.fetchall()]


def list_pending_sync(submitted_by_prefix: str | None = None) -> list[dict[str, Any]]:
    """Return pending items optionally filtered by submitter prefix."""
    with sync_session_maker() as db:
        if submitted_by_prefix:
            res = db.execute(text(
                f"SELECT * FROM {_TABLE} "
                "WHERE status = 'pending' AND submitted_by LIKE :prefix "
                "ORDER BY submitted_at DESC"
            ), {"prefix": f"{submitted_by_prefix}%"})
        else:
            res = db.execute(text(
                f"SELECT * FROM {_TABLE} WHERE status = 'pending' ORDER BY submitted_at DESC"
            ))
        return [_row_to_dict(r) for r in res.fetchall()]


def has_open_infra_for(reason: str) -> bool:
    """Idempotency check for `_open_infra_incident`."""
    with sync_session_maker() as db:
        res = db.execute(text(
            f"SELECT 1 FROM {_TABLE} "
            "WHERE status = 'pending' AND type = 'infrastructure' "
            "AND reason LIKE :pat LIMIT 1"
        ), {"pat": f"%{reason}%"})
        return res.first() is not None


def has_open_infra_for_watchdog() -> bool:
    """Suppress stale escalation when any pipeline-watchdog infra row is open."""
    with sync_session_maker() as db:
        res = db.execute(text(
            f"SELECT 1 FROM {_TABLE} "
            "WHERE status = 'pending' AND type = 'infrastructure' "
            "AND submitted_by LIKE 'Pipeline Watchdog%' LIMIT 1"
        ))
        return res.first() is not None


def task_recently_escalated(task_id: str, cooldown_sec: int = 24 * 3600) -> bool:
    """An open item blocks escalation indefinitely; decided items have a cooldown."""
    with sync_session_maker() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_sec)
        res = db.execute(text(
            f"SELECT 1 FROM {_TABLE} "
            "WHERE (task_id = :tid OR summary LIKE :pat) "
            "AND (status IN ('pending', 'revision_requested') "
            "OR submitted_at > :cutoff) LIMIT 1"
        ), {"tid": _task_id_hex(task_id), "pat": f"%{task_id}%", "cutoff": cutoff})
        return res.first() is not None


def _task_id_hex(task_id: str | None) -> str | None:
    """Normalize a task id to canonical hex for cross-dialect UUID columns.

    Raw-SQL inserts bind strings; Postgres uuid columns accept hex without
    dashes, and SQLite CHAR(32) columns (tasks.id) store exactly that, so
    normalizing keeps JOINs working on both."""
    if not task_id:
        return None
    try:
        return UUID(str(task_id)).hex
    except (ValueError, TypeError, AttributeError):
        return str(task_id)


def insert_sync(*,
    title: str,
    type_: str,
    summary: str | None,
    reason: str | None,
    proposed_action: str | None,
    submitted_by: str,
    task_id: str | None = None,
) -> bool:
    """Insert once per open issue; False means duplicate or insertion failure."""
    try:
        # T37 (2026-05-14): supply `submitted_at` explicitly so SQLite test
        # fixtures (which ignore the migration's `NOW()` server default) get
        # a valid timestamp. Postgres prod path is unaffected — explicit
        # value beats server default.
        now_utc = datetime.now(timezone.utc)
        with sync_session_maker() as db:
            # Same task/submitter/issue, not a timer. PEV step evidence is distinct.
            key = {
                "tid": _task_id_hex(task_id), "by": submitted_by, "type": type_,
                "title": title, "reason": reason or "",
                "summary": summary or "" if submitted_by == "PEV Loop" else "",
            }
            if db.get_bind().dialect.name == "postgresql":
                import json

                db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                           {"key": "owner-review:" + json.dumps(key, sort_keys=True)})
            task_match = "task_id = :tid" if task_id else "task_id IS NULL"
            existing = db.execute(text(
                f"SELECT 1 FROM {_TABLE} WHERE {task_match} "
                "AND submitted_by = :by AND type = :type AND title = :title "
                "AND COALESCE(reason, '') = :reason "
                "AND (:by != 'PEV Loop' OR COALESCE(summary, '') = :summary) "
                "AND status IN ('pending', 'revision_requested') LIMIT 1"
            ), key).first()
            if existing is not None:
                return False  # Preserve evidence, owner notes, snooze and original clocks.
            db.execute(text(
                f"INSERT INTO {_TABLE} "
                "(id, title, type, summary, reason, proposed_action, submitted_by, task_id, "
                " submitted_at, status, created_at, updated_at) "
                "VALUES (:id, :title, :type, :summary, :reason, :action, :by, :tid, "
                " :ts, 'pending', :ts, :ts)"
            ), {
                "id": str(uuid4()),
                "title": title,
                "type": type_,
                "summary": summary,
                "reason": reason,
                "action": proposed_action,
                "by": submitted_by,
                "tid": _task_id_hex(task_id),
                "ts": now_utc,
            })
            db.commit()
        return True
    except Exception as exc:
        logger.warning("owner_reviews.insert_failed err=%s", exc)
        return False


def close_infra_incident(reason: str) -> int:
    """Mark pending infra rows matching `reason` resolved once the resource is
    reachable again (confirmed terminal evidence: the originating probe now
    succeeds).

    2026-10-05: resolved, NOT approved — an auto-recovery never pretends the
    owner approved anything. The audit trail (reviewer_note) is preserved.
    """
    try:
        with sync_session_maker() as db:
            res = db.execute(text(
                f"UPDATE {_TABLE} SET status='resolved', "
                "reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, "
                "reviewer_note='Auto-resolved: resource reachable again.' "
                "WHERE status='pending' AND type='infrastructure' "
                "AND reason LIKE :pat"
            ), {"pat": f"%{reason}%"})
            db.commit()
            return res.rowcount
    except Exception as exc:
        logger.warning("owner_reviews.close_infra_failed err=%s", exc)
        return 0


def cleanup_harness_items(age_sec: int = 600) -> int:
    """Drop pending rows from harness submitters older than `age_sec`."""
    try:
        with sync_session_maker() as db:
            res = db.execute(text(
                f"DELETE FROM {_TABLE} "
                "WHERE status = 'pending' "
                "AND (submitted_by IN ('anonymous', 'tests-harness') "
                "     OR LOWER(title) LIKE 'test:%') "
                "AND submitted_at < :cutoff"
            ), {"cutoff": datetime.now(timezone.utc) - timedelta(seconds=age_sec)})
            db.commit()
            return res.rowcount
    except Exception as exc:
        logger.warning("owner_reviews.cleanup_failed err=%s", exc)
        return 0


def reconcile_resolved_with_tasks() -> int:
    """Auto-close pending watchdog rows whose underlying task is confirmed
    terminal (done/closed/cancelled). Marked RESOLVED — the owner never
    approved anything by proxy; the audit note says why it closed."""
    try:
        with sync_session_maker() as db:
            # task_id column first (new rows); summary regex as fallback (legacy compat).
            res = db.execute(text(
                f"UPDATE {_TABLE} SET status='resolved', "
                "reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, "
                "reviewer_note='Auto-resolved: underlying task reached terminal state.' "
                "WHERE status='pending' AND submitted_by LIKE 'Pipeline Watchdog%' "
                "AND (task_id IS NOT NULL AND task_id IN ("
                "  SELECT id FROM tasks WHERE status IN ('done', 'closed', 'cancelled')"
                "))"
            ))
            db.commit()
            return res.rowcount
    except Exception as exc:
        logger.warning("owner_reviews.reconcile_failed err=%s", exc)
        return 0


def auto_resolve_stale(stale_sec: int = 4 * 3600) -> list[str]:
    """Close stale pending watchdog rows ONLY on confirmed evidence.

    2026-10-05 bug fix: this used to approve every pending watchdog item older
    than 4h purely because of its age — unreviewed work was recorded as owner
    approved. Now an item closes (status RESOLVED, never APPROVED, audit note
    preserved) only when the underlying task is confirmed terminal
    (done/closed/cancelled) or confirmed missing (deleted / never existed).
    Live, healthy, still-unresolved items stay pending for a human or the
    delegated acceptance reviewer; nothing is reopened or overridden.

    Returns the task IDs of the rows it resolved.
    """
    try:
        with sync_session_maker() as db:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_sec)
            # LEFT JOIN so a missing task (obsolete escalation) is visible;
            # the join compares UUID columns directly, portable across
            # Postgres and SQLite test fixtures.
            res = db.execute(text(
                f"SELECT r.id, r.task_id, t.status AS task_status "
                f"FROM {_TABLE} r "
                "LEFT JOIN tasks t ON t.id = r.task_id "
                "WHERE r.status = 'pending' "
                "AND r.submitted_by LIKE 'Pipeline Watchdog%' "
                "AND r.submitted_at < :cutoff"
            ), {"cutoff": cutoff})
            rows = res.fetchall()
            if not rows:
                return []
            task_ids: list[str] = []
            for r in rows:
                tid = r._mapping.get("task_id")
                task_status = r._mapping.get("task_status")
                if not tid:
                    # No linked task at all: nothing confirmable — stay pending,
                    # never resolve by age alone.
                    continue
                if task_status is None:
                    note = (
                        "Auto-resolved: underlying task no longer exists; "
                        "escalation is obsolete."
                    )
                elif task_status in ("done", "closed", "cancelled"):
                    note = (
                        "Auto-resolved: underlying task reached terminal "
                        f"state ({task_status}); escalation is obsolete."
                    )
                else:
                    # Live and unresolved: leave pending for a human decision.
                    continue
                task_ids.append(_uuid_str(tid))
                db.execute(text(
                    f"UPDATE {_TABLE} SET status='resolved', "
                    "reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, "
                    "reviewer_note=:note WHERE id=:id"
                ), {
                    "id": r._mapping["id"],
                    "note": note,
                })
            db.commit()
            return task_ids
    except Exception as exc:
        logger.warning("owner_reviews.auto_resolve_stale_failed err=%s", exc)
        return []
