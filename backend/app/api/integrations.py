"""Integrations registry — the tool/skill/MCP inventory surfaced on the Agents
page. Reads the live openclaw config files (mounted at OPENCLAW_HOME) and the
mcporter MCP registry, plus the global agent-default tool/skill knobs from
runtime_settings. Also toggles global MCP servers + skills on/off.

MCP scope note: openclaw MCP servers are GLOBAL (mcporter.json / openclaw.json
`mcp.servers`). Per-agent MCP is modelled elsewhere as per-agent allow/deny of
the MCP tool names (agent.tool_config) — this module owns the GLOBAL registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_user_or_agent
from app.services.runtime_settings import get_setting

router = APIRouter(prefix="/integrations", tags=["integrations"])

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_OPENCLAW_JSON = _OPENCLAW_HOME / "openclaw.json"
_MCPORTER_JSON = _OPENCLAW_HOME / "config" / "mcporter.json"

# Curated built-in tool names so the per-agent allow/deny picker has options
# even before the gateway tool-inventory RPC is wired. Not exhaustive.
_KNOWN_BUILTIN_TOOLS = [
    "web_fetch", "web_search", "exec", "fs_read", "fs_write", "fs_list",
    "edit", "apply_patch", "agent_to_agent", "subagents", "memory_search",
]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: same in-place-then-replace pattern as model_eval so a
    half-written file can never be read by the gateway or the heartbeat
    provisioning loop."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _parse_json_setting(key: str) -> Any:
    raw = (get_setting(key, "") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _mcp_registry() -> list[dict[str, Any]]:
    """MCP servers from mcporter.json. Presence in `mcpServers` = enabled;
    entries parked under `mcpServersDisabled` = disabled (config preserved)."""
    data = _read_json(_MCPORTER_JSON)
    out: list[dict[str, Any]] = []
    for name, cfg in (data.get("mcpServers") or {}).items():
        out.append({
            "name": name,
            "enabled": True,
            "endpoint": (cfg or {}).get("baseUrl") or (cfg or {}).get("command") or "",
            "config": cfg,
        })
    for name, cfg in (data.get("mcpServersDisabled") or {}).items():
        out.append({
            "name": name,
            "enabled": False,
            "endpoint": (cfg or {}).get("baseUrl") or (cfg or {}).get("command") or "",
            "config": cfg,
        })
    out.sort(key=lambda x: x["name"])
    return out


def _skills_registry(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    skills = cfg.get("skills") if isinstance(cfg, dict) else {}
    skills = skills if isinstance(skills, dict) else {}
    entries = skills.get("entries") if isinstance(skills.get("entries"), dict) else {}
    out: list[dict[str, Any]] = []
    for sid, val in entries.items():
        enabled = bool(val.get("enabled", True)) if isinstance(val, dict) else bool(val)
        out.append({"id": sid, "enabled": enabled})
    out.sort(key=lambda x: x["id"])
    return out


@router.get("", summary="List the tool/skill/MCP integrations registry")
async def list_integrations(_actor=Depends(require_user_or_agent)) -> dict[str, Any]:
    cfg = _read_json(_OPENCLAW_JSON)
    tools = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
    return {
        "mcp_servers": _mcp_registry(),
        "skills": _skills_registry(cfg),
        "tools": {
            "policy": {
                "profile": tools.get("profile"),
                "allow": tools.get("allow") or [],
                "deny": tools.get("deny") or [],
            },
            "known_builtins": _KNOWN_BUILTIN_TOOLS,
        },
        "defaults": {
            "tools": _parse_json_setting("AGENT_DEFAULT_TOOLS"),
            "skills": _parse_json_setting("AGENT_DEFAULT_SKILLS"),
        },
    }


@router.patch("/mcp/{name}", summary="Enable/disable a global MCP server")
async def toggle_mcp_server(
    name: str,
    payload: dict[str, Any],
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Body: `{"enabled": bool}`. Moves the entry between `mcpServers` (enabled)
    and `mcpServersDisabled` (disabled) in mcporter.json — config preserved."""
    enabled = bool(payload.get("enabled"))
    data = _read_json(_MCPORTER_JSON)
    if not data:
        raise HTTPException(status_code=404, detail="mcporter.json not found or empty")
    servers = data.get("mcpServers") or {}
    disabled = data.get("mcpServersDisabled") or {}
    if name in servers and not enabled:
        disabled[name] = servers.pop(name)
    elif name in disabled and enabled:
        servers[name] = disabled.pop(name)
    elif name not in servers and name not in disabled:
        raise HTTPException(status_code=404, detail=f"Unknown MCP server: {name}")
    data["mcpServers"] = servers
    data["mcpServersDisabled"] = disabled
    try:
        _MCPORTER_JSON.write_text(json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc
    return {"name": name, "enabled": enabled}


@router.patch("/skills/{skill_id}", summary="Enable/disable a global skill")
async def toggle_skill(
    skill_id: str,
    payload: dict[str, Any],
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Body: `{"enabled": bool}`. Sets `skills.entries[skill_id].enabled` in
    openclaw.json — the same knob the gateway itself writes when a skill is
    enabled from the CLI. Applied by the gateway on its next reload.

    Not a live toggle: the runner never bounces the gateway, so this only
    records intent until the gateway reloads (which it does on its own file
    watch, or on next restart)."""
    enabled = bool(payload.get("enabled"))
    data = _read_json(_OPENCLAW_JSON)
    if not data:
        raise HTTPException(status_code=404, detail="openclaw.json not found or empty")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        data["skills"] = skills
    entries = skills.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        skills["entries"] = entries
    if skill_id not in entries:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_id}")
    entry = entries[skill_id]
    if isinstance(entry, dict):
        entry["enabled"] = enabled
    else:
        entries[skill_id] = {"enabled": enabled}
    try:
        _write_json(_OPENCLAW_JSON, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc
    return {"id": skill_id, "enabled": enabled, "requires_reload": True}


@router.patch("/tools-policy", summary="Update the global tools policy")
async def update_tools_policy(
    payload: dict[str, Any],
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Body: `{"profile": str|null, "allow": [str], "deny": [str]}`. Writes the
    top-level `tools.profile/allow/deny` in openclaw.json. `web`/`exec`
    sub-config is left untouched.

    Distinct from `AGENT_DEFAULT_TOOLS` (agents.defaults.tools): that knob is a
    per-agent default layered by provisioning, while this is the gateway's
    global tools policy. Both are labelled as such in the UI.

    Applied by the gateway on its next reload, not live."""
    data = _read_json(_OPENCLAW_JSON)
    if not data:
        raise HTTPException(status_code=404, detail="openclaw.json not found or empty")
    tools = data.get("tools")
    if not isinstance(tools, dict):
        tools = {}
        data["tools"] = tools

    if "profile" in payload:
        profile = payload.get("profile")
        if profile is None or (isinstance(profile, str) and not profile.strip()):
            tools.pop("profile", None)
        elif isinstance(profile, str):
            tools["profile"] = profile
        else:
            raise HTTPException(status_code=422, detail="profile must be a string or null")
    if "allow" in payload:
        allow = payload.get("allow")
        if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
            raise HTTPException(status_code=422, detail="allow must be a list of strings")
        tools["allow"] = allow
    if "deny" in payload:
        deny = payload.get("deny")
        if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
            raise HTTPException(status_code=422, detail="deny must be a list of strings")
        tools["deny"] = deny

    try:
        _write_json(_OPENCLAW_JSON, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc
    return {
        "policy": {
            "profile": tools.get("profile"),
            "allow": tools.get("allow") or [],
            "deny": tools.get("deny") or [],
        },
        "requires_reload": True,
    }
