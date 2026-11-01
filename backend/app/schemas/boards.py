"""Schemas for board create/update/read API operations."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import model_validator
from sqlmodel import Field, SQLModel

_ERR_GOAL_FIELDS_REQUIRED = "Confirmed goal boards require objective and success_metrics"
_ERR_GATEWAY_REQUIRED = "gateway_id is required"
_ERR_DESCRIPTION_REQUIRED = "description is required"
RUNTIME_ANNOTATION_TYPES = (datetime, UUID)


class BoardBase(SQLModel):
    """Shared board fields used across create and read payloads."""

    name: str
    slug: str
    description: str
    gateway_id: UUID | None = None
    board_group_id: UUID | None = None
    board_type: str = "goal"
    objective: str | None = None
    success_metrics: dict[str, object] | None = None
    target_date: datetime | None = None
    goal_confirmed: bool = False
    goal_source: str | None = None
    require_approval_for_done: bool = True
    require_review_before_done: bool = False
    comment_required_for_review: bool = False
    block_status_changes_with_pending_approval: bool = False
    only_lead_can_change_status: bool = False
    max_agents: int = Field(default=1, ge=0)
    autonomous_tasks_enabled: bool = True
    cron_tasks_enabled: bool = True
    cloud_providers_enabled: bool = True
    autonomous_enabled: bool = False  # T105 Phase 5: PEV loop on/off


class BoardCreate(BoardBase):
    """Payload for creating a board."""

    gateway_id: UUID | None = None

    @model_validator(mode="after")
    def validate_goal_fields(self) -> Self:
        """Require gateway and goal details when creating a confirmed goal board."""
        description = self.description.strip()
        if not description:
            raise ValueError(_ERR_DESCRIPTION_REQUIRED)
        self.description = description
        if self.gateway_id is None:
            raise ValueError(_ERR_GATEWAY_REQUIRED)
        if (
            self.board_type == "goal"
            and self.goal_confirmed
            and (not self.objective or not self.success_metrics)
        ):
            raise ValueError(_ERR_GOAL_FIELDS_REQUIRED)
        return self


class BoardUpdate(SQLModel):
    """Payload for partial board updates.

    Fields stored NOT NULL are typed non-nullable, so an explicit null is a 422 and the
    OpenAPI schema (which the settings gear reads) shows they cannot be cleared.
    """

    name: str = Field(default=None)
    slug: str = Field(default=None)
    description: str = Field(default=None)
    gateway_id: UUID = Field(default=None)
    board_group_id: UUID | None = None
    board_type: str = Field(default=None)
    objective: str | None = None
    success_metrics: dict[str, object] | None = None
    target_date: datetime | None = None
    goal_confirmed: bool = Field(default=None)
    goal_source: str | None = None
    require_approval_for_done: bool = Field(default=None)
    require_review_before_done: bool = Field(default=None)
    comment_required_for_review: bool = Field(default=None)
    block_status_changes_with_pending_approval: bool = Field(default=None)
    only_lead_can_change_status: bool = Field(default=None)
    max_agents: int = Field(default=None, ge=0)
    autonomous_tasks_enabled: bool = Field(default=None)
    cron_tasks_enabled: bool = Field(default=None)
    cloud_providers_enabled: bool = Field(default=None)
    autonomous_enabled: bool = Field(default=None)  # T105 Phase 5

    @model_validator(mode="after")
    def validate_description(self) -> Self:
        """Reject blank descriptions in patch payloads."""
        if "description" in self.model_fields_set:
            description = self.description.strip()
            if not description:
                raise ValueError(_ERR_DESCRIPTION_REQUIRED)
            self.description = description
        return self


class BoardRead(BoardBase):
    """Board payload returned from read endpoints."""

    id: UUID
    organization_id: UUID
    created_at: datetime
    updated_at: datetime
