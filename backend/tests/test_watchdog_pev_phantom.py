"""The PEV phantom reset must not re-block a task that moved on after the verdict."""
import re
from pathlib import Path


def _watchdog_src() -> str:
    src = Path(__file__).resolve().parents[1] / "app/services/watchdog_loop.py"
    return src.read_text()


def _phantom_reset_sql() -> str:
    text = _watchdog_src()
    start = text.index("UPDATE tasks t\n                SET status='blocked'")
    return text[start:text.index("RETURNING t.id", start)]


def test_phantom_reset_posts_an_explanatory_comment():
    # A silent flip to `blocked` violates the invariant that every blocked
    # task carries a reason a human can read. The reset must comment.
    src = _watchdog_src()
    start = src.index("UPDATE tasks t\n                SET status='blocked'")
    block = src[start:src.index('stats["pev_phantom_reset"]', start)]
    assert "RETURNING t.id, t.board_id" in block
    assert "[watchdog-pev-phantom-reset]" in block
    assert "_post_comment(" in block


def test_phantom_reset_only_fires_on_a_verdict_newer_than_the_task():
    sql = _phantom_reset_sql()
    assert "s.status IN ('escalated','rolled_back')" in sql
    # Without this guard an escalated PEV row is permanent: the task is dragged
    # back to `blocked` every tick and can never reach `review` again.
    assert re.search(r"s\.updated_at\s*>\s*t\.updated_at", sql)


if __name__ == "__main__":
    test_phantom_reset_only_fires_on_a_verdict_newer_than_the_task()
    test_phantom_reset_posts_an_explanatory_comment()
    print("ok")
