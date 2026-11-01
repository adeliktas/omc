"""In-process task change notifier (T51, 2026-05-14).

Replaces the SSE stream's 2s blind poll with per-board asyncio.Event wakeups.
Mutating code paths call `notify_task_changed(board_id)` after commit. SSE
generators call `wait_for_task_change(board_id, timeout=2.0)` to block until
notified or until the timeout (which acts as a safety floor for cross-process
changes the in-process bus can't see, e.g., watchdog tick in another worker).

Design intentionally lightweight — no PG LISTEN/NOTIFY trigger required.
Single-process MC is the common deployment; cross-process is covered by the
2s safety floor. For a true cross-process wake-up, swap this module's
internals for asyncpg LISTEN once multi-worker becomes common.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

# Per-board event registry. New events created lazily on first listener.
_events: dict[UUID, asyncio.Event] = {}
_global_event: asyncio.Event | None = None


def _get_or_create(board_id: UUID) -> asyncio.Event:
    e = _events.get(board_id)
    if e is None:
        e = asyncio.Event()
        _events[board_id] = e
    return e


def _get_or_create_global() -> asyncio.Event:
    global _global_event
    if _global_event is None:
        _global_event = asyncio.Event()
    return _global_event


def notify_task_changed(board_id: UUID | None = None) -> None:
    """Signal that one or more tasks changed.

    Call after commit on any path that creates / updates / deletes a task or
    related activity_event row. Cheap: O(1) per board.
    """
    if board_id is not None:
        e = _events.get(board_id)
        if e is not None:
            e.set()
    # Always pulse global so blanket consumers wake too.
    if _global_event is not None:
        _global_event.set()


async def wait_for_task_change(
    board_id: UUID | None,
    *,
    timeout: float = 2.0,
) -> bool:
    """Wait for a notify on the given board (or any board when None).

    Returns True if a notify arrived, False if the timeout elapsed. Caller
    iterates regardless — the timeout is the safety-floor fallback.
    """
    e = _get_or_create(board_id) if board_id is not None else _get_or_create_global()
    e.clear()
    try:
        await asyncio.wait_for(e.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
