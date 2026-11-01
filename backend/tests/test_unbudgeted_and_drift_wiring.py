# ruff: noqa: S101
"""Patch 1 (2026-07-16): wiring + contract checks for the unbudgeted
long-runner marker and session-key drift audit. The SQL bodies are
Postgres-only (NOW(), INTERVAL, NOT EXISTS) — per repo precedent
(test_watchdog_llm_integration.py) these tests pin the report-only
contracts and the tick wiring at source level instead of spinning up PG.
"""

from __future__ import annotations

import inspect

import app.services.watchdog_loop as wl


# ── unbudgeted long runners ─────────────────────────────────────────────────


def test_unbudgeted_threshold_default_24h() -> None:
    assert wl._UNBUDGETED_FLAG_HOURS == 24


def test_unbudgeted_is_report_only() -> None:
    src = inspect.getsource(wl._flag_unbudgeted_long_runners)
    # Posts a marker, never mutates task rows.
    assert "_post_comment" in src
    assert "UPDATE tasks" not in src
    assert "SET status" not in src
    assert "assigned_agent_id = " not in src


def test_unbudgeted_marker_is_cycle_scoped_idempotent() -> None:
    src = inspect.getsource(wl._flag_unbudgeted_long_runners)
    assert "[watchdog-stalled] reason=no-budget" in src
    # Idempotency: an existing marker newer than in_progress_at suppresses.
    assert "ae.created_at > t.in_progress_at" in src


def test_unbudgeted_covers_null_and_zero_budgets() -> None:
    src = inspect.getsource(wl._flag_unbudgeted_long_runners)
    assert "max_seconds_budget IS NULL OR t.max_seconds_budget = 0" in src


def test_unbudgeted_wired_into_tick() -> None:
    src = inspect.getsource(wl)
    assert "_flag_unbudgeted_long_runners(db)" in src
    assert 'stats["unbudgeted_flagged"]' in src


# ── session-key drift audit ─────────────────────────────────────────────────


def test_drift_audit_never_rewrites_keys() -> None:
    src = inspect.getsource(wl._flag_session_key_drift)
    assert "UPDATE agents" not in src  # Patch 2 decision — report only
    assert "agent.session_key_drift" in src


def test_drift_audit_deduped_7_days() -> None:
    src = inspect.getsource(wl._flag_session_key_drift)
    assert "INTERVAL '7 days'" in src


def test_drift_audit_wired_into_reconciler() -> None:
    src = inspect.getsource(wl._reconcile_openclaw_session_status)
    assert "_flag_session_key_drift" in src
    # Reconciler still KEEPS keys on dead sessions (2026-07-02 lesson).
    assert "KEEP openclaw_session_id" in src
