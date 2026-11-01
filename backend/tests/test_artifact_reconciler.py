# ruff: noqa: S101
"""Patch 1 (2026-07-16): declared-artifact (`;artifact=`) helpers.

Report-only file-vs-claim reconciler. These
tests cover the pure pieces (declaration parsing, untrusted-input
validation, filesystem evaluation under a tmp workspace root); the SQL
step itself is Postgres-only and exercised in production behind the same
try/except-per-task pattern as every other watchdog step.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.services.watchdog_loop as wl


# ── declaration parsing ─────────────────────────────────────────────────────


def test_parse_absent_returns_none() -> None:
    assert wl._parse_artifact_declaration(None) is None
    assert wl._parse_artifact_declaration("poem-cron;role=QA Engineer") is None


def test_parse_extracts_value() -> None:
    assert (
        wl._parse_artifact_declaration("cron;artifact=workspace-lead-x/out.md")
        == "workspace-lead-x/out.md"
    )


def test_parse_stops_at_next_semicolon_key() -> None:
    assert (
        wl._parse_artifact_declaration("cron;artifact=a/b.md;role=QA Engineer")
        == "a/b.md"
    )


def test_parse_empty_value_is_none() -> None:
    assert wl._parse_artifact_declaration("cron;artifact=;role=X") is None


def test_role_pin_parse_stops_at_artifact_key() -> None:
    # Companion contract: `;role=QA Engineer;artifact=...` must not swallow
    # the artifact declaration into the role hint (routing side).
    auto_reason = "cron;role=QA Engineer;artifact=a/b.md"
    hint = auto_reason.split(";role=", 1)[1].strip().split(";", 1)[0].strip()
    assert hint == "QA Engineer"


# ── untrusted-input validation ──────────────────────────────────────────────


def test_validate_accepts_relative_glob() -> None:
    assert wl._validate_artifact_pattern("workspace-lead-x/poems.md") is None
    assert wl._validate_artifact_pattern("workspace-*/reports/*.md") is None


def test_validate_rejects_absolute_and_home() -> None:
    assert wl._validate_artifact_pattern("/etc/passwd") is not None
    assert wl._validate_artifact_pattern("~/secrets.txt") is not None


def test_validate_rejects_traversal() -> None:
    assert wl._validate_artifact_pattern("../outside.md") is not None
    assert wl._validate_artifact_pattern("a/../../b.md") is not None


def test_validate_rejects_control_chars_and_backslash() -> None:
    assert wl._validate_artifact_pattern("a\x00b.md") is not None
    assert wl._validate_artifact_pattern("dir\\file.md") is not None


def test_validate_rejects_oversized() -> None:
    assert wl._validate_artifact_pattern("x/" * 300 + "f.md") is not None


def test_validate_rejects_empty() -> None:
    assert wl._validate_artifact_pattern("") is not None


# ── filesystem evaluation ───────────────────────────────────────────────────


def _touch(path: Path, when: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content\n")
    if when is not None:
        ts = when.timestamp()
        os.utime(path, (ts, ts))


def test_evaluate_missing(tmp_path: Path) -> None:
    verdict, _ = wl._evaluate_artifact(tmp_path, "ws/out.md", None)
    assert verdict == "missing"


def test_evaluate_fresh_ok(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _touch(tmp_path / "ws" / "out.md", now)
    verdict, _ = wl._evaluate_artifact(
        tmp_path, "ws/out.md", now - timedelta(hours=1)
    )
    assert verdict == "ok"


def test_evaluate_stale(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _touch(tmp_path / "ws" / "out.md", now - timedelta(hours=5))
    verdict, detail = wl._evaluate_artifact(
        tmp_path, "ws/out.md", now - timedelta(hours=1)
    )
    assert verdict == "stale"
    assert "predates cycle start" in detail


def test_evaluate_no_since_is_existence_only(tmp_path: Path) -> None:
    # Without a cycle boundary, an old file is still 'ok' — no false stale.
    _touch(
        tmp_path / "ws" / "out.md",
        datetime.now(timezone.utc) - timedelta(days=30),
    )
    verdict, _ = wl._evaluate_artifact(tmp_path, "ws/out.md", None)
    assert verdict == "ok"


def test_evaluate_glob_picks_newest(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _touch(tmp_path / "ws" / "old.md", now - timedelta(hours=9))
    _touch(tmp_path / "ws" / "new.md", now)
    verdict, detail = wl._evaluate_artifact(
        tmp_path, "ws/*.md", now - timedelta(hours=1)
    )
    assert verdict == "ok"
    assert "new.md" in detail


def test_evaluate_naive_since_treated_as_utc(tmp_path: Path) -> None:
    # Postgres may hand back tz-naive datetimes; must not raise.
    now = datetime.now(timezone.utc)
    _touch(tmp_path / "ws" / "out.md", now)
    naive_since = (now - timedelta(hours=1)).replace(tzinfo=None)
    verdict, _ = wl._evaluate_artifact(tmp_path, "ws/out.md", naive_since)
    assert verdict == "ok"


def test_evaluate_symlink_escape_is_missing(tmp_path: Path) -> None:
    # A symlink pointing outside the workspace root must not count.
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _touch(outside / "secret.md")
    (root / "ws").mkdir(parents=True)
    (root / "ws" / "out.md").symlink_to(outside / "secret.md")
    verdict, _ = wl._evaluate_artifact(root, "ws/out.md", None)
    assert verdict == "missing"


def test_evaluate_directory_match_ignored(tmp_path: Path) -> None:
    (tmp_path / "ws" / "out.md").mkdir(parents=True)  # dir, not file
    verdict, _ = wl._evaluate_artifact(tmp_path, "ws/out.md", None)
    assert verdict == "missing"


# ── wiring + root resolution ────────────────────────────────────────────────


def test_artifact_root_env_override(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_ARTIFACT_ROOT", "/tmp/custom-root")
    assert wl._artifact_root() == Path("/tmp/custom-root")
    monkeypatch.delenv("WATCHDOG_ARTIFACT_ROOT")
    monkeypatch.setenv("OPENCLAW_HOME", "/home/host_openclaw")
    assert wl._artifact_root() == Path("/home/host_openclaw")


def test_reconciler_wired_into_tick() -> None:
    import inspect

    src = inspect.getsource(wl)
    assert "_reconcile_expected_artifacts(db)" in src
    assert "stats[\"artifact_verdicts\"]" in src


def test_reconciler_ignores_token_counts() -> None:
    import inspect

    src = inspect.getsource(wl._reconcile_expected_artifacts)
    # tokens_used must NOT gate or trigger artifact verdicts.
    assert "tokens_used" not in src


def test_no_task_is_executed_without_an_agent() -> None:
    """Recurring tasks run through the agent lane; the watchdog must not carry a
    task-specific script that does the work itself (the old poem cron did)."""
    import inspect

    src = inspect.getsource(wl)
    assert "poem-cron" not in src
    assert "status='done'" not in inspect.getsource(wl._auto_resolve_stale_reviews)
