"""T110 — Model Ranking Board API.

GET  /api/v1/ranking           — full board (levels, entries, usage, picks)
PUT  /api/v1/ranking           — persist drag-drop moves / metadata
POST /api/v1/ranking/seed     — seed the default owner ranking (idempotent)
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select as sa_select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelSession

from app.api.deps import require_org_admin, require_user_or_agent, get_session as _db_dep
from app.core.time import utcnow
from app.models.provider_capacity import ProviderCapacity
from app.services import mc_sidecar, model_ranking
from app.services.activity_log import AdminChangeFailed, audited_admin_change
from app.services.organizations import OrganizationContext

router = APIRouter(prefix="/ranking", tags=["ranking"])

# Internal policy metadata is never a public model/ranking identity.
PublicProvider = Annotated[str, Field(min_length=1, pattern=r"^[^_]")]


class RankingMove(BaseModel):
    provider: PublicProvider
    model: str
    level: int
    rank: int | None = None


class RankingEntryMeta(BaseModel):
    provider: PublicProvider
    model: str
    level: int | None = None
    rank: int | None = None
    provider_priority: int | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=32)
    max_complexity: int | None = None
    write_enabled: StrictBool = False
    fully_trusted: StrictBool = False
    canonical_pi_selected: StrictBool | None = None
    cost_class: str | None = None
    budget: dict[str, Any] | None = None
    pricing: dict[str, Any] | None = None
    disabled: bool | None = None
    disabled_reason: str | None = None
    unavailable_until: str | None = None
    availability_reason: str | None = None
    # Owner pin for every attempt on this model (null = role/complexity policy).
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    # An engine whose reliability is unproven: picked only for local drafts, never normal routing.
    draft_only: StrictBool | None = None


class RankingUsageEvent(BaseModel):
    provider: PublicProvider
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    source: str = "cli_report"
    success: bool = False
    unavailable_until: str | None = None
    availability_reason: str | None = None
    # What the run loaded, e.g. {"skills": ["ponytail:ultra"], "mcp": []}, shown per model on /models.
    harness: dict[Literal["skills", "mcp"], list[Annotated[str, Field(max_length=80)]]] | None = None


class RankingMoveList(BaseModel):
    moves: list[RankingMove]


async def _provider_rows(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(sa_select(ProviderCapacity))
    ).scalars().all()
    return [{"provider_name": r.provider_name, "status": r.status} for r in rows]


@router.get("", summary="Full ranking board with usage + eligibility")
async def get_ranking(
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    return await model_ranking.get_board(await _provider_rows(db))


@router.get("/overview", summary="Models and team roles: settings and the last 7 days of use")
async def get_overview(
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """What /models shows next to the ranking: the effort policy, every team role with its
    configured members, and which engines and thinking levels the last 7 days really used.
    A member's engine is only a default; each attempt picks its engine from the ranking."""
    from app.services import team_cycle as tc

    since = utcnow() - timedelta(days=7)
    used = (await db.execute(text(
        "select a.provider, a.model, a.role_kind, coalesce(m.role_slug, ''), a.thinking_level, a.status, "
        "count(*), max(a.created_at) from team_attempts a join team_members m on m.id = a.member_id "
        "where a.created_at > :since group by 1, 2, 3, 4, 5, 6"), {"since": since})).all()
    members = (await db.execute(text(
        "select b.id, b.name, m.role_kind, coalesce(m.role_slug, ''), m.provider, m.model, m.thinking_level, "
        "m.enabled, m.created_by, m.name from team_members m join boards b on b.id = m.board_id "
        "where m.role_kind != 'coordinator' order by b.name"))).all()
    kinds = {"pm": "pm", "developer": "developer", "qa": "qa"}
    roles = ([{"kind": "pm", "slug": "project-manager", "label": "Planner (PM)"}]
             + [{"kind": "developer", "slug": "", "label": "Developer"}]
             + [{"kind": "specialist", "slug": s, "label": f"Specialist: {s}"} for s in sorted(tc.SPECIALIST_SLUGS)]
             + [{"kind": "qa", "slug": "qa-engineer", "label": "QA reviewer"}])
    for r in roles:
        mine = [u for u in used if u[2] == r["kind"] and u[3] == r["slug"]]
        r["capability"] = tc.KIND_CAPABILITY[kinds.get(r["kind"], "specialist")]
        r["effort"] = {lvl: tc.resolve_effort(r["kind"], lvl, tc.TeamMember(), "")["requested"] for lvl in range(1, 6)}
        # Only the role's canonical member: retired rows (team-manager-*) keep their history but never run.
        own = tc.member_name(r["kind"], r["slug"] if r["kind"] == "specialist" else None)
        r["members"] = [{"board_id": str(b), "board": n, "provider": p, "model": mo, "thinking_level": t,
                         "enabled": e, "created_by": c}
                        for b, n, k, s, p, mo, t, e, c, name in members if name == own]
        r["attempts"] = sum(u[6] for u in mine)
        r["succeeded"] = sum(u[6] for u in mine if u[5] == "succeeded")
        r["engines"] = {}
        for u in mine:
            r["engines"][f"{u[0]}/{u[1]}"] = r["engines"].get(f"{u[0]}/{u[1]}", 0) + u[6]
        r["last_used"] = max((str(u[7]) for u in mine), default=None)
        # A role loads what its engines load: the runner reports skills/MCP per engine and run.
        seen = [mc_sidecar.get_model_meta(*e.split("/", 1)).get("observed_harness") or {} for e in r["engines"]]
        r["harness"] = {k: sorted({x for h in seen for x in h.get(k) or []}) for k in ("skills", "mcp")}
    models: dict[str, dict[str, Any]] = {}
    for p, mo, kind, _slug, level, _status, n, _last in used:
        m = models.setdefault(f"{p}/{mo}", {"roles": {}, "thinking": {}})
        m["roles"][kind] = m["roles"].get(kind, 0) + n
        m["thinking"][level] = m["thinking"].get(level, 0) + n
    return {"roles": roles, "models": models, "levels": list(tc.EFFORT_LEVELS)}


@router.put("", summary="Persist ranking moves (drag-drop)")
async def put_ranking(
    payload: RankingMoveList,
    _admin=Depends(require_org_admin),
) -> dict[str, Any]:
    written = model_ranking.apply_moves(
        [m.model_dump() for m in payload.moves]
    )
    return {"written": written}


@router.put("/entry", summary="Set ranking metadata for one entry")
async def put_entry(
    payload: RankingEntryMeta,
    admin: OrganizationContext = Depends(require_org_admin),
    session: SQLModelSession = Depends(_db_dep),
) -> dict[str, Any]:
    values = payload.model_dump()
    meta = {
        key: values[key]
        for key in payload.model_fields_set
        if key not in ("provider", "model")
    }
    entry = (payload.provider, payload.model)

    def saved() -> dict[str, Any]:
        current = mc_sidecar.get_model_meta(*entry, strict=True)
        return {k: current.get(k) for k in sorted(meta)}  # meta comes from a set

    def apply() -> None:
        model_ranking.set_entry_meta(*entry, meta)
        if saved() != meta:  # the sidecar writer logs and swallows write errors
            raise OSError("ranking metadata was not saved")

    try:
        before = saved()
    except Exception:
        raise HTTPException(
            status_code=503, detail="Ranking entry could not be read; nothing changed"
        )
    try:
        await audited_admin_change(
            session,
            admin.member,
            f"Ranking entry {entry[0]}/{entry[1]}: "
            + ", ".join(f"{k} {before[k]!r} -> {v!r}" for k, v in sorted(meta.items())),
            apply=apply,
            state=lambda: repr(saved()),
        )
    except AdminChangeFailed:
        raise HTTPException(status_code=503, detail="Ranking entry could not be saved")
    return {"ok": True}


@router.post("/usage", summary="Record usage reported by a coding-agent harness")
async def post_usage(
    payload: RankingUsageEvent,
    _admin=Depends(require_org_admin),
) -> dict[str, Any]:
    model_ranking.record_usage(payload.provider, payload.model, payload.model_dump())
    return {"ok": True}


@router.post("/policy/cloud-first", summary="Apply the owner cloud-first harness policy")
async def apply_cloud_first_policy(
    _admin=Depends(require_org_admin),
) -> dict[str, Any]:
    model_ranking.seed_default_ranking()
    return {"updated": model_ranking.apply_cloud_policy()}


@router.post("/seed", summary="Seed the default owner ranking (idempotent)")
async def seed_ranking(
    _admin=Depends(require_org_admin),
) -> dict[str, Any]:
    seeded = model_ranking.seed_default_ranking()
    return {"seeded": seeded}