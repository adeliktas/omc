"""/settings/runtime must not lie about what the system is actually using.

Until 2026-09-22 the page advertised every knob as "owner-editable, applies on
the next tick". In reality most knobs were resolved once from `os.environ` at
module import, so the sidecar override was inert *and* several registry
defaults disagreed with the real code default — the page showed
WATCHDOG_AUTONOMOUS_IDLE_MINUTES = 0 ("0 disables") while the watchdog ran it
at 15.

Two properties are pinned here:

1. Every registered knob is actually resolved through `get_setting`, so an
   owner override reaches the consuming module (`applies=live` → next tick,
   `applies=restart` → next backend start).
2. Every entry carries an `applies` verdict, so the UI can say which is which.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from app.services import runtime_settings


def _entry(key: str) -> dict[str, Any]:
    return next(s for s in runtime_settings.list_settings() if s["key"] == key)


def test_every_entry_declares_how_it_applies() -> None:
    for entry in runtime_settings.list_settings():
        assert entry["applies"] in ("live", "restart"), entry["key"]


def test_live_keys_are_all_registered() -> None:
    registered = {s["key"] for s in runtime_settings.SETTINGS}
    assert runtime_settings._LIVE_KEYS <= registered


@pytest.mark.parametrize(
    ("module", "attr", "key"),
    [
        ("app.services.watchdog_loop", "_ENABLED", "WATCHDOG_ENABLED"),
        ("app.services.watchdog_loop", "_MAX_NUDGES_BEFORE_ESCALATE", "WATCHDOG_MAX_NUDGES"),
        (
            "app.services.watchdog_loop",
            "_STALE_IN_PROGRESS",
            "WATCHDOG_STALE_INPROGRESS_SECONDS",
        ),
        ("app.services.watchdog_loop", "_STALE_REVIEW", "WATCHDOG_STALE_REVIEW_SECONDS"),
        (
            "app.services.watchdog_loop",
            "_AUTONOMOUS_IDLE_MINUTES",
            "WATCHDOG_AUTONOMOUS_IDLE_MINUTES",
        ),
        (
            "app.services.watchdog_loop",
            "_INBOX_VOLUNTEER_GRACE_SECONDS",
            "WATCHDOG_INBOX_VOLUNTEER_GRACE_SECONDS",
        ),
        ("app.services.traffic_etl", "_POLL_INTERVAL", "TRAFFIC_ETL_INTERVAL_SECONDS"),
        (
            "app.services.complexity_assessor",
            "_ASSESSOR_MAX_TOKENS",
            "COMPLEXITY_ASSESSOR_MAX_TOKENS",
        ),
        (
            "app.api.provider_stats",
            "_RECENT_WINDOW_SECONDS",
            "PROVIDER_STATS_WINDOW_SECONDS",
        ),
    ],
)
def test_owner_override_reaches_the_consuming_module(
    module: str,
    attr: str,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override written from the UI must be what the module resolves on its
    next import. Pins the regression: these were `os.environ.get(KEY, literal)`
    at module scope, which ignored the sidecar entirely and drifted from the
    default the page advertised.
    """
    spec = runtime_settings._BY_KEY[key]
    probe: Any = {"int": 4321, "float": 43.21, "bool": None, "str": "probe"}[spec["type"]]
    if probe is None:  # bool: flip whatever the registry default is
        probe = not spec["default"]

    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: {key: probe})
    mod = importlib.reload(importlib.import_module(module))
    try:
        assert getattr(mod, attr) == probe, (
            f"{key}: override {probe!r} did not reach {module}.{attr}"
        )
        assert _entry(key)["value"] == probe
    finally:
        monkeypatch.undo()
        importlib.reload(mod)


def test_live_benchmark_failure_knobs_follow_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two benchmark failure knobs used to be read from module constants,
    so editing them in the UI did nothing at all."""
    from app.services import provider_benchmark

    monkeypatch.setattr(
        runtime_settings,
        "_load_sidecar",
        lambda: {"BENCHMARK_FAILURE_THRESHOLD": 9, "BENCHMARK_FAILURE_WINDOW": 42.0},
    )
    assert provider_benchmark._failure_threshold() == 9
    assert provider_benchmark._failure_window() == 42.0


def test_pipeline_thresholds_follow_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """/pipeline's staleness clocks and WIP limits were hardcoded constants."""
    from app.api import pipeline

    monkeypatch.setattr(
        runtime_settings,
        "_load_sidecar",
        lambda: {"PIPELINE_WIP_LIMIT_WORKER": 99, "PIPELINE_ATLAS_OFFLINE_SECONDS": 7},
    )
    assert pipeline._knob("PIPELINE_WIP_LIMIT_WORKER", 2) == 99
    assert pipeline._atlas_offline() == 7


def test_pipeline_defaults_preserve_the_previous_hardcoded_values() -> None:
    """Registering these knobs must not change what /pipeline reports."""
    from app.api import pipeline

    assert _entry("PIPELINE_STALE_INPROGRESS_SECONDS")["value"] == pipeline._STALE_IN_PROGRESS
    assert _entry("PIPELINE_STALE_REVIEW_SECONDS")["value"] == pipeline._STALE_REVIEW
    assert _entry("PIPELINE_ATLAS_OFFLINE_SECONDS")["value"] == pipeline._ATLAS_OFFLINE
    assert _entry("PIPELINE_WIP_LIMIT_LEAD")["value"] == pipeline._WIP_LIMIT_LEAD
    assert _entry("PIPELINE_WIP_LIMIT_WORKER")["value"] == pipeline._WIP_LIMIT_WORKER
    assert _entry("PIPELINE_WIP_LIMIT_QA")["value"] == pipeline._WIP_LIMIT_QA


def test_live_watchdog_cadence_follows_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import watchdog_loop

    monkeypatch.setattr(
        runtime_settings,
        "_load_sidecar",
        lambda: {"WATCHDOG_INTERVAL": 7, "WATCHDOG_IDLE_INTERVAL": 11},
    )
    assert watchdog_loop._active_interval() == 7
    assert watchdog_loop._idle_interval() == 11
