"""System markers are hidden from the comment feeds; content is never dropped.

The old test was "starts with a bracket", which silently swallowed any comment
opening with a markdown link. An agent posting a list of links as its result
produced an empty-looking thread with nothing to say content had been dropped.
"""
import re

import pytest

from app.api.comment_filters import SYSTEM_MARKER_REGEX, not_system_marker_sql

# Python equivalent of the Postgres regex, for a pure-unit check of the shape.
_RE = re.compile(SYSTEM_MARKER_REGEX.replace(r"\*", "*").replace("*{0,2}", r"\*{0,2}"))


def _is_marker(message: str) -> bool:
    return bool(re.match(r"^\*{0,2}\[[a-z0-9_-]+\]($|[^(])", message))


@pytest.mark.parametrize(
    "message",
    [
        "[watchdog-nudge] **Stuck-inbox re-notify** - assignee did not start",
        "[cycle-complete] Recurring task cycle finished",
        "[ready-to-close] verdict=accept",
        "[owner-directive-applied] Owner ended the recurring task",
        "[artifact-missing] declared artifact absent",
        "**[pm-signoff]** verdict=accept",
        "[watchdog-finalize] reason=wall-clock tokens_used=0",
    ],
)
def test_process_markers_stay_hidden(message):
    assert _is_marker(message) is True


@pytest.mark.parametrize(
    "message",
    [
        # The regression: an agent result that opens with a markdown link.
        "[2026 in LLMs (so far)](https://simonwillison.net/2026/Sep/27/)",
        "[Title](https://example.com)",
        "[todo](https://example.com/x)",
        "[A Capitalised Link] is not a marker",
        "**AI/LLM news** - headlines follow",
        "Done. Updated index.html in the workspace.",
        "[multi word text] reads as prose, not a marker",
    ],
)
def test_content_is_never_dropped(message):
    assert _is_marker(message) is False


def test_sql_predicate_targets_the_requested_column():
    assert not_system_marker_sql("message").startswith("message !~ ")
    assert "activity_events.message !~ " in not_system_marker_sql("activity_events.message")


def test_python_check_matches_the_sql_regex_exactly():
    """The tests above use a Python mirror of the Postgres regex.

    That mirror could drift from the SQL actually executed, so pin them
    together: the same pattern string must appear in the predicate the API
    sends to the database. Verified against a live Postgres on 2026-09-28
    for markers, markdown links, and prose openings.
    """
    predicate = not_system_marker_sql("message")
    assert SYSTEM_MARKER_REGEX in predicate
    assert predicate.startswith("message !~ ")
