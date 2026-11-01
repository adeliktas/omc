"""Provider capacity model — tracks concurrent LLM provider limits and usage."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class ProviderCapacity(SQLModel, table=True):
    """Per-provider concurrency tracking for task routing decisions."""

    __tablename__ = "provider_capacity"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    provider_name: str = Field(index=True, unique=True)
    provider_type: str = Field(default="openai-compatible")
    max_concurrent: int = Field(default=3)
    current_usage: int = Field(default=0)
    is_unlimited: bool = Field(default=False)
    base_url: str | None = None
    api_key: str | None = None
    status: str = Field(default="online")
    last_checked_at: datetime | None = None
    quota_reset_at: datetime | None = None
    priority: int = Field(default=0)  # 0=highest, higher=lower priority
    # P1 (2026-09-01, GRAND-PLAN): provider tier label for difficulty→tier→
    # provider-pool routing. NULL = untiered = DORMANT (combo ordering + routing
    # unchanged). Owner sets via PATCH /providers/<name>. Ordered by env
    # PROVIDER_TIER_ORDER in omniroute_combo_sync.
    tier: str | None = Field(default=None, index=True)
    # T76 (2026-05-16): last observed failure reason. Set when watchdog or
    # benchmark loop disables the provider (e.g. "all models disabled",
    # "401 unauthorized on benchmark"). Cleared on first successful benchmark.
    last_error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
