"""Bounded controller hold; not a privileged Gateway/media/input-classification guard."""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException

# Boards whose content may only reach local models: PRIVATE_BOARD_IDS, comma-separated (unset: none).
PRIVATE_BOARD_IDS = frozenset(
    b.strip().lower() for b in os.environ.get("PRIVATE_BOARD_IDS", "").split(",") if b.strip()
)
PRIVATE_LEAD_IDS = frozenset(f"lead-{b}" for b in PRIVATE_BOARD_IDS)
PRIVATE_HOLD_DETAIL = (
    "Private-board hold: Gateway lifecycle and content transport are not qualified"
)


def private_gateway_held(board_id: object, session_key: str | None = None) -> bool:
    """Use authenticated board/session metadata, never prompt or artifact prose."""
    return str(board_id).strip().lower() in PRIVATE_BOARD_IDS or targets_private_lead(
        {"sessionKey": session_key}
    )


def require_private_gateway_release(
    board_id: object, session_key: str | None = None
) -> None:
    if private_gateway_held(board_id, session_key):
        raise HTTPException(status_code=409, detail=PRIVATE_HOLD_DETAIL)


def targets_private_lead(params: dict[str, Any] | None) -> bool:
    """Current verified lead only; new board agents are held at creation/lifecycle."""
    for key in ("agentId", "sessionKey", "key", "id", "keys"):
        value = (params or {}).get(key)
        for target in value if isinstance(value, (list, tuple)) else [value]:
            if not isinstance(target, str):
                continue
            target = target.strip().lower()
            parts = target.split(":")
            if target in PRIVATE_LEAD_IDS or (
                len(parts) >= 2 and parts[0] == "agent" and parts[1] in PRIVATE_LEAD_IDS
            ):
                return True
    return False
