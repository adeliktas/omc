"""Per-task harness engine, ponytail skill, and prompt guardrails."""
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-claude-runner.sh"


def _runner() -> str:
    return RUNNER.read_text()


def test_a_task_can_pin_its_own_engine_and_skip_the_ranking_plan():
    body = _runner()
    assert 'if [ -n "${TASK_PROVIDER:-}" ] && [ -n "${TASK_MODEL:-}" ]; then' in body
    assert "task-pinned engine" in body
    # A pinned step must not be silently upgraded to an expensive model: the
    # branch builds PLAN itself instead of reading the ranking board. Assert
    # the relationship, not a character offset, so unrelated edits inside the
    # branch do not fail this.
    i = body.index('if [ -n "${TASK_PROVIDER:-}" ]')
    j = body.index('else', i)
    branch = body[i:j]
    assert "PLAN=" in branch
    assert "ranking_json=" not in branch


def test_thinking_level_comes_from_the_task_and_defaults_low():
    body = _runner()
    assert '--thinking "${TASK_THINKING:-low}"' in body
    # The old unconditional high burned reasoning tokens on cheap steps.
    assert "--thinking high" not in body


def test_ponytail_skill_is_loaded_when_present():
    body = _runner()
    assert 'PONYTAIL_SKILL="${RUNNER_PONYTAIL_SKILL:-$HOME/.claude/skills/ponytail}"' in body
    assert '[ -d "$PONYTAIL_SKILL" ] || PONYTAIL_SKILL=""' in body
    assert '${PONYTAIL_SKILL:+--skill "$PONYTAIL_SKILL"}' in body


def test_guardrails_state_what_not_to_do():
    body = _runner()
    i = body.index("GUARDRAILS=")
    block = body[i : i + 1200]
    assert "Do NOT scan the filesystem" in block
    assert "STOP and post a comment asking the owner" in block
    assert "never change layout, CSS or" in block
    assert "$GUARDRAILS" in body
