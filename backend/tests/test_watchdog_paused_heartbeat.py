"""Pausing the watchdog stops automation, not the gateway-liveness heartbeat.

2026-10-06: with WATCHDOG_ENABLED=false the heartbeat file went stale, so the hourly
health report said "critical" and the host gateway watchdog lost its restart guard.
"""

import asyncio

import pytest

from app.services import watchdog_loop as wl


async def test_paused_watchdog_keeps_writing_the_heartbeat(monkeypatch):
    beats: list[bool] = []
    monkeypatch.setattr(wl, "_ENABLED", False)
    monkeypatch.setattr(wl, "_gateway_alive", lambda: True)
    monkeypatch.setattr(wl, "_write_gateway_heartbeat", beats.append)

    async def no_task_actions():
        raise AssertionError("a paused watchdog must not run task/agent steps")

    monkeypatch.setattr(wl, "_process_once", no_task_actions)
    real_sleep = asyncio.sleep

    async def stop_after_two(_seconds):
        if len(beats) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(wl.asyncio, "sleep", stop_after_two)
    with pytest.raises(asyncio.CancelledError):
        await wl.watchdog_loop()
    assert beats == [True, True]
