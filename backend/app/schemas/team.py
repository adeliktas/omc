"""Schemas for native team execution APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import StrictBool, StrictInt, StringConstraints
from sqlmodel import Field, SQLModel

RUNTIME_ANNOTATION_TYPES = (datetime, UUID)


class TeamMemberRead(SQLModel):
    id: UUID
    name: str
    role_kind: str
    role_slug: str | None
    capability: str
    provider: str | None
    model: str | None
    thinking_level: str
    enabled: bool
    created_by: str


class TeamMemberAssign(SQLModel):
    """Admin assignment. Capability is derived server-side and cannot be supplied."""

    role_kind: Literal["pm", "developer", "specialist", "qa"]
    specialist_slug: str | None = None
    auto_assign: bool = False
    provider: str
    model: str
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] = "low"
    enabled: bool = True


class TeamAttemptRead(SQLModel):
    id: UUID
    member_id: UUID
    member_name: str | None = None
    role_kind: str
    stage_key: str
    round: int
    attempt_no: int
    status: str
    provider: str
    model: str
    thinking_level: str
    base_sha: str | None
    head_sha: str | None
    verdict: str | None
    error: str | None
    artifact: str | None
    usage_reported: bool
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    total_tokens: int | None
    cli_cost_estimate_usd: float | None
    started_at: datetime | None
    finished_at: datetime | None


class TeamCycleRead(SQLModel):
    id: UUID
    task_id: UUID
    state: str
    version: int
    round: int
    max_rounds: int
    base_sha: str | None
    integration_sha: str | None
    reviewed_sha: str | None
    published_sha: str | None
    block_reason: str | None
    deadline_at: datetime
    plan: str | None


class TeamCycleView(SQLModel):
    cycle: TeamCycleRead
    attempts: list[TeamAttemptRead]
    usage: dict[str, Any]


class TeamView(SQLModel):
    members: list[TeamMemberRead]
    cycles: list[TeamCycleView]


class TeamStartResponse(SQLModel):
    cycle: TeamCycleRead
    resumed: bool


class TeamClaimRequest(SQLModel):
    base_sha: str
    # The runner publishes this board's work as independent PRs off upstream: no
    # cross-cycle integration, so other cycles' scopes do not lock it.
    independent: bool = False


class TeamClaimResponse(SQLModel):
    attempt: TeamAttemptRead
    claim_token: str
    brief: str
    role_kind: str
    capability: str
    scope: list[str] = Field(default_factory=list)
    lease_expires_at: datetime | None
    # Actual retained bounded UTF-8 files, not extracted from artifact prose.
    evidence_files: dict[str, str] = Field(default_factory=dict, max_length=16)


class TeamUsage(SQLModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


class TeamCompleteRequest(SQLModel):
    claim_token: str
    ok: bool
    artifact: str = ""
    error: str = ""
    head_sha: str | None = None
    changed_files: list[str] | None = None
    verdict: Literal["pass", "fail"] | None = None
    usage: TeamUsage | None = None
    # Host-observed completed built-in tool counts, no arguments or output.
    tool_counts: dict[Literal["read", "bash", "edit", "write", "grep", "find", "ls"], Annotated[StrictInt, Field(ge=0, le=100000)]] | None = Field(default=None, max_length=7)
    # Text files the worker left under its /tmp, vetted and attached by the backend.
    evidence_files: dict[str, str] | None = Field(default=None, max_length=64)


class TeamClaimToken(SQLModel):
    claim_token: str


class TeamIntegrateRequest(SQLModel):
    integration_sha: str


class TeamRefreshIntegration(TeamIntegrateRequest):
    base_sha: str
    version: int


class TeamRetainedQA(SQLModel):
    version: StrictInt = Field(ge=1)
    expected_updated_at: datetime
    expected_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    original_token_cap: StrictInt | None = Field(gt=0)
    retained_integration_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    candidate_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    failed_attempt_id: UUID | None = None
    passed_attempt_id: UUID | None = None
    original_seconds_budget: StrictInt | None = Field(default=1200, gt=0)
    authorized_token_cap: StrictInt | None = Field(default=None, gt=0)
    external_input_output_charge: StrictInt = Field(default=0, ge=0)


class TeamRetryQA(SQLModel):
    version: int = Field(ge=1)
    recover_expired: StrictBool = False
    evidence_files: dict[str, str] | None = Field(default=None, max_length=16)


class TeamResumeCorrection(SQLModel):
    version: int = Field(ge=1)
    failed_attempt_id: UUID


class TeamResumeWork(SQLModel):
    version: int = Field(ge=1)
    cause: Literal[
        "expired_unstarted", "retained_code_failure", "reconciled_plan", "report_evidence",
        "transport_unstarted",
    ]
    failed_attempt_id: UUID | None = None


class TeamPublishRequest(SQLModel):
    sha: str | None = None
    version: int
    reason: str = ""
