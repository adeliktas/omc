# ruff: noqa: INP001
"""Containment regressions for the agent workspace MD viewer.

The viewer must never read anything outside the agent's authorized
workspace directory. Two escape classes are covered here:

* a symlink inside the workspace pointing at a *same-prefix sibling*
  directory (``ws`` vs ``ws-other``): the historic
  ``str(p.resolve()).startswith(str(ws.resolve()))`` check passes for
  such siblings because ``.../ws-other/...`` has ``.../ws`` as a string
  prefix, so a naive prefix comparison is not a path-boundary check;
* ordinary valid files inside the workspace must keep working, with
  secret redaction applied;
* organization authorization stays in front of any filesystem access.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.api.agents as agents_api


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture()
def ws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A workspace ``ws`` plus a same-prefix sibling ``ws-other`` under one root."""
    root = tmp_path / "openclaw"
    ws = root / "ws"
    sibling = root / "ws-other"
    ws.mkdir(parents=True)
    sibling.mkdir(parents=True)

    # Harmless fixture content, plus a credential-like file in the sibling
    # workspace that must never be reachable through ``ws``.
    (ws / "SOUL.md").write_text("# Soul\nStay kind.\n")
    (ws / "NOTES.md").write_text("## Environment\nAUTH_TOKEN=fake-plain-token\n")
    (sibling / "SECRET.md").write_text(
        "GATEWAY_TOKEN=sibling-credential-do-not-leak\n"
        "This line lives in the sibling workspace marker-0f831.\n"
    )

    async def allow(*_a: object, **_k: object) -> None:
        return None

    async def fake_resolve(_session: object, _agent_id: str) -> Path:
        return ws

    # Authorization is exercised separately; here it is permissive.
    monkeypatch.setattr(agents_api.AgentLifecycleService, "get_agent", allow)
    monkeypatch.setattr(agents_api, "_resolve_workspace_dir", fake_resolve)
    return {"ws": ws, "sibling": sibling}


def test_get_rejects_symlink_into_same_prefix_sibling(ws_env: dict[str, Path]) -> None:
    """A symlink from ``ws`` into ``ws-other`` must be rejected, not read.

    Fails on baseline: the string-prefix check lets ``.../ws-other/x.md``
    through because it starts with ``.../ws``.
    """
    ws = ws_env["ws"]
    os.symlink(ws_env["sibling"] / "SECRET.md", ws / "leak.md")

    with pytest.raises(HTTPException) as exc:
        _run(agents_api.get_workspace_file("a", "leak.md", session=None, ctx=None))
    assert exc.value.status_code in (400, 403)


def test_get_rejects_symlink_to_credential_file_outside_workspace(
    ws_env: dict[str, Path],
) -> None:
    """Symlink escape to an arbitrary outside file must not be readable."""
    ws = ws_env["ws"]
    outside = ws_env["sibling"].parent / "creds.env"
    outside.write_text("AUTH_TOKEN=top-secret-outside\n")
    os.symlink(outside, ws / "stolen.md")

    with pytest.raises(HTTPException):
        _run(agents_api.get_workspace_file("a", "stolen.md", session=None, ctx=None))


def test_list_does_not_expose_symlinked_sibling_content(ws_env: dict[str, Path]) -> None:
    """Whitelisted names that are symlinks out of the workspace must not be
    followed by the listing endpoint.

    Fails on baseline: ``list_workspace_files`` reads through the symlink
    and returns the sibling credential in ``content``.
    """
    ws = ws_env["ws"]
    os.symlink(ws_env["sibling"] / "SECRET.md", ws / "MEMORY.md")

    out = _run(agents_api.list_workspace_files("a", session=None, ctx=None))
    assert isinstance(out, dict)
    for entry in out["files"]:
        if entry["name"] == "MEMORY.md":
            assert entry.get("content") is None or "sibling-credential" not in (
                entry.get("content") or ""
            ), "list must not follow a symlink escaping the workspace"
            assert entry.get("exists") is not True or (
                "sibling-credential" not in (entry.get("content") or "")
            )
    assert "sibling-credential" not in str(out)
    assert "marker-0f831" not in str(out), (
        "list must not read file contents from outside the workspace"
    )


def test_valid_files_still_read_with_redaction(ws_env: dict[str, Path]) -> None:
    """Ordinary valid MD files keep working; redaction still applies."""
    ws = ws_env["ws"]
    (ws / "IDENTITY.md").write_text("## Environment\nAUTH_TOKEN=agent-real-token\n")

    listing = _run(agents_api.list_workspace_files("a", session=None, ctx=None))
    assert isinstance(listing, dict)
    assert listing["workspace_dir"] == str(ws)
    by_name = {e["name"]: e for e in listing["files"]}
    assert by_name["SOUL.md"]["exists"] is True
    assert "Stay kind." in by_name["SOUL.md"]["content"]
    assert "agent-real-token" not in by_name["IDENTITY.md"]["content"]
    assert "***redacted***" in by_name["IDENTITY.md"]["content"]
    assert "NOTES.md" in listing["other_md_files"]

    single = _run(agents_api.get_workspace_file("a", "NOTES.md", session=None, ctx=None))
    assert isinstance(single, dict)
    assert single["name"] == "NOTES.md"
    assert "fake-plain-token" not in single["content"]
    assert "***redacted***" in single["content"]


def test_foreign_organization_denial_precedes_workspace_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization stays in front: another org's agent is a 404 before any
    workspace path is resolved or read."""
    calls: list[str] = []

    async def deny(self: object, *, agent_id: str, ctx: object) -> None:
        calls.append(agent_id)
        raise HTTPException(status_code=404, detail="agent not found")

    async def must_not_resolve(*_a: object, **_k: object) -> Path:
        raise AssertionError("workspace resolved without an access check")

    monkeypatch.setattr(agents_api.AgentLifecycleService, "get_agent", deny)
    monkeypatch.setattr(agents_api, "_resolve_workspace_dir", must_not_resolve)

    for call in (
        agents_api.list_workspace_files("a", session=None, ctx=None),
        agents_api.get_workspace_file("a", "SOUL.md", session=None, ctx=None),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(call)
        assert exc.value.status_code == 404
    assert calls == ["a", "a"]
