"""Validate openclaw.json agent.model refs vs provider_capacity registry.

Codifies STATE.md §3 invariant 1 (split source of truth):
  • provider_capacity table = registry of base_url / api_key / limits / status
  • openclaw.json agent.model.primary prefix (e.g. "vllm/") = agent→provider mapping

Non-blocking: logs warnings only. Called from:
  • api/provider_capacity.py create_provider() — full logging on registry change
  • services/watchdog_loop.py _process_once() — throttled tick check, offline-suppressed
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select as sa_select

from app.core.logging import get_logger
from app.models.provider_capacity import ProviderCapacity

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_CONFIG = _OPENCLAW_HOME / "openclaw.json"

logger = get_logger(__name__)


async def validate_agent_provider_mapping(db) -> list[dict[str, Any]]:
    """Return list of {agent_name, provider_ref, kind, issue} entries for each
    agent.model.* reference in openclaw.json that does not resolve to a row in
    provider_capacity (prefix match before "/"). Empty list = healthy.

    Issue codes:
      • unknown_provider — prefix has no matching provider_capacity row
      • provider_offline — row exists but status == "offline"
    """
    issues: list[dict[str, Any]] = []
    if not _CONFIG.exists():
        return issues
    try:
        cfg = json.loads(_CONFIG.read_text())
    except Exception as exc:
        logger.warning("provider_registry.config_parse_failed err=%s", exc)
        return issues

    res = await db.execute(sa_select(ProviderCapacity))
    rows = res.scalars().all()
    known: dict[str, str] = {r.provider_name: (r.status or "online") for r in rows}

    def _check(agent_name: str, ref: str | None, kind: str) -> None:
        if not ref:
            return
        prefix = ref.split("/", 1)[0] if "/" in ref else ref
        if prefix not in known:
            issues.append({
                "agent_name": agent_name, "provider_ref": ref,
                "kind": kind, "issue": "unknown_provider",
            })
        elif known[prefix] == "offline":
            issues.append({
                "agent_name": agent_name, "provider_ref": ref,
                "kind": kind, "issue": "provider_offline",
            })

    agents_block = cfg.get("agents", {}) or {}
    defaults = (agents_block.get("defaults", {}) or {}).get("model", {}) or {}
    _check("__defaults__", defaults.get("primary"), "primary")
    for fb in defaults.get("fallbacks", []) or []:
        _check("__defaults__", fb, "fallback")

    for agent in agents_block.get("list", []) or []:
        name = agent.get("name", "?")
        model = agent.get("model", {}) or {}
        _check(name, model.get("primary"), "primary")
        for fb in model.get("fallbacks", []) or []:
            _check(name, fb, "fallback")

    return issues


async def warn_on_mapping_issues(db, suppress_offline: bool = False) -> int:
    """Log a WARNING per issue. Returns count of issues actually emitted.

    `suppress_offline=True` filters out provider_offline issues; useful for the
    watchdog tick path where cloud-toggle OFF produces noisy benign offline refs.
    """
    issues = await validate_agent_provider_mapping(db)
    n = 0
    for i in issues:
        if suppress_offline and i["issue"] == "provider_offline":
            continue
        logger.warning(
            "provider_registry.%s agent=%r ref=%r kind=%s",
            i["issue"], i["agent_name"], i["provider_ref"], i["kind"],
        )
        n += 1
    return n
