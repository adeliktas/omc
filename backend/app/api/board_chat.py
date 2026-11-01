"""Route a Pi/OpenAI chat session through a board task and its agent comments."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
import re
import time
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import tasks as tasks_api
from app.api.comment_filters import SYSTEM_MARKER_REGEX
from app.api.deps import ActorContext, get_board_for_user_read, get_board_for_user_write, require_user_auth
from app.core.auth import AuthContext
from app.db.session import async_session_maker, get_session
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.models.boards import Board
from app.models.tasks import Task
from app.schemas.tasks import TaskCommentCreate, TaskCreate, TaskUpdate

router = APIRouter(prefix="/boards/{board_id}/v1/chat/completions", tags=["board-chat"])
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


class BoardCompletion(BaseModel):
    model: str = "extern-session"
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    wait_seconds: int | None = Field(default=None, ge=0, le=1800)
    stream: bool = False


class AgentReply(BaseModel):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[dict[str, Any]]
    session_id: str
    task_id: UUID
    after: UUID
    task_status: str


class PendingReply(BaseModel):
    session_id: str
    task_id: UUID
    after: UUID | None
    task_status: str
    status: Literal["pending", "no_reply"]


def _session_id(raw: str | None) -> str:
    if raw is None or not _SESSION_ID.fullmatch(raw):
        raise HTTPException(422, "A valid session_id header is required (Pi: sendSessionAffinityHeaders)")
    return raw


async def _session(session: AsyncSession, board: Board, session_id: str) -> Task | None:
    return (await session.exec(
        select(Task).where(col(Task.board_id) == board.id)
        .where(col(Task.created_by_type) == "board-chat")
        .where(col(Task.created_by_name) == session_id)
        .limit(1),
    )).first()


async def _reply(
    session: AsyncSession, task: Task, after: UUID | None, wait_seconds: int, response: Response,
) -> dict[str, object]:
    cursor = None
    if after is not None:
        cursor = await ActivityEvent.objects.by_id(after).first(session)
        if cursor is None or cursor.task_id != task.id or cursor.event_type != "task.comment":
            raise HTTPException(404, "Comment cursor not found on this task")

    deadline = time.monotonic() + wait_seconds
    while True:
        query = (
            select(ActivityEvent)
            .where(col(ActivityEvent.task_id) == task.id)
            .where(col(ActivityEvent.event_type) == "task.comment")
            .where(col(ActivityEvent.agent_id).is_not(None))
            .order_by(col(ActivityEvent.created_at), col(ActivityEvent.id))
        )
        if cursor is not None:
            query = query.where(or_(
                col(ActivityEvent.created_at) > cursor.created_at,
                (col(ActivityEvent.created_at) == cursor.created_at)
                & (col(ActivityEvent.id) > cursor.id),
            ))
        else:
            query = query.where(col(ActivityEvent.created_at) >= task.created_at)
        # ponytail: scans agent comments per poll; page them if a task grows huge.
        event = next((row for row in await session.exec(query)
                      if not re.match(SYSTEM_MARKER_REGEX, row.message or "")), None)
        await session.refresh(task)
        if event:
            return {
                "id": f"chatcmpl-{event.id}", "object": "chat.completion",
                "created": int(event.created_at.timestamp()), "model": "extern-session",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": event.message},
                             "finish_reason": "stop"}],
                "session_id": task.created_by_name or "", "task_id": str(task.id),
                "after": str(event.id), "task_status": task.status,
            }
        if time.monotonic() >= deadline or task.status in {"done", "cancelled", "trash"}:
            response.status_code = 202
            return {"session_id": task.created_by_name or "", "task_id": str(task.id),
                    "after": str(after) if after else None, "task_status": task.status,
                    "status": "no_reply" if task.status in {"done", "cancelled", "trash"} else "pending"}
        await asyncio.sleep(1)


def _chunk(task: Task, delta: dict[str, str], finish: str | None = None) -> str:
    return "data: " + json.dumps({
        "id": f"chatcmpl-{task.id}", "object": "chat.completion.chunk",
        "created": int(task.created_at.timestamp()), "model": "extern-session",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n"


async def _stream(task: Task, after: UUID | None, seconds: int, request: Request) -> AsyncIterator[str]:
    yield _chunk(task, {"role": "assistant"})
    deadline = time.monotonic() + seconds
    while not await request.is_disconnected():
        async with async_session_maker() as db:
            current = await Task.objects.by_id(task.id).first(db)
            if current is None:
                break
            result = await _reply(db, current, after, min(10, max(0, int(deadline - time.monotonic()))), Response())
        if "choices" in result:
            text = result["choices"][0]["message"]["content"]  # type: ignore[index]
            yield _chunk(task, {"content": text})
            yield _chunk(task, {}, "stop")
            yield "data: [DONE]\n\n"
            return
        if result["status"] == "no_reply" or time.monotonic() >= deadline:
            break
        yield ": waiting for board agent\n\n"
    yield "data: " + json.dumps({"error": {"message": "Board task is still pending; check its task thread",
                                            "type": "board_task_pending"}}) + "\n\n"
    yield "data: [DONE]\n\n"


@router.post("", response_model=AgentReply | PendingReply,
             responses={202: {"model": PendingReply}},
             summary="Pi/OpenAI session → board task and agent replies")
async def submit(
    payload: BoardCompletion,
    request: Request,
    response: Response,
    board: Board = Depends(get_board_for_user_write),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_user_auth),
    session_id: str | None = Header(default=None, alias="session_id", convert_underscores=False),
    x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
    x_session_affinity: str | None = Header(default=None, alias="X-Session-Affinity"),
) -> dict[str, object] | StreamingResponse:
    ids = {value for value in (session_id, x_session_id, x_session_affinity) if value}
    if len(ids) > 1:
        raise HTTPException(422, "Conflicting session IDs")
    session_id = _session_id(next(iter(ids), None))
    last = payload.messages[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, str):
        raise HTTPException(422, "The last message must be a text user prompt")
    prompt = content.strip()
    if not prompt or len(prompt) > 16000:
        raise HTTPException(422, "Prompt must contain 1–16000 characters")

    task = await _session(session, board, session_id)
    after = None
    if task is None:
        lead = (await session.exec(
            select(Agent.id).where(col(Agent.board_id) == board.id)
            .where(col(Agent.is_board_lead).is_(True))
            .where(col(Agent.status).not_in(("retired", "deleting", "updating"))).limit(1),
        )).first()
        if lead is None:
            raise HTTPException(409, "Board has no provisioned lead agent; complete onboarding first")
        try:
            created = await tasks_api.create_task(
                TaskCreate(title=prompt.splitlines()[0][:160], description=prompt,
                           execution_lane="agent", created_by_type="board-chat",
                           created_by_name=session_id),
                board=board, session=session, auth=auth,
            )
            task = await Task.objects.by_id(created.id).first(session)
        except IntegrityError:
            await session.rollback()
            task = await _session(session, board, session_id)
            if task is None:
                raise
    if task is None:
        raise HTTPException(500, "Task creation failed")

    # A retry of the same Pi turn should wait for the same reply, not post the
    # prompt twice. A deliberate repeat after Pi has seen the prior reply has
    # that assistant message in its history and becomes a new comment.
    last_owner = next((row for row in await session.exec(
        select(ActivityEvent).where(col(ActivityEvent.task_id) == task.id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .where(col(ActivityEvent.agent_id).is_(None))
        .order_by(col(ActivityEvent.created_at).desc()),
    ) if not re.match(SYSTEM_MARKER_REGEX, row.message or "")), None)
    last_prompt = last_owner.message if last_owner else task.description
    last_agent = (await session.exec(
        select(ActivityEvent).where(col(ActivityEvent.task_id) == task.id)
        .where(col(ActivityEvent.event_type) == "task.comment")
        .where(col(ActivityEvent.agent_id).is_not(None))
        .order_by(col(ActivityEvent.created_at).desc()).limit(1),
    )).first()
    seen_reply = (last_agent is not None
                  and (last_owner is None or last_agent.created_at > last_owner.created_at)
                  and any(m.get("role") == "assistant" and m.get("content") == last_agent.message
                          for m in payload.messages[:-1]))
    if last_prompt == prompt and not seen_reply:
        after = last_owner.id if last_owner else None
    else:
        if task.status in {"cancelled", "trash", "blocked"}:
            raise HTTPException(409, "Task is closed or blocked; review it on the board")
        if task.status in {"review", "done"}:
            await tasks_api.update_task(
                TaskUpdate(status="inbox"), task=task, session=session,
                actor=ActorContext(actor_type="user", user=auth.user),
            )
            await session.refresh(task)
        event = await tasks_api.create_task_comment(
            TaskCommentCreate(message=prompt), task=task, session=session,
            actor=ActorContext(actor_type="user", user=auth.user),
        )
        after = event.id
    if payload.stream:
        return StreamingResponse(
            _stream(task, after, payload.wait_seconds if payload.wait_seconds is not None else 900, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-MC-Task-ID": str(task.id)},
        )
    return await _reply(session, task, after,
                        payload.wait_seconds if payload.wait_seconds is not None else 20, response)


@router.get("/{session_id}", response_model=AgentReply | PendingReply,
            responses={202: {"model": PendingReply}},
            summary="Poll the next agent comment by Pi session ID")
async def poll(
    session_id: str,
    response: Response,
    after: UUID | None = None,
    wait_seconds: int = Query(default=20, ge=0, le=25),
    board: Board = Depends(get_board_for_user_read),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    task = await _session(session, board, _session_id(session_id))
    if task is None:
        raise HTTPException(404, "Board conversation not found")
    return await _reply(session, task, after, wait_seconds, response)
