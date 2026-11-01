"""An owner STOP directive ends a recurring task, so an incidental word must not."""
import pytest

from app.services.watchdog_loop import _is_owner_stop_directive as stop


@pytest.mark.parametrize(
    "comment",
    [
        "finished",
        "Done.",
        "stop",
        "stop the repeat",
        "please cancel the recurring schedule",
        "no more",
        "that's enough",
        "mark it as done",
        "you can close this",
    ],
)
def test_real_directives_are_detected(comment):
    assert stop(comment) is True


@pytest.mark.parametrize(
    "comment",
    [
        # The regression: an operator note that merely used the word.
        "Operator reset: the nudge was restarting the assignee turn before it "
        "could finish. Throttle fixed and deployed; giving this a clean window.",
        "The agent did not finish within the budget, retrying next tick.",
        "I had to stop and restart omniroute to clear the semaphore.",
        "Closed the gap between the two lanes; no action needed.",
        "",
        "   ",
    ],
)
def test_incidental_words_do_not_end_the_schedule(comment):
    assert stop(comment) is False
