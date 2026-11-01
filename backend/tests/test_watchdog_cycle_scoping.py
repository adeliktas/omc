"""Give-up caps must count the current cycle, not the task's whole life.

A recurring task keeps one row forever, so a lifetime count only grows: after
enough cycles every fresh cycle starts already past the cap and the task is
written off while the current cycle is healthy. Observed as "Auto-blocked --
stuck in inbox for 32 min after 61 re-notify cycles".
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app/services/watchdog_loop.py"


def _source() -> str:
    return SRC.read_text()


def test_stuck_inbox_caps_are_cycle_scoped():
    src = _source()
    for var in ("rotation_count", "stalled_already", "renotify_count"):
        assert f"{var} = await _count_comments_this_cycle(" in src, var


def test_cycle_helper_anchors_on_the_cycle_complete_marker():
    src = _source()
    helper = src[src.index("async def _count_comments_this_cycle("):]
    window = helper[:1400]
    assert "[cycle-complete]%" in window
    assert "MAX(c.created_at)" in window
    # A one-shot task has no cycle marker: its whole life is one cycle.
    assert "1970-01-01" in window


if __name__ == "__main__":
    test_stuck_inbox_caps_are_cycle_scoped()
    test_cycle_helper_anchors_on_the_cycle_complete_marker()
    print("ok")
