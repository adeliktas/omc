"""Cause-specific F1/F2/F3 from independent Sol review; originals retained in scratch."""

import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.agents import Agent
from app.services.openclaw import gateway_rpc as rpc
from app.services.openclaw import provisioning as p
from app.services.openclaw import provisioning_db as db
from app.services.openclaw.admin_service import GatewayAdminLifecycleService

BOARD = UUID("00000000-0000-4000-8000-00000000b0a7")
LEAD = f"lead-{BOARD}"
KEY = f"agent:{LEAD}:main"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["list", "entries"])
@pytest.mark.parametrize("rejected", ["deny", "tools", "every"])
async def test_unrelated_heartbeat_rejection_does_not_strip_held_policy(
    monkeypatch, shape, rejected
):
    held = {
        "id": LEAD,
        "workspace": "/tmp/fixture-held",
        "heartbeat": {"every": "0s"},
        "tools": {"allow": ["*"], "deny": ["*"]},
    }
    other = {
        "id": "other",
        "workspace": "/tmp/fixture-other",
        "heartbeat": {"every": "1h"},
    }
    roster = (
        {"list": [held, other]}
        if shape == "list"
        else {
            "entries": {
                row["id"]: {k: v for k, v in row.items() if k != "id"}
                for row in (held, other)
            }
        }
    )
    config = {
        "agents": roster,
        "tools": {"exec": {"host": "gateway"}},
        "channels": {
            "defaults": {
                "heartbeatVisibility": dict(p.DEFAULT_CHANNEL_HEARTBEAT_VISIBILITY)
            }
        },
    }
    original = copy.deepcopy(held)
    wire = []

    async def transport(method, params, **kwargs):
        if method == "config.get":
            return {"hash": "fixture-CAS", "config": copy.deepcopy(config)}
        assert method == "config.patch" and params["baseHash"] == "fixture-CAS"
        wire.append(json.loads(params["raw"]))
        raise rpc.OpenClawGatewayError(f'Unrecognized key: "{rejected}"')

    monkeypatch.setattr(rpc, "_openclaw_call_once", transport)
    monkeypatch.setattr(p, "_agent_defaults_patch", lambda _: None)
    gateway = NS(
        url="synthetic-only",
        token="fixture-token",
        allow_insecure_tls=False,
        disable_device_pairing=True,
        workspace_root="/tmp/fixture",
    )
    stopped = Agent(name="held fixture", board_id=BOARD, openclaw_session_id=KEY)
    unrelated = Agent(
        name="other fixture",
        board_id=uuid4(),
        openclaw_session_id="agent:other:main",
        heartbeat_config={"every": "2h"},
    )
    with pytest.raises(rpc.OpenClawGatewayError, match="Unrecognized key"):
        await p.OpenClawGatewayProvisioner().sync_gateway_agent_heartbeats(
            gateway, [stopped, unrelated]
        )
    assert len(wire) == 1  # Refuse the rejected full-roster patch, never strip/retry.
    agents = wire[0]["agents"]
    actual = (
        next(row for row in agents["list"] if row["id"] == LEAD)
        if shape == "list"
        else {"id": LEAD, **agents["entries"][LEAD]}
    )
    assert actual == original


@pytest.mark.asyncio
@pytest.mark.parametrize("main", [True, False])
async def test_repair_checks_current_alias_before_any_mutation(monkeypatch, main):
    board = NS(id=uuid4())
    gateway = NS(id=uuid4(), name="fixture gateway")
    agent = Agent(
        name="fixture",
        board_id=None if main else board.id,
        gateway_id=gateway.id,
        is_board_lead=not main,
        openclaw_session_id=KEY,
        status="online",
    )
    before = agent.model_dump()
    effect = Mock(side_effect=AssertionError("repair effect reached"))
    session = NS(
        exec=AsyncMock(return_value=NS(first=lambda: agent)), add=effect, commit=effect
    )
    with pytest.raises(HTTPException) as exc:
        if main:
            service = GatewayAdminLifecycleService(session)
            monkeypatch.setattr(
                service, "find_main_agent", AsyncMock(return_value=agent)
            )
            await service.upsert_main_agent_record(gateway)
        else:
            await db.OpenClawProvisioningService(session).ensure_board_lead_agent(
                request=db.LeadAgentRequest(
                    board=board,
                    gateway=gateway,
                    config=rpc.GatewayConfig("synthetic-only"),
                    user=None,
                )
            )
    assert exc.value.status_code == 409 and agent.model_dump() == before
    effect.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_templates_skip_held_row_before_token_resolution_and_continue(
    monkeypatch,
):
    gateway = NS(id=uuid4())
    board = NS(id=BOARD)
    held = Agent(name="fixture held", board_id=BOARD, openclaw_session_id=KEY)
    other = Agent(name="fixture other", board_id=uuid4())
    resolve = AsyncMock(return_value=(None, False))
    monkeypatch.setattr(db, "_resolve_agent_auth_token", resolve)
    ctx = NS(gateway=gateway)
    result = db._base_result(gateway, include_main=True, reset_sessions=False)
    assert await db._sync_one_agent(ctx, result, held, board) is False
    assert result.agents_skipped == 1 and len(result.errors) == 1
    resolve.assert_not_called()
    assert await db._sync_one_agent(ctx, result, other, NS(id=other.board_id)) is False
    resolve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["alias", "normalized_alias", "board"])
async def test_heartbeat_repair_preserves_original_held_metadata(held):
    agent = Agent(
        name="fixture heartbeat",
        board_id=BOARD if held == "board" else uuid4(),
        openclaw_session_id=(
            "agent:fixture-other:main"
            if held == "board"
            else f" {KEY.upper()} "
            if held == "normalized_alias"
            else KEY
        ),
        agent_token_hash="fixture-existing-hash",
    )
    before = agent.model_dump()
    session = NS(add=Mock(), commit=AsyncMock(), refresh=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await db.AgentLifecycleService(session).ensure_heartbeat_session_key(
            agent=agent, actor=NS()
        )
    assert exc.value.status_code == 409 and agent.model_dump() == before
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    session.refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_held", [False, True])
async def test_heartbeat_repair_preserves_unrelated_and_canonical_held_behavior(
    canonical_held,
):
    agent = Agent(
        name="fixture heartbeat",
        board_id=BOARD if canonical_held else uuid4(),
        is_board_lead=canonical_held,
        openclaw_session_id=KEY if canonical_held else "agent:fixture-stale:main",
    )
    session = NS(add=Mock(), commit=AsyncMock(), refresh=AsyncMock())
    service = db.AgentLifecycleService(session)
    desired = service.resolve_session_key(agent)
    await service.ensure_heartbeat_session_key(agent=agent, actor=NS())
    assert agent.openclaw_session_id == desired
    assert session.commit.await_count == (0 if canonical_held else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [KEY, f" {KEY.upper()} "])
async def test_bulk_main_skips_held_alias_before_token_file_read(monkeypatch, key):
    main = Agent(name="fixture main", openclaw_session_id=key)
    query = NS(first=AsyncMock(return_value=main))
    query.filter = lambda *args: query
    monkeypatch.setattr(Agent, "objects", NS(all=lambda: query))
    resolve = AsyncMock(side_effect=AssertionError("token resolution reached"))
    monkeypatch.setattr(db, "_resolve_agent_auth_token", resolve)
    gateway = NS(id=uuid4())
    result = db._base_result(gateway, include_main=True, reset_sessions=False)
    ctx = NS(gateway=gateway, session=NS())
    before = main.model_dump()
    assert await db._sync_main_agent(ctx, result) is False
    assert result.main_updated is False and result.agents_skipped == 1
    assert len(result.errors) == 1 and main.model_dump() == before
    resolve.assert_not_awaited()


@pytest.mark.parametrize("lead", [LEAD, f" {LEAD.upper()} "])
@pytest.mark.parametrize("every", ["0s", "2h"])
def test_automatic_watchdog_preserves_hold_without_reading_its_workspace(
    monkeypatch, tmp_path, lead, every
):
    import os
    import time
    from pathlib import Path

    from app.db import session as db_session
    from app.services import watchdog_loop as w

    home = tmp_path / "synthetic-openclaw"
    for name in ("held", "other"):
        memory = home / f"workspace-{name}" / "memory"
        memory.mkdir(parents=True)
        file = memory / "fixture.md"
        file.write_text(
            "synthetic token-burning marker" if name == "other" else "synthetic clean"
        )
        if name == "held":
            os.utime(file, (time.time() - 25000, time.time() - 25000))
    held = {
        "id": lead,
        "workspace": "/fixture/.openclaw/workspace-held",
        "tools": {"allow": ["*"], "deny": ["*"]},
        "heartbeat": {"every": every, "original_every": "fixture"},
    }
    config = {
        "agents": {
            "list": [
                held,
                {
                    "id": "fixture-other",
                    "workspace": "/fixture/.openclaw/workspace-other",
                    "heartbeat": {"every": "10m"},
                },
            ]
        }
    }
    path = home / "openclaw.json"
    path.write_text(json.dumps(config))
    original_read = Path.read_text
    held_reads = []

    def read(p, *args, **kwargs):
        if p.parent.parent.name == "workspace-held":
            held_reads.append(p)
            raise AssertionError("held workspace read reached")
        return original_read(p, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(w, "_OPENCLAW_HOME", home)
    monkeypatch.setattr(
        db_session,
        "sync_session_maker",
        Mock(side_effect=RuntimeError("synthetic DB deliberately disabled")),
    )
    result = w._detect_token_burn_agents()
    actual = json.loads(path.read_text())
    assert not held_reads
    assert actual["agents"]["list"][0] == held
    assert actual["agents"]["list"][1]["heartbeat"]["every"] == "2h"
    assert result == {"throttled": 1, "recovered": 0}
