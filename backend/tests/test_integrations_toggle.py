"""Tests for the integrations skills + tools-policy toggles.

These endpoints write openclaw.json directly (the same file the gateway
owns), so the tests pin three properties:

- toggling a skill flips only that entry and preserves every other key;
- the tools-policy write touches only profile/allow/deny, leaving the
  load-bearing `web`/`exec` sub-config and all other top-level keys intact;
- unknown skills 404 instead of silently creating entries.

The module under test resolves its paths at import time; the tests swap the
`_OPENCLAW_JSON` path for a temp file so nothing touches the real gateway
config.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app.api import integrations as mod


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data))


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "openclaw.json"
    _write(p, {
        "tools": {
            "profile": "full",
            "allow": [],
            "deny": ["Bash"],
            "web": {"search": {"enabled": True}},
            "exec": {"host": "gateway", "mode": "full"},
        },
        "skills": {
            "allowBundled": ["clawhub"],
            "entries": {
                "caveman": {"enabled": True},
                "mcporter": {"enabled": True},
                "memory-merger": {"enabled": False},
            },
        },
        "agents": {"defaults": {"model": {"primary": "omniroute/omniroute-auto"}}},
    })
    monkeypatch.setattr(mod, "_OPENCLAW_JSON", p)
    return p


async def test_toggle_skill_flips_only_target(cfg_path: Path) -> None:
    await mod.toggle_skill("caveman", {"enabled": False}, _actor=object())
    data = json.loads(cfg_path.read_text())
    assert data["skills"]["entries"]["caveman"]["enabled"] is False
    # untouched
    assert data["skills"]["entries"]["mcporter"]["enabled"] is True
    assert data["skills"]["entries"]["memory-merger"]["enabled"] is False
    assert data["skills"]["allowBundled"] == ["clawhub"]
    assert data["tools"]["profile"] == "full"
    assert data["agents"]["defaults"]["model"]["primary"] == "omniroute/omniroute-auto"

    # idempotent: re-enable back to original
    await mod.toggle_skill("caveman", {"enabled": True}, _actor=object())
    data = json.loads(cfg_path.read_text())
    assert data["skills"]["entries"]["caveman"]["enabled"] is True


async def test_toggle_skill_unknown_404s(cfg_path: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        await mod.toggle_skill("nope", {"enabled": True}, _actor=object())
    assert exc.value.status_code == 404
    # file untouched
    data = json.loads(cfg_path.read_text())
    assert "nope" not in data["skills"]["entries"]


async def test_tools_policy_round_trip_preserves_subconfig(cfg_path: Path) -> None:
    result = await mod.update_tools_policy(
        {"profile": "restricted", "allow": ["Read", "Write"], "deny": ["Bash", "exec"]},
        _actor=object(),
    )
    assert result["policy"]["profile"] == "restricted"
    assert result["policy"]["allow"] == ["Read", "Write"]
    assert result["policy"]["deny"] == ["Bash", "exec"]
    assert result["requires_reload"] is True

    data = json.loads(cfg_path.read_text())
    tools = data["tools"]
    assert tools["profile"] == "restricted"
    assert tools["allow"] == ["Read", "Write"]
    assert tools["deny"] == ["Bash", "exec"]
    # load-bearing sub-config untouched
    assert tools["web"]["search"]["enabled"] is True
    assert tools["exec"] == {"host": "gateway", "mode": "full"}
    # skills untouched
    assert data["skills"]["entries"]["caveman"]["enabled"] is True


async def test_tools_policy_clears_profile_on_null(cfg_path: Path) -> None:
    await mod.update_tools_policy({"profile": None}, _actor=object())
    data = json.loads(cfg_path.read_text())
    assert "profile" not in data["tools"]
    # web/exec still present
    assert data["tools"]["web"]["search"]["enabled"] is True
    assert data["tools"]["exec"]["host"] == "gateway"


async def test_tools_policy_rejects_non_list_allow(cfg_path: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        await mod.update_tools_policy({"allow": "Read,Write"}, _actor=object())
    assert exc.value.status_code == 422
