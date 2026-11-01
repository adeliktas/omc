"""T87 — Chat tab API.

Owner-facing simple multi-turn chat against any registered provider /
model. Independent of the agent + task pipelines; this is just the LLM
without the workflow ceremony.

Endpoints (all under `/api/v1/chat`):
  GET    /sessions                — list (newest first)
  POST   /sessions                — create {title?, provider, model}
  GET    /sessions/{id}           — detail + messages
  PATCH  /sessions/{id}           — rename / change model / change provider
  DELETE /sessions/{id}           — drop session + messages
  POST   /sessions/{id}/messages  — send user message + receive assistant reply

Messages tagged `AGENT_NAME=chat-user` so `/traffic` filter can isolate
them.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select as sa_select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.core.logging import get_logger
from app.db.session import get_session
from app.models.chat import ChatMessage, ChatSession
from app.models.provider_capacity import ProviderCapacity

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


_REQUEST_TIMEOUT = 120.0
_MAX_TOKENS_DEFAULT = 2048

# T88 (2026-05-16): openclaw-gateway backend wiring. Mirrors how the
# `openclaw infer model run --gateway` CLI dispatches over HTTP — the
# gateway exposes a Bearer-auth-gated OpenAI-compat /v1/chat/completions
# on the same port as the web UI (default 18789).
# Honor the split host+port convention used elsewhere in the codebase
# (`watchdog_loop`, `proxy_vllm`). If `OPENCLAW_GATEWAY_HOST` already
# contains a ":", treat it as host:port. Otherwise append the port from
# `OPENCLAW_GATEWAY_PORT` (default 18789).
_GW_HOST_RAW = os.environ.get("OPENCLAW_GATEWAY_HOST", "host.docker.internal")
_GW_PORT_ENV = os.environ.get("OPENCLAW_GATEWAY_PORT", "18789")
_GATEWAY_HOST = _GW_HOST_RAW if ":" in _GW_HOST_RAW else f"{_GW_HOST_RAW}:{_GW_PORT_ENV}"
_GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
_BACKENDS = {"direct", "openclaw-gateway"}


# ── schemas ──────────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    title: str | None = None
    provider: str
    model: str
    backend: str = "direct"


class SessionUpdate(BaseModel):
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    backend: str | None = None


class SessionRead(BaseModel):
    id: UUID
    title: str
    provider: str
    model: str
    backend: str = "direct"
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class MessageRead(BaseModel):
    id: int | None
    role: str
    content: str
    created_at: datetime
    model_used: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None


class MessageSend(BaseModel):
    content: str
    max_tokens: int | None = None
    temperature: float = 0.7


# ── helpers ──────────────────────────────────────────────────────────


async def _get_provider_row(
    db: AsyncSession, provider_name: str,
) -> ProviderCapacity:
    row = (
        await db.execute(
            sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, f"Provider '{provider_name}' not registered")
    return row


# ── endpoints ────────────────────────────────────────────────────────


@router.get(
    "/sessions",
    response_model=list[SessionRead],
    summary="List chat sessions (newest first)",
    dependencies=[Depends(require_user_or_agent)],
)
async def list_sessions(db: AsyncSession = Depends(get_session)) -> list[SessionRead]:
    rows = (
        await db.execute(
            sa_select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(200)
        )
    ).scalars().all()
    out: list[SessionRead] = []
    for s in rows:
        cnt = (
            await db.execute(
                sa_select(ChatMessage).where(ChatMessage.session_id == s.id)
            )
        ).scalars().all()
        out.append(SessionRead(
            id=s.id, title=s.title, provider=s.provider, model=s.model,
            backend=s.backend or "direct",
            created_at=s.created_at, updated_at=s.updated_at, message_count=len(cnt),
        ))
    return out


@router.post(
    "/sessions",
    response_model=SessionRead,
    status_code=201,
    summary="Create a new chat session",
    dependencies=[Depends(require_user_or_agent)],
)
async def create_session(
    payload: SessionCreate,
    db: AsyncSession = Depends(get_session),
) -> SessionRead:
    # Verify the provider exists (only for `direct` backend; openclaw
    # gateway resolves models via openclaw.json, no DB row required).
    backend = payload.backend if payload.backend in _BACKENDS else "direct"
    if backend == "direct":
        await _get_provider_row(db, payload.provider)
    s = ChatSession(
        title=(payload.title or "New chat")[:200],
        provider=payload.provider,
        model=payload.model,
        backend=backend,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return SessionRead(
        id=s.id, title=s.title, provider=s.provider, model=s.model,
        backend=s.backend or "direct",
        created_at=s.created_at, updated_at=s.updated_at, message_count=0,
    )


@router.get(
    "/sessions/{session_id}",
    summary="Session detail + messages",
    dependencies=[Depends(require_user_or_agent)],
)
async def get_session_detail(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = (
        await db.execute(
            sa_select(ChatSession).where(ChatSession.id == session_id)
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "session not found")
    msgs = (
        await db.execute(
            sa_select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()
    return {
        "session": SessionRead(
            id=s.id, title=s.title, provider=s.provider, model=s.model,
            created_at=s.created_at, updated_at=s.updated_at, message_count=len(msgs),
        ).model_dump(),
        "messages": [
            MessageRead(
                id=m.id, role=m.role, content=m.content, created_at=m.created_at,
                model_used=m.model_used, prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens, duration_ms=m.duration_ms,
            ).model_dump()
            for m in msgs
        ],
    }


@router.patch(
    "/sessions/{session_id}",
    response_model=SessionRead,
    summary="Update session (rename / change provider+model)",
    dependencies=[Depends(require_user_or_agent)],
)
async def update_session(
    session_id: UUID,
    payload: SessionUpdate,
    db: AsyncSession = Depends(get_session),
) -> SessionRead:
    s = (
        await db.execute(
            sa_select(ChatSession).where(ChatSession.id == session_id)
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "session not found")
    if payload.title is not None:
        s.title = payload.title[:200]
    if payload.backend is not None and payload.backend in _BACKENDS:
        s.backend = payload.backend
    if payload.provider is not None:
        if (payload.backend or s.backend) == "direct":
            await _get_provider_row(db, payload.provider)
        s.provider = payload.provider
    if payload.model is not None:
        s.model = payload.model
    s.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(s)
    cnt = (
        await db.execute(
            sa_select(ChatMessage).where(ChatMessage.session_id == s.id)
        )
    ).scalars().all()
    return SessionRead(
        id=s.id, title=s.title, provider=s.provider, model=s.model,
        backend=s.backend or "direct",
        created_at=s.created_at, updated_at=s.updated_at, message_count=len(cnt),
    )


@router.delete(
    "/sessions/{session_id}",
    summary="Delete a chat session + all its messages",
    dependencies=[Depends(require_user_or_agent)],
)
async def delete_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = (
        await db.execute(
            sa_select(ChatSession).where(ChatSession.id == session_id)
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "session not found")
    await db.delete(s)
    await db.commit()
    return {"deleted": str(session_id)}


@router.post(
    "/sessions/{session_id}/messages",
    summary="Append user message + return assistant reply",
    dependencies=[Depends(require_user_or_agent)],
)
async def send_message(
    session_id: UUID,
    payload: MessageSend,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = (
        await db.execute(
            sa_select(ChatSession).where(ChatSession.id == session_id)
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "session not found")

    backend = s.backend or "direct"

    # T103 v6 (2026-05-18): all chat backends route through OmniRoute now.
    # - "openclaw-gateway" → openclaw-gateway → OmniRoute (full agent runtime
    #   path; lets owner test agent context/skills/tools)
    # - "direct" → OmniRoute /v1/chat/completions directly (raw chat, no agent)
    # Provider-direct URLs no longer used; provider name maps to OmniRoute
    # node prefix in the model field (`<prefix>/<model_id>`).
    if backend == "openclaw-gateway":
        if not _GATEWAY_TOKEN:
            raise HTTPException(
                400,
                "openclaw-gateway backend selected but OPENCLAW_GATEWAY_TOKEN is unset",
            )
        target_base_url = f"http://{_GATEWAY_HOST}/v1"
        api_key_for_upstream: str | None = _GATEWAY_TOKEN
        upstream_label = f"openclaw-gateway:{_GATEWAY_HOST}"
    else:
        or_url = os.environ.get("OMNIROUTE_RUNTIME_URL", "http://omniroute:20129/v1")
        or_key = os.environ.get("OMNIROUTE_RUNTIME_KEY", "")
        if not or_key:
            raise HTTPException(
                400,
                "OmniRoute backend selected but OMNIROUTE_RUNTIME_KEY is unset",
            )
        target_base_url = or_url
        api_key_for_upstream = or_key
        upstream_label = f"omniroute:{s.provider}"

    # Persist the user message first so it's recorded even if upstream fails.
    user_msg = ChatMessage(session_id=session_id, role="user", content=payload.content)
    db.add(user_msg)
    await db.commit()

    # Pull prior turns for the upstream context.
    history = (
        await db.execute(
            sa_select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()

    # Tag the AGENT_NAME so /traffic can isolate Chat-tab calls.
    system_tag = (
        "You are a helpful assistant. "
        "`AGENT_NAME=chat-user` `AGENT_ID=chat-tab` "
        f"`SESSION_ID={session_id}` `BACKEND={backend}`"
    )
    messages_payload: list[dict[str, str]] = [{"role": "system", "content": system_tag}]
    for m in history:
        if m.role in ("user", "assistant", "system"):
            messages_payload.append({"role": m.role, "content": m.content})

    # T103 v6 (2026-05-18): when going through OmniRoute (default backend),
    # model id must include the upstream prefix so OmniRoute routes to the
    # right node. Skip prefixing if already prefixed or if going via gateway.
    if backend == "openclaw-gateway":
        model_for_body = s.model
    else:
        model_for_body = (
            s.model if "/" in s.model
            else f"{s.provider.lower()}/{s.model}"
        )
    body = {
        "model": model_for_body,
        "messages": messages_payload,
        "max_tokens": payload.max_tokens or _MAX_TOKENS_DEFAULT,
        "temperature": payload.temperature,
        "stream": False,
    }

    headers = {"Content-Type": "application/json"}
    if api_key_for_upstream and api_key_for_upstream.lower() not in ("noapikey", "none", ""):
        headers["Authorization"] = f"Bearer {api_key_for_upstream}"

    url = f"{target_base_url.rstrip('/')}/chat/completions"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, f"Upstream timeout: {upstream_label}")
    except httpx.ConnectError:
        raise HTTPException(502, f"Cannot connect to {upstream_label}")
    duration_ms = int((time.monotonic() - t0) * 1000)

    if r.status_code != 200:
        # Record the failure as an assistant message so the UI can show it.
        err_text = r.text[:500] if r.text else ""
        err_msg = ChatMessage(
            session_id=session_id, role="assistant",
            content=f"[error] upstream returned {r.status_code}\n{err_text}",
            model_used=s.model, duration_ms=duration_ms,
        )
        db.add(err_msg)
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Upstream {r.status_code}: {err_text[:200]}",
        )

    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, "non-JSON upstream response")

    content = ""
    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or msg.get("reasoning") or ""
    except Exception:
        pass

    if not content.strip():
        content = "[no content returned]"

    assistant_msg = ChatMessage(
        session_id=session_id, role="assistant", content=content,
        model_used=s.model, prompt_tokens=pt, completion_tokens=ct,
        duration_ms=duration_ms,
    )
    db.add(assistant_msg)

    # Auto-title the session if the title is still the placeholder.
    if s.title == "New chat" and history:
        first_user = next(
            (m.content for m in history if m.role == "user"), payload.content,
        )
        s.title = (first_user.strip().splitlines()[0] or "New chat")[:80]

    s.updated_at = datetime.now(timezone.utc)
    db.add(s)
    await db.commit()
    await db.refresh(assistant_msg)
    await db.refresh(s)

    return {
        "session": {
            "id": str(s.id),
            "title": s.title,
            "provider": s.provider,
            "model": s.model,
            "updated_at": s.updated_at.isoformat(),
        },
        "user_message": MessageRead(
            id=user_msg.id, role="user", content=user_msg.content,
            created_at=user_msg.created_at,
        ).model_dump(),
        "assistant_message": MessageRead(
            id=assistant_msg.id, role="assistant", content=assistant_msg.content,
            created_at=assistant_msg.created_at, model_used=s.model,
            prompt_tokens=pt, completion_tokens=ct, duration_ms=duration_ms,
        ).model_dump(),
    }
