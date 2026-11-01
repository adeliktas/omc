"""Calendar API — Thunderbird-style scheduled-task view.

Lists every task in the system that has any kind of schedule
(`scheduled_at`, `repeat_cron`, or `repeat_interval_minutes`) and
expands them into actual fire-times within a requested window.

Backs the `/calendar` UI tab. Source data is the same `tasks` table
the boards use — Calendar is a read-only view + a create-helper that
posts back to the existing `POST /boards/{board_id}/tasks` endpoint.

T104 (2026-05-24).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent
from app.core.auth import SESSION_DEP

router = APIRouter(prefix="/calendar", tags=["calendar"])


# Predefined "routines" the owner can drop into the calendar with one
# click. These are not stored on disk — they're returned from
# `GET /calendar/templates` and the UI POSTs the resolved payload to
# `POST /boards/{board_id}/tasks` like any other task.
_TEMPLATES = [
    {
        "key": "trend_radar_midnight",
        "title": "Trend Radar",
        "description": "Capture late-night trend signals before the daily cycle resets.",
        "cron": "0 0 * * *",  # 00:00 daily
        "color": "violet",
    },
    {
        "key": "morning_kickoff",
        "title": "Morning Kickoff",
        "description": "Plan the day. Surface stuck tasks. Re-prioritize.",
        "cron": "0 7 * * *",  # 07:00 daily
        "color": "emerald",
    },
    {
        "key": "scout_morning_research",
        "title": "Scout Morning Research",
        "description": "Scout new tooling + research items. Drop findings into wiki `_raw/`.",
        "cron": "0 8 * * *",
        "color": "sky",
    },
    {
        "key": "morning_brief",
        "title": "Morning Brief",
        "description": "Synthesize overnight events into a one-page brief for the owner.",
        "cron": "0 8 * * *",
        "color": "blue",
    },
    {
        "key": "trend_radar_daily",
        "title": "Trend Radar Daily",
        "description": "Daily trend-radar update: net new vs net stale.",
        "cron": "0 8 * * *",
        "color": "violet",
    },
    {
        "key": "daily_digest",
        "title": "Daily Digest",
        "description": "Roll up board activity, agent output, owner-review queue.",
        "cron": "0 9 * * *",
        "color": "amber",
    },
    {
        "key": "evening_wrapup",
        "title": "Evening Wrap Up",
        "description": "Close-out summary. Push unblocked tasks to next-day plan.",
        "cron": "0 21 * * *",
        "color": "rose",
    },
]


def _parse_window(from_iso: str | None, to_iso: str | None) -> tuple[datetime, datetime]:
    """Default window = today 00:00 → +7 days. Tolerates Z + offset suffixes."""
    def _p(s: str) -> datetime:
        s = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(s)

    now = datetime.now(timezone.utc)
    if from_iso:
        try:
            t0 = _p(from_iso)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad `from` ISO: {exc}") from exc
    else:
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if to_iso:
        try:
            t1 = _p(to_iso)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad `to` ISO: {exc}") from exc
    else:
        t1 = t0 + timedelta(days=7)
    if t1 <= t0:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")
    if t1 - t0 > timedelta(days=62):
        raise HTTPException(status_code=400, detail="window must be <= 62 days")
    # Normalize to aware UTC if naive supplied.
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=timezone.utc)
    return t0, t1


def _expand_cron(cron_expr: str, start: datetime, end: datetime, cap: int = 200) -> list[datetime]:
    """Return list of `datetime` instances of `cron_expr` firing in `[start, end)`."""
    try:
        from croniter import croniter
    except Exception:
        return []
    try:
        # croniter operates in the timezone of `start`.
        it = croniter(cron_expr, start - timedelta(seconds=1))
        out: list[datetime] = []
        while len(out) < cap:
            nxt = it.get_next(datetime)
            if nxt >= end:
                break
            out.append(nxt)
        return out
    except Exception:
        return []


def _expand_interval(
    interval_minutes: int,
    anchor: datetime | None,
    start: datetime,
    end: datetime,
    cap: int = 200,
) -> list[datetime]:
    """Repeat every N minutes from anchor. If no anchor, anchor = start."""
    if interval_minutes <= 0:
        return []
    step = timedelta(minutes=interval_minutes)
    a = (anchor or start).astimezone(timezone.utc)
    # Roll forward to the first instance ≥ start.
    if a < start:
        delta = start - a
        n = int(delta.total_seconds() // step.total_seconds())
        a = a + n * step
        while a < start:
            a += step
    out: list[datetime] = []
    cur = a
    while cur < end and len(out) < cap:
        out.append(cur)
        cur += step
    return out


@router.get("/templates", summary="Predefined daily-routine templates")
async def list_templates(_: None = Depends(require_user_or_agent)) -> list[dict[str, Any]]:
    """Return Thunderbird-style preset routines the owner can drop on calendar."""
    return _TEMPLATES


@router.get(
    "/occurrences",
    summary="Expand scheduled tasks into actual fire-times within a window",
)
async def list_occurrences(
    session: AsyncSession = SESSION_DEP,
    from_iso: str | None = Query(default=None, alias="from"),
    to_iso: str | None = Query(default=None, alias="to"),
    board_id: UUID | None = None,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    """List every fire-time of every scheduled task within `[from, to)`.

    Returns one row per OCCURRENCE (a task with `0 8 * * *` over 7 days
    yields 7 rows). UI groups by date for the day-column view.
    """
    t0, t1 = _parse_window(from_iso, to_iso)
    where = [
        "(t.scheduled_at IS NOT NULL "
        " OR t.repeat_cron IS NOT NULL "
        " OR (t.repeat_enabled = true AND t.repeat_interval_minutes IS NOT NULL))",
        "t.status NOT IN ('done', 'cancelled', 'trash')",
    ]
    params: dict[str, Any] = {}
    if board_id is not None:
        where.append("t.board_id = :bid")
        params["bid"] = str(board_id)
    sql = (
        "SELECT t.id, t.board_id, b.name AS board_name, t.title, t.description, "
        "       t.scheduled_at, t.repeat_cron, t.repeat_interval_minutes, "
        "       t.repeat_enabled, t.complexity, t.priority, t.status, "
        "       t.assigned_agent_id, a.name AS assignee_name "
        "FROM tasks t "
        "LEFT JOIN boards b ON b.id = t.board_id "
        "LEFT JOIN agents a ON a.id = t.assigned_agent_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY COALESCE(t.scheduled_at, t.created_at) ASC LIMIT 500"
    )
    res = await session.execute(text(sql), params)
    rows = res.fetchall()

    occurrences: list[dict[str, Any]] = []
    for r in rows:
        m = r._mapping
        sched = m["scheduled_at"]
        cron = m["repeat_cron"]
        interval = m["repeat_interval_minutes"]
        enabled = m["repeat_enabled"]
        fires: list[datetime] = []
        if cron and enabled:
            fires.extend(_expand_cron(str(cron), t0, t1))
        elif interval and enabled:
            anchor = sched.astimezone(timezone.utc) if isinstance(sched, datetime) and sched.tzinfo else (
                sched.replace(tzinfo=timezone.utc) if isinstance(sched, datetime) else None
            )
            fires.extend(_expand_interval(int(interval), anchor, t0, t1))
        elif sched is not None:
            s = sched if (isinstance(sched, datetime) and sched.tzinfo) else (
                sched.replace(tzinfo=timezone.utc) if isinstance(sched, datetime) else None
            )
            if s and t0 <= s < t1:
                fires.append(s)
        for f in fires:
            occurrences.append({
                "task_id": str(m["id"]),
                "board_id": str(m["board_id"]) if m["board_id"] else None,
                "board_name": m["board_name"] or "",
                "title": m["title"],
                "description": (m["description"] or "")[:300],
                "fire_at": f.isoformat(),
                "schedule_kind": "cron" if cron else ("interval" if interval else "once"),
                "schedule_value": cron or (str(interval) if interval else (sched.isoformat() if sched else "")),
                "complexity": m["complexity"],
                "priority": m["priority"],
                "status": m["status"],
                "assignee_name": m["assignee_name"],
            })

    occurrences.sort(key=lambda x: x["fire_at"])
    return {
        "from": t0.isoformat(),
        "to": t1.isoformat(),
        "count": len(occurrences),
        "items": occurrences,
    }


@router.get("/tasks", summary="List all tasks with any schedule (no occurrence expansion)")
async def list_scheduled_tasks(
    session: AsyncSession = SESSION_DEP,
    board_id: UUID | None = None,
    _: None = Depends(require_user_or_agent),
) -> list[dict[str, Any]]:
    """List the underlying scheduled-task rows. UI uses this for the
    "manage schedule" sidebar (rename, change cron, disable, delete)."""
    where = [
        "(t.scheduled_at IS NOT NULL "
        " OR t.repeat_cron IS NOT NULL "
        " OR (t.repeat_enabled = true AND t.repeat_interval_minutes IS NOT NULL))",
        "t.status NOT IN ('done', 'cancelled', 'trash')",
    ]
    params: dict[str, Any] = {}
    if board_id is not None:
        where.append("t.board_id = :bid")
        params["bid"] = str(board_id)
    sql = (
        "SELECT t.id, t.board_id, b.name AS board_name, t.title, "
        "       t.scheduled_at, t.repeat_cron, t.repeat_interval_minutes, "
        "       t.repeat_enabled, t.status, t.complexity "
        "FROM tasks t "
        "LEFT JOIN boards b ON b.id = t.board_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY t.title ASC LIMIT 500"
    )
    res = await session.execute(text(sql), params)
    return [
        {
            "id": str(r._mapping["id"]),
            "board_id": str(r._mapping["board_id"]) if r._mapping["board_id"] else None,
            "board_name": r._mapping["board_name"] or "",
            "title": r._mapping["title"],
            "scheduled_at": r._mapping["scheduled_at"].isoformat() if r._mapping["scheduled_at"] else None,
            "repeat_cron": r._mapping["repeat_cron"],
            "repeat_interval_minutes": r._mapping["repeat_interval_minutes"],
            "repeat_enabled": r._mapping["repeat_enabled"],
            "status": r._mapping["status"],
            "complexity": r._mapping["complexity"],
        }
        for r in res.fetchall()
    ]
