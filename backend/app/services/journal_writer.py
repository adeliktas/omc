"""Daily journal auto-writer.

T107 (2026-05-26). The Memory tab's `wiki-vault/journal/YYYY-MM-DD.md`
files used to be manual-only — owner had to type entries. This module
hooks into the watchdog loop to auto-append entries based on
`activity_events` rows (task created, completed, escalated, comment
posted by a lead, etc.) so every day has a structured diary.

Two surfaces:
- `tick(db)` — called from watchdog every slow tick. Reads new
  activity_events since the last marker, appends to today's journal.
- `write_daily_summary(date)` — called from a nightly cron / first-tick-
  after-midnight. Aggregates the day's events into a structured roll-up:
  Achievements, Decisions, Open todos.

Idempotency: a marker file `<vault>/journal/.last_processed_at` stores
the timestamp of the latest activity_event we've appended. Watchdog only
processes events newer than that.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)

_VAULT = Path(
    os.environ.get("OBSIDIAN_VAULT_PATH")
    or (Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")) / "wiki-vault").as_posix()
)
_JOURNAL_DIR = _VAULT / "journal"
_MARKER_FILE = _JOURNAL_DIR / ".last_processed_at"

# Event-type → journal section mapping. Events not listed are skipped.
_EVENT_TO_SECTION: dict[str, str] = {
    "task.created": "Recommendations given",
    "task.auto_assigned": "Recommendations given",
    "task.auto_split": "Recommendations given",
    "task.status_changed": "Results",
    "task.completed": "Results",
    "task.escalated": "Decisions",
    "task.assignee_woken": "What we discussed",
    "task.leads_pms_woken": "Decisions",
    "approval.created": "Decisions",
    "approval.resolved": "Decisions",
    "owner_review.created": "Decisions",
    "owner_review.resolved": "Decisions",
    "agent.provisioned": "What we discussed",
    "agent.retired": "Decisions",
    "pev.consent_required": "Decisions",
    "pev.completed": "Results",
    "pev.escalated": "Decisions",
}


def _ensure_dir() -> None:
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def _today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _file_for_date(date_str: str) -> Path:
    return _JOURNAL_DIR / f"{date_str}.md"


def _naive_utc(ts: datetime) -> datetime:
    """Normalize to tz-NAIVE UTC. `activity_events.created_at` is a
    `timestamp without time zone` column, so every datetime this module
    compares against DB rows must be naive-UTC — an aware marker made
    `ts > latest_ts` raise "can't compare offset-naive and offset-aware
    datetimes" on every tick, silently killing the journal (found
    2026-07-16 during Patch-1 deploy verification).
    """
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _read_marker() -> datetime | None:
    if not _MARKER_FILE.exists():
        return None
    try:
        s = _MARKER_FILE.read_text().strip()
        if not s:
            return None
        return _naive_utc(datetime.fromisoformat(s))
    except Exception as exc:
        logger.warning("journal_writer.marker_read_failed err=%s", exc)
        return None


def _write_marker(ts: datetime) -> None:
    _ensure_dir()
    try:
        _MARKER_FILE.write_text(ts.isoformat(), encoding="utf-8")
    except Exception as exc:
        logger.warning("journal_writer.marker_write_failed err=%s", exc)


def _ensure_day_file(date_str: str) -> Path:
    _ensure_dir()
    p = _file_for_date(date_str)
    if not p.exists():
        p.write_text(f"# Journal — {date_str}\n\n", encoding="utf-8")
    return p


def _append_section(date_str: str, section: str, body: str) -> None:
    """Append a timestamped H2 block to the day's file."""
    if not body.strip():
        return
    p = _ensure_day_file(date_str)
    now_hm = datetime.now().strftime("%H:%M")
    block = f"## {section} — {now_hm}\n{body.strip()}\n\n"
    existing = p.read_text()
    if not existing.endswith("\n\n"):
        existing = existing.rstrip() + "\n\n"
    p.write_text(existing + block, encoding="utf-8")


def _format_event(row: dict[str, Any]) -> str:
    """Render a single activity_event row into a journal bullet line."""
    et = row.get("event_type") or ""
    msg = (row.get("message") or "").strip()
    task_id = row.get("task_id")
    agent_name = row.get("agent_name") or ""
    ts = row.get("created_at")
    if isinstance(ts, datetime):
        time_str = ts.strftime("%H:%M:%S")
    else:
        time_str = str(ts or "")[-8:]
    # Trim very long messages — journal entries should be scannable.
    if len(msg) > 220:
        msg = msg[:220].rstrip() + "…"
    parts = [f"- `{time_str}`"]
    if et:
        parts.append(f"**{et}**")
    if agent_name:
        parts.append(f"({agent_name})")
    if task_id:
        parts.append(f"[task:{str(task_id)[:8]}]")
    if msg:
        parts.append(msg)
    return " ".join(parts)


async def tick(db: AsyncSession) -> dict[str, int]:
    """Process new activity_events since last marker, append to today's journal."""
    _ensure_dir()
    marker = _read_marker()
    # First run ever: anchor at "now - 1h" so we don't drown today's file in
    # the entire backlog. Naive-UTC to match the `timestamp without time
    # zone` column (see _naive_utc).
    if marker is None:
        marker = _naive_utc(datetime.now(timezone.utc) - timedelta(hours=1))
    # ORDER BY created_at ASC so we process oldest first; LIMIT to keep tick fast.
    res = await db.execute(text("""
        SELECT ae.id, ae.event_type, ae.message, ae.agent_id, ae.task_id,
               ae.board_id, ae.created_at, a.name AS agent_name
        FROM activity_events ae
        LEFT JOIN agents a ON a.id = ae.agent_id
        WHERE ae.created_at > :marker
          AND ae.event_type = ANY(:types)
        ORDER BY ae.created_at ASC
        LIMIT 200
    """), {
        "marker": marker,
        "types": list(_EVENT_TO_SECTION.keys()),
    })
    rows = res.fetchall()
    if not rows:
        # Nothing new — still ensure today's empty header exists so
        # frontend "List of all days" never has gaps.
        _ensure_day_file(_today_iso())
        return {"appended": 0, "skipped": 0}

    # Group by (date, section)
    grouped: dict[tuple[str, str], list[str]] = {}
    latest_ts = marker
    for r in rows:
        m = r._mapping
        et = m["event_type"]
        section = _EVENT_TO_SECTION.get(et)
        if not section:
            continue
        ts = m["created_at"]
        if isinstance(ts, datetime):
            ts = _naive_utc(ts)  # defensive — column is naive today
            date_str = ts.strftime("%Y-%m-%d")
            if ts > latest_ts:
                latest_ts = ts
        else:
            date_str = _today_iso()
        line = _format_event({
            "event_type": et, "message": m["message"],
            "task_id": m["task_id"], "agent_name": m["agent_name"],
            "created_at": ts,
        })
        grouped.setdefault((date_str, section), []).append(line)

    appended = 0
    for (date_str, section), lines in grouped.items():
        body = "\n".join(lines)
        _append_section(date_str, section, body)
        appended += len(lines)

    _write_marker(latest_ts)
    return {"appended": appended, "skipped": len(rows) - appended}


async def write_daily_summary(db: AsyncSession, date_str: str | None = None) -> dict[str, Any]:
    """Write the end-of-day roll-up section to a day's file.

    Aggregates:
      - Achievements: tasks transitioned to `done` today
      - Open todos: tasks still in `in_progress`/`review` from today
      - Issues: tasks escalated to owner-review or marked blocked today
      - High-level activity: counts of comments, agent wakes, PEV advances
    """
    if date_str is None:
        date_str = _today_iso()
    # Day window in UTC.
    try:
        day_start = datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
    except Exception:
        raise ValueError(f"bad date: {date_str}")
    day_end = day_start + timedelta(days=1)

    done_rows = (await db.execute(text("""
        SELECT t.title, b.name, a.name AS agent_name
        FROM tasks t
        LEFT JOIN boards b ON b.id = t.board_id
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        WHERE t.status = 'done'
          AND t.updated_at >= :start AND t.updated_at < :end
        ORDER BY t.updated_at DESC LIMIT 100
    """), {"start": day_start, "end": day_end})).fetchall()

    in_progress_rows = (await db.execute(text("""
        SELECT t.title, b.name, a.name AS agent_name, t.priority
        FROM tasks t
        LEFT JOIN boards b ON b.id = t.board_id
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        WHERE t.status IN ('in_progress','review')
        ORDER BY (CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END), t.updated_at ASC
        LIMIT 100
    """))).fetchall()

    escalated_rows = (await db.execute(text("""
        SELECT title, submitted_at, submitted_by
        FROM owner_reviews
        WHERE submitted_at >= :start AND submitted_at < :end
        ORDER BY submitted_at DESC LIMIT 50
    """), {"start": day_start, "end": day_end})).fetchall()

    blocked_rows = (await db.execute(text("""
        SELECT t.title, b.name
        FROM tasks t LEFT JOIN boards b ON b.id = t.board_id
        WHERE t.status = 'blocked'
          AND t.updated_at >= :start AND t.updated_at < :end
        ORDER BY t.updated_at DESC LIMIT 50
    """), {"start": day_start, "end": day_end})).fetchall()

    activity_counts = (await db.execute(text("""
        SELECT event_type, COUNT(*) AS n
        FROM activity_events
        WHERE created_at >= :start AND created_at < :end
        GROUP BY event_type
        ORDER BY n DESC LIMIT 20
    """), {"start": day_start, "end": day_end})).fetchall()

    lines_achievements = [
        f"- **{r._mapping['title']}** — {r._mapping['name'] or '(no board)'} · by {r._mapping['agent_name'] or 'unassigned'}"
        for r in done_rows
    ] or ["- _(no tasks completed today)_"]

    lines_todos = [
        f"- [{r._mapping['priority']}] **{r._mapping['title']}** — {r._mapping['name'] or '(no board)'} · {r._mapping['agent_name'] or 'unassigned'}"
        for r in in_progress_rows[:20]
    ] or ["- _(no open work)_"]

    lines_issues: list[str] = []
    for r in blocked_rows:
        lines_issues.append(f"- BLOCKED · **{r._mapping['title']}** — {r._mapping['name'] or '(no board)'}")
    for r in escalated_rows:
        lines_issues.append(f"- ESCALATED · {r._mapping['title']} (by {r._mapping['submitted_by']})")
    if not lines_issues:
        lines_issues = ["- _(no issues raised today)_"]

    lines_activity = [
        f"- {r._mapping['event_type']}: {r._mapping['n']}"
        for r in activity_counts
    ] or ["- _(no activity)_"]

    p = _ensure_day_file(date_str)
    body = p.read_text()

    # Replace any prior auto-summary block (idempotent) — marker pair lets us
    # re-run write_daily_summary safely.
    body = re.sub(
        r"## Daily Summary — auto.*?<!-- /daily-summary-auto -->\s*",
        "", body, flags=re.DOTALL,
    )
    if not body.endswith("\n\n"):
        body = body.rstrip() + "\n\n"

    block = (
        "## Daily Summary — auto\n"
        f"_Generated {datetime.now().strftime('%H:%M')} by watchdog._\n\n"
        "### Achievements\n" + "\n".join(lines_achievements) + "\n\n"
        "### Open todos\n" + "\n".join(lines_todos) + "\n\n"
        "### Issues\n" + "\n".join(lines_issues) + "\n\n"
        "### Activity counts\n" + "\n".join(lines_activity) + "\n\n"
        "<!-- /daily-summary-auto -->\n"
    )

    p.write_text(body + block, encoding="utf-8")
    return {
        "ok": True, "date": date_str,
        "done_count": len(done_rows),
        "in_progress_count": len(in_progress_rows),
        "blocked_count": len(blocked_rows),
        "escalated_count": len(escalated_rows),
    }
