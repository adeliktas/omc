"""Scripted health reports remain board-visible without waking an LLM."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import tasks as api


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor_type", "message", "should_send"),
    [
        ("user", "**Automated state report** — synthetic\nStatus: **ok**", False),
        ("user", "**Automated state report** — synthetic\nStatus: **critical**", False),
        ("user", "Please investigate the automated state report.", True),
        ("agent", "**Automated state report** — not the operator script", True),
    ],
)
async def test_scripted_report_does_not_wake_agent(monkeypatch, actor_type, message, should_send):
    board = SimpleNamespace(name="Synthetic board")
    lookup = AsyncMock(return_value=board)
    monkeypatch.setattr(
        api.Board, "objects", SimpleNamespace(by_id=lambda _: SimpleNamespace(first=lookup))
    )
    dispatch = SimpleNamespace(optional_gateway_config_for_board=AsyncMock(return_value=object()))
    monkeypatch.setattr(api, "GatewayDispatchService", lambda _: dispatch)
    send = AsyncMock()
    monkeypatch.setattr(api, "_send_agent_task_message", send)
    task = SimpleNamespace(id=uuid4(), board_id=uuid4(), title="Synthetic health report")
    agent = SimpleNamespace(id=uuid4(), name="Worker", openclaw_session_id="synthetic-session")
    request = api._TaskCommentNotifyRequest(
        task=task,
        actor=SimpleNamespace(actor_type=actor_type, agent=agent),
        message=message,
        targets={agent.id: agent},
        mention_names=set(),
    )
    await api._notify_task_comment_targets(object(), request=request)
    assert send.await_count == int(should_send)
    assert lookup.await_count == int(should_send)
