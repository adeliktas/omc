"""The harness gives each task its own session, scoped to the task.

Measured 2026-09-29 on an identical three-step job: three one-shot
invocations cost 5,513 weighted tokens, the same three turns in one session
cost 2,109 - 62% less for the same result. The saving is fresh input, 3,836
against 954, because a continuing session does not re-establish context on
every tick.

Scope matters as much as reuse: the session ends with the task, so growth is
bounded, unlike an always-on agent session that accumulates indefinitely.
"""
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-claude-runner.sh"


def _runner() -> str:
    return RUNNER.read_text()


def test_pi_uses_one_session_per_task():
    body = _runner()
    assert '--session-id "mc-task-$TASK_ID"' in body
    # The previous behaviour threw the context away on every invocation.
    assert "--no-session" not in body


def test_session_id_is_derived_from_the_task_not_the_tick():
    body = _runner()
    i = body.index("--session-id")
    call = body[i : i + 80]
    # Anything tick-scoped (a timestamp, a random id) would defeat the point.
    assert "$TASK_ID" in call
    for tick_scoped in ("date", "RANDOM", "$$"):
        assert tick_scoped not in call
