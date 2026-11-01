"""Stopping board automation must not wake a cloud lead during privacy setup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import boards
from app.models.boards import Board


@pytest.mark.parametrize(
    "changes,notify",
    [
        ({"goal_confirmed": (True, False)}, False),
        (
            {
                "goal_confirmed": (True, False),
                "autonomous_tasks_enabled": (True, False),
                "autonomous_enabled": (True, False),
                "cron_tasks_enabled": (True, False),
                "cloud_providers_enabled": (True, False),
            },
            False,
        ),
        ({"goal_confirmed": (False, True)}, True),
        ({"goal_confirmed": (True, False), "name": ("old", "new")}, True),
        ({"require_approval_for_done": (True, False)}, True),
        (
            {"goal_confirmed": (True, False), "cloud_providers_enabled": (False, True)},
            True,
        ),
    ],
)
async def test_disabling_only_execution_flags_does_not_notify_lead(
    monkeypatch, changes, notify
):
    lead = SimpleNamespace(
        openclaw_session_id="synthetic-session", name="synthetic-lead", id=uuid4()
    )
    query = SimpleNamespace(first=AsyncMock(return_value=lead))
    query.filter = lambda *args: query
    monkeypatch.setattr(
        type(boards.Agent.objects), "filter_by", lambda self, **kwargs: query
    )
    send = AsyncMock(return_value=None)
    dispatch = SimpleNamespace(
        optional_gateway_config_for_board=AsyncMock(return_value=object()),
        try_send_agent_message=send,
    )
    monkeypatch.setattr(boards, "GatewayDispatchService", lambda session: dispatch)
    monkeypatch.setattr(boards, "record_activity", lambda *args, **kwargs: None)
    session = SimpleNamespace(commit=AsyncMock())
    board = Board(
        id=uuid4(),
        organization_id=uuid4(),
        name="synthetic",
        slug="synthetic",
        gateway_id=uuid4(),
    )
    await boards._notify_lead_on_board_update(
        session=session, board=board, changed_fields=changes
    )
    assert send.await_count == int(notify)
    assert session.commit.await_count == int(notify)
