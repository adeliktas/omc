"""Deterministic session-key helpers for OpenClaw agents.

Session keys are part of Mission Control's contract with the OpenClaw gateway.
Centralize the string formats here to avoid drift across provisioning, DB workflows,
and API-facing services.
"""

from __future__ import annotations

from uuid import UUID

from app.services.openclaw.constants import AGENT_SESSION_PREFIX
from app.services.openclaw.shared import GatewayAgentIdentity


def gateway_main_session_key(gateway_id: UUID) -> str:
    """Return the deterministic session key for a gateway-main agent."""
    return GatewayAgentIdentity.session_key_for_id(gateway_id)


def board_lead_session_key(board_id: UUID) -> str:
    """Return the deterministic session key for a board lead agent."""
    return f"{AGENT_SESSION_PREFIX}:lead-{board_id}:main"


def board_agent_session_key(agent_id: UUID) -> str:
    """Return the deterministic session key for a non-lead, board-scoped agent."""
    return f"{AGENT_SESSION_PREFIX}:mc-{agent_id}:main"


def board_scoped_session_key(
    *,
    agent_id: UUID,
    board_id: UUID,
    is_board_lead: bool,
) -> str:
    """Return the deterministic session key for a board-scoped agent."""
    if is_board_lead:
        return board_lead_session_key(board_id)
    return board_agent_session_key(agent_id)


def is_gateway_parseable_session_key(key: str | None) -> bool:
    """Structural check mirroring the gateway's `parseAgentSessionKey`
    contract (openclaw >= v2026.6.11): a key that CLAIMS the agent form
    (`agent:` prefix) must have at least 3 non-empty colon-separated parts
    (`agent:<agentId>:<rest>`). A 2-part `agent:mc-<id>` key parses to null
    on the gateway side and the turn is refused with "Malformed agent
    session key" (the 2026-07-06 incident shape) — validate before sending.

    Keys WITHOUT the `agent:` prefix are not provably malformed (plain /
    legacy / bridge session-key shapes exist) and are accepted — this is
    deliberately the smallest safe validator, not a canonical-form check.
    """
    if not key or not isinstance(key, str):
        return False
    if not key.startswith(f"{AGENT_SESSION_PREFIX}:"):
        return True
    parts = key.split(":")
    return len(parts) >= 3 and all(p.strip() for p in parts)


def canonical_session_key_mismatch(
    key: str,
    *,
    agent_id: UUID,
    board_id: UUID | None,
    is_board_lead: bool,
) -> str | None:
    """Return the expected canonical key when `key` claims a canonical MC
    form (`agent:mc-…` / `agent:lead-…`) but does not match the builders;
    None when the key is canonical, unverifiable, or a non-MC form.

    Non-canonical prefixes (gateway `agent:mc-gateway-…` and bridge keys)
    are deliberately left alone — only keys that assert MC/lead ownership
    are compared against the deterministic builders.
    """
    if key.startswith(f"{AGENT_SESSION_PREFIX}:mc-gateway-"):
        return None
    if is_board_lead or key.startswith(f"{AGENT_SESSION_PREFIX}:lead-"):
        if board_id is None:
            return None
        expected = board_lead_session_key(board_id)
        return expected if key != expected else None
    if key.startswith(f"{AGENT_SESSION_PREFIX}:mc-"):
        expected = board_agent_session_key(agent_id)
        return expected if key != expected else None
    return None
