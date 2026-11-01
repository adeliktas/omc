"""T87 — Chat tab: simple multi-turn LLM chat sessions.

Two tables:
- `chat_sessions`: one per owner-initiated conversation. Holds the
  selected provider + model + title.
- `chat_messages`: ordered messages within a session. Roles: `user`,
  `assistant`, `system`. The send endpoint appends `user`, then
  forwards full history to the upstream, then appends the `assistant`
  reply.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str = Field(default="New chat", max_length=200)
    provider: str = Field(max_length=64)
    model: str = Field(max_length=256)
    # T88 (2026-05-16): which backend serves this chat:
    #   "direct"            — POST straight to provider's base_url
    #   "openclaw-gateway"  — POST through the openclaw gateway's
    #                         OpenAI-compat `/v1/chat/completions` (auth
    #                         via `OPENCLAW_GATEWAY_TOKEN`). Equivalent of
    #                         `openclaw infer model run --prompt ... --gateway`.
    backend: str = Field(default="direct", max_length=32)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by_user_id: UUID | None = None


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: int | None = Field(default=None, primary_key=True)
    session_id: UUID = Field(foreign_key="chat_sessions.id", index=True)
    role: str = Field(max_length=20)  # 'user' | 'assistant' | 'system'
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    model_used: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
