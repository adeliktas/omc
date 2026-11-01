# ruff: noqa: S101
"""Regression: journal writer naive/aware datetime crash (fixed 2026-07-16).

`activity_events.created_at` is `timestamp without time zone`; the writer's
first-run marker was tz-aware, so `ts > latest_ts` raised on every tick and
the daily journal (G15) silently stopped appending. All marker paths must
yield tz-NAIVE UTC datetimes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.services.journal_writer as jw


def test_naive_utc_passthrough_for_naive() -> None:
    ts = datetime(2026, 7, 16, 12, 0, 0)
    assert jw._naive_utc(ts) is ts


def test_naive_utc_converts_aware_to_utc_naive() -> None:
    aware = datetime(2026, 7, 16, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    out = jw._naive_utc(aware)
    assert out.tzinfo is None
    assert out == datetime(2026, 7, 16, 12, 0, 0)


def test_read_marker_normalizes_legacy_aware_value(
    tmp_path: Path, monkeypatch,
) -> None:
    # A marker written by the old code could carry "+00:00" — reading it
    # must come back naive so DB-row comparisons cannot raise.
    marker = tmp_path / ".last_processed_at"
    marker.write_text("2026-07-16T10:00:00+00:00")
    monkeypatch.setattr(jw, "_MARKER_FILE", marker)
    out = jw._read_marker()
    assert out is not None
    assert out.tzinfo is None
    assert out == datetime(2026, 7, 16, 10, 0, 0)


def test_read_marker_naive_value_unchanged(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / ".last_processed_at"
    marker.write_text("2026-07-16T10:00:00")
    monkeypatch.setattr(jw, "_MARKER_FILE", marker)
    out = jw._read_marker()
    assert out == datetime(2026, 7, 16, 10, 0, 0)
    assert out.tzinfo is None


def test_first_run_marker_is_naive() -> None:
    import inspect

    src = inspect.getsource(jw.tick)
    assert "_naive_utc(datetime.now(timezone.utc)" in src


def test_db_ts_comparison_cannot_mix_awareness() -> None:
    # Simulate the exact crash: naive DB ts vs (previously aware) marker.
    naive_db_ts = datetime(2026, 7, 16, 12, 30, 0)
    marker = jw._naive_utc(datetime.now(timezone.utc) - timedelta(hours=1))
    assert isinstance(naive_db_ts > marker, bool)  # must not raise
