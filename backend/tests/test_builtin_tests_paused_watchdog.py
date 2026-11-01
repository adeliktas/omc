"""Built-in checks skip only a watchdog that is actually not running."""

from types import SimpleNamespace

import pytest

import app.services.runtime_settings as runtime_settings
from app.api import tests as tests_api
from app.services import watchdog_loop

IDS = ["system.watchdog_recent", "pipeline.repeat_reset_loop_alive"]


@pytest.mark.parametrize("test_id", IDS)
async def test_disabled_running_loop_is_skipped_with_its_reason(monkeypatch, test_id):
    monkeypatch.setattr(watchdog_loop, "_ENABLED", False)
    status, detail = await tests_api._run_auto(SimpleNamespace(id=test_id), db=None)
    assert status == "skipped" and "WATCHDOG_ENABLED" in detail


async def test_restart_pending_disable_does_not_hide_a_dead_running_loop(monkeypatch):
    # Saved setting says false, but the loop was started enabled and is still the
    # one that must tick (independent QA finding, 2026-10-06).
    monkeypatch.setattr(watchdog_loop, "_ENABLED", True)
    original = runtime_settings.get_setting
    monkeypatch.setattr(runtime_settings, "get_setting",
                        lambda key, *a: False if key == "WATCHDOG_ENABLED" else original(key, *a))

    class NoTicks:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(first=lambda: None)

    status, detail = await tests_api._run_auto(SimpleNamespace(id=IDS[0]), db=NoTicks())
    assert status == "fail" and "no watchdog.tick" in detail
