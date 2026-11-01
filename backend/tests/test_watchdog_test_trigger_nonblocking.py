"""The watchdog's self-call to /tests/run must not block the backend's own event loop."""

import asyncio
import time

import pytest

from app.services import watchdog_loop


@pytest.mark.asyncio
async def test_test_trigger_does_not_block_event_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_AUTH_TOKEN", "x" * 50)
    monkeypatch.setattr(watchdog_loop, "_TESTS_DB", tmp_path / "tests.db")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"summary": {"pass": 1}}'

    def slow_urlopen(req, timeout):
        time.sleep(0.5)  # stands in for the request the event loop itself must serve
        return Resp()

    monkeypatch.setattr(watchdog_loop.urllib.request, "urlopen", slow_urlopen)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        assert await watchdog_loop._trigger_test_run() == 1
    finally:
        task.cancel()
    assert ticks >= 10  # the loop kept serving while the trigger waited
