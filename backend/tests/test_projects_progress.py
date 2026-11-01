from app.api.projects import _progress_pct


def test_progress_pct_near_complete_does_not_round_to_100():
    assert _progress_pct(1117, 1121) == 99


def test_progress_pct_all_done_is_100():
    assert _progress_pct(1121, 1121) == 100


def test_progress_pct_empty_board_is_0():
    assert _progress_pct(0, 0) == 0
