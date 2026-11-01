"""Repeated calls with a valid agent token skip the PBKDF2 scan; rotation invalidates."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import agent_auth


@pytest.mark.asyncio
async def test_valid_token_is_verified_once_and_rotation_invalidates(monkeypatch):
    agent = SimpleNamespace(id=uuid4(), agent_token_hash="h1", status="online")

    class _Session:
        async def exec(self, _stmt):
            return [agent] if agent.status != "retired" else []  # SQL filters retired

        async def get(self, _model, _id):
            return agent

    calls = {"n": 0}

    def _verify(token, stored):
        calls["n"] += 1
        return token == "tok" and stored == "h1"  # "h1" is the hash of "tok"

    monkeypatch.setattr(agent_auth, "verify_agent_token", _verify)
    agent_auth._TOKEN_CACHE.clear()
    assert await agent_auth._find_agent_for_token(_Session(), "tok") is agent
    assert await agent_auth._find_agent_for_token(_Session(), "tok") is agent
    assert calls["n"] == 1  # second call served from the cache

    agent.agent_token_hash = "h2"  # token rotated: old token must stop working
    assert await agent_auth._find_agent_for_token(_Session(), "tok") is None

    agent.agent_token_hash = "h1"
    assert await agent_auth._find_agent_for_token(_Session(), "tok") is agent  # cached again
    agent.status = "retired"  # retired after caching: must not authenticate
    assert await agent_auth._find_agent_for_token(_Session(), "tok") is None


@pytest.mark.asyncio
async def test_verified_tokens_bypass_the_shared_rate_limit(monkeypatch):
    """All gateway agents share one bridge IP; a per-IP limit on every call throttled the fleet."""
    from fastapi import HTTPException
    from unittest.mock import AsyncMock

    agent = SimpleNamespace(id=uuid4(), agent_token_hash="h1", status="online", last_seen_at=None)

    class _Session:
        async def exec(self, _stmt):
            return [agent]

        async def get(self, _model, _id):
            return agent

    monkeypatch.setattr(agent_auth, "verify_agent_token", lambda t, h: t == "tok" and h == "h1")
    monkeypatch.setattr(agent_auth, "_touch_agent_presence", AsyncMock())
    monkeypatch.setattr(agent_auth.agent_auth_limiter, "is_allowed", AsyncMock(return_value=False))
    agent_auth._TOKEN_CACHE.clear()
    request = SimpleNamespace(client=SimpleNamespace(host="172.18.0.1"), headers={}, url=SimpleNamespace(path="/x"), method="GET")

    with pytest.raises(HTTPException) as unknown:  # unverified token: limited
        await agent_auth.get_agent_auth_context(request, agent_token="tok", authorization=None, session=_Session())
    assert unknown.value.status_code == 429

    await agent_auth._find_agent_for_token(_Session(), "tok")  # verified once (e.g. before the burst)
    ctx = await agent_auth.get_agent_auth_context(request, agent_token="tok", authorization=None, session=_Session())
    assert ctx.agent is agent  # cached token is not rate limited
