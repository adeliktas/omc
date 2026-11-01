# ruff: noqa: S101
"""Patch 1 (2026-07-16): routing observability.

The `[watchdog-inbox]` marker used to always claim "role-matched from task
title" even when routing fell through to the QA default or the lead, and a
typo'd `;role=` pin degraded silently. These tests pin the truthful-comment
composer, the pin validator's data sources, and the unchanged-fallback
contract (invalid pins are flagged, NOT rerouted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import app.services.watchdog_loop as wl


# ── truthful routing comment ────────────────────────────────────────────────


def test_comment_role_pin() -> None:
    msg = wl._compose_routing_comment("QA Engineer", "role-pin")
    assert msg.startswith("[watchdog-inbox]")
    assert "reason=role-pin" in msg
    assert "role-matched from task title" not in msg


def test_comment_keyword_match() -> None:
    msg = wl._compose_routing_comment("Security Architect", "keyword-match")
    assert "reason=keyword-match" in msg


def test_comment_qa_default() -> None:
    msg = wl._compose_routing_comment(
        "QA Engineer (default worker)", "qa-default (no role matched)"
    )
    assert "reason=qa-default" in msg


def test_comment_lead_last_resort() -> None:
    msg = wl._compose_routing_comment(
        "Lead (Atlas)", "lead-last-resort (no worker available in any pool)"
    )
    assert "reason=lead-last-resort" in msg


def test_comment_carries_invalid_pin_note() -> None:
    note = " note=invalid role pin 'QA Enigneer' (no matching role or agent pool)."
    msg = wl._compose_routing_comment("QA Engineer (default worker)", "qa-default", note)
    assert "invalid role pin 'QA Enigneer'" in msg


def test_comment_keeps_bracket_prefix_for_display_filters() -> None:
    # Comment views hide `[`-prefixed markers; the routing marker must keep
    # that shape so it lands in the details tab, not the owner comment feed.
    assert wl._compose_routing_comment("X", "role-pin").startswith("[")


# ── pin validator ───────────────────────────────────────────────────────────


_BOARD = "11111111-1111-1111-1111-111111111111"


@dataclass
class _FakeResult:
    row: Any

    def first(self) -> Any:
        return self.row


@dataclass
class _FakeDb:
    agent_exists: bool = False
    queries: list[str] = field(default_factory=list)
    params: list[dict | None] = field(default_factory=list)

    async def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:
        self.queries.append(str(stmt))
        self.params.append(params)
        return _FakeResult((1,) if self.agent_exists else None)


@pytest.mark.asyncio
async def test_pin_valid_when_in_route_keywords_without_db_query() -> None:
    db = _FakeDb(agent_exists=False)
    assert await wl._is_valid_role_pin(db, "QA Engineer", _BOARD) is True
    assert db.queries == []  # keyword-role set answers without a DB roundtrip


@pytest.mark.asyncio
async def test_pin_valid_when_agent_prefix_exists() -> None:
    db = _FakeDb(agent_exists=True)
    assert await wl._is_valid_role_pin(db, "RE-Alpha", _BOARD) is True
    assert len(db.queries) == 1
    # Agent pools are board-scoped, like `_pick_worker_for_role`.
    assert "board_id = :bid" in db.queries[0]
    assert db.params[0] == {"bid": _BOARD, "h": "RE-Alpha", "p": "RE-Alpha%"}


@pytest.mark.asyncio
async def test_pin_invalid_when_unknown_everywhere() -> None:
    db = _FakeDb(agent_exists=False)
    assert await wl._is_valid_role_pin(db, "QA Enigneer", _BOARD) is False


# ── behavior-preservation contracts (source-level) ──────────────────────────


def test_invalid_pin_does_not_reroute() -> None:
    """Invalid pins keep today's fallback chain: the hint still goes to the
    picker (miss → QA default → lead). Only visibility was added."""
    import inspect

    src = inspect.getsource(wl._route_manual_inbox_tasks)
    # matched_role is still seeded from hint_role unconditionally...
    assert "matched_role = hint_role" in src
    # ...and the invalid-pin branch only builds a note + warning.
    assert "invalid_role_pin" in src
    assert "hint_role = None" not in src.split("pin_note")[1].split("body_lower")[0]


def test_harness_tasks_are_not_stolen_by_gateway_agents() -> None:
    import inspect

    triage = inspect.getsource(wl._route_manual_inbox_tasks)
    consensus = inspect.getsource(wl._solicit_consensus_for_tool_tasks)
    # Ownership is decided by the explicit lane first; the title prefix is the
    # legacy fallback and only applies when the lane is unset.
    assert "COALESCE(t.execution_lane, '') <> 'harness'" in triage
    assert "t.title ~ '^\\\\[(agent|claude|codex)\\\\]'" in triage
    assert "t.assigned_agent_id IS NULL" in consensus
    assert "_is_static_re_task(title)" in consensus
    assert not wl._is_static_re_task("[agent] Audit the UI pages")
    assert not wl._is_static_re_task("[codex] Reverse engineer the UI")
    assert not wl._is_static_re_task("Update the dashboard")
    assert wl._is_static_re_task("Solve crackme password using IDA Pro")


def test_routing_reason_states_exist() -> None:
    import inspect

    src = inspect.getsource(wl._route_manual_inbox_tasks)
    for token in (
        '"role-pin"',
        '"keyword-match"',
        '"llm-classifier"',
        "qa-default",
        "lead-last-resort",
    ):
        assert token in src, f"missing routing reason: {token}"
