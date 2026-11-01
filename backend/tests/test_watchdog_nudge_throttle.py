"""The stuck-inbox nudge must not fire on every watchdog tick.

Stage 1 leaves `tasks.updated_at` alone so the 30-min rotation stage stays
reachable, but that removed its only throttle: it re-woke the assignee every
~72s, restarting a turn that takes minutes, so the agent could never finish.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app/services/watchdog_loop.py"


def _stage_one_source() -> str:
    src = SRC.read_text()
    start = src.index('renotify_count = await _count_comments_this_cycle(')
    end = src.index('**Stuck-inbox re-notify** — assignee ', start)
    return src[start:end]


def test_stage_one_is_throttled_by_its_own_last_nudge():
    body = _stage_one_source()
    assert "_seconds_since_comment_matching(" in body
    assert "_STUCK_INBOX_RENOTIFY_SECONDS" in body
    # It must skip, not fall through to sending another wake message.
    assert "continue" in body


def test_throttle_helper_reads_the_newest_matching_comment():
    src = SRC.read_text()
    helper = src[src.index("async def _seconds_since_comment_matching("):]
    assert "MAX(created_at)" in helper[:800]
    assert "task.comment" in helper[:800]


if __name__ == "__main__":
    test_stage_one_is_throttled_by_its_own_last_nudge()
    test_throttle_helper_reads_the_newest_matching_comment()
    print("ok")
