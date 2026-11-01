"""The zero-token block must not bury work the harness runner actually did."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app/services/watchdog_loop.py"


def _source() -> str:
    return SRC.read_text()


def test_zero_token_wall_clock_finalize_blocks_instead_of_review():
    src = _source()
    i = src.index('f"[watchdog-finalize] {detail}')
    window = src[i - 2200:i]
    assert "ran_nothing = not tok_used" in window
    assert 'next_status = "blocked" if ran_nothing else "review"' in window


def test_harness_lane_work_is_not_treated_as_zero_token():
    src = _source()
    i = src.index('f"[watchdog-finalize] {detail}')
    window = src[i - 2200:i]
    # tasks.tokens_used only tracks the gateway lane; the runner reports its
    # usage to the ranking sidecar, so its result comment is the evidence.
    assert "_harness_worked(" in window
    helper = src[src.index("async def _harness_worked("):]
    assert "**agent-runner**" in helper[:1200]
    assert "in_progress_at" in helper[:1200]


def test_auto_promote_is_scoped_to_the_current_assignment():
    src = _source()
    i = src.index("_auto_promote_inbox_on_assignee_activity")
    window = src[i:i + 2500]
    assert "task.assignee_notified" in window
    assert "MAX(a2.created_at)" in window


def test_stuck_inbox_repeat_task_blocks_not_done():
    # A repeating task stuck in *inbox* was never picked up this cycle, so no
    # work ran. The old branch closed it `done` "whether agent replied or not"
    # using a lifetime reply count — a stale prior-cycle comment read as
    # completion (newsfeed auto-closed 2026-09-28 05:35). Must go to `blocked`;
    # `_process_interval_repeats` re-fires blocked repeating tasks.
    src = _source()
    i = src.index("if renotify_count >= 3 and age > _STUCK_INBOX_REASSIGN_SECONDS:")
    window = src[i:i + 1400]
    assert "status='blocked'" in window
    assert "status='done'" not in window


if __name__ == "__main__":
    test_zero_token_wall_clock_finalize_blocks_instead_of_review()
    test_harness_lane_work_is_not_treated_as_zero_token()
    test_auto_promote_is_scoped_to_the_current_assignment()
    test_stuck_inbox_repeat_task_blocks_not_done()
    print("ok")
