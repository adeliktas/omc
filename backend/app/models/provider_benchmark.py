"""T76 — Provider benchmark observations.

One row per (provider, model, run). Watchdog appends; UI reads the most
recent row per (provider, model) for the dashboard TPS chip.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class ProviderBenchmark(SQLModel, table=True):
    __tablename__ = "provider_benchmark"

    id: int | None = Field(default=None, primary_key=True)
    provider_name: str = Field(index=True)
    model_id: str = Field(index=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tps: float = 0.0
    ok: bool = True
    error: str | None = None
