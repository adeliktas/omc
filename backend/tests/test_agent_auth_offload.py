# ruff: noqa: INP001, SLF001
"""Event-loop liveness regression for agent token verification.

`_find_agent_for_token` performs an O(N) PBKDF2(200k) scan. Doing that work
inline on the event loop blocks every other coroutine for ~N*verify. The fix
offloads the scan to a worker thread via ``asyncio.to_thread``; this test proves
the loop stays responsive while verification runs.

Note: the O(1) lookup improvement (single verify) is a separate, migration-gated
concern documented in ``test_agent_auth_token_lookup_regression.py`` (xfail).
This test only asserts the *blocking* defect is fixed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core import agent_auth
from app.core.agent_tokens import hash_agent_token


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_token_scan() -> None:
    # A real PBKDF2 hash so the verify path does genuine CPU work, but it never
    # matches the probe token, forcing a full scan over many candidates.
    real_hash = hash_agent_token("a-real-but-unrelated-agent-token")

    class _FakeSession:
        async def exec(self, _stmt: object) -> list[object]:
            return [SimpleNamespace(agent_token_hash=real_hash) for _ in range(20)]

    # A concurrent loop timer: it should complete in roughly its sleep duration,
    # not be delayed by the blocking verify scan. With inline PBKDF2 over 20
    # candidates (~27ms each) the timer would be delayed by ~0.5s.
    sleep_for = 0.05

    async def _loop_timer() -> float:
        loop_start = time.monotonic()
        await asyncio.sleep(sleep_for)
        return time.monotonic() - loop_start

    timer_task = asyncio.ensure_future(_loop_timer())
    # Yield once so the timer is scheduled before we start the heavy scan.
    await asyncio.sleep(0)
    await agent_auth._find_agent_for_token(_FakeSession(), "invalid-probe")  # type: ignore[arg-type]
    elapsed = await timer_task

    # The timer must have completed within a small tolerance of its target. If
    # the verify scan had blocked the loop, elapsed would be dominated by the
    # ~0.5s of synchronous PBKDF2 work instead of the 0.05s sleep.
    assert elapsed < sleep_for + 0.15, (
        f"event loop was blocked during token scan: timer took {elapsed:.3f}s "
        f"for a {sleep_for}s sleep"
    )


@pytest.mark.asyncio
async def test_find_agent_for_token_still_returns_matching_agent() -> None:
    # Semantics unchanged: a matching token still resolves to its agent.
    token = "correct-horse-battery-staple"
    real_hash = hash_agent_token(token)
    other_hash = hash_agent_token("a-different-token")
    matching = SimpleNamespace(agent_token_hash=real_hash, id="agent-match")

    class _FakeSession:
        async def exec(self, _stmt: object) -> list[object]:
            return [
                SimpleNamespace(agent_token_hash=other_hash, id="agent-other"),
                matching,
            ]

    out = await agent_auth._find_agent_for_token(_FakeSession(), token)  # type: ignore[arg-type]
    assert out is matching
