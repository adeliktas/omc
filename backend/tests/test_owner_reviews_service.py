"""Tests for owner_reviews service (T18 + T37, 2026-05-14).

Targets the sync path used by watchdog. Uses an in-memory SQLite engine
because the service signatures match psycopg dialect for portability.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import SQLModel
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def patched_session(monkeypatch: pytest.MonkeyPatch):
    """Swap `sync_session_maker` for an in-memory SQLite session factory."""
    # Importing app.models triggers SQLModel table registration so
    # metadata.create_all knows about owner_reviews.
    import app.models  # noqa: F401
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    from app.db import session as session_mod
    monkeypatch.setattr(session_mod, "sync_session_maker", factory)
    from app.services import owner_reviews as svc
    monkeypatch.setattr(svc, "sync_session_maker", factory)
    yield factory
    engine.dispose()


def test_insert_and_list_pending(patched_session) -> None:
    from app.services.owner_reviews import insert_sync, list_pending_sync, list_all_sync

    ok = insert_sync(
        title="T37: smoke",
        type_="other",
        summary="Task 11111111-1111-1111-1111-111111111111 stuck",
        reason="auto-test",
        proposed_action="ignore",
        submitted_by="Pipeline Watchdog",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    assert ok is True
    pending = list_pending_sync()
    assert len(pending) == 1
    assert pending[0]["title"] == "T37: smoke"
    assert pending[0]["submitted_by"] == "Pipeline Watchdog"
    assert pending[0]["task_id"] == "11111111-1111-1111-1111-111111111111"

    all_rows = list_all_sync()
    assert len(all_rows) == 1


def test_has_open_infra_for_returns_false_when_empty(patched_session) -> None:
    from app.services.owner_reviews import has_open_infra_for
    assert has_open_infra_for("anything") is False


def test_has_open_infra_for_matches_substring(patched_session) -> None:
    from app.services.owner_reviews import has_open_infra_for, insert_sync
    insert_sync(
        title="INFRA: vllm down",
        type_="infrastructure",
        summary="probe failed",
        reason="vllm probe failed 2x",
        proposed_action="restart vllm",
        submitted_by="Pipeline Watchdog (infra)",
    )
    assert has_open_infra_for("vllm probe failed") is True
    assert has_open_infra_for("ollama probe failed") is False


def test_close_infra_incident_marks_resolved_not_approved(patched_session) -> None:
    """2026-10-05: auto-recovery resolves the item; it never pretends the
    owner approved anything."""
    from app.services.owner_reviews import (
        close_infra_incident,
        has_open_infra_for,
        insert_sync,
        list_all_sync,
    )
    insert_sync(
        title="INFRA: vllm down",
        type_="infrastructure",
        summary="probe failed",
        reason="vllm probe failed 2x",
        proposed_action="x",
        submitted_by="Pipeline Watchdog (infra)",
    )
    assert has_open_infra_for("vllm probe failed") is True
    n = close_infra_incident("vllm probe failed")
    assert n == 1
    assert has_open_infra_for("vllm probe failed") is False
    row = list_all_sync()[0]
    assert row["status"] == "resolved"
    assert "reachable again" in (row["reviewer_note"] or "")


def test_task_recently_escalated_cooldown(patched_session) -> None:
    from app.services.owner_reviews import insert_sync, task_recently_escalated
    insert_sync(
        title="WATCHDOG: stale task",
        type_="other",
        summary="Task 22222222-2222-2222-2222-222222222222 stuck",
        reason="auto",
        proposed_action="x",
        submitted_by="Pipeline Watchdog",
        task_id="22222222-2222-2222-2222-222222222222",
    )
    assert task_recently_escalated("22222222-2222-2222-2222-222222222222") is True
    assert task_recently_escalated("99999999-9999-9999-9999-999999999999") is False


def test_cleanup_harness_items_only_drops_old_harness(patched_session) -> None:
    from app.services.owner_reviews import cleanup_harness_items, insert_sync, list_all_sync
    insert_sync(
        title="test: x", type_="other", summary="", reason="", proposed_action="",
        submitted_by="anonymous",
    )
    insert_sync(
        title="real escalation", type_="other", summary="", reason="", proposed_action="",
        submitted_by="Pipeline Watchdog",
    )
    # Force harness row to be old enough to drop.
    factory = patched_session
    with factory() as db:
        db.execute(text(
            "UPDATE owner_reviews SET submitted_at = '2020-01-01' "
            "WHERE submitted_by = 'anonymous'"
        ))
        db.commit()
    dropped = cleanup_harness_items(age_sec=60)
    assert dropped == 1
    remaining = list_all_sync()
    assert len(remaining) == 1
    assert remaining[0]["submitted_by"] == "Pipeline Watchdog"


# ── 2026-10-05: stale-queue semantics — never approve by age ───────────────


def _insert_task(factory, task_id: str, status: str) -> None:
    from uuid import UUID

    from app.models.tasks import Task

    with factory() as db:
        db.add(
            Task(
                id=UUID(task_id),
                title=f"task {task_id[:4]}",
                status=status,
                priority="medium",
            )
        )
        db.commit()


def _age_out_pending(factory, submitted_by: str = "Pipeline Watchdog") -> None:
    with factory() as db:
        db.execute(text(
            "UPDATE owner_reviews SET submitted_at = '2020-01-01' "
            f"WHERE submitted_by = '{submitted_by}'"
        ))
        db.commit()


def test_auto_resolve_stale_leaves_live_pending(patched_session) -> None:
    """A pending watchdog item older than 4h whose task is LIVE stays
    pending — age alone was never owner approval."""
    from app.services.owner_reviews import auto_resolve_stale, insert_sync, list_all_sync

    task_id = "33333333-3333-3333-3333-333333333333"
    insert_sync(
        title="WATCHDOG: stale task",
        type_="other",
        summary=f"Task {task_id} stuck",
        reason="auto",
        proposed_action="x",
        submitted_by="Pipeline Watchdog",
        task_id=task_id,
    )
    _insert_task(patched_session, "33333333-3333-3333-3333-333333333333", "review")
    _age_out_pending(patched_session)

    assert auto_resolve_stale(stale_sec=4 * 3600) == []
    row = list_all_sync()[0]
    assert row["status"] == "pending"


def test_auto_resolve_stale_resolves_terminal_task_as_resolved(patched_session) -> None:
    from app.services.owner_reviews import auto_resolve_stale, insert_sync, list_all_sync

    task_id = "44444444-4444-4444-4444-444444444444"
    insert_sync(
        title="WATCHDOG: stale task",
        type_="other",
        summary=f"Task {task_id} stuck",
        reason="auto",
        proposed_action="x",
        submitted_by="Pipeline Watchdog",
        task_id=task_id,
    )
    _insert_task(patched_session, "44444444-4444-4444-4444-444444444444", "done")
    _age_out_pending(patched_session)

    resolved = auto_resolve_stale(stale_sec=4 * 3600)
    assert resolved == [task_id]
    row = list_all_sync()[0]
    assert row["status"] == "resolved"
    assert "terminal" in (row["reviewer_note"] or "")


def test_auto_resolve_stale_resolves_missing_task_as_resolved(patched_session) -> None:
    """Confirmed-obsolete: the linked task no longer exists (the old
    revision_requested-style orphan case)."""
    from app.services.owner_reviews import auto_resolve_stale, insert_sync, list_all_sync

    task_id = "55555555-5555-5555-5555-555555555555"
    insert_sync(
        title="WATCHDOG: stale task",
        type_="other",
        summary=f"Task {task_id} stuck",
        reason="auto",
        proposed_action="x",
        submitted_by="Pipeline Watchdog",
        task_id=task_id,
    )
    _age_out_pending(patched_session)

    resolved = auto_resolve_stale(stale_sec=4 * 3600)
    assert resolved == [task_id]
    row = list_all_sync()[0]
    assert row["status"] == "resolved"
    assert "no longer exists" in (row["reviewer_note"] or "")


def test_auto_resolve_stale_never_touches_taskless_or_fresh_rows(patched_session) -> None:
    """No linked task at all: nothing confirmable, stays pending. Fresh
    (young) rows are not stale and are left alone."""
    from app.services.owner_reviews import auto_resolve_stale, insert_sync, list_all_sync

    insert_sync(
        title="INFRA: no task link",
        type_="infrastructure",
        summary="probe failed",
        reason="r",
        proposed_action="x",
        submitted_by="Pipeline Watchdog (infra)",
    )
    insert_sync(
        title="WATCHDOG: fresh row",
        type_="other",
        summary="Task 66666666-6666-6666-6666-666666666666 stuck",
        reason="auto",
        proposed_action="x",
        submitted_by="Pipeline Watchdog",
        task_id="66666666-6666-6666-6666-666666666666",
    )
    with patched_session() as db:
        db.execute(text(
            "UPDATE owner_reviews SET submitted_at = '2020-01-01' "
            "WHERE type = 'infrastructure'"
        ))
        db.commit()

    assert auto_resolve_stale(stale_sec=4 * 3600) == []
    rows = {(r["title"], r["status"]) for r in list_all_sync()}
    assert ("INFRA: no task link", "pending") in rows
    assert ("WATCHDOG: fresh row", "pending") in rows


def test_reconcile_resolved_with_tasks_marks_resolved(patched_session) -> None:
    from app.services.owner_reviews import (
        insert_sync,
        list_all_sync,
        reconcile_resolved_with_tasks,
    )

    done_id = "77777777-7777-7777-7777-777777777777"
    live_id = "88888888-8888-8888-8888-888888888888"
    for tid in (done_id, live_id):
        insert_sync(
            title=f"WATCHDOG: stale {tid[:4]}",
            type_="other",
            summary=f"Task {tid} stuck",
            reason="auto",
            proposed_action="x",
            submitted_by="Pipeline Watchdog",
            task_id=tid,
        )
    _insert_task(patched_session, "77777777-7777-7777-7777-777777777777", "done")
    _insert_task(patched_session, "88888888-8888-8888-8888-888888888888", "review")

    assert reconcile_resolved_with_tasks() == 1
    by_task = {r["task_id"]: r for r in list_all_sync()}
    assert by_task[done_id]["status"] == "resolved"
    assert by_task[live_id]["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "revision_requested"])
def test_open_item_has_no_age_expiry_and_duplicate_insert_preserves_it(patched_session, status):
    from app.services.owner_reviews import insert_sync, list_all_sync, task_recently_escalated

    task_id = "99999999-9999-9999-9999-999999999999"
    payload = dict(title="WATCHDOG: stale", type_="other", summary="first findings",
                   reason="stale", proposed_action="x", submitted_by="Pipeline Watchdog",
                   task_id=task_id)
    assert insert_sync(**payload)
    with patched_session() as db:
        db.execute(text("UPDATE owner_reviews SET status=:status, submitted_at='2020-01-01', "
                        "reviewer_note='owner hold'"), {"status": status})
        db.commit()
    before = list_all_sync()
    assert task_recently_escalated(task_id)
    assert not insert_sync(**{**payload, "summary": "new age, same issue"})
    assert list_all_sync() == before


@pytest.mark.parametrize("status", ["resolved", "approved", "rejected"])
def test_closed_item_only_has_finite_cooldown(patched_session, status):
    from app.services.owner_reviews import insert_sync, task_recently_escalated

    task_id = "99999999-9999-9999-9999-999999999999"
    payload = dict(title="WATCHDOG: stale", type_="other", summary="stale", reason="r",
                   proposed_action="x", submitted_by="Pipeline Watchdog", task_id=task_id)
    assert insert_sync(**payload)
    with patched_session() as db:
        db.execute(text("UPDATE owner_reviews SET status=:status"), {"status": status})
        db.commit()
    assert task_recently_escalated(task_id)
    _age_out_pending(patched_session)
    assert not task_recently_escalated(task_id)
    assert insert_sync(**payload)  # A genuinely later incident is not suppressed forever.


def test_sync_dedup_keeps_distinct_infra_and_pev_findings(patched_session):
    from app.services.owner_reviews import insert_sync, list_all_sync

    payload = dict(title="INFRA", type_="infrastructure", summary="probe", reason="vllm down",
                   proposed_action="inspect", submitted_by="Pipeline Watchdog (infra)")
    assert insert_sync(**payload)
    assert not insert_sync(**{**payload, "summary": "probe repeated"})
    assert insert_sync(**{**payload, "reason": "gateway down"})
    pev = {**payload, "submitted_by": "PEV Loop", "summary": "Step 1/2"}
    assert insert_sync(**pev)
    assert not insert_sync(**pev)
    assert insert_sync(**{**pev, "summary": "Step 2/2"})
    assert len(list_all_sync()) == 4
