"""Role profiles: the templates agents inherit their identity from.

Profiles are per ROLE, not per agent: every agent whose role normalizes to the
same slug renders the same `IDENTITY.md` and `SOUL.md`. Until now that registry
lived only as `.j2` files on disk, so the one thing that defines how an agent
behaves was the one thing the UI could not show.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.db.session import get_session
from app.services.openclaw.provisioning import (
    _role_profile_template,
    _templates_root,
    canonical_role_slug,
)

router = APIRouter(prefix="/roles", tags=["roles"])

SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)

_PROFILE_FILES = ("IDENTITY.md", "SOUL.md")


def _registry_slugs() -> list[str]:
    root = _templates_root() / "roles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


@router.get("", summary="Role profiles and the agents inheriting them")
async def list_roles(
    session: AsyncSession = SESSION_DEP,
    _actor=ACTOR_DEP,
) -> dict[str, Any]:
    rows = (
        await session.execute(
            text(
                # Same rule as AgentLifecycleService.with_computed_status, so
                # /roles and /agents agree on who is offline.
                "SELECT name, identity_profile->>'role' AS role,"
                " CASE WHEN status IN ('deleting', 'updating') THEN status"
                "      WHEN last_seen_at IS NULL THEN 'provisioning'"
                "      WHEN last_seen_at < (NOW() AT TIME ZONE 'UTC') - INTERVAL '10 minutes'"
                "      THEN 'offline' ELSE status END AS status,"
                " (identity_template IS NOT NULL) AS has_identity_override,"
                " (soul_template IS NOT NULL) AS has_soul_override"
                " FROM agents WHERE status <> 'retired' ORDER BY name"
            )
        )
    ).all()

    by_slug: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[str] = []
    for name, role, agent_status, ident_override, soul_override in rows:
        slug = canonical_role_slug(role or "")
        if not slug:
            # An agent with no role silently falls back to the generic
            # profile, which is exactly the gap worth surfacing.
            unassigned.append(name)
            continue
        by_slug.setdefault(slug, []).append(
            {
                "name": name,
                "role": role,
                "status": agent_status,
                "has_override": bool(ident_override or soul_override),
            }
        )

    slugs = sorted(set(_registry_slugs()) | set(by_slug))
    items = []
    for slug in slugs:
        present = [f for f in _PROFILE_FILES if _role_profile_template(slug, f)]
        items.append(
            {
                "slug": slug,
                "has_profile": len(present) == len(_PROFILE_FILES),
                "profile_files": present,
                "agents": by_slug.get(slug, []),
                "agent_count": len(by_slug.get(slug, [])),
            }
        )
    return {"items": items, "agents_without_role": unassigned}


@router.get("/{slug}", summary="One role profile, rendered source")
async def get_role(
    slug: str,
    _actor=ACTOR_DEP,
) -> dict[str, Any]:
    normalized = canonical_role_slug(slug)
    if not normalized:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role slug")
    templates = {
        name: _role_profile_template(normalized, name) for name in _PROFILE_FILES
    }
    if not any(templates.values()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No profile for role {normalized}")
    return {"slug": normalized, "templates": templates}
