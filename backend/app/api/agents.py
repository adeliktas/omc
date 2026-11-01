"""Thin API wrappers for async agent lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ActorContext, require_org_admin, require_user_or_agent
from app.core.auth import AuthContext, get_auth_context
from app.db.session import get_session
from app.schemas.agents import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatCreate,
    AgentRead,
    AgentUpdate,
)
from app.schemas.common import OkResponse
from app.schemas.pagination import DefaultLimitOffsetPage
from app.services.openclaw.provisioning_db import AgentLifecycleService, AgentUpdateOptions
from app.services.organizations import OrganizationContext

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/agents", tags=["agents"])

BOARD_ID_QUERY = Query(default=None)
GATEWAY_ID_QUERY = Query(default=None)
SINCE_QUERY = Query(default=None)
SESSION_DEP = Depends(get_session)
ORG_ADMIN_DEP = Depends(require_org_admin)
ACTOR_DEP = Depends(require_user_or_agent)
AUTH_DEP = Depends(get_auth_context)


@dataclass(frozen=True, slots=True)
class _AgentUpdateParams:
    force: bool
    auth: AuthContext
    ctx: OrganizationContext


def _agent_update_params(
    *,
    force: bool = False,
    auth: AuthContext = AUTH_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> _AgentUpdateParams:
    return _AgentUpdateParams(force=force, auth=auth, ctx=ctx)


AGENT_UPDATE_PARAMS_DEP = Depends(_agent_update_params)


@router.get("", response_model=DefaultLimitOffsetPage[AgentRead])
async def list_agents(
    board_id: UUID | None = BOARD_ID_QUERY,
    gateway_id: UUID | None = GATEWAY_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> LimitOffsetPage[AgentRead]:
    """List agents visible to the active organization admin."""
    service = AgentLifecycleService(session)
    return await service.list_agents(
        board_id=board_id,
        gateway_id=gateway_id,
        ctx=ctx,
    )


@router.get("/stream")
async def stream_agents(
    request: Request,
    board_id: UUID | None = BOARD_ID_QUERY,
    since: str | None = SINCE_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> EventSourceResponse:
    """Stream agent updates as SSE events."""
    service = AgentLifecycleService(session)
    return await service.stream_agents(
        request=request,
        board_id=board_id,
        since=since,
        ctx=ctx,
    )


@router.post("", response_model=AgentRead)
async def create_agent(
    payload: AgentCreate,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> AgentRead:
    """Create and provision an agent."""
    service = AgentLifecycleService(session)
    return await service.create_agent(payload=payload, actor=actor)


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(
    agent_id: str,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> AgentRead:
    """Get a single agent by id."""
    service = AgentLifecycleService(session)
    return await service.get_agent(agent_id=agent_id, ctx=ctx)


@router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    params: _AgentUpdateParams = AGENT_UPDATE_PARAMS_DEP,
    session: AsyncSession = SESSION_DEP,
) -> AgentRead:
    """Update agent metadata and optionally reprovision."""
    service = AgentLifecycleService(session)
    return await service.update_agent(
        agent_id=agent_id,
        payload=payload,
        options=AgentUpdateOptions(
            force=params.force,
            user=params.auth.user,
            context=params.ctx,
        ),
    )


@router.post("/{agent_id}/heartbeat", response_model=AgentRead)
async def heartbeat_agent(
    agent_id: str,
    payload: AgentHeartbeat,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> AgentRead:
    """Record a heartbeat for a specific agent."""
    service = AgentLifecycleService(session)
    return await service.heartbeat_agent(agent_id=agent_id, payload=payload, actor=actor)


@router.post("/heartbeat", response_model=AgentRead)
async def heartbeat_or_create_agent(
    payload: AgentHeartbeatCreate,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> AgentRead:
    """Heartbeat an existing agent or create/provision one if needed."""
    service = AgentLifecycleService(session)
    return await service.heartbeat_or_create_agent(payload=payload, actor=actor)


@router.delete("/{agent_id}", response_model=OkResponse)
async def delete_agent(
    agent_id: str,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Delete an agent and clean related task state."""
    service = AgentLifecycleService(session)
    return await service.delete_agent(agent_id=agent_id, ctx=ctx)


# ── T104 (2026-05-24): per-agent workspace MD viewer ────────────────────────
# Surfaces SOUL.md, IDENTITY.md, USER.md, AGENTS.md, MEMORY.md, TOOLS.md,
# HEARTBEAT.md, DREAMS.md, BOOTSTRAP.md from the agent's workspace dir
# (`$OPENCLAW_HOME/workspace-<workspace-id>`). Read-only; edit happens
# via wiki or direct file write — agents own their workspace.

import os as _os
import re as _re
from pathlib import Path as _Path
from fastapi import HTTPException as _HTTPException
from sqlalchemy import text as _text

_OPENCLAW_HOME = _Path(_os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_WORKSPACE_FILE_WHITELIST = {
    "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "MEMORY.md",
    "TOOLS.md", "HEARTBEAT.md", "DREAMS.md", "BOOTSTRAP.md",
    "PERSONALITY.md", "SKILLS.md", "ROLE.md",
    "ORGANIZATION-STRUCTURE-SUMMARY.md",
}


async def _resolve_workspace_dir(session: "AsyncSession", agent_id: str) -> _Path | None:
    """Find the agent's workspace dir.

    openclaw.json `agents.list[*].workspace` is the canonical source. Map
    by `agents.openclaw_session_id` → `agent.id` in JSON → `.workspace`.
    Fallback: `workspace-<openclaw_session_id-derived>` under OPENCLAW_HOME.
    """
    res = await session.execute(_text(
        "SELECT name, openclaw_session_id FROM agents WHERE id = :aid"
    ), {"aid": agent_id})
    row = res.first()
    if not row:
        return None
    name, session_key = row[0], row[1]

    # Try openclaw.json
    cfg_path = _OPENCLAW_HOME / "openclaw.json"
    if cfg_path.exists():
        try:
            import json as _json
            cfg = _json.loads(cfg_path.read_text())
            for cfg_agent in (cfg.get("agents", {}) or {}).get("list", []):
                if not isinstance(cfg_agent, dict):
                    continue
                if cfg_agent.get("name") == name:
                    ws = cfg_agent.get("workspace")
                    if ws:
                        p = _Path(ws)
                        # Map host path → container path if needed.
                        if not p.exists():
                            # If host root path doesn't exist in container,
                            # rewrite under OPENCLAW_HOME.
                            # HOST_OPENCLAW_HOME: the host path of the mounted ~/.openclaw.
                            host_home = _os.environ.get("HOST_OPENCLAW_HOME", "")
                            try:
                                if host_home:
                                    p = _OPENCLAW_HOME / p.relative_to(_Path(host_home))
                            except ValueError:
                                pass
                        if p.exists() and p.is_dir():
                            return p
        except Exception:
            pass

    # Fallback: derive from session_key suffix
    if session_key:
        # session_key e.g. "agent:lead-0000a01c-...:main" or "agent:mc-0000a010-...:main"
        m = _re.match(r"agent:([^:]+)", session_key)
        if m:
            suffix = m.group(1)
            candidate = _OPENCLAW_HOME / f"workspace-{suffix}"
            if candidate.exists():
                return candidate
    return None


# Workspace AGENTS.md carries the agent's own credential (`AUTH_TOKEN=...`) in
# its env block (also quoted, Markdown and any-case header forms); the viewer is
# for reading profiles, not for handing out tokens.
_SECRET_LINE = _re.compile(
    r"(?im)(\b[\w-]*(?:TOKEN|SECRET|API[_-]?KEY|PASSWORD)[\w-]*[`'\"]*\s*[=:]\s*[`'\"]*)[^\s`'\"]+"
)


def _redact_secrets(text_: str) -> str:
    return _SECRET_LINE.sub(r"\1***redacted***", text_)


def _is_within_workspace(ws: _Path, p: _Path) -> bool:
    """True if `p` resolves inside the resolved workspace `ws`.

    Real path-boundary check on resolve() results (never string prefix);
    resolution failure is treated as rejection so symlink escapes to
    other workspaces/credential files are never followed.
    """
    try:
        return p.resolve().is_relative_to(ws.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


@router.get(
    "/{agent_id}/workspace-files",
    summary="List + read agent workspace MD files (SOUL.md, IDENTITY.md, ...)",
)
async def list_workspace_files(
    agent_id: str,
    session: "AsyncSession" = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> dict:
    """Return file metadata + content for every whitelisted MD file in
    the agent's workspace directory. Read-only.

    The frontend renders these as tabs on `/agents/<id>` so the owner
    can see the agent's soul / identity / skills / etc. at a glance.
    Same access as `GET /agents/{id}`: an admin of the agent's organization.
    """
    await AgentLifecycleService(session).get_agent(agent_id=agent_id, ctx=ctx)
    ws = await _resolve_workspace_dir(session, agent_id)
    if ws is None:
        return {"workspace_dir": None, "files": [], "missing_reason": "workspace not found"}

    files = []
    for name in sorted(_WORKSPACE_FILE_WHITELIST):
        p = ws / name
        if not _is_within_workspace(ws, p) or not p.exists() or not p.is_file():
            files.append({"name": name, "exists": False, "content": None, "size": 0})
            continue
        try:
            stat = p.stat()
            text_ = _redact_secrets(p.read_text(errors="replace"))
            # Cap large files for safety
            truncated = False
            if len(text_) > 65_536:
                text_ = text_[:65_536] + "\n\n…[truncated]"
                truncated = True
            files.append({
                "name": name,
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "content": text_,
                "truncated": truncated,
            })
        except Exception as exc:
            files.append({"name": name, "exists": True, "content": None, "error": str(exc)})

    # Also list any *.md files NOT in whitelist (let owner discover extras)
    other_md = []
    try:
        for p in sorted(ws.glob("*.md")):
            if p.name not in _WORKSPACE_FILE_WHITELIST and _is_within_workspace(ws, p):
                other_md.append(p.name)
    except Exception:
        pass

    return {
        "workspace_dir": str(ws),
        "files": files,
        "other_md_files": other_md,
    }


@router.get(
    "/{agent_id}/workspace-files/{filename}",
    summary="Read one workspace file (allows non-whitelisted *.md)",
)
async def get_workspace_file(
    agent_id: str,
    filename: str,
    session: "AsyncSession" = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> dict:
    if not filename.endswith(".md") or "/" in filename or ".." in filename:
        raise _HTTPException(status_code=400, detail="only relative *.md filenames allowed")
    await AgentLifecycleService(session).get_agent(agent_id=agent_id, ctx=ctx)
    ws = await _resolve_workspace_dir(session, agent_id)
    if ws is None:
        raise _HTTPException(status_code=404, detail="workspace not found")
    p = ws / filename
    if not _is_within_workspace(ws, p):
        raise _HTTPException(status_code=400, detail="path escape rejected")
    if not p.exists():
        raise _HTTPException(status_code=404, detail=f"{filename} not found")
    try:
        text_ = _redact_secrets(p.read_text(errors="replace"))
        truncated = False
        if len(text_) > 200_000:
            text_ = text_[:200_000] + "\n\n…[truncated]"
            truncated = True
        return {
            "name": filename,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
            "content": text_,
            "truncated": truncated,
        }
    except Exception as exc:
        raise _HTTPException(status_code=500, detail=str(exc))
