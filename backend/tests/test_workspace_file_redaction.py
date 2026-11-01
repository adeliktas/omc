"""The agent profile viewer must not hand out the agent's credential."""

from app.api.agents import _redact_secrets


def test_env_block_secrets_are_masked_and_prose_kept():
    text = (
        "## Environment\n"
        "- `AUTH_TOKEN=abc123XYZ`\n"
        "- `BASE_URL=http://mc:8000`\n"
        "OPENCLAW_GATEWAY_TOKEN: deadbeef\n"
        "Never print your token to the board.\n"
    )
    out = _redact_secrets(text)
    assert "abc123XYZ" not in out and "deadbeef" not in out
    assert "`AUTH_TOKEN=***redacted***`" in out
    assert "BASE_URL=http://mc:8000" in out
    assert "Never print your token to the board." in out


def test_quoted_markdown_and_mixed_case_header_forms_are_masked():
    cases = {
        'AUTH_TOKEN="fake-value"': 'AUTH_TOKEN="***redacted***"',
        "AUTH_TOKEN='fake-value'": "AUTH_TOKEN='***redacted***'",
        "- `AUTH_TOKEN`: fake-value": "- `AUTH_TOKEN`: ***redacted***",
        "`X-Agent-Token: fake-value`": "`X-Agent-Token: ***redacted***`",
        "x-AGENT-token: fake-value": "x-AGENT-token: ***redacted***",
    }
    for text, expected in cases.items():
        assert _redact_secrets(text) == expected


def test_workspace_routes_check_org_access_before_reading(monkeypatch):
    """Another organization's agent is a 404 before its workspace is touched."""
    import asyncio

    import pytest
    from fastapi import HTTPException

    import app.api.agents as agents_api

    async def deny(self, *, agent_id, ctx):
        raise HTTPException(status_code=404)

    async def must_not_read(*_a, **_k):
        raise AssertionError("workspace resolved without an access check")

    monkeypatch.setattr(agents_api.AgentLifecycleService, "get_agent", deny)
    monkeypatch.setattr(agents_api, "_resolve_workspace_dir", must_not_read)
    for call in (
        agents_api.list_workspace_files("a", session=None, ctx=None),
        agents_api.get_workspace_file("a", "SOUL.md", session=None, ctx=None),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == 404
