"""A pinned engine must defer while its provider is on a quota hold.

Pinning removes the fallback chain by design, so without this the runner
re-ran an exhausted provider every tick: 38 ticks in 30 minutes against a
claude subscription that had already reported its own reset time.

This exercises the extracted hold check itself rather than grepping the
shell script, so a logic change is caught, not just a deleted line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-claude-runner.sh"


def _hold_script() -> str:
    """The python -c program embedded in the runner's pinned branch."""
    body = RUNNER.read_text()
    start = body.index('HOLD="$("$MC" get /api/v1/ranking --json 2>>"$LOG" | python3 -c \'')
    start = body.index("'", start) + 1
    end = body.index('\' "$TASK_PROVIDER" "$TASK_MODEL"', start)
    return body[start:end]


def _run(board: dict, provider: str, model: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", _hold_script(), provider, model, "1"],
        input=json.dumps(board),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _board(until: str | None, provider="claude-code", model="opus-4-8") -> dict:
    entry = {"provider": provider, "model": model, "availability_reason": "quota_exhausted"}
    if until is not None:
        entry["unavailable_until"] = until
    return {"levels": [{"entries": [entry]}]}


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def test_future_hold_is_reported():
    out = _run(_board(_iso(timedelta(hours=1))), "claude-code", "opus-4-8")
    assert "quota_exhausted" in out


def test_expired_hold_is_ignored():
    assert _run(_board(_iso(timedelta(hours=-1))), "claude-code", "opus-4-8") == ""


def test_no_hold_field_is_ignored():
    assert _run(_board(None), "claude-code", "opus-4-8") == ""


def test_hold_on_a_different_model_does_not_block_this_one():
    board = _board(_iso(timedelta(hours=1)), model="sonnet")
    board["levels"][0]["entries"].extend(_board(None)["levels"][0]["entries"])
    assert _run(board, "claude-code", "opus-4-8") == ""


def test_hold_on_a_different_provider_does_not_block_this_one():
    board = _board(_iso(timedelta(hours=1)), provider="deepseek-sub")
    board["levels"][0]["entries"].extend(_board(None)["levels"][0]["entries"])
    assert _run(board, "claude-code", "opus-4-8") == ""


def test_malformed_timestamp_does_not_crash_the_tick():
    assert "invalid availability timestamp" in _run(
        _board("not-a-timestamp"), "claude-code", "opus-4-8"
    )


def test_empty_board_defers_an_unknown_pinned_engine():
    assert "not registered" in _run({}, "claude-code", "opus-4-8")


def test_disabled_or_ineligible_pins_are_not_executable():
    for field in ("disabled", "eligible"):
        board = _board(None)
        entry = board["levels"][0]["entries"][0]
        entry[field] = field == "disabled"
        assert _run(board, "claude-code", "opus-4-8")


def test_lg_transfer_exclusion_is_independent_of_ranking_flags():
    for provider in ("openai-codex-1", "openai-codex"):
        board = _board(None, provider=provider)
        board["levels"][0]["entries"][0].update(disabled=False, eligible=True)
        assert "OmniRoute" in _run(board, provider, "opus-4-8")
