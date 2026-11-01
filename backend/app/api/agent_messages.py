"""Typed inter-agent message bus API (T25, E2, 2026-05-14).

Endpoints:
  POST   /agent-messages              — send (agent or owner)
  GET    /agent-messages/inbox        — fetch unread for the current agent
  POST   /agent-messages/{id}/ack     — mark read

Routing strategies supported via `message_type`:
  - handoff       : worker → next worker on a DAG/split task
  - peer-review   : worker → peer for review request
  - vote-request  : PM → voters (broadcast: to_agent_id=NULL, board_id set)
  - signoff       : PM → reviewer to request `[pm-signoff]` decision
  - heads-up      : informational; never blocks

Agents check their inbox at heartbeat tick (or via PG LISTEN/NOTIFY if wired).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc as sa_desc, select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent, get_session as _db_dep
from app.core.logging import get_logger
from app.models.agent_messages import AgentMessage

logger = get_logger(__name__)
router = APIRouter(prefix="/agent-messages", tags=["agent-messages"])

ALLOWED_MESSAGE_TYPES = {
    "handoff", "peer-review", "vote-request", "signoff", "heads-up",
}


class MessageCreate(BaseModel):
    to_agent_id: UUID | None = None
    board_id: UUID | None = None
    task_id: UUID | None = None
    message_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class MessageRead(BaseModel):
    id: UUID
    from_agent_id: UUID | None
    to_agent_id: UUID | None
    board_id: UUID | None
    task_id: UUID | None
    message_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    read_at: datetime | None


def _to_read(m: AgentMessage) -> MessageRead:
    return MessageRead(
        id=m.id,
        from_agent_id=m.from_agent_id,
        to_agent_id=m.to_agent_id,
        board_id=m.board_id,
        task_id=m.task_id,
        message_type=m.message_type,
        payload=m.payload or {},
        status=m.status,
        created_at=m.created_at,
        read_at=m.read_at,
    )


@router.post("", response_model=MessageRead, status_code=201)
async def send_message(
    payload: MessageCreate,
    db: AsyncSession = Depends(_db_dep),
    actor=Depends(require_user_or_agent),
) -> MessageRead:
    if payload.message_type not in ALLOWED_MESSAGE_TYPES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_message_type",
                "supported": sorted(ALLOWED_MESSAGE_TYPES),
            },
        )
    from_agent_id = getattr(getattr(actor, "agent", None), "id", None)
    m = AgentMessage(
        from_agent_id=from_agent_id,
        to_agent_id=payload.to_agent_id,
        board_id=payload.board_id,
        task_id=payload.task_id,
        message_type=payload.message_type,
        payload=payload.payload,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    logger.info(
        "agent_messages.sent type=%s from=%s to=%s task=%s",
        m.message_type,
        str(from_agent_id)[:8] if from_agent_id else "owner",
        str(payload.to_agent_id)[:8] if payload.to_agent_id else "broadcast",
        str(payload.task_id)[:8] if payload.task_id else "—",
    )
    return _to_read(m)


@router.get("/inbox", response_model=list[MessageRead])
async def inbox(
    agent_id: UUID | None = None,
    include_broadcast: bool = True,
    limit: int = 50,
    db: AsyncSession = Depends(_db_dep),
    actor=Depends(require_user_or_agent),
) -> list[MessageRead]:
    """Return unread messages for the calling agent. Owner caller must pass
    `agent_id` explicitly. Broadcast messages (to_agent_id=NULL) included by
    default when called by an agent.
    """
    resolved_id = agent_id or getattr(getattr(actor, "agent", None), "id", None)
    if resolved_id is None:
        raise HTTPException(
            status_code=422,
            detail="agent_id required (or call as an agent)",
        )
    stmt = sa_select(AgentMessage).where(AgentMessage.status == "unread")
    if include_broadcast:
        from sqlalchemy import or_ as _or
        stmt = stmt.where(_or(
            AgentMessage.to_agent_id == resolved_id,
            AgentMessage.to_agent_id.is_(None),
        ))
    else:
        stmt = stmt.where(AgentMessage.to_agent_id == resolved_id)
    stmt = stmt.order_by(sa_desc(AgentMessage.created_at)).limit(min(limit, 200))
    res = await db.execute(stmt)
    rows = res.scalars().all()
    return [_to_read(m) for m in rows]


@router.post("/{message_id}/ack", response_model=MessageRead)
async def ack_message(
    message_id: UUID,
    db: AsyncSession = Depends(_db_dep),
    actor=Depends(require_user_or_agent),
) -> MessageRead:
    m = await db.get(AgentMessage, message_id)
    if m is None:
        raise HTTPException(status_code=404, detail="message not found")
    m.status = "read"
    m.read_at = datetime.now(timezone.utc)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return _to_read(m)
