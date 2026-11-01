"""Which task comments are system markers rather than content.

The feeds hide process markers (`[watchdog-nudge]`, `[cycle-complete]`,
`[ready-to-close]`, ...). The original test was "starts with a bracket", which
also swallowed any comment opening with a markdown link -- `[Title](url)` --
silently, with nothing shown to say content had been dropped. An agent posting
a list of links as its result produced an empty-looking thread.

A marker is a lowercase slug in brackets, optionally bold, NOT immediately
followed by `(`. A markdown link is exactly the case that is followed by `(`,
and link text with capitals or spaces fails the slug shape anyway.
"""

from __future__ import annotations

# Postgres regex. Anchored at the start; `($|[^(])` is what lets a markdown
# link through.
SYSTEM_MARKER_REGEX = r"^\*{0,2}\[[a-z0-9_-]+\]($|[^(])"


def not_system_marker_sql(column: str = "message") -> str:
    """SQL predicate keeping content and dropping system markers."""
    return f"{column} !~ '{SYSTEM_MARKER_REGEX}'"
