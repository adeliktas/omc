"""Agent authentication helpers for token-backed API access.

This module is used for *agent-originated* API calls (as opposed to human users).

Key ideas:
- Agents authenticate with an opaque token presented as `X-Agent-Token: <token>`.
- For convenience, some deployments may also allow `Authorization: Bearer <token>`
  for agents (controlled by caller/dependency).
- To reduce write-amplification, we only touch `Agent.last_seen_at` at a fixed
  interval and we avoid touching it for safe/read-only HTTP methods.

This is intentionally separate from user authentication (Clerk/local bearer token)
so we can evolve agent policy independently.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import col, select

from app.core.agent_tokens import verify_agent_token
from app.core.client_ip import get_client_ip
from app.core.logging import get_logger
from app.core.rate_limit import agent_auth_limiter
from app.core.time import utcnow
from app.db.session import get_session
from app.models.agents import Agent

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = get_logger(__name__)

_LAST_SEEN_TOUCH_INTERVAL = timedelta(seconds=30)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_DEP = Depends(get_session)


@dataclass
class AgentAuthContext:
    """Authenticated actor payload for agent-originated requests."""

    actor_type: Literal["agent"]
    agent: Agent


# sha256(token) -> (agent id, the stored hash it verified against). Every agent
# call used to PBKDF2-verify (~24 ms each) against every agent with a hash,
# retired ones included: ~1.5 s CPU per request with 64 rows. A rotated token
# changes the stored hash, so a stale entry can never authenticate.
# ponytail: per-process and cleared wholesale at 1000 entries; fine for tens of agents.
_TOKEN_CACHE: dict[str, tuple[object, str]] = {}


def _token_cached(token: str) -> bool:
    """A token already verified this process: the rate limit is for unknown tokens.

    Every gateway agent calls from the same Docker bridge IP, so a per-IP limit on
    all calls was one shared 20/min ceiling for the whole fleet.
    """
    return hashlib.sha256(token.encode()).hexdigest() in _TOKEN_CACHE


async def _find_agent_for_token(session: AsyncSession, token: str) -> Agent | None:
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _TOKEN_CACHE.get(key)
    if hit is not None:
        cached = await session.get(Agent, hit[0])
        if cached is not None and cached.agent_token_hash == hit[1] and cached.status != "retired":
            return cached
        _TOKEN_CACHE.pop(key, None)
    found = await _scan_agents_for_token(session, token)
    if found is not None and found.agent_token_hash:
        if len(_TOKEN_CACHE) >= 1000:
            _TOKEN_CACHE.clear()
        _TOKEN_CACHE[key] = (found.id, found.agent_token_hash)
    return found


async def _scan_agents_for_token(session: AsyncSession, token: str) -> Agent | None:
    # Fetch candidate agents on the event loop (the async session must stay on
    # the loop), then offload the CPU-bound PBKDF2 verification scan to a worker
    # thread. Each `verify_agent_token` is ~tens of ms (PBKDF2 200k iterations);
    # doing this inline blocks the event loop for ~N*verify under concurrent
    # agent load, which stalls every other request and is a trivial
    # unauthenticated DoS amplifier. Moving the whole scan into one `to_thread`
    # call keeps auth semantics identical while keeping the loop responsive.
    agents = list(
        await session.exec(
            select(Agent)
            .where(col(Agent.agent_token_hash).is_not(None))
            .where(col(Agent.status) != "retired"),
        ),
    )
    candidates = [
        (agent, agent.agent_token_hash) for agent in agents if agent.agent_token_hash
    ]
    return await asyncio.to_thread(_verify_token_against_agents, token, candidates)


def _verify_token_against_agents(
    token: str,
    candidates: list[tuple[Agent, str]],
) -> Agent | None:
    """Synchronous PBKDF2 scan; run off the event loop via ``asyncio.to_thread``."""
    for agent, stored_hash in candidates:
        if verify_agent_token(token, stored_hash):
            return agent
    return None


def _resolve_agent_token(
    agent_token: str | None,
    authorization: str | None,
    *,
    accept_authorization: bool = True,
) -> str | None:
    if agent_token:
        return agent_token
    if not accept_authorization:
        return None
    if not authorization:
        return None
    value = authorization.strip()
    if not value:
        return None
    if value.lower().startswith("bearer "):
        return value.split(" ", 1)[1].strip() or None
    return None


async def _touch_agent_presence(
    request: Request,
    session: AsyncSession,
    agent: Agent,
) -> None:
    """Best-effort update of last_seen/status for any authenticated agent request.

    Heartbeats are the primary presence mechanism, but agents may still make API
    calls (task comments, memory updates, etc). Touch presence so the UI reflects
    real activity even if the heartbeat loop isn't running.
    """
    now = utcnow()
    if (
        agent.last_seen_at is not None
        and now - agent.last_seen_at < _LAST_SEEN_TOUCH_INTERVAL
    ):
        return

    agent.last_seen_at = now
    agent.updated_at = now
    if agent.status not in {"updating", "deleting"}:
        agent.status = "online"
    session.add(agent)

    # For safe HTTP methods, endpoints typically do not commit. Persist the touch
    # so agents that only poll/read still show as online.
    if request.method.upper() in _SAFE_METHODS:
        await session.commit()


async def get_agent_auth_context(
    request: Request,
    agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = SESSION_DEP,
) -> AgentAuthContext:
    """Require and validate agent auth token from request headers."""
    resolved = _resolve_agent_token(
        agent_token,
        authorization,
        accept_authorization=True,
    )
    if resolved and not _token_cached(resolved):
        if not await agent_auth_limiter.is_allowed(get_client_ip(request)):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    if not resolved:
        logger.warning(
            "agent auth missing token path=%s x_agent=%s authorization=%s",
            request.url.path,
            bool(agent_token),
            bool(authorization),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    agent = await _find_agent_for_token(session, resolved)
    if agent is None:
        logger.warning(
            "agent auth invalid token path=%s token_prefix=%s",
            request.url.path,
            resolved[:6],
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_TOKEN_DETAIL)
    await _touch_agent_presence(request, session, agent)
    return AgentAuthContext(actor_type="agent", agent=agent)


# Agents kept retrying with a token recalled from session memory after it had
# been rotated, and logged the 401s as a backend outage (lead, 2026-10-04).
INVALID_TOKEN_DETAIL = (
    "Token not recognised. Agent tokens are rotated: re-read AUTH_TOKEN from AGENTS.md "
    "in your workspace and retry. A token recalled from memory or an earlier session may be revoked."
)


async def get_agent_auth_context_optional(
    request: Request,
    agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = SESSION_DEP,
) -> AgentAuthContext | None:
    """Optionally resolve agent auth context from `X-Agent-Token` or `Authorization: Bearer`.

    Both `X-Agent-Token` and `Authorization: Bearer <token>` are accepted so that
    routes depending on this function (e.g. board/task dependency resolvers) behave
    consistently with `get_agent_auth_context`, which also accepts both headers.
    Previously, `accept_authorization=False` caused 401 on any route that resolved
    a board or task via the shared `ACTOR_DEP` chain (e.g. PATCH /tasks/{id},
    POST /tasks/{id}/comments) when the caller used `Authorization: Bearer`.
    """
    resolved = _resolve_agent_token(
        agent_token,
        authorization,
        accept_authorization=True,
    )
    if not resolved:
        if agent_token:
            logger.warning(
                "agent auth optional missing token path=%s x_agent=%s authorization=%s",
                request.url.path,
                bool(agent_token),
                bool(authorization),
            )
        return None
    # Rate-limit any request that is actually attempting agent auth on the
    # optional path. Shared user/agent dependencies resolve user auth first.
    if not _token_cached(resolved) and not await agent_auth_limiter.is_allowed(
        get_client_ip(request),
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    agent = await _find_agent_for_token(session, resolved)
    if agent is None:
        logger.warning(
            "agent auth optional invalid token path=%s token_prefix=%s",
            request.url.path,
            resolved[:6],
        )
        # The only caller (require_user_or_agent) answers 401 anyway; say why.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_TOKEN_DETAIL)
    await _touch_agent_presence(request, session, agent)
    return AgentAuthContext(actor_type="agent", agent=agent)
