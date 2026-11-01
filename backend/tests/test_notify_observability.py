# ruff: noqa: S101
"""Patch 1 (2026-07-16): delivery skips are observable, never silent.

`_notify_agent_on_task_assign` / `_notify_agent_on_task_rework` used to
`return` silently on a missing `openclaw_session_id` or unresolvable
gateway config — the 2026-07-02 incident class where assignments never
reached agents and nothing recorded it. Every skip must now emit a
`task.assignee_notify_failed` / `task.rework_notify_failed` activity event
with a specific reason. No retries, no key rewriting (Patch 2 decisions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from app.api import tasks as tasks_api
from app.models.agents import Agent
from app.models.boards import Board
from app.models.tasks import Task
from app.services.openclaw.internal.session_keys import board_agent_session_key


@dataclass
class _FakeSession:
    added: list[object] = field(default_factory=list)
    commits: int = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, task: Task, *, with_for_update: bool = False) -> None:
        # Unit fixture only: FOR UPDATE authority read or post-refusal hydration.
        assert isinstance(with_for_update, bool)

    async def rollback(self) -> None:
        return


def _board() -> Board:
    return Board(
        id=uuid4(),
        organization_id=uuid4(),
        name="Platform",
        slug="platform",
        gateway_id=uuid4(),
    )


def _task(board: Board) -> Task:
    return Task(id=uuid4(), board_id=board.id, title="poem cycle", status="inbox")


def _agent(key: str | None) -> Agent:
    return Agent(
        id=uuid4(),
        gateway_id=uuid4(),
        board_id=uuid4(),
        name="QA Engineer",
        openclaw_session_id=key,
    )


@pytest.fixture()
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _fake_record_activity(session: Any, **kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(tasks_api, "record_activity", _fake_record_activity)
    return events


@pytest.fixture()
def dispatch_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a dispatch service means a send was attempted — the
    precondition-guard tests must never get that far."""

    class _Boom:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("gateway dispatch must not be constructed")

    monkeypatch.setattr(tasks_api, "GatewayDispatchService", _Boom)


# ── assignment path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_missing_key_records_event(
    captured_events: list[dict[str, Any]], dispatch_forbidden: None
) -> None:
    board = _board()
    session = _FakeSession()
    await tasks_api._notify_agent_on_task_assign(
        session=session, board=board, task=_task(board), agent=_agent(None)
    )
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["event_type"] == "task.assignee_notify_failed"
    assert "missing openclaw_session_id" in ev["message"]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_assign_malformed_key_records_event_and_blocks_send(
    captured_events: list[dict[str, Any]],
    dispatch_forbidden: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board()
    # The exact 2026-07-06 incident shape: 2-part agent-form key — the
    # gateway refuses it. Wake still happens (assignment bookkeeping,
    # preserved original behavior); only the send is blocked.
    agent = _agent(f"agent:mc-{uuid4()}")
    wakes: list[str] = []

    async def _fake_wake(**kwargs: Any) -> None:
        wakes.append(kwargs["reason"])

    monkeypatch.setattr(tasks_api, "_wake_agent_online_for_task", _fake_wake)
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=_task(board), agent=agent
    )
    assert wakes == ["assignment"]
    assert len(captured_events) == 1
    assert "malformed openclaw_session_id" in captured_events[0]["message"]


@pytest.mark.asyncio
async def test_assign_bare_legacy_key_still_sends(
    captured_events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-agent-form keys (legacy/bridge/plain) are NOT provably malformed —
    # delivery must proceed exactly as before Patch 1.
    board = _board()
    agent = _agent("session-worker")
    sends: list[str] = []

    class _Dispatch:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def optional_gateway_config_for_board(self, _board: Any) -> object:
            return object()

    async def _noop_wake(**_k: Any) -> None:
        return None

    async def _fake_send(**kwargs: Any) -> None:
        sends.append(kwargs["session_key"])
        return None

    monkeypatch.setattr(tasks_api, "GatewayDispatchService", _Dispatch)
    monkeypatch.setattr(tasks_api, "_wake_agent_online_for_task", _noop_wake)
    monkeypatch.setattr(tasks_api, "_send_agent_task_message", _fake_send)
    task = _task(board)
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=task, agent=agent
    )
    # Still sent; the per-task suffix is appended to the bare key as-is.
    assert sends == [f"session-worker:task-{task.id}"]
    assert [e["event_type"] for e in captured_events] == ["task.assignee_notified"]


@pytest.mark.asyncio
async def test_assign_no_gateway_config_records_event(
    captured_events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board()
    agent = _agent(board_agent_session_key(uuid4()))

    class _NoConfigDispatch:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def optional_gateway_config_for_board(self, _board: Any) -> None:
            return None

    async def _noop_wake(**_k: Any) -> None:
        return None

    monkeypatch.setattr(tasks_api, "GatewayDispatchService", _NoConfigDispatch)
    monkeypatch.setattr(tasks_api, "_wake_agent_online_for_task", _noop_wake)
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=_task(board), agent=agent
    )
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["event_type"] == "task.assignee_notify_failed"
    assert "no gateway config" in ev["message"]


@pytest.mark.asyncio
async def test_assign_done_task_stays_silent(
    captured_events: list[dict[str, Any]], dispatch_forbidden: None
) -> None:
    # Intentional policy: done/trash tasks need no delivery and no event.
    board = _board()
    task = _task(board)
    task.status = "done"
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=task, agent=_agent(None)
    )
    assert captured_events == []


@pytest.mark.asyncio
async def test_assign_success_path_unaffected(
    captured_events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board()
    agent_key_id = uuid4()
    agent = _agent(board_agent_session_key(agent_key_id))
    sends: list[str] = []

    class _Dispatch:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def optional_gateway_config_for_board(self, _board: Any) -> object:
            return object()

    async def _noop_wake(**_k: Any) -> None:
        return None

    async def _fake_send(**kwargs: Any) -> None:
        sends.append(kwargs["session_key"])
        return None  # no error

    monkeypatch.setattr(tasks_api, "GatewayDispatchService", _Dispatch)
    monkeypatch.setattr(tasks_api, "_wake_agent_online_for_task", _noop_wake)
    monkeypatch.setattr(tasks_api, "_send_agent_task_message", _fake_send)
    task = _task(board)
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=task, agent=agent
    )
    # Per-task conversation: same agent, `:main` replaced by `:task-<id>`.
    assert sends == [f"agent:mc-{agent_key_id}:task-{task.id}"]
    assert [e["event_type"] for e in captured_events] == ["task.assignee_notified"]


@pytest.mark.asyncio
async def test_assign_send_error_still_records_failed_event(
    captured_events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board()
    agent = _agent(board_agent_session_key(uuid4()))

    class _Dispatch:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def optional_gateway_config_for_board(self, _board: Any) -> object:
            return object()

    async def _noop_wake(**_k: Any) -> None:
        return None

    async def _fake_send(**_k: Any) -> str:
        return "protocol mismatch"

    monkeypatch.setattr(tasks_api, "GatewayDispatchService", _Dispatch)
    monkeypatch.setattr(tasks_api, "_wake_agent_online_for_task", _noop_wake)
    monkeypatch.setattr(tasks_api, "_send_agent_task_message", _fake_send)
    await tasks_api._notify_agent_on_task_assign(
        session=_FakeSession(), board=board, task=_task(board), agent=agent
    )
    assert [e["event_type"] for e in captured_events] == [
        "task.assignee_notify_failed"
    ]
    assert "protocol mismatch" in captured_events[0]["message"]


# ── rework path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rework_missing_key_records_event(
    captured_events: list[dict[str, Any]], dispatch_forbidden: None
) -> None:
    board = _board()
    await tasks_api._notify_agent_on_task_rework(
        session=_FakeSession(),
        board=board,
        task=_task(board),
        agent=_agent(None),
        lead=_agent(board_agent_session_key(uuid4())),
    )
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["event_type"] == "task.rework_notify_failed"
    assert "missing openclaw_session_id" in ev["message"]


@pytest.mark.asyncio
async def test_rework_malformed_key_records_event(
    captured_events: list[dict[str, Any]], dispatch_forbidden: None
) -> None:
    board = _board()
    await tasks_api._notify_agent_on_task_rework(
        session=_FakeSession(),
        board=board,
        task=_task(board),
        agent=_agent("agent:mc-nomain"),
        lead=_agent(board_agent_session_key(uuid4())),
    )
    assert len(captured_events) == 1
    assert "malformed openclaw_session_id" in captured_events[0]["message"]
