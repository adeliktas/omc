"""Auto-watchdog loop — periodically scans tasks, posts nudges, escalates stale items.

Lifecycle: started in app lifespan, runs every WATCHDOG_INTERVAL seconds.
Best-effort: failures logged but never crash the loop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
import sqlite3
import urllib.request
import urllib.error
import uuid

from app.core.logging import get_logger
from app.services.openclaw.private_board import targets_private_lead
from app.services.runtime_settings import get_setting
from app.core.time import utcnow
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import async_session_maker

logger = get_logger(__name__)

# Configurable via env — defaults tightened for autonomous pipeline
# T101 (2026-05-17): tighter default cadence — 180s → 60s active, 600s
# → 180s idle. The watchdog steps are all wrapped in try/except + most
# are cheap SQL counts. A 60s tick + active task pickups within a tick
# of agent wake-up = much faster end-to-end. Owner can override to
# revert via the env vars below.
# Minimize LLM calls: routing uses keyword match + QA fallback only unless this
# is explicitly enabled. The per-task LLM classification round-trip is optional.
_ROUTING_LLM_ENABLED = os.environ.get("WATCHDOG_ROUTING_LLM_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
)
# Freeform-completion LLM disambiguation (up to 5 calls/tick). Off by default;
# canonical markers promote without an LLM call via the SQL fast-path.
_COMPLETION_LLM_ENABLED = os.environ.get("WATCHDOG_COMPLETION_LLM_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
)
# Owner-directive evaluation: when the OWNER posts a freeform comment on a task,
# LLM-evaluate whether it's a directive to change the goal/scope and, if so,
# rewrite the task description. ON by default — owner comments are intentional
# and rare, so this is one LLM call per NEW owner comment, not a recurring loop.
_OWNER_DIRECTIVES_ENABLED = os.environ.get("WATCHDOG_OWNER_DIRECTIVES_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
)
_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_INTERVAL", "60"))  # T101: 180 → 60
_IDLE_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_IDLE_INTERVAL", "180"))  # T101: 600 → 180


# 2026-09-22: re-read per tick so /settings/runtime edits to the cadence apply
# without a restart. Module constants above remain the fallback.
def _active_interval() -> int:
    return int(get_setting("WATCHDOG_INTERVAL", _INTERVAL_SECONDS))


def _idle_interval() -> int:
    return int(get_setting("WATCHDOG_IDLE_INTERVAL", _IDLE_INTERVAL_SECONDS))
_STALE_IN_PROGRESS = int(get_setting("WATCHDOG_STALE_INPROGRESS_SECONDS"))  # T104 (2026-05-24): 45 → 20 min — owner expects faster escalation
_STALE_REVIEW = int(get_setting("WATCHDOG_STALE_REVIEW_SECONDS"))  # T104 (2026-05-24): 90 → 20 min — review stalls are the most owner-visible
_NUDGE_COOLDOWN = int(os.environ.get("WATCHDOG_NUDGE_COOLDOWN", "600"))  # T104: 20 → 10 min
_MAX_NUDGES_BEFORE_ESCALATE = int(get_setting("WATCHDOG_MAX_NUDGES"))  # T104: 2 → 1
_ENABLED = bool(get_setting("WATCHDOG_ENABLED"))
# Grace period before watchdog auto-routes an unclaimed inbox task — allows
# agents to self-volunteer first.
_INBOX_VOLUNTEER_GRACE_SECONDS = int(get_setting("WATCHDOG_INBOX_VOLUNTEER_GRACE_SECONDS"))
# After this long w/o the assignee picking up the task (status still 'inbox'),
# watchdog re-notifies. After STUCK_INBOX_REASSIGN_SECONDS, rotate to next pool agent.
_STUCK_INBOX_RENOTIFY_SECONDS = int(
    os.environ.get("WATCHDOG_STUCK_INBOX_RENOTIFY_SECONDS", "600")  # 10 min
)
_STUCK_INBOX_REASSIGN_SECONDS = int(
    os.environ.get("WATCHDOG_STUCK_INBOX_REASSIGN_SECONDS", "1800")  # 30 min
)

_TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
_TESTS_DB = Path(os.environ.get("TESTS_DB_FILE", "/traffic-data/tests.db"))
# Legacy file path retained for tests.py compatibility shims (T18 migration
# 2026-05-14: all watchdog writes now go to Postgres `owner_reviews` via
# `app/services/owner_reviews.py`). Kept here so `_REVIEW_FILE.stat()` lookups
# in api/tests.py don't NameError — they degrade gracefully when the file
# doesn't exist.
_REVIEW_FILE = Path(os.environ.get("REVIEW_FILE", "/traffic-data/OWNER_REVIEW.json"))

_GATEWAY_HOST = os.environ.get("OPENCLAW_GATEWAY_HOST", "host.docker.internal")
_GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
_gw_probe_ts: float = 0.0
_gw_probe_state: bool = False
_gw_consecutive_failures: int = 0
# Exponential backoff cache windows (seconds). Each consecutive failure extends
# the cache so we don't hammer a down gateway with TCP probes 36×/min.
_GW_BACKOFF_LADDER = (5.0, 5.0, 15.0, 30.0, 60.0)


def _gateway_alive() -> bool:
    """TCP-probe openclaw-gateway. Exponential cache backoff on failure:
    5s → 15s → 30s → 60s (capped). Resets to 5s on first success.

    Watchdog-side guard: when the gateway is down, skip every action that
    touches tasks/agents — owner has explicitly halted the platform.
    """
    import socket as _socket
    global _gw_probe_ts, _gw_probe_state, _gw_consecutive_failures
    now = time.monotonic()
    # Pick cache window based on consecutive-failure streak.
    idx = min(_gw_consecutive_failures, len(_GW_BACKOFF_LADDER) - 1)
    cache_window = _GW_BACKOFF_LADDER[idx]
    if (now - _gw_probe_ts) < cache_window:
        return _gw_probe_state
    alive = False
    try:
        with _socket.create_connection((_GATEWAY_HOST, _GATEWAY_PORT), timeout=1.0):
            alive = True
    except Exception:
        alive = False
    _gw_probe_ts = now
    _gw_probe_state = alive
    if alive:
        _gw_consecutive_failures = 0
    else:
        _gw_consecutive_failures += 1
    return alive


# T90 (2026-05-16): gateway-liveness heartbeat file path. A host-side
# watchdog script reads this file each minute and restarts the gateway
# systemd-user service after N consecutive `alive=false` reports. Path
# resolves against `OPENCLAW_HOME` so the container + host see the same
# bind-mounted file (`~/.openclaw/heartbeat.gateway` on the host,
# `/home/host_openclaw/heartbeat.gateway` in the container).
_GATEWAY_HEARTBEAT_PATH = os.environ.get(
    "GATEWAY_HEARTBEAT_PATH",
    str(Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")) / "heartbeat.gateway"),
)


def _write_gateway_heartbeat(alive: bool) -> None:
    """Append one JSON line per watchdog tick documenting gateway state.
    File is truncated to last 20 lines to prevent unbounded growth — the
    host script only needs recent history to detect a flap.

    Schema: `{"ts": ISO-8601, "alive": bool, "host": str, "port": int, "failures": int}`
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    line = _json.dumps({
        "ts": _dt.now(_tz.utc).isoformat(),
        "alive": alive,
        "host": _GATEWAY_HOST,
        "port": _GATEWAY_PORT,
        "failures": _gw_consecutive_failures,
    })
    p = Path(_GATEWAY_HEARTBEAT_PATH)
    # Read prior tail, append, truncate to last 20.
    try:
        tail: list[str] = []
        if p.exists():
            try:
                tail = p.read_text().splitlines()[-19:]
            except Exception:
                tail = []
        tail.append(line)
        p.write_text("\n".join(tail) + "\n")
    except Exception:
        pass

# Tag in comment body so we can identify our own nudges and count them
_NUDGE_TAG = "[watchdog-nudge]"

# ── canonical comment markers (STATE.md §3 invariant 8) ────────────────────
# Watchdog never invents free-text equivalents — only parse these.
_MARKER_RE = re.compile(r"\[(?P<tag>[a-z][a-z0-9-]*)\]\s*(?P<rest>[^\n]*)", re.IGNORECASE)
# Values may be: "double-quoted" | 'single-quoted' | bare-token. Single-quoted
# support added 2026-05-14 (E1) so PM agents can embed JSON-as-string in
# `[pm-plan] subtasks='[...]'` without escaping every internal double quote.
_KV_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)")

# PM mutex TTL — STATE.md §3 invariant 2.
_PM_LOCK_TTL_SECONDS = int(os.environ.get("WATCHDOG_PM_LOCK_TTL_SECONDS", "300"))

# Tick counter for throttled checks (e.g. provider registry validator runs
# every Nth tick to avoid log spam). Incremented at the start of _process_once.
_TICK_COUNT = 0
_PROVIDER_VALIDATOR_EVERY = int(os.environ.get("WATCHDOG_PROVIDER_VALIDATOR_EVERY", "10"))


def _parse_markers(message: str | None) -> list[dict[str, Any]]:
    """Return list of {tag, kv} dicts found in a comment message.

    Only first match per tag per line. Quoted values keep spaces.
    """
    if not message:
        return []
    out: list[dict[str, Any]] = []
    for m in _MARKER_RE.finditer(message):
        tag = m.group("tag").lower()
        kv: dict[str, str] = {}
        for k, v in _KV_RE.findall(m.group("rest") or ""):
            # Strip surrounding quote of either kind; leave inner characters intact.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            kv[k.lower()] = v
        out.append({"tag": tag, "kv": kv})
    return out


# ── helpers ────────────────────────────────────────────────────────────────


def _iso_to_ts(raw: str | datetime | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.timestamp()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ── DB queries ─────────────────────────────────────────────────────────────


# T104 (2026-05-24): owner-question patterns — when an agent writes one
# of these phrases in a comment on a `review`/`in_progress` task, the
# system treats it as "agent has output that needs owner confirmation"
# and escalates to /review immediately, bypassing the 20-min stale wait.
# Owner request: "Only when there is a significant system issue or
# something unresolvable by the agents it should be asking for my input
# in /review."
_OWNER_QUESTION_PATTERNS = (
    "prompt to user",
    "prompt to owner",
    "awaiting owner",
    "awaiting user",
    "owner input",
    "owner approval",
    "owner confirmation",
    "owner decision",
    "please confirm",
    "please verify",
    "does this match",
    "is this correct",
    "ready for review",
    "ready for owner",
    "blocked: need owner",
    "human-in-the-loop",
    "[needs-owner]",
)


def _legacy_where(alias: str = "") -> str:
    """SQL equivalent of PEV's _harness_owned; aliases are source constants."""
    p = alias + "." if alias else ""
    return (f"COALESCE({p}execution_mode, 'single') != 'team' "
            f"AND COALESCE({p}execution_lane, '') != 'harness' "
            f"AND (COALESCE({p}execution_lane, '') != '' OR "
            f"({p}title NOT LIKE '[agent]%' AND {p}title NOT LIKE '[claude]%' "
            f"AND {p}title NOT LIKE '[codex]%'))")


async def _detect_owner_questions(db) -> int:
    """T104 (2026-05-24): scan recent comments for owner-input requests.

    When an agent's comment matches any pattern in `_OWNER_QUESTION_PATTERNS`,
    escalate the parent task to Owner Review immediately. Dedup: skip
    tasks that already have an open `owner_reviews` row pointing at them.
    """
    res = await db.execute(text(f"""
        SELECT
            t.id, t.title, t.status, t.board_id, t.assigned_agent_id,
            t.created_at, t.updated_at,
            a.name AS agent_name,
            ae.message, t.execution_mode, t.execution_lane
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        JOIN activity_events ae
          ON ae.task_id = t.id
         AND ae.event_type = 'task.comment'
         AND ae.created_at > NOW() - INTERVAL '2 hours'
        WHERE t.status IN ('in_progress', 'review')
          AND {_legacy_where('t')}
          ORDER BY ae.created_at DESC
        LIMIT 200
    """))
    rows = res.fetchall()
    if not rows:
        return 0

    # Already-escalated tasks (open owner_reviews row)
    open_res = await db.execute(text(
        "SELECT task_id FROM owner_reviews WHERE status IN ('pending','revision_requested') AND task_id IS NOT NULL"
    ))
    open_task_ids = {str(r[0]) for r in open_res.fetchall()}

    escalated = 0
    seen_task: set[str] = set()
    for r in rows:
        tid = str(r[0])
        if tid in seen_task or tid in open_task_ids:
            continue
        msg = (r[8] or "").lower()
        if not any(p in msg for p in _OWNER_QUESTION_PATTERNS):
            continue
        seen_task.add(tid)
        task_dict = {
            "id": tid,
            "title": r[1],
            "status": r[2],
            "board_id": str(r[3]) if r[3] else None,
            "assigned_agent_id": str(r[4]) if r[4] else None,
            "agent_name": r[7],
            "execution_mode": r[9], "execution_lane": r[10],
            "age_hours": 0.0,
        }
        try:
            if await _lock_legacy_task(db, tid, r[6], r[2]) is None:
                continue
            if _escalate_to_owner_review(task_dict, severity="stale"):
                await _post_comment(
                    db, tid, task_dict["board_id"] or "",
                    f"{_NUDGE_TAG} **Escalated to Owner Review** — agent posted "
                    f"an owner-input request (matched pattern: "
                    f"{next(p for p in _OWNER_QUESTION_PATTERNS if p in msg)!r}). "
                    f"See /review."
                )
                escalated += 1
            await db.rollback()
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.owner_question.escalate_failed task=%s err=%s", tid, e)
    return escalated


async def _stale_tasks(db) -> list[dict[str, Any]]:
    """Return tasks currently stuck in_progress or review.

    T107 (2026-05-26): exclude tasks owned by an active PEV state machine.
    PEV writes to `task_pev_state` table — `tasks.updated_at` stays at the
    initial value while PEV runs, so the bare `updated_at` age check
    flagged every PEV task as stale. PEV has its own escalation path
    (planner/executor/verifier failures file their own owner-review items),
    so the staleness path should leave PEV alone.

    2026-10-05: also exclude (a) tasks with a healthy live native team cycle
    (cycle in an active state whose deadline has not passed — lease expiry
    and reaping are owned by team_cycle, so a quiet `updated_at` there is not
    staleness) and (b) review tasks with a PUBLISHED team cycle — those are
    successfully published work awaiting owner/delegated acceptance, not
    stuck work; escalating them as "stale" was pure queue noise. Cycles past
    their deadline and genuinely stuck agent tasks still escalate as before.
    Harness/team work, including legacy single-mode harness cards, belongs to
    its own execution/acceptance controls, never legacy nudges or reassignment.
    """
    result = await db.execute(
        text(f"""
        SELECT
            t.id, t.title, t.status, t.board_id, t.assigned_agent_id,
            t.created_at, t.updated_at,
            a.name AS agent_name, t.execution_mode, t.execution_lane
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        WHERE t.status IN ('in_progress','review')
          AND {_legacy_where('t')}
          AND NOT EXISTS (
              SELECT 1 FROM task_pev_state s
              WHERE s.task_id = t.id
                AND s.status IN ('planning','executing','verifying')
          )
          AND NOT EXISTS (
              SELECT 1 FROM team_cycles c
              WHERE c.task_id = t.id
                AND (
                    (c.state IN ('planning','developing','qa','qa_passed','publishing')
                     AND c.deadline_at > NOW())
                    OR (c.state = 'published' AND t.status = 'review')
                    OR (c.state = 'delegated' AND t.split_count > 0)
                )
          )
        ORDER BY t.updated_at ASC
        LIMIT 200
    """)
    )
    rows = result.fetchall()
    out = []
    now_ts = datetime.now(timezone.utc).timestamp()
    for r in rows:
        upd_ts = _iso_to_ts(r[6])
        age = (now_ts - upd_ts) if upd_ts else 0.0
        threshold = _STALE_REVIEW if r[2] == "review" else _STALE_IN_PROGRESS
        if age <= threshold:
            continue
        out.append(
            {
                "id": str(r[0]),
                "title": r[1],
                "status": r[2],
                "board_id": str(r[3]) if r[3] else None,
                "assigned_agent_id": str(r[4]) if r[4] else None,
                "agent_name": r[7],
                "execution_mode": r[8], "execution_lane": r[9],
                "updated_at": r[6],
                "age_hours": round(age / 3600, 1),
            }
        )
    return out


async def _existing_nudge_count(db, task_id: str) -> tuple[int, float | None]:
    """Count prior watchdog nudges + return ts of latest nudge.
    Comments are activity_events rows with event_type='task.comment'.
    """
    result = await db.execute(
        text("""
            SELECT created_at FROM activity_events
            WHERE task_id = :tid
              AND event_type = 'task.comment'
              AND message LIKE :pat
            ORDER BY created_at DESC
        """),
        {"tid": task_id, "pat": f"%{_NUDGE_TAG}%"},
    )
    rows = result.fetchall()
    if not rows:
        return 0, None
    latest_ts = _iso_to_ts(rows[0][0])
    return len(rows), latest_ts


async def _count_comments_matching(db, task_id: str, needle: str) -> int:
    """Return number of comments on task_id that contain needle."""
    result = await db.execute(
        text("""
            SELECT COUNT(*) FROM activity_events
            WHERE task_id = :tid
              AND event_type = 'task.comment'
              AND message LIKE :pat
        """),
        {"tid": task_id, "pat": f"%{needle}%"},
    )
    return result.scalar() or 0




# An owner STOP directive ends a recurring task, so it must be an instruction,
# not an incidental word. The previous pattern matched a bare `finish` anywhere
# in the text, so an operator note containing "...before it could finish"
# silently disabled the schedule. Require either a terse whole-comment
# directive, or a phrase that explicitly names the recurrence.
_STOP_WHOLE_COMMENT_RE = re.compile(
    r"^\s*(finished|finish|done|stop|cancel|close)\b[\s.!]*$", re.IGNORECASE
)
_STOP_PHRASE_RE = re.compile(
    r"\b(?:"
    r"(?:stop|cancel|disable|end)\s+(?:the\s+)?(?:repeat\w*|recurr\w*|cron|schedule)"
    r"|no\s+more"
    r"|that'?s\s+enough"
    r"|mark\s+(?:it\s+)?(?:as\s+)?done"
    r"|(?:you\s+can\s+)?close\s+(?:it|this)"
    r")\b",
    re.IGNORECASE,
)


def _is_owner_stop_directive(comment: str) -> bool:
    """True when an owner comment tells us to end a recurring task."""
    text_value = comment.strip()
    if not text_value:
        return False
    return bool(
        _STOP_WHOLE_COMMENT_RE.match(text_value) or _STOP_PHRASE_RE.search(text_value)
    )


async def _seconds_since_comment_matching(db, task_id: str, needle: str) -> float | None:
    """Seconds since the newest comment containing `needle`, None if there is none."""
    result = await db.execute(
        text("""
            SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))
            FROM activity_events
            WHERE task_id = :tid
              AND event_type = 'task.comment'
              AND message LIKE :pat
        """),
        {"tid": task_id, "pat": f"%{needle}%"},
    )
    value = result.scalar()
    return float(value) if value is not None else None



async def _count_comments_this_cycle(db, task_id: str, needle: str) -> int:
    """Count matching comments since the task's current cycle began.

    A recurring task keeps one row for its whole life, so a lifetime count
    only ever grows: after enough cycles every fresh cycle starts already past
    the give-up caps below, and the task is written off while the current
    cycle is perfectly healthy. `[cycle-complete]` is the cycle boundary; a
    one-shot task has none, so its whole life is one cycle and the count is
    unchanged.
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*) FROM activity_events
            WHERE task_id = :tid
              AND event_type = 'task.comment'
              AND message LIKE :pat
              AND created_at > COALESCE((
                  SELECT MAX(c.created_at) FROM activity_events c
                  WHERE c.task_id = :tid
                    AND c.event_type = 'task.comment'
                    AND c.message LIKE '[cycle-complete]%'
              ), TIMESTAMP '1970-01-01')
        """),
        {"tid": task_id, "pat": f"%{needle}%"},
    )
    return result.scalar() or 0


async def _lock_legacy_task(db, task_id: str, stamp, status=None):
    """Fence stale legacy scans without holding a row lock during inference."""
    from app.models.tasks import Task
    from app.services.pev_loop import _harness_owned

    task = await db.get(Task, uuid.UUID(str(task_id)))
    if task is not None:
        await db.refresh(task, with_for_update=True)
    expected = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    if (task is None or _harness_owned(task.model_dump())
            or task.status in {"blocked", "cancelled", "paused", "trash"}
            or task.updated_at != expected
            or (status is not None and task.status != status)):
        await db.rollback()
        return None
    return task


async def _post_comment(db, task_id: str, board_id: str, content: str, *, commit=True, allow_native=False) -> bool:
    """Post comment; guarded legacy callers keep status and markers atomic."""
    try:
        inserted = await db.execute(
            text(f"""
                INSERT INTO activity_events (id, event_type, message, agent_id, task_id, board_id, created_at)
                SELECT :id, 'task.comment', :msg, NULL, :tid, :bid, NOW()
                WHERE :allow_native OR EXISTS (
                    SELECT 1 FROM tasks WHERE id=:tid
                    AND {_legacy_where()}
                    )
            """),
            {
                "id": str(uuid.uuid4()),
                "msg": content,
                "tid": task_id,
                "bid": board_id if board_id else None,
                "allow_native": allow_native,
            },
        )
        if inserted.rowcount != 1:
            await db.rollback()  # A refused marker must not commit earlier caller effects.
            if not commit:
                raise RuntimeError("legacy comment fenced by current native decision")
            return False
        if commit:
            await db.commit()
        return True
    except Exception as e:
        logger.warning("watchdog.comment.insert_failed task=%s err=%s", task_id, e)
        await db.rollback()
        if not commit:
            raise  # do not continue guarded effects after losing the row lock
        return False


async def _has_marker(db, task_id: str, marker: str) -> bool:
    """True if the task already has an activity comment starting with `marker`
    (e.g. '[understand]'). Used to make P3 stages idempotent across ticks."""
    try:
        r = await db.execute(
            text(
                "SELECT 1 FROM activity_events WHERE task_id = :tid "
                "AND event_type = 'task.comment' AND message LIKE :m LIMIT 1"
            ),
            {"tid": task_id, "m": marker + "%"},
        )
        return r.first() is not None
    except Exception:
        return False


# P3 Slices 3+4 (2026-09-01, GRAND-PLAN): explicit UNDERSTAND + RESEARCH stages.
# Tool-less `call_model` summaries posted as [understand] / [research] markers on
# inbox tasks of AUTONOMOUS boards. Gated OFF by default + idempotent per marker.
# Additive: never changes task status/routing. Real tool-backed browsing (camoufox)
# is a later gateway-agent phase; this slice is deterministic enrichment.
_UNDERSTAND_PROMPT = (
    "You are the UNDERSTAND stage of an autonomous task pipeline. Read the task "
    "title and description and state, in 3-5 tight bullets: the real goal, the "
    "acceptance criteria (how we'd know it's done), and the key unknowns/risks. "
    "No preamble, bullets only."
)
_RESEARCH_PROMPT = (
    "You are the RESEARCH stage of an autonomous task pipeline. Given the task "
    "and its understanding, list in 3-5 tight bullets what needs to be gathered "
    "or investigated to do it well, plus any known approach or prior art to reuse. "
    "No preamble, bullets only."
)


async def _run_pipeline_stages() -> int:
    """P3: run UNDERSTAND then RESEARCH enrichment on autonomous-board inbox
    tasks, each behind its own runtime flag. Returns count of markers posted."""
    from app.services.runtime_settings import get_setting as _gs
    understand_on = _gs("WATCHDOG_UNDERSTAND_ENABLED", False)
    research_on = _gs("WATCHDOG_RESEARCH_ENABLED", False)
    if not (understand_on or research_on):
        return 0
    posted = 0
    try:
        from app.services.llm_evaluator import call_model
        async with async_session_maker() as db:
            res = await db.execute(text("""
                SELECT t.id, t.title, t.description, t.board_id
                FROM tasks t
                JOIN boards b ON b.id = t.board_id
                WHERE b.autonomous_enabled = true
                  AND t.status = 'inbox'
                ORDER BY t.updated_at ASC
                LIMIT 8
            """))
            rows = res.fetchall()
        for r in rows:
            if posted >= 2:  # cap LLM load per tick
                break
            tid, title, desc, bid = str(r[0]), r[1] or "", r[2] or "", str(r[3]) if r[3] else ""
            if not (title or desc).strip():
                continue
            task_text = f"TITLE: {title}\n\nDESCRIPTION:\n{desc}".strip()
            async with async_session_maker() as db:
                # UNDERSTAND first
                if understand_on and not await _has_marker(db, tid, "[understand]"):
                    try:
                        out = await call_model(
                            "omniroute-auto",
                            f"{_UNDERSTAND_PROMPT}\n\n{task_text}",
                            max_tokens=256, timeout_seconds=60,
                        )
                    except Exception as exc:
                        logger.debug("pipeline.understand_err task=%s err=%s", tid[:8], exc)
                        out = None
                    if out and out.strip():
                        if await _post_comment(db, tid, bid, f"[understand]\n{out.strip()}"):
                            posted += 1
                            logger.info("watchdog.pipeline.understand task=%s", tid[:8])
                        continue  # let RESEARCH run on the NEXT tick (needs understand first)
                # RESEARCH — only after understand exists
                if (
                    research_on
                    and await _has_marker(db, tid, "[understand]")
                    and not await _has_marker(db, tid, "[research]")
                ):
                    try:
                        out = await call_model(
                            "omniroute-auto",
                            f"{_RESEARCH_PROMPT}\n\n{task_text}",
                            max_tokens=256, timeout_seconds=60,
                        )
                    except Exception as exc:
                        logger.debug("pipeline.research_err task=%s err=%s", tid[:8], exc)
                        out = None
                    if out and out.strip():
                        if await _post_comment(db, tid, bid, f"[research]\n{out.strip()}"):
                            posted += 1
                            logger.info("watchdog.pipeline.research task=%s", tid[:8])
    except Exception as e:
        logger.warning("watchdog.pipeline_stages_failed err=%s", e)
    return posted


# Meta-task title prefixes that should NEVER reach Owner Review.
# Test harness + autonomy harness already manage their own retry/cleanup.
_META_PREFIXES = (
    "TEST:", "AUTONOMY-FIX:", "WATCHDOG:", "AUTONOMY-FIX",
    "Stale review: AUTONOMY", "Stale review: TEST",
)


def _is_meta_task(task: dict[str, Any]) -> bool:
    title = task.get("title", "") or ""
    return any(title.startswith(p) for p in _META_PREFIXES)


def _escalate_to_owner_review(task: dict[str, Any], severity: str = "stale") -> bool:
    """Append task to Owner Review queue (Postgres `owner_reviews` table — T18).

    severity ∈ {'stale', 'infra', 'catastrophic'}.
    Per POLICY P11.1, only `catastrophic` and `infra` should ever reach Owner Review.
    `stale` items are filtered: meta-tasks dropped entirely; non-meta are dropped if
    any open infra incident is filed.
    """
    from app.services.owner_reviews import (
        has_open_infra_for_watchdog,
        insert_sync,
        task_recently_escalated,
    )

    from app.services.pev_loop import _harness_owned

    if _harness_owned(task):
        return False

    # Hard filter: never escalate meta-tasks; agent retry loops handle them.
    if severity == "stale" and _is_meta_task(task):
        logger.info(
            "watchdog.escalate.skipped_meta task=%s title=%s",
            task["id"], (task.get("title") or "")[:60],
        )
        return False

    # If there's an open watchdog-infra incident, suppress per-task stale escalations.
    if severity == "stale" and has_open_infra_for_watchdog():
        logger.info(
            "watchdog.escalate.suppressed task=%s reason=open-infra-incident",
            task["id"],
        )
        return False

    # Open owner work never expires into a new escalation; closed work has a cooldown.
    if task_recently_escalated(task["id"], cooldown_sec=24 * 3600):
        logger.info("watchdog.escalate.cooldown task=%s", task["id"])
        return False

    return insert_sync(
        title=f"WATCHDOG: stale task — {task['title'][:80]}",
        type_="other",
        summary=(
            f"Task {task['id']} ({task['status']}) stuck for {task['age_hours']}h, "
            f"assigned to {task.get('agent_name') or 'unassigned'}. "
            f"{_MAX_NUDGES_BEFORE_ESCALATE} nudges sent without progress."
        ),
        reason="Auto-escalated by pipeline watchdog after exhausting nudge attempts.",
        proposed_action=(
            "Owner to: (a) reassign manually, (b) close as won't-fix, or "
            "(c) accept assignee status and resume."
        ),
        submitted_by="Pipeline Watchdog",
        task_id=task["id"],
    )


def _open_infra_incident(reason: str, detail: str) -> bool:
    """File a single Owner Review item for a provider/hardware outage.
    Idempotent — won't duplicate an existing pending infra item with same reason.
    """
    from app.services.owner_reviews import has_open_infra_for, insert_sync
    if has_open_infra_for(reason):
        return False
    ok = insert_sync(
        title=f"INFRA: {reason}",
        type_="infrastructure",
        summary=detail,
        reason=reason,
        proposed_action=(
            "Owner: bring resource back online OR confirm permanent retirement. "
            "Watchdog will auto-resolve this item once the resource is reachable again."
        ),
        submitted_by="Pipeline Watchdog (infra)",
    )
    if ok:
        logger.warning("watchdog.infra.opened reason=%s", reason)
    return ok


def _close_infra_incident_if_resolved(reason: str) -> bool:
    """If the originating reason no longer applies, mark the infra item approved with note."""
    from app.services.owner_reviews import close_infra_incident
    closed = close_infra_incident(reason)
    if closed > 0:
        logger.info("watchdog.infra.auto_resolved reason=%s n=%d", reason, closed)
        return True
    return False


# T40f (2026-05-14): independent cadence for `_check_provider_health` so
# its 2-attempt HTTP probes don't pin the main 180s tick. Tracks last run
# at module scope; tick decides whether to invoke.
_PROVIDER_HEALTH_INTERVAL = float(
    get_setting("WATCHDOG_PROVIDER_HEALTH_INTERVAL_SECONDS"),
)
_provider_health_last_run: float = 0.0

# T103 v16 (2026-05-19): event-driven OR→MC sync via the webhook receiver
# is the primary path. The watchdog still runs `reverse_sync_to_mc_db`
# every ~10 min as a failsafe (in case OR forgets to fire or the webhook
# subscription drifts). 0 disables the failsafe entirely.
_ORSYNC_FAILSAFE_INTERVAL = float(
    get_setting("WATCHDOG_OMNIROUTE_REVERSE_SYNC_FAILSAFE_SECONDS"),
)
_orsync_failsafe_last_run: float = 0.0


def _provider_health_due() -> bool:
    """True if the configured interval has elapsed since the last probe."""
    global _provider_health_last_run
    now = time.monotonic()
    if (now - _provider_health_last_run) < _PROVIDER_HEALTH_INTERVAL:
        return False
    _provider_health_last_run = now
    return True


async def _check_provider_health() -> None:
    """T103 v6 (2026-05-18): NEUTRALIZED.

    OmniRoute now owns per-upstream health probing (ModelSync cycle,
    `testStatus` field). MC `reverse_sync_to_mc_db` (Step 0d1) pulls
    that state into `provider_capacity.status` + `last_error` every
    watchdog tick — no duplicate probing needed.

    Kept as a no-op stub so callers / tests that reference it don't
    break. Body retained below for reference; gated behind env opt-in.
    """
    if os.environ.get("LEGACY_PROVIDER_HEALTH_PROBE", "0") not in ("1", "true", "yes"):
        return
    import httpx
    from datetime import datetime, timezone
    from app.db.session import async_session_maker
    from app.models.provider_capacity import ProviderCapacity
    from sqlalchemy import select as sa_select

    async def _ok(url: str, timeout: float = 10.0) -> bool:
        # 10s allows for VPN tun0 cold-syn + slow first connect.
        # Two-attempt: if first fails, retry once before declaring outage.
        for _ in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get(url)
                if r.status_code < 500:
                    return True
            except Exception:
                continue
        return False

    # ── Legacy critical-path probes (vllm + ollama) keep the incident path ──
    vllm_url = os.environ.get(
        "VLLM_HEALTH_URL", "http://host.docker.internal:9090/v1/models"
    )
    ollama_url = os.environ.get(
        "OLLAMA_HEALTH_URL",
        "http://host.docker.internal:8000/api/v1/proxy/ollama/v1/models",
    )

    vllm_ok = await _ok(vllm_url)
    ollama_ok = await _ok(ollama_url)

    if not vllm_ok:
        _open_infra_incident(
            "lan-local primary unreachable",
            f"LAN-local primary endpoint {vllm_url} did not respond. "
            "Agents will failover to their configured cloud-tier fallback chain. "
            "Tasks requiring large-context analysis may be blocked until restored.",
        )
    else:
        _close_infra_incident_if_resolved("lan-local primary unreachable")

    if not ollama_ok:
        _open_infra_incident(
            "cloud-tier fallback unreachable",
            f"Cloud-tier proxy {ollama_url} did not respond. "
            "If LAN-local primary also down, all LLM operations halted. CRITICAL.",
        )
    else:
        _close_infra_incident_if_resolved("cloud-tier fallback unreachable")

    # ── T68: probe every DB-registered FREE provider with a `base_url` ──
    # Updates `provider_capacity.status` to "online" / "offline" so the
    # worker picker (`_pick_worker_for_role`) skips offline providers
    # automatically.
    #
    # Filter: providers with `api_key` set are presumed paid (deepseek-
    # style) and the auto-probe must NOT auto-revive them — T49 cost
    # lockdown disabled them on purpose. Owner re-enables via the UI
    # explicitly. Free local providers (vllm, hauhau, ollama-proxy) have
    # no api_key and DO get auto-revival when reachable.
    try:
        async with async_session_maker() as db:
            rows = (await db.execute(sa_select(ProviderCapacity))).scalars().all()
            now_utc = datetime.now(timezone.utc)
            for p in rows:
                if not p.base_url:
                    continue
                if p.api_key:
                    # Paid provider — owner-managed only; skip the auto-probe.
                    continue
                # Probe `<base_url>/models` (works for OpenAI-compat providers).
                probe_url = p.base_url.rstrip("/") + "/models"
                alive = await _ok(probe_url, timeout=5.0)
                new_status = "online" if alive else "offline"
                if p.status != new_status:
                    logger.info(
                        "provider_health.flipped provider=%s status=%s→%s",
                        p.provider_name, p.status, new_status,
                    )
                    p.status = new_status
                p.last_checked_at = now_utc
            await db.commit()
    except Exception as exc:
        logger.warning("provider_health.db_probe_failed err=%s", exc)


# ── main loop ──────────────────────────────────────────────────────────────


async def _find_senior_variant(db, role_keyword: str, exclude_agent_id: str | None = None) -> str | None:
    """Find Senior (ollama-backed) variant agent for given role for tier-2 escalation.
    Looks for an agent named '<role> Senior'. Returns DB UUID or None.
    """
    senior_name = f"{role_keyword} Senior"
    result = await db.execute(text("""
        SELECT a.id, COALESCE((SELECT COUNT(*) FROM tasks t
                WHERE t.assigned_agent_id = a.id AND t.status = 'in_progress'), 0) AS wip
        FROM agents a
        WHERE a.name = :name AND a.status != 'retired'
        LIMIT 1
    """), {"name": senior_name})
    row = result.first()
    if not row:
        return None
    if exclude_agent_id and str(row[0]) == exclude_agent_id:
        return None
    if row[1] >= 3:  # Senior variant is overloaded
        return None
    return str(row[0])


async def _detect_contested_verdict(db) -> int:
    """Find review tasks with BOTH PASS and FAIL QA verdict comments → open ballot.
    Returns count of ballots opened.
    """
    result = await db.execute(text(f"""
        SELECT DISTINCT ae.task_id, t.title, t.board_id, t.updated_at
        FROM activity_events ae JOIN tasks t ON t.id = ae.task_id
        WHERE ae.event_type='task.comment'
          AND t.status='review'
          AND {_legacy_where('t')}
          AND ae.task_id IN (
            SELECT task_id FROM activity_events
            WHERE event_type='task.comment' AND LOWER(message) LIKE '%qa verdict: pass%'
          )
          AND ae.task_id IN (
            SELECT task_id FROM activity_events
            WHERE event_type='task.comment' AND LOWER(message) LIKE '%qa verdict: fail%'
          )
        LIMIT 10
    """))
    rows = result.fetchall()
    if not rows:
        return 0
    # Skip tasks that already have an open ballot for them
    import sqlite3
    votes_db = Path(os.environ.get("VOTES_DB_FILE", "/traffic-data/votes.db"))
    if not votes_db.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{votes_db}?mode=rw", uri=True, check_same_thread=False)
    except Exception:
        return 0
    opened = 0
    for tid, title, bid, stamp in rows:
        tid_s = str(tid)
        if await _lock_legacy_task(db, tid_s, stamp, "review") is None:
            continue
        # Hold current task ownership through the independently committed ballot.
        existing = conn.execute(
            "SELECT 1 FROM ballots WHERE target_task_id=? AND status='open' LIMIT 1",
            (tid_s,),
        ).fetchone()
        if existing:
            await db.rollback()
            continue
        bid_uuid = str(uuid.uuid4())
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        conn.execute("""
            INSERT INTO ballots
            (id, topic, description, proposed_action, target_task_id, target_board_id,
             required_quorum, veto_roles, status, created_by, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            bid_uuid,
            f"Contested QA Verdict: {(title or '')[:80]}",
            "Both QA Verdict: PASS and FAIL exist for this review task. Vote to resolve.",
            "Resolve verdict by majority vote; QA + Security have veto power.",
            tid_s, str(bid) if bid else None,
            3, "qa engineer,security architect",
            "open", "Pipeline Watchdog",
            datetime.now(timezone.utc).isoformat(), expires,
        ))
        conn.commit()
        # Comment on task
        await _post_comment(db, tid_s, str(bid) if bid else "",
            f"{_NUDGE_TAG} **Tier 3 — Contested verdict ballot opened** "
            f"(ballot {bid_uuid[:8]}, expires 6h). Voters resolve majority + veto. "
            f"Per POLICY P11.3.")
        opened += 1
    conn.close()
    return opened


async def _close_expired_ballots() -> int:
    """Scan open ballots past expires_at; auto-close per quorum/majority rules."""
    import sqlite3
    votes_db = Path(os.environ.get("VOTES_DB_FILE", "/traffic-data/votes.db"))
    if not votes_db.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{votes_db}?mode=rw", uri=True, check_same_thread=False)
    except Exception:
        return 0
    closed = 0
    # T107 round-7 (2026-05-27): ALSO close ballots > 24h old even when
    # expires_at is NULL. Old test-harness ballots had no expires_at and
    # piled up — conflict.tiebreaker invariant failed because of them.
    rows = conn.execute("""
        SELECT id FROM ballots
        WHERE status='open'
          AND (
            (expires_at IS NOT NULL AND datetime(expires_at) < datetime('now'))
            OR datetime(created_at) < datetime('now', '-24 hours')
          )
    """).fetchall()
    for (bid,) in rows:
        # Tally
        tally = conn.execute(
            "SELECT verdict, COUNT(*) FROM votes WHERE ballot_id=? GROUP BY verdict",
            (bid,),
        ).fetchall()
        counts = {"approve": 0, "reject": 0, "abstain": 0}
        for v, n in tally:
            counts[v] = n
        if counts["approve"] > counts["reject"]:
            outcome, reason = "approved", f"{counts['approve']}/{counts['reject']} on expiry"
        elif counts["reject"] > counts["approve"]:
            outcome, reason = "rejected", f"{counts['reject']}/{counts['approve']} on expiry"
        else:
            outcome, reason = "expired", "no quorum / tied at expiry"
        conn.execute(
            "UPDATE ballots SET status='closed', outcome=?, outcome_reason=?, closed_at=? WHERE id=?",
            (outcome, reason, datetime.now(timezone.utc).isoformat(), bid),
        )
        conn.commit()
        closed += 1
    conn.close()
    return closed


async def _find_idle_clone(db, role_keyword: str) -> str | None:
    """Find an idle clone of given role to receive overflow work.
    role_keyword examples: 'Software Architect', 'QA Engineer', 'DevOps Engineer', 'Reverse Engineer'.
    Returns DB UUID or None.

    T107 (2026-05-26): require `status='online'`. Previously offline agents
    (e.g. retired clones, dead leads) were valid reassignment targets,
    sending fresh tasks to a black hole that immediately re-staled.
    """
    result = await db.execute(
        text("""
        SELECT a.id, a.name,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.assigned_agent_id = a.id AND t.status = 'in_progress') AS wip
        FROM agents a
        WHERE a.name LIKE :kw
          AND a.status = 'online'
          AND a.name NOT IN (
              SELECT name FROM agents WHERE name = :exact
          )
        ORDER BY wip ASC, a.name ASC
    """),
        {"kw": f"{role_keyword}%", "exact": role_keyword},
    )
    rows = result.fetchall()
    # Prefer agents (including clones) with WIP < 4
    for r in rows:
        if r[2] < 4:  # WIP < 4 (raised from 2 to support dual-agent architecture)
            return str(r[0])
    return None


# ── Role keyword mapping for inbox routing ─────────────────────────────────

_ROUTE_KEYWORDS = (
    # Order matters — first match wins. Longest-most-specific first.
    ("security", "Security Architect"),
    ("auth", "Security Architect"),
    ("cryptography", "Security Architect"),
    ("crackme", "Reverse Engineer"),
    ("ida-pro", "Reverse Engineer"),
    ("disassembl", "Reverse Engineer"),
    ("reverse", "Reverse Engineer"),
    ("binary forensic", "Binary Analyst"),
    ("malware", "Binary Analyst"),
    ("binary", "Binary Analyst"),
    ("crack", "Binary Analyst"),
    ("pentest", "Red Team Operator"),
    ("exploit", "Red Team Operator"),
    ("att&ck", "Red Team Operator"),
    ("attack", "Red Team Operator"),
    ("defense", "Red Team Operator"),
    ("red", "Red Team Operator"),
    ("deploy", "DevOps Engineer"),
    ("pipelines", "DevOps Engineer"),
    ("ci-cd", "DevOps Engineer"),
    ("docker", "DevOps Engineer"),
    ("infra", "DevOps Engineer"),
    ("qa", "QA Engineer"),
    ("test", "QA Engineer"),
    ("debug", "QA Engineer"),
    ("multi-stage", "Software Architect"),
    ("distributed", "Software Architect"),
    ("cross-board", "Software Architect"),
    ("migration", "Software Architect"),
    ("schema", "Software Architect"),
        ("architecture", "Software Architect"),
    ("system", "Software Architect"),
    ("research", "Software Architect"),
    # T64 (2026-05-15): German keywords. Generic owner-written tasks
    # ("zustandsabfrage", "prüfung", "antworte ...") route to QA Engineer
    # by default since they're typically "answer this question" tasks.
    ("zustand", "QA Engineer"),
    ("abfrage", "QA Engineer"),
    ("antwort", "QA Engineer"),
    ("antworte", "QA Engineer"),
    ("prüfung", "QA Engineer"),
    ("pruefung", "QA Engineer"),
    ("überprüf", "QA Engineer"),
    ("ueberpruef", "QA Engineer"),
    ("frage", "QA Engineer"),
    ("status", "QA Engineer"),
    ("check", "QA Engineer"),
    ("verify", "QA Engineer"),
    ("reply", "QA Engineer"),
    ("respond", "QA Engineer"),
    ("answer", "QA Engineer"),
)


async def _pm_acquire_lock(db, task_id: str, pm_agent_id: str) -> bool:
    """Atomic mutex over the PM cohort. STATE.md §3 invariant 2.

    Lock granted when none held OR existing lock older than TTL. Returns True if
    we acquired (or refreshed our own); False if another PM holds an active lock.
    """
    result = await db.execute(text(f"""
        UPDATE tasks
        SET pm_lock_agent_id = :aid, pm_lock_at = NOW()
        WHERE {_legacy_where()} AND id = :tid
          AND (pm_lock_agent_id IS NULL
               OR pm_lock_agent_id = :aid
               OR pm_lock_at < NOW() - INTERVAL '{_PM_LOCK_TTL_SECONDS} seconds')
        RETURNING id
    """), {"aid": pm_agent_id, "tid": task_id})
    return result.first() is not None


async def _pm_release_lock(db, task_id: str, pm_agent_id: str) -> None:
    await db.execute(text(f"""
        UPDATE tasks
        SET pm_lock_agent_id = NULL, pm_lock_at = NULL
        WHERE {_legacy_where()} AND id = :tid AND pm_lock_agent_id = :aid
    """), {"tid": task_id, "aid": pm_agent_id})


_AGENT_PROVIDER_MAP_CACHE: dict[str, str] = {}
_AGENT_PROVIDER_MAP_MTIME: float = 0.0

# Capacity-accounting aliases. Agents are configured with
# `model.primary = "omniroute/omniroute-auto"` — a single egress PROXY, not a
# real backend. `_agent_provider_map` used to split that to the literal
# "omniroute", which has NO row in `provider_capacity` (that table is keyed by
# real backends: vllm/ollama/…). A missing cap made the worker-picker treat
# every omniroute agent as a zero-capacity/saturated provider and skip it,
# routing ALL work to the board lead (who doesn't execute). omniroute-auto
# routes to the vllm GPU, so its real capacity IS vllm's — resolve it here so
# usage/caps/tps all key on the same real provider.
_PROVIDER_CAPACITY_ALIAS: dict[str, str] = {
    "omniroute": "vllm",
    "omniroute-auto": "vllm",
}


async def _agent_provider_map() -> dict[str, str]:
    """Map agent NAME → provider ('vllm' / 'deepseek' / 'ollama') from
    openclaw.json. mtime-invalidated cache so repeated calls inside a single
    tick don't re-parse the JSON.
    """
    global _AGENT_PROVIDER_MAP_CACHE, _AGENT_PROVIDER_MAP_MTIME
    cfg_path = _OPENCLAW_HOME / "openclaw.json"
    if not cfg_path.exists():
        return {}
    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        return _AGENT_PROVIDER_MAP_CACHE
    if mtime == _AGENT_PROVIDER_MAP_MTIME and _AGENT_PROVIDER_MAP_CACHE:
        return _AGENT_PROVIDER_MAP_CACHE
    out: dict[str, str] = {}
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        return _AGENT_PROVIDER_MAP_CACHE
    for agent in cfg.get("agents", {}).get("list", []):
        name = agent.get("name") or ""
        primary = (agent.get("model") or {}).get("primary") or ""
        prov = primary.split("/", 1)[0].lower() if "/" in primary else ""
        if prov:
            # Resolve egress-proxy labels (omniroute) to the real backend
            # so capacity accounting lines up with `provider_capacity`.
            out[name] = _PROVIDER_CAPACITY_ALIAS.get(prov, prov)
    _AGENT_PROVIDER_MAP_CACHE = out
    _AGENT_PROVIDER_MAP_MTIME = mtime
    return out


async def _provider_live_usage(db, include_review: bool = False) -> dict[str, int]:
    """Return {provider: working_task_count} computed live.

    T40a (2026-05-14): default counts only `in_progress` tasks (the worker
    actively holds the agent). Pass `include_review=True` to add `review`
    tasks too (legacy behavior — previously overcounted because review-stage
    tasks no longer occupy the worker but were still counted).

    Joins tasks → agents → openclaw.json provider map. Authoritative; replaces
    the stale provider_capacity.current_usage column.
    """
    name_to_prov = await _agent_provider_map()
    if not name_to_prov:
        return {}
    statuses = ("in_progress", "review") if include_review else ("in_progress",)
    placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
    params = {f"s{i}": s for i, s in enumerate(statuses)}
    res = await db.execute(text(f"""
        SELECT a.name, COUNT(*) AS wip
        FROM tasks t JOIN agents a ON a.id = t.assigned_agent_id
        WHERE t.status IN ({placeholders})
        GROUP BY a.name
    """), params)
    counts: dict[str, int] = {}
    for name, wip in res.fetchall():
        prov = name_to_prov.get(name)
        if prov:
            counts[prov] = counts.get(prov, 0) + int(wip)
    return counts


async def _provider_caps(db) -> dict[str, dict[str, Any]]:
    res = await db.execute(text(
        "SELECT provider_name, max_concurrent, is_unlimited, status FROM provider_capacity"
    ))
    out: dict[str, dict[str, Any]] = {}
    for name, cap, unlim, status in res.fetchall():
        out[name] = {"max": int(cap), "unlimited": bool(unlim), "status": status}
    return out


async def _ollama_healthy() -> bool:
    """Check if ollama provider is healthy (not rate-limited).

    Uses traffic data: if >50% of recent ollama calls returned 503
    in the last 10 minutes, consider the provider degraded.
    """
    try:
        import sqlite3 as _sql
        conn = _sql.connect("file:/traffic-data/llm_flows.db?mode=ro", uri=True)
        recent_503 = conn.execute(
            "SELECT COUNT(*) FROM flows WHERE host='ollama.com' AND status_code=503 "
            "AND ts > datetime('now','-10 minutes')"
        ).fetchone()[0]
        recent_total = conn.execute(
            "SELECT COUNT(*) FROM flows WHERE host='ollama.com' "
            "AND ts > datetime('now','-10 minutes')"
        ).fetchone()[0]
        conn.close()
        if recent_total > 0 and recent_503 >= 5 and recent_503 >= recent_total * 0.5:
            return False
        return True
    except Exception:
        return True  # Assume healthy on error


_MAX_AGENT_CONCURRENT_TASKS = int(os.environ.get("MAX_AGENT_CONCURRENT_TASKS", "2"))


async def _provider_recent_tps(db) -> dict[str, float]:
    """T102 (2026-05-17): max TPS observed per provider in the last
    `_PROVIDER_TPS_WINDOW_MIN` minutes from `provider_benchmark`. Used
    by `_pick_worker_for_role` to prefer agents on fastest providers
    when multiple candidates tie on WIP.
    """
    win_min = int(os.environ.get("PROVIDER_TPS_WINDOW_MIN", "120"))
    try:
        res = await db.execute(text(f"""
            SELECT provider_name, MAX(tps) AS best_tps
            FROM provider_benchmark
            WHERE ts > NOW() - INTERVAL '{win_min} minutes' AND ok = true
            GROUP BY provider_name
        """))
        return {r[0]: float(r[1] or 0.0) for r in res.fetchall()}
    except Exception:
        return {}

# T95 (2026-05-17): role spillover map. When the primary role pool is
# fully saturated (every agent at WIP cap), try the adjacent role pools
# in this preference order. Keeps work flowing instead of queueing.
_ROLE_SPILLOVER: dict[str, list[str]] = {
    "QA Engineer": ["Reverse Engineer", "Binary Analyst", "Software Architect"],
    "Reverse Engineer": ["Binary Analyst", "QA Engineer", "Software Architect"],
    "Binary Analyst": ["Reverse Engineer", "QA Engineer", "Software Architect"],
    "DevOps Engineer": ["Software Architect", "QA Engineer"],
    "Security Architect": ["Reverse Engineer", "Software Architect", "Red Team Operator"],
    "Red Team Operator": ["Security Architect", "Reverse Engineer", "Binary Analyst"],
    "Software Architect": ["QA Engineer", "DevOps Engineer"],
}


async def _pick_worker_for_role(db, role_keyword: str, board_id: str) -> str | None:
    """Pool router. STATE.md §4.

    Considers all agents whose name starts with `role_keyword`. Filters to:
      • status = online
      • not retired
      • not a project manager or board lead (PMs route via _pick_pm_for_board)
      • provider has capacity (current_in_progress < max_concurrent OR unlimited)
      • **per-agent WIP cap (T92): skip agents already holding
        `MAX_AGENT_CONCURRENT_TASKS` in_progress tasks** so work distributes
        across the pool instead of piling on the first match.
    Picks the agent with lowest WIP. Stable tie-break by name.
    Returns agent uuid or None when pool fully saturated.

    Fallback safety: if PM-excluded query returns zero rows AND the pool has
    fewer than 2 non-PM agents online, retry without the PM filter and emit
    a `watchdog.pm_fallback_used` warning. Keeps single-PM/single-agent boards
    functional.
    """
    sql_strict = """
        SELECT a.id, a.name,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.assigned_agent_id = a.id AND t.status = 'in_progress') AS wip
        FROM agents a
        WHERE a.board_id = :board_id
          AND (a.name = :exact OR a.name LIKE :prefix)
          -- Include idle-OFFLINE workers, not just online ones. Workers are
          -- offline-when-idle on-demand (OFFLINE_AFTER=10min < heartbeat gap),
          -- so an online-only filter finds nothing during idle windows and the
          -- task wrongly falls back to the board lead (who doesn't execute).
          -- The assignment path wakes the picked worker; delivery re-instantiates
          -- its session. Only exclude genuinely un-assignable lifecycle states.
          AND a.status NOT IN ('retired', 'deleting', 'updating')
          AND COALESCE(a.is_project_manager, false) = false
          AND COALESCE(a.is_board_lead, false) = false
        ORDER BY wip ASC, a.name ASC
    """
    params = {"board_id": board_id, "exact": role_keyword, "prefix": f"{role_keyword}%"}
    res = await db.execute(text(sql_strict), params)
    rows = res.fetchall()
    if not rows:
        # Fallback: legacy query (no PM filter) so boards with only PM-flagged
        # agents in a role pool can still route work.
        res = await db.execute(text("""
            SELECT a.id, a.name,
                   (SELECT COUNT(*) FROM tasks t
                    WHERE t.assigned_agent_id = a.id AND t.status = 'in_progress') AS wip
            FROM agents a
            WHERE a.board_id = :board_id
              AND (a.name = :exact OR a.name LIKE :prefix)
              AND a.status NOT IN ('retired', 'deleting', 'updating')
            ORDER BY wip ASC, a.name ASC
        """), params)
        rows = res.fetchall()
        if rows:
            logger.warning(
                "watchdog.pm_fallback_used role=%r matched=%d "
                "(PM-strict query returned zero rows)",
                role_keyword, len(rows),
            )
    if not rows:
        return None
    name_to_prov = await _agent_provider_map()
    usage = await _provider_live_usage(db)
    caps = await _provider_caps(db)

    # T102 (2026-05-17): predictive routing — re-sort candidates by
    # provider TPS (descending) so agents on the fastest healthy
    # provider win ties. Falls back to original wip-asc/name-asc when
    # benchmark data is missing.
    tps_by_prov = await _provider_recent_tps(db)
    def _candidate_key(row):
        _aid, _name, _wip = row
        prov = name_to_prov.get(_name) or ""
        return (
            int(_wip or 0),                       # lowest WIP first
            -float(tps_by_prov.get(prov, 0.0)),   # highest TPS first (negative for asc)
            _name,                                 # deterministic tiebreak
        )
    rows = sorted(rows, key=_candidate_key)

    for aid, name, wip in rows:
        # T92 (2026-05-17): per-agent in_progress cap. Forces round-robin.
        if int(wip or 0) >= _MAX_AGENT_CONCURRENT_TASKS:
            continue
        prov = name_to_prov.get(name)
        if prov:
            cap = caps.get(prov)
            # Fail OPEN on an unknown provider (no capacity row): a missing cap
            # means "unmetered/unknown", NOT "zero capacity". Failing closed
            # here silently blocked all routing when agents moved to a provider
            # label absent from `provider_capacity`.
            if cap is not None:
                if not cap.get("unlimited", False):
                    if usage.get(prov, 0) >= cap.get("max", 0):
                        continue  # provider saturated; try next agent
                if cap.get("status") not in (None, "online"):
                    continue  # provider degraded
            if prov == "ollama" and not await _ollama_healthy():
                continue  # ollama returning 503 rate-limit errors
        return str(aid)

    # T95 (2026-05-17): primary pool exhausted (every agent at cap or
    # provider saturated). Spill over into adjacent role pools so work
    # keeps flowing. Tries spillover roles in preference order. Marked
    # in logs so owner can see when their role distribution is too thin.
    for spill_role in _ROLE_SPILLOVER.get(role_keyword, []):
        res2 = await db.execute(text(sql_strict),
                                {**params, "exact": spill_role, "prefix": f"{spill_role}%"})
        spill_rows = res2.fetchall()
        for aid, name, wip in spill_rows:
            if int(wip or 0) >= _MAX_AGENT_CONCURRENT_TASKS:
                continue
            prov = name_to_prov.get(name)
            if prov:
                cap = caps.get(prov)
                if cap is not None:
                    if not cap.get("unlimited", False) and usage.get(prov, 0) >= cap.get("max", 0):
                        continue
                    if cap.get("status") not in (None, "online"):
                        continue
                if prov == "ollama" and not await _ollama_healthy():
                    continue
            logger.info(
                "watchdog.role_spillover primary=%r → spill=%r agent=%r",
                role_keyword, spill_role, name,
            )
            return str(aid)
    return None


async def _pick_pm_for_board(db, board_id: str) -> str | None:
    """Priority: board_lead online → any online PM → board_lead offline → None.
    STATE.md §4: L4/L5 require a PM; L1–L3 fall back to keyword routing.
    """
    res = await db.execute(text("""
        SELECT id FROM agents
        WHERE board_id = :bid AND status = 'online'
          AND (is_board_lead = true OR is_project_manager = true)
        ORDER BY is_board_lead DESC, last_seen_at DESC NULLS LAST
        LIMIT 1
    """), {"bid": board_id})
    row = res.first()
    if row:
        return str(row[0])
    res = await db.execute(text("""
        SELECT id FROM agents
        WHERE board_id = :bid AND is_board_lead = true AND status != 'retired'
        LIMIT 1
    """), {"bid": board_id})
    row = res.first()
    return str(row[0]) if row else None


# Free-text patterns workers emit instead of canonical `[ready-to-close]`.
# Conservative — must be at start of comment line, case-insensitive.
# T23a (2026-05-14): broadened with `## Result:` / `## Summary:` / `## Findings:` /
# `## Conclusion:` markdown section headers commonly used by writer agents.
_COMPLETION_PATTERNS = re.compile(
    r"^\s*(COMPLETED[:\s]|DONE[:\s]|FINISHED[:\s]|TASK\s+COMPLETE|"
    r"PASSWORD\s*[:=]|ANSWER\s*[:=]|SOLVED[:\s]|"
    r"##\s+(Result|Summary|Findings|Conclusion|Outcome|Final)[:\s])",
    re.IGNORECASE | re.MULTILINE,
)

# T61 (2026-05-15): short-form completion phrases. Workers (especially
# multilingual) often reply with a single phrase like "ALLES KLAR" or
# "ERLEDIGT" instead of the canonical markers. Whole-message match (the
# entire comment is one of these phrases, plus optional punctuation).
_SHORT_COMPLETION_PATTERNS = re.compile(
    r"^\s*(?:\*+\s*)?(?:"
    r"OK|"
    r"DONE|"
    r"FERTIG|"
    r"ERLEDIGT|"
    r"ALLES\s+KLAR|"
    r"COMPLETE[D]?|"
    r"FINISHED|"
    r"GELÖST|"
    r"GELOEST|"
    r"SOLVED|"
    r"✓|"          # ✓
    r"✅"           # ✅
    r")(?:\s*[.!]+)?\s*(?:\*+)?\s*$",
    re.IGNORECASE,
)



async def _harness_worked(db, task_id: str) -> bool:
    """True when the coding-harness runner posted a result for this cycle.

    The runner stamps `**agent-runner** (harness=..., rc=...)` on the task. Its
    token usage never reaches `tasks.tokens_used`, so this comment is the only
    in-band evidence that a model actually ran.
    """
    res = await db.execute(text("""
        SELECT 1
        FROM activity_events ae
        JOIN tasks t ON t.id = ae.task_id
        WHERE ae.task_id = :tid
          AND ae.event_type = 'task.comment'
          AND ae.message LIKE '%**agent-runner**%'
          AND ae.created_at >= COALESCE(t.in_progress_at, t.created_at)
        LIMIT 1
    """), {"tid": task_id})
    return res.first() is not None


async def _auto_promote_inbox_on_assignee_activity(db) -> int:
    """T61 (2026-05-15): when the assigned agent posts a comment on an
    `inbox` task, that's a strong signal the agent has begun work. Move
    the task to `in_progress` automatically so the rest of the pipeline
    (freeform-completion detection, stage timing, watchdog nudges) sees
    the correct state. Without this, agents that just reply with a
    comment but never PATCH status leave tasks permanently in inbox even
    though they're being worked.

    Idempotent: only promotes tasks where status='inbox' AND the
    assignee has at least one comment (any type), AND the task wasn't
    just created in the last 10s (avoid racing the assignment notify).
    """
    res = await db.execute(text(f"""
        UPDATE tasks t
        SET status = 'in_progress',
            in_progress_at = NOW(),
            updated_at = NOW()
        WHERE {_legacy_where('t')} AND t.status = 'inbox'
          AND t.assigned_agent_id IS NOT NULL
          AND t.created_at < NOW() - INTERVAL '10 seconds'
          AND EXISTS (
              SELECT 1 FROM activity_events ae
              WHERE ae.task_id = t.id
                AND ae.event_type = 'task.comment'
                AND ae.agent_id = t.assigned_agent_id
                -- Scope to the CURRENT assignment, not the task's whole life.
                -- A recurring task keeps its original created_at, so comparing
                -- against it promoted every new cycle on the strength of a
                -- comment the agent made weeks ago -- the task looked actively
                -- worked while nothing ran.
                AND ae.created_at > COALESCE(
                    (SELECT MAX(a2.created_at)
                       FROM activity_events a2
                      WHERE a2.task_id = t.id
                        AND a2.event_type IN (
                            'task.assignee_notified', 'task.assignee_woken')),
                    t.created_at)
          )
        RETURNING t.id, t.title, t.board_id
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    for tid, title, board_id in rows:
        try:
            await _post_comment(
                db, str(tid), str(board_id) if board_id else "",
                f"{_NUDGE_TAG} **Auto-promoted to `in_progress`** — assignee "
                f"posted a comment, marking the task as actively being worked.",
            )
        except Exception:
            pass
    await db.commit()
    return len(rows)


async def _apply_owner_directives(db) -> int:
    """Owner posts a freeform comment on a task → LLM-evaluate whether it is a
    directive to change the task's goal/scope/constraints and, if so, rewrite
    the task description in place.

    Detection: latest `task.comment` with `agent_id IS NULL` (human/owner or
    system) that does NOT start with `[`/`**[` (excludes every `[watchdog-…]`,
    `[repeat-reset…]`, `[owner-directive…]` system marker → leaves genuine
    freeform owner text), with no `[owner-directive…]` marker posted at/after
    it (idempotent — processed once). One LLM call per new owner comment.
    """
    if not _OWNER_DIRECTIVES_ENABLED:
        return 0
    try:
        from app.services.llm_evaluator import evaluate_owner_directive
    except Exception:
        return 0
    res = await db.execute(text(fr"""
        SELECT t.id, t.board_id, t.description, oc.message, t.updated_at, t.status
        FROM tasks t
        JOIN LATERAL (
          SELECT message, created_at
          FROM activity_events
          WHERE task_id = t.id AND event_type = 'task.comment' AND agent_id IS NULL
            AND message !~ '^\s*\**\s*\['
            -- scripts/mc-health-report.sh and mc-claude-runner.sh post with the
            -- operator token; neither is the owner speaking.
            AND message NOT LIKE '**Automated state report**%'
            AND message NOT LIKE '**agent-runner**%'
            AND message NOT LIKE '**team/%'
            AND message NOT LIKE '**team-coordinator**%'
            AND message NOT LIKE '**team-runner**%'
            AND length(message) BETWEEN 3 AND 2000
          ORDER BY created_at DESC
          LIMIT 1
        ) oc ON TRUE
        -- Include 'done' tasks: repeating (cron) tasks rest in 'done' between
        -- cycles, and an owner comment changes the goal for FUTURE cycles.
        WHERE t.status != 'trash'
          -- Null-agent events have no authenticated human provenance. Native
          -- goals change only through the explicit owner PATCH/CAS boundary.
          AND {_legacy_where('t')}
          AND oc.created_at > NOW() - INTERVAL '1 day'
          AND NOT EXISTS (
            SELECT 1 FROM activity_events m
            WHERE m.task_id = t.id AND m.event_type = 'task.comment'
              AND m.message LIKE '%[owner-directive%'
              AND m.created_at >= oc.created_at
          )
        LIMIT 5
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    applied = 0
    for tid_u, bid_u, description, comment, stamp, status in rows:
        tid = str(tid_u)
        board_id = str(bid_u) if bid_u else ""
        try:
            # STOP intent: owner says a recurring task is finished → move to
            # `done`; `_process_interval_repeats` then disables the repeat so it
            # stays done. Keyword pre-check (no LLM) — a clear owner stop signal.
            if _is_owner_stop_directive(comment or ""):
                if await _lock_legacy_task(db, tid, stamp, status) is None:
                    continue
                reprow = (await db.execute(text(
                    "SELECT repeat_enabled FROM tasks WHERE id = :t"
                ), {"t": tid})).first()
                if reprow and reprow[0]:
                    await db.execute(
                        text("UPDATE tasks SET status='done', repeat_enabled=false, "
                             f"assigned_agent_id=NULL, updated_at=NOW() WHERE {_legacy_where()} AND id = :t"),
                        {"t": tid},
                    )
                    await _post_comment(
                        db, tid, board_id,
                        f"[owner-directive-applied] Owner ended the recurring task: "
                        f"\"{(comment or '').strip()[:200]}\" → moved to done, "
                        f"recurring schedule disabled.", commit=False)
                    await db.commit()
                    applied += 1
                    logger.info("watchdog.owner_directive.stopped task=%s", tid[:8])
                    continue
                # STOP's read-only probe must release its lock before inference.
                await db.rollback()
            revised = await evaluate_owner_directive(description or "", comment or "")
            if await _lock_legacy_task(db, tid, stamp, status) is None:
                continue
            if revised is None:
                # No verdict (LLM unreachable / unauthorized / unusable output).
                # Say so instead of claiming a review; the marker still makes
                # this comment processed, so a dead LLM cannot cause a retry storm.
                await _post_comment(
                    db, tid, board_id,
                    "[owner-directive-failed] Owner comment could NOT be evaluated "
                    "(LLM unavailable) — task description unchanged. Edit the "
                    "description directly if this was an instruction.", commit=False)
                await db.commit()
                logger.warning("watchdog.owner_directive.no_verdict task=%s", tid[:8])
                continue
            if revised and revised.strip() != (description or "").strip():
                await db.execute(
                    text(f"UPDATE tasks SET description = :d, updated_at = NOW() WHERE {_legacy_where()} AND id = :t"),
                    {"d": revised.strip(), "t": tid},
                )
                # Visible, reversible marker: record the owner comment that drove
                # the change + a snippet of the prior description.
                await _post_comment(
                    db, tid, board_id,
                    f"[owner-directive-applied] Updated task goal per owner comment: "
                    f"\"{(comment or '').strip()[:200]}\". "
                    f"Prior description archived below.\n\n---PRIOR---\n{(description or '')[:1500]}", commit=False)
                await db.commit()
                applied += 1
                logger.info("watchdog.owner_directive.applied task=%s", tid[:8])
            else:
                # Not a change directive — mark processed so we don't re-eval.
                await _post_comment(
                    db, tid, board_id,
                    "[owner-directive-noop] Owner comment reviewed — not a goal "
                    "change; no update applied.", commit=False)
                await db.commit()
        except Exception as e:
            logger.warning("watchdog.owner_directive.failed task=%s err=%s", tid[:8], e)
        finally:
            await db.rollback()
    return applied


async def _settle_repeating_cycle(db, tid: str, board_id: str, note: str, *, commit=True) -> bool:
    """If task `tid` is a REPEATING task, settle its finished cycle by returning
    it to `inbox` (a non-terminal 'waiting for next run' state) instead of
    `done`, and stamp a `[cycle-complete]` marker (the router's cooldown keys on
    it). Returns True when handled (caller must NOT write `done`).

    Repeating tasks must NEVER auto-close to `done` — `done` is reserved for the
    OWNER explicitly ending the recurrence (manual move to done, or a 'finished'
    comment → handled in `_process_interval_repeats`).
    """
    row = (await db.execute(text(
        "SELECT repeat_enabled, repeat_interval_minutes FROM tasks WHERE id = :t"
    ), {"t": tid})).first()
    if not row or not row[0]:
        return False  # not repeating → caller closes to done as normal
    interval = int(row[1] or 60)
    await db.execute(text(f"""
        UPDATE tasks
        SET status = 'inbox', assigned_agent_id = NULL, in_progress_at = NULL,
            previous_in_progress_at = NULL, tokens_used = 0,
            budget_exhausted_at = NULL, updated_at = NOW()
        WHERE {_legacy_where()} AND id = :t
    """), {"t": tid})
    await _post_comment(
        db, tid, board_id,
        f"[cycle-complete] {note} Recurring task — next run in ~{interval} min. "
        f"(To stop: move to done, or comment that it's finished.)",
        commit=commit,
    )
    return True


# ── Declared-artifact reconciliation (Patch 1, 2026-07-16) ─────────────────
# Report-only file-vs-claim check: a task opts in by carrying `;artifact=<workspace-relative path or glob>`
# in `auto_reason` (same convention as the `;role=` routing hint). When a
# cycle claims completion but the declared artifact is absent or stale, an
# idempotent `[artifact-…]` marker is posted. NEVER changes task state,
# repeats, or routing. `tokens_used` is deliberately NOT a trigger: token
# attribution from the shared egress key is unreliable, and deterministic/
# local jobs legitimately finish with 0 tokens.

_ARTIFACT_DECL_MAX_LEN = 512


def _artifact_root() -> Path:
    """Container-visible workspace root that declared artifact paths are
    resolved against. compose.yml mounts `${HOME}/.openclaw` at
    `/home/host_openclaw` and exports it as OPENCLAW_HOME; agent-side
    absolute paths (the host user's `~/.openclaw/...`) map to it 1:1.
    Overridable for tests via WATCHDOG_ARTIFACT_ROOT.
    """
    return Path(
        os.environ.get("WATCHDOG_ARTIFACT_ROOT")
        or os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")
    )


def _parse_artifact_declaration(auto_reason: str | None) -> str | None:
    """Extract the `;artifact=` value from auto_reason (None when absent).
    Value runs to the next `;` or end of string, whitespace-trimmed.
    """
    if not auto_reason or ";artifact=" not in auto_reason:
        return None
    raw = auto_reason.split(";artifact=", 1)[1]
    return raw.split(";", 1)[0].strip() or None


def _validate_artifact_pattern(decl: str) -> str | None:
    """Untrusted-input gate for a declared artifact pattern. Returns an
    error string, or None when the pattern is safe to glob under the
    workspace root. Rejects absolute paths, traversal, home expansion,
    control characters, backslashes, and oversized declarations.
    """
    if not decl:
        return "empty declaration"
    if len(decl) > _ARTIFACT_DECL_MAX_LEN:
        return f"declaration exceeds {_ARTIFACT_DECL_MAX_LEN} chars"
    if any(ord(c) < 32 or c == "\x7f" for c in decl):
        return "control characters not allowed"
    if "\\" in decl:
        return "backslashes not allowed (POSIX relative path expected)"
    if decl.startswith(("/", "~")):
        return "absolute/home path not allowed (workspace-relative only)"
    if any(part == ".." for part in decl.split("/")):
        return "path traversal ('..') not allowed"
    return None


def _evaluate_artifact(
    root: Path,
    decl: str,
    since: datetime | None,
) -> tuple[str, str]:
    """Return (verdict, detail) for a validated declaration. Verdicts:
    'ok' | 'missing' | 'stale' | 'invalid'. Matches are constrained to the
    resolved root (symlink escapes are ignored), files only.
    """
    try:
        root_resolved = root.resolve()
        matches = []
        for m in root.glob(decl):
            try:
                if not m.is_file():
                    continue
                if not m.resolve().is_relative_to(root_resolved):
                    continue  # symlink escape — treat as outside the root
            except OSError:
                continue
            matches.append(m)
    except (ValueError, OSError) as exc:
        return "invalid", f"glob failed: {exc}"
    if not matches:
        return "missing", f"no file matches pattern under {root}"
    newest = max(matches, key=lambda p: p.stat().st_mtime)
    newest_mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    if since is not None:
        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        if newest_mtime < since_utc:
            return "stale", (
                f"newest match {newest.name} mtime={newest_mtime.isoformat()} "
                f"predates cycle start {since_utc.isoformat()}"
            )
    return "ok", f"{newest.name} mtime={newest_mtime.isoformat()}"


async def _reconcile_expected_artifacts(db) -> int:
    """Report-only artifact-vs-claim reconciliation for tasks declaring
    `;artifact=`. For each task whose latest completion-claim marker
    (`[cycle-complete]` / `[ready-to-close]` / `[watchdog-finalize]`) has no
    artifact verdict yet, evaluate the declared pattern and post ONE
    `[artifact-missing]` / `[artifact-stale]` / `[artifact-declaration-invalid]`
    marker (cycle-scoped idempotency: skip when a verdict marker is newer
    than the claim). Healthy artifacts post nothing (no comment spam).
    Returns count of markers posted.
    """
    rows = (await db.execute(text("""
        SELECT t.id, t.board_id, t.auto_reason, t.in_progress_at,
               t.repeat_enabled, t.repeat_interval_minutes
        FROM tasks t
        WHERE t.auto_reason LIKE '%;artifact=%'
          AND t.status != 'trash'
        ORDER BY t.updated_at DESC
        LIMIT 20
    """))).fetchall()
    if not rows:
        return 0
    root = _artifact_root()
    posted = 0
    for tid, bid, auto_reason, in_progress_at, repeat_on, interval_min in rows:
        try:
            decl = _parse_artifact_declaration(auto_reason)
            if decl is None:
                continue
            claim = (await db.execute(text("""
                SELECT MAX(created_at) FROM activity_events
                WHERE task_id = :tid AND event_type = 'task.comment'
                  AND (message LIKE '[cycle-complete]%'
                       OR message LIKE '[ready-to-close]%'
                       OR message LIKE '[watchdog-finalize]%')
            """), {"tid": str(tid)})).scalar()
            if claim is None:
                continue  # nothing claimed yet — nothing to reconcile
            already = (await db.execute(text("""
                SELECT 1 FROM activity_events
                WHERE task_id = :tid AND event_type = 'task.comment'
                  AND message LIKE '[artifact-%'
                  AND created_at >= :claim
                LIMIT 1
            """), {"tid": str(tid), "claim": claim})).first()
            if already:
                continue  # this cycle's verdict already posted
            err = _validate_artifact_pattern(decl)
            if err is not None:
                await _post_comment(
                    db, str(tid), str(bid) if bid else "",
                    f"[artifact-declaration-invalid] pattern={decl!r} error=\"{err}\" "
                    f"notes=\"fix the ;artifact= declaration in auto_reason "
                    f"(workspace-relative POSIX path/glob)\"",
                )
                posted += 1
                continue
            since: datetime | None = in_progress_at
            if since is None and repeat_on:
                # Settled repeating cycle: in_progress_at was reset — bound
                # staleness by one repeat interval before the claim.
                from datetime import timedelta
                since = claim - timedelta(minutes=int(interval_min or 60))
            verdict, detail = _evaluate_artifact(root, decl, since)
            if verdict in ("missing", "stale", "invalid"):
                await _post_comment(
                    db, str(tid), str(bid) if bid else "",
                    f"[artifact-{verdict if verdict != 'invalid' else 'declaration-invalid'}] "
                    f"pattern={decl!r} {detail} "
                    f"notes=\"claimed completion without the declared artifact "
                    f"(report-only, no state change)\"",
                )
                posted += 1
        except Exception as exc:
            logger.warning(
                "watchdog.artifact_reconcile.task_failed task=%s err=%s",
                str(tid)[:8], exc,
            )
            await db.rollback()
    return posted


async def _detect_freeform_completion(db) -> int:
    """Workers often post `COMPLETED: ...` free text instead of canonical
    `[ready-to-close]`. Detect that in `in_progress` tasks → synthesize
    `[ready-to-close]` marker, transition `review`, route to QA Engineer.

    Idempotent: skips tasks that already have a `[ready-to-close]` or
    `[qa-verdict]` marker.
    """
    # T61 (2026-05-15): also match short-form completion replies — the
    # whole comment is just a phrase like "ALLES KLAR", "OK", "DONE",
    # "FERTIG", "ERLEDIGT", or "✓". Regex anchors on the entire comment
    # so we don't match those words mid-paragraph.
    res = await db.execute(text(fr"""
        SELECT t.id, t.board_id, t.assigned_agent_id, t.complexity, t.updated_at
        FROM tasks t
        WHERE t.status = 'in_progress'
          AND {_legacy_where('t')}
          AND EXISTS (
            SELECT 1 FROM activity_events ae
            WHERE ae.task_id = t.id AND ae.event_type = 'task.comment'
              -- Only count completion comments from the CURRENT cycle. Repeating
              -- tasks (cron) carry prior-cycle completion comments; without this
              -- guard a stale "COMPLETED:"/"## Summary" from the last run
              -- auto-closes the fresh cycle at t+5s before the worker does any
              -- new work (poem never re-scraped). in_progress_at is stamped when
              -- THIS cycle entered in_progress.
              AND ae.created_at > COALESCE(t.in_progress_at, t.updated_at)
              AND (ae.message ILIKE 'COMPLETED:%'
                OR ae.message ILIKE 'DONE:%'
                OR ae.message ILIKE 'FINISHED:%'
                OR ae.message ILIKE 'PASSWORD:%'
                OR ae.message ILIKE 'ANSWER:%'
                OR ae.message ILIKE 'SOLVED:%'
                OR ae.message ILIKE 'TASK COMPLETE%'
                OR ae.message ILIKE '## Result%'
                OR ae.message ILIKE '## Summary%'
                OR ae.message ILIKE '## Findings%'
                OR ae.message ILIKE '## Conclusion%'
                OR ae.message ILIKE '## Outcome%'
                OR ae.message ILIKE '## Final%'
                OR ae.message ~* '^\s*\*?\s*(ok|done|fertig|erledigt|alles\s+klar|complete[d]?|finished|gelöst|geloest|solved|✓|✅)\s*[.!]*\s*\*?\s*$'
              )
          )
        LIMIT 50
    """))
    rows = res.fetchall()
    if not rows:
        return 0

    promoted = 0
    for tid_u, bid_u, aid_u, complexity, stamp in rows:
        tid = str(tid_u)
        board_id = str(bid_u) if bid_u else ""
        level = int(complexity) if complexity is not None else 0
        try:
            markers = await _task_markers(db, tid)
            if any(m["tag"] in ("ready-to-close", "qa-verdict", "pm-signoff") for m in markers):
                continue  # already in canonical flow
            if await _lock_legacy_task(db, tid, stamp, "in_progress") is None:
                continue

            # L1 trivial tasks: worker's free-form completion IS the final answer.
            # Bypass the review pipeline. REPEATING tasks settle to inbox (next
            # cycle) instead of done — they must never auto-close.
            if level == 1:
                if await _settle_repeating_cycle(
                    db, tid, board_id, "L1 cycle complete — worker posted completion.", commit=False
                ):
                    await db.commit()
                    promoted += 1
                    continue
                await db.execute(text(f"""
                    UPDATE tasks SET status='done', updated_at=NOW()
                    WHERE {_legacy_where()} AND id = :t AND status = 'in_progress'
                """), {"t": tid})
                await _post_comment(
                    db, tid, board_id,
                    f"{_NUDGE_TAG} **Auto-closed to `done`** — L1 trivial task. "
                    f"Worker posted free-form completion; complexity=1 → no review required.", commit=False)
                await db.commit()
                promoted += 1
                continue

            # Find QA Engineer for the task's pool.
            qa_id = await _pick_worker_for_role(db, "QA Engineer", board_id)
            if not qa_id:
                # No idle QA — fall through; reviewer can be picked later.
                qa_id = None

            changed = await db.execute(text(f"""
                UPDATE tasks SET status='review', updated_at=NOW(),
                                 assigned_agent_id = COALESCE(:qa, assigned_agent_id)
                WHERE {_legacy_where()} AND id = :t AND status = 'in_progress'
                RETURNING updated_at
            """), {"t": tid, "qa": qa_id})
            assignment_stamp = changed.scalar_one_or_none()
            if assignment_stamp is None:
                await db.rollback()
                continue

            # Synthesize the canonical marker for downstream gates.
            ready_msg = (
                f"[ready-to-close] auto from worker free-text completion\n"
                f"{_NUDGE_TAG} **Auto-promoted to `review`** — worker posted "
                f"a free-text completion (COMPLETED/PASSWORD/etc). Watchdog "
                f"emitted canonical `[ready-to-close]` marker and routed to "
                f"{('QA Engineer' if qa_id else 'current assignee')} for verdict."
            )
            await _post_comment(db, tid, board_id, ready_msg, commit=False)
            await db.commit()
            promoted += 1

            # Wake QA so they pick up.
            if qa_id:
                try:
                    from uuid import UUID as _UUID
                    from app.models.agents import Agent
                    from app.models.boards import Board
                    from app.api.tasks import notify_agent_on_task_assign
                    a_obj = await db.get(Agent, _UUID(qa_id))
                    b_obj = await db.get(Board, _UUID(board_id)) if board_id else None
                    t_obj = await _lock_legacy_task(db, tid, assignment_stamp, "review")
                    if a_obj and b_obj and t_obj:
                        await notify_agent_on_task_assign(
                            session=db, board=b_obj, task=t_obj, agent=a_obj,
                        )
                except Exception as e:
                    logger.warning("watchdog.freeform_completion.notify_failed task=%s err=%s", tid, e)
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.freeform_completion.failed task=%s err=%s", tid, e)
    return promoted


async def _detect_llm_completion(db) -> int:
    """T65 (2026-05-15): LLM-based completion detection. Catches assignee
    comments that don't match the SQL ILIKE patterns but still semantically
    indicate the task is finished. Runs after `_detect_freeform_completion`
    so tasks already promoted by the fast SQL path don't burn an LLM call.

    Scans `in_progress` tasks whose most-recent assignee comment is not a
    canonical marker, then asks the LLM `is_completion_comment`. If YES,
    re-uses the existing `_detect_freeform_completion` promotion logic by
    injecting the SQL-pattern-matched marker via `_post_comment` then
    letting the next tick's SQL path promote it (idempotent, single source
    of truth for transition rules).

    Returns count of tasks for which an LLM-yes marker was posted.
    """
    try:
        from app.services.llm_evaluator import is_completion_comment
    except Exception:
        return 0

    # Cap: 5 tasks per tick. LLM calls cost real tokens — keep bounded.
    res = await db.execute(text(fr"""
        SELECT t.id, t.board_id, ae.message, t.updated_at
        FROM tasks t
        JOIN LATERAL (
          SELECT message, agent_id, created_at
          FROM activity_events
          WHERE task_id = t.id AND event_type = 'task.comment'
            AND agent_id = t.assigned_agent_id
            -- current cycle only (see _detect_freeform_completion): don't act on
            -- a prior cycle's completion comment on a repeating task.
            AND created_at > COALESCE(t.in_progress_at, t.updated_at)
          ORDER BY created_at DESC
          LIMIT 1
        ) ae ON TRUE
        WHERE t.status = 'in_progress'
          AND {_legacy_where('t')}
          AND t.assigned_agent_id IS NOT NULL
          AND ae.message IS NOT NULL
          AND length(ae.message) BETWEEN 2 AND 2000
          -- Skip rows the SQL fast-path will catch on the SAME tick.
          AND NOT (
            ae.message ILIKE 'COMPLETED:%' OR ae.message ILIKE 'DONE:%'
            OR ae.message ILIKE 'FINISHED:%' OR ae.message ILIKE 'PASSWORD:%'
            OR ae.message ILIKE 'ANSWER:%' OR ae.message ILIKE 'SOLVED:%'
            OR ae.message ILIKE 'TASK COMPLETE%' OR ae.message ILIKE '## Result%'
            OR ae.message ILIKE '## Summary%' OR ae.message ILIKE '## Findings%'
            OR ae.message ILIKE '## Conclusion%' OR ae.message ILIKE '## Outcome%'
            OR ae.message ILIKE '## Final%'
            OR ae.message ~* '^\s*\*?\s*(ok|done|fertig|erledigt|alles\s+klar|complete[d]?|finished|gelöst|geloest|solved|✓|✅)\s*[.!]*\s*\*?\s*$'
          )
          -- Skip tasks already in canonical flow.
          AND NOT EXISTS (
            SELECT 1 FROM activity_events ae2
            WHERE ae2.task_id = t.id AND ae2.event_type = 'task.comment'
              AND (ae2.message LIKE '%[ready-to-close]%'
                OR ae2.message LIKE '%[qa-verdict]%'
                OR ae2.message LIKE '%[pm-signoff]%')
          )
        ORDER BY t.updated_at DESC
        LIMIT 5
    """))
    rows = res.fetchall()
    await db.rollback()  # No transaction or row lock during completion inference.
    if not rows:
        return 0

    flagged = 0
    for tid_u, bid_u, message, stamp in rows:
        tid = str(tid_u)
        board_id = str(bid_u) if bid_u else ""
        try:
            verdict = await is_completion_comment(message)
        except Exception as exc:
            logger.warning("watchdog.llm_completion.eval_failed task=%s err=%s", tid[:8], exc)
            continue
        if verdict is True:
            # Post a synthetic completion marker that the SQL-fast-path
            # on the NEXT tick will pick up via the `COMPLETED:%` ILIKE.
            # That keeps the promotion logic in one place.
            try:
                if await _lock_legacy_task(db, tid, stamp, "in_progress") is None:
                    continue
                await _post_comment(
                    db, tid, board_id,
                    "COMPLETED: LLM-evaluated assignee comment as task completion.\n"
                    f"{_NUDGE_TAG} **Auto-marker** — `llm_evaluator.is_completion_comment` "
                    f"returned YES on the assignee's comment. Next watchdog tick will "
                    f"promote `in_progress` → review/done via the canonical SQL path.",
                    commit=False,
                )
                await db.commit()
                flagged += 1
                logger.info(
                    "watchdog.llm_completion.flagged task=%s len=%d",
                    tid[:8], len(message),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("watchdog.llm_completion.marker_post_failed task=%s err=%s", tid[:8], exc)
    return flagged


_PARENT_MAJORITY_STUCK_MIN = int(os.environ.get("PARENT_MAJORITY_STUCK_MIN", "30"))
_PARENT_MAJORITY_THRESHOLD = float(os.environ.get("PARENT_MAJORITY_THRESHOLD", "0.66"))


_SUBTASK_REASSIGN_AFTER_MIN = int(os.environ.get("SUBTASK_REASSIGN_AFTER_MIN", "20"))


async def _reassign_stuck_subtasks(db: AsyncSession) -> int:
    """T98 (2026-05-17): rebalance laggard subtasks while parent waits.

    Looks for children that have been stuck >`SUBTASK_REASSIGN_AFTER_MIN`
    (default 20) in their current assignment without any task.comment
    activity from the assignee. Reassigns them to a different agent in
    the same (or spillover) role pool via `_pick_worker_for_role`. The
    new assignee gets the wake event.

    Only acts on subtasks (parent_task_id IS NOT NULL) whose parent is
    still in `in_progress` / `review`. Skips if a single reassignment
    has already been done in the current cycle (to avoid ping-pong).
    """
    threshold = _SUBTASK_REASSIGN_AFTER_MIN
    res = await db.execute(text(f"""
        SELECT c.id, c.title, c.assigned_agent_id, c.board_id, c.parent_task_id,
               c.updated_at, c.description
        FROM tasks c
        JOIN tasks p ON p.id = c.parent_task_id
        WHERE c.parent_task_id IS NOT NULL
          AND c.status IN ('in_progress', 'inbox')
          AND p.status IN ('in_progress', 'review')
          AND c.assigned_agent_id IS NOT NULL
          AND c.updated_at < NOW() - INTERVAL '{threshold} minutes'
          AND NOT EXISTS (
              SELECT 1 FROM activity_events ae
              WHERE ae.task_id = c.id
                AND ae.event_type = 'task.comment'
                AND ae.agent_id = c.assigned_agent_id
                AND ae.created_at > c.updated_at - INTERVAL '5 minutes'
          )
        LIMIT 20
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    from app.api.router import _role_keyword_for_task as _rkt
    from app.models.tasks import Task
    reassigned = 0
    for cid, title, old_aid, bid, pid, _upd, desc in rows:
        try:
            role_kw = _rkt(title or "", desc or "") or "QA Engineer"
            new_aid = await _pick_worker_for_role(db, role_kw, str(bid)) if bid else None
            if not new_aid or new_aid == str(old_aid):
                continue
            task = await _lock_legacy_task(db, str(cid), _upd)
            if task is None:
                continue
            task.assigned_agent_id = new_aid
            from datetime import datetime as _dt, timezone as _tz
            task.updated_at = _dt.now(_tz.utc)
            db.add(task)
            await _post_comment(
                db, str(cid), str(bid) if bid else "",
                f"[watchdog-reassign] Stuck >{threshold} min with no progress; "
                f"reassigned from {str(old_aid)[:8]} to {str(new_aid)[:8]}.",
            )
            await db.commit()
            reassigned += 1
            logger.info(
                "watchdog.subtask_reassign task=%s parent=%s old=%s new=%s",
                cid, pid, old_aid, new_aid,
            )
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.subtask_reassign.failed task=%s err=%s", cid, e)
    return reassigned


async def _detect_split_parent_majority_completion(db: AsyncSession) -> int:
    """T97 (2026-05-17): close parent when majority of children done AND
    the laggard child(ren) have been stuck >PARENT_MAJORITY_STUCK_MIN.

    Without this, one stuck subtask wedges the parent forever even when
    enough children have completed to provide signal. Owner intent on
    auto-split was parallel execution — accept partial completion when
    the laggard is clearly stuck.

    Threshold: done_children / split_count >= PARENT_MAJORITY_THRESHOLD (0.66 default).
    Posts a `[watchdog-majority-completed]` marker explaining which child(ren) lagged.
    """
    threshold = _PARENT_MAJORITY_THRESHOLD
    stuck_min = _PARENT_MAJORITY_STUCK_MIN
    res = await db.execute(text(f"""
        WITH parents AS (
            SELECT p.id, p.split_type, p.board_id, p.split_count, p.updated_at
            FROM tasks p
            WHERE p.split_count > 0
              AND p.parent_task_id IS NULL
              AND p.status = 'in_progress'
              -- Native team DAG parents never complete by majority: the full
              -- backlog must never be represented as complete with pending
              -- or blocked child work. Legacy historic/non-native split
              -- parents keep the existing majority behavior unchanged.
              AND NOT (COALESCE(p.split_type, '') = 'dag'
                       AND COALESCE(p.execution_mode, '') = 'team')
        )
        SELECT p.id, p.split_type, p.board_id, p.split_count, p.updated_at,
               (SELECT COUNT(*) FROM tasks c
                WHERE c.parent_task_id = p.id AND c.status = 'done') AS done_n,
               (SELECT MIN(c.updated_at) FROM tasks c
                WHERE c.parent_task_id = p.id AND c.status <> 'done') AS oldest_pending_updated
        FROM parents p
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    completed = 0
    cutoff = _dt.now(_tz.utc) - _td(minutes=stuck_min)
    for pid, split_type, board_id, split_count, stamp, done_n, oldest_pending in rows:
        if not split_count or split_count <= 0:
            continue
        ratio = (done_n or 0) / split_count
        if ratio < threshold:
            continue
        # Laggard must have been stuck >= cutoff
        if oldest_pending is None:
            continue
        if isinstance(oldest_pending, str):  # sqlite affinity: parse to datetime
            try:
                oldest_pending = datetime.fromisoformat(oldest_pending)
            except ValueError:
                continue
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=_tz.utc)
        if oldest_pending > cutoff:
            continue  # not stuck long enough
        try:
            from app.models.tasks import Task

            if isinstance(pid, str):  # raw SQL returns str ids on sqlite
                pid = uuid.UUID(pid)
            parent = await _lock_legacy_task(db, str(pid), stamp, "in_progress")
            if parent is None:
                continue
            parent.status = "review" if split_type == "dag" else "done"
            parent.updated_at = _dt.now(_tz.utc)
            db.add(parent)
            if board_id:
                await _post_comment(
                    db,
                    str(pid),
                    str(board_id),
                    f"[watchdog-majority-completed] {done_n}/{split_count} children done "
                    f"({int(ratio*100)}%); laggard stuck since "
                    f"{oldest_pending.isoformat()}. Auto-promoted parent to "
                    f"`{parent.status}` per PARENT_MAJORITY_THRESHOLD={threshold}.",
                )
            await db.commit()
            completed += 1
            logger.info(
                "watchdog.split_parent_majority parent=%s done=%d/%d target=%s",
                pid,
                done_n,
                split_count,
                parent.status,
            )
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.split_parent_majority.failed parent=%s err=%s", pid, e)
    return completed


async def _detect_split_parent_completion(db: AsyncSession) -> int:
    """Find parent tasks whose children are all 'done' and auto-complete the parent.

    Queries for tasks where:
      - split_count > 0 (parent was split)
      - parent_task_id IS NULL (this task IS the parent)
      - status = 'in_progress'

    Then checks: do ALL children have status = 'done'? If yes:
      - split_type='dag' → parent goes to 'review' (E1 Q2 default — QA gate
        still applies via _auto_close_review_tasks).
      - split_type in {'by_count', 'by_sections', NULL} → parent goes to 'done'
        (existing T11 behavior preserved).

    Posts a canonical `[watchdog-dag-completed]` marker on DAG-parents so the
    review gate has audit context.
    """
    from sqlalchemy import text

    res = await db.execute(text("""
        SELECT p.id, p.split_type, p.board_id, p.updated_at
        FROM tasks p
        WHERE p.split_count > 0
          AND p.parent_task_id IS NULL
          AND p.status = 'in_progress'
          -- native team DAG parents are handled by the stricter
          -- _detect_native_split_parent_completion pass (count-checked,
          -- nesting-aware); legacy parents keep this historic behavior.
          AND NOT (COALESCE(p.split_type, '') = 'dag'
                   AND COALESCE(p.execution_mode, '') = 'team')
          AND NOT EXISTS (
              SELECT 1 FROM tasks c
              WHERE c.parent_task_id = p.id
                AND c.status != 'done'
          )
          AND EXISTS (
              SELECT 1 FROM tasks c2
              WHERE c2.parent_task_id = p.id
          )
        LIMIT 50
    """))
    rows = res.fetchall()
    if not rows:
        # No legacy candidates — native parents still need their stricter pass.
        return await _detect_native_split_parent_completion(db)

    completed = 0
    from app.models.tasks import Task

    for pid, split_type, board_id, stamp in rows:
        try:
            if isinstance(pid, str):  # raw SQL returns str ids on sqlite
                pid = uuid.UUID(pid)
            parent = await _lock_legacy_task(db, str(pid), stamp, "in_progress")
            if parent is None:
                continue
            if split_type == "dag":
                parent.status = "review"
                target = "review"
            else:
                parent.status = "done"
                target = "done"
            parent.updated_at = datetime.now(timezone.utc)
            db.add(parent)
            if split_type == "dag" and board_id:
                await _post_comment(
                    db,
                    str(pid),
                    str(board_id),
                    f"[watchdog-dag-completed] children={parent.split_count} "
                    f"all done; promoted parent to `review` for QA gate.",
                )
            await db.commit()
            completed += 1
            logger.info(
                "watchdog.split_parent_completed parent=%s split_type=%s target=%s children=%d",
                pid,
                split_type,
                target,
                parent.split_count,
            )
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.split_parent_complete.failed parent=%s err=%s", pid, e)
    completed += await _detect_native_split_parent_completion(db)
    return completed


async def _detect_native_split_parent_completion(db: AsyncSession) -> int:
    """Native (team-mode, DAG-split) parents: truthfulness invariants.

    - Never complete by majority (the majority pass skips them entirely).
    - Promote to Review — never Done — only when ALL direct children are Done.
    - The actual child count must equal split_count; a mismatch (deleted or
      foreign children) wedges the parent for human review instead of
      silently declaring the backlog complete.
    - Nested native parents (a [backlog] child that itself split) are included:
      ancestry is not dependency depth, and a nested parent must not be left
      permanently in_progress just because it is also somebody's child.
    """
    from app.models.tasks import Task
    from sqlmodel import select

    res = await db.execute(text("""
        SELECT p.id, p.board_id, p.split_count,
               (SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id = p.id) AS child_n,
               (SELECT COUNT(*) FROM tasks c
                WHERE c.parent_task_id = p.id AND c.status != 'done') AS pending_n
        FROM tasks p
        WHERE p.split_count > 0
          AND COALESCE(p.split_type, '') = 'dag'
          AND COALESCE(p.execution_mode, '') = 'team'
          AND p.status = 'in_progress'
        LIMIT 50
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    completed = 0
    for pid, board_id, split_count, child_n, pending_n in rows:
        if pending_n:
            continue  # pending or blocked child work: never represent the backlog complete
        if not child_n or child_n != split_count:
            logger.warning(
                "watchdog.native_split_parent.count_mismatch parent=%s children=%d split_count=%d",
                pid,
                child_n,
                split_count,
            )
            continue
        try:
            if isinstance(pid, str):  # raw SQL returns str ids on sqlite
                pid = uuid.UUID(pid)
            # Descendant -> ancestor matches worker/owner task -> cycle ->
            # ancestor locking; parent -> child would deadlock an active child.
            child_query = select(Task).where(Task.parent_task_id == pid).order_by(Task.id).with_for_update().execution_options(populate_existing=True)
            await db.exec(child_query)
            parent = await db.get(Task, pid)
            if not parent:
                await db.rollback()
                continue
            await db.refresh(parent, with_for_update=True)
            if parent.status != "in_progress" or parent.execution_mode != "team" or parent.split_count != child_n:
                await db.rollback()
                continue  # preserve a newer human status/input decision
            children = list(await db.exec(child_query))
            if len(children) != parent.split_count or any(c.board_id != parent.board_id or c.status != "done" for c in children):
                await db.rollback()
                continue
            from app.services.team_cycle import task_fingerprint
            expected = f"native-backlog:{parent.id}:{task_fingerprint(parent)}"
            if any((c.auto_reason or "").startswith("native-backlog:") and c.auto_reason != expected for c in children):
                await db.rollback()
                continue  # completed old-goal children are not proof of an edited goal
            parent.status = "review"
            parent.updated_at = datetime.now(timezone.utc)
            db.add(parent)
            # Commit the promotion BEFORE the best-effort comment so a comment
            # failure can never roll the truthful state change back.
            await db.commit()
            completed += 1
            if board_id:
                await _post_comment(
                    db,
                    str(pid),
                    str(board_id),
                    f"[watchdog-dag-completed] native team parent: all {child_n} "
                    "children done (count matches split_count); promoted parent to "
                    "`review`. Aggregate acceptance remains a separate human, "
                    "evidence-based review — never automatic Done.",
                    allow_native=True,
                )
            logger.info(
                "watchdog.native_split_parent_completed parent=%s children=%d",
                pid,
                child_n,
            )
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.native_split_parent_complete.failed parent=%s err=%s", pid, e)
    return completed


async def _propagate_dag_cancellations(db: AsyncSession) -> int:
    """T24b (E1 Phase 2, 2026-05-14): when a DAG child closes with a failure
    marker, cancel dependent agent-lane descendants with a
    `[watchdog-dag-cancelled]` marker explaining why.

    Trigger criterion: a DAG child task in `done` whose last marker is either
    `[pm-signoff] verdict=reject` OR `[watchdog-dag-cancelled]` (chain effect).

    Returns number of descendants closed.
    """
    from sqlalchemy import text
    # Find broken-chain children. A child is "broken" when it's `done` AND
    # carries a reject/cancel marker. The propagated=true marker on the
    # child itself prevents re-propagation each tick.
    res = await db.execute(text(f"""
        SELECT c.id AS child_id, c.board_id AS board_id, c.title AS child_title
        FROM tasks c
        WHERE c.parent_task_id IS NOT NULL
          AND c.split_type = 'dag'
          AND {_legacy_where('c')}
          AND c.status IN ('done', 'cancelled')
          AND EXISTS (
              SELECT 1 FROM activity_events ae
              WHERE ae.task_id = c.id
                AND ae.event_type = 'task.comment'
                AND (ae.message LIKE '[pm-signoff]%verdict=reject%'
                  OR ae.message LIKE '[watchdog-dag-cancelled]%')
                AND ae.message NOT LIKE '%propagated=true%'
          )
          AND NOT EXISTS (
              -- Skip if we already propagated for this child.
              SELECT 1 FROM activity_events ae2
              WHERE ae2.task_id = c.id
                AND ae2.event_type = 'task.comment'
                AND ae2.message LIKE '[watchdog-dag-cancelled]%propagated=true%'
          )
        LIMIT 50
    """))
    broken = res.fetchall()
    if not broken:
        return 0

    cancelled_total = 0
    for child_id, board_id, child_title in broken:
        # Walk forward: every inbox task whose deps include this child →
        # cancel + record marker.
        descendants_res = await db.execute(text(f"""
            WITH RECURSIVE descendants(task_id) AS (
                SELECT td.task_id FROM task_dependencies td
                WHERE td.depends_on_task_id = :cid
                UNION
                SELECT td.task_id FROM task_dependencies td
                JOIN descendants d ON td.depends_on_task_id = d.task_id
            )
            SELECT t.id, t.title, t.updated_at FROM tasks t
            JOIN descendants d ON d.task_id = t.id
            WHERE t.status = 'inbox'
              AND {_legacy_where('t')}
        """), {"cid": str(child_id)})
        descendants = descendants_res.fetchall()
        cancelled_child = 0
        for d_id, d_title, stamp in descendants:
            try:
                if await _lock_legacy_task(db, str(d_id), stamp, "inbox") is None:
                    continue
                # Failed dependency is cancellation, never successful completion.
                await db.execute(text(f"""
                    UPDATE tasks SET status='cancelled', updated_at=NOW()
                    WHERE {_legacy_where()} AND id = :tid AND status = 'inbox'
                """), {"tid": str(d_id)})
                await _post_comment(
                    db, str(d_id), str(board_id) if board_id else "",
                    f"[watchdog-dag-cancelled] reason=upstream-failed "
                    f"upstream={str(child_id)[:8]} "
                    f"notes=\"Predecessor task '{child_title[:60]}' was cancelled; "
                    f"this dependent task can no longer make progress.\"",
                    commit=False,
                )
                await db.commit()
                cancelled_total += 1
                cancelled_child += 1
            except Exception as exc:
                await db.rollback()
                logger.warning(
                    "watchdog.dag_cancel.propagation_failed descendant=%s err=%s",
                    d_id, exc,
                )
        # Mark the broken child so we don't re-propagate next tick.
        try:
            await _post_comment(
                db, str(child_id), str(board_id) if board_id else "",
                f"[watchdog-dag-cancelled] propagated=true "
                f"descendants_cancelled={cancelled_child}"
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "watchdog.dag_cancel.mark_failed child=%s err=%s", child_id, exc,
            )
    if cancelled_total:
        logger.info(
            "watchdog.dag_cancel.propagated descendants=%d sources=%d",
            cancelled_total, len(broken),
        )
    return cancelled_total


async def _aggregate_token_usage(db: AsyncSession) -> int:
    """E4 Phase 2 (T26, 2026-05-14): aggregate per-task token usage from the
    sqlite traffic DB into `tasks.tokens_used`.

    Method: for each `in_progress` task with `assigned_agent_id` set, sum
    `total_tokens` over traffic flows where `agent_id = task.assigned_agent_id`
    AND `ts > task.in_progress_at`. Update `tokens_used` in one batch UPDATE.

    Skipped if either DB is unreachable. Returns count of tasks updated.
    """
    from sqlalchemy import text
    if not _TRAFFIC_DB.exists():
        return 0
    res = await db.execute(text("""
        SELECT t.id, t.assigned_agent_id::text AS aid, t.in_progress_at
        FROM tasks t
        WHERE t.status = 'in_progress'
          AND t.assigned_agent_id IS NOT NULL
          AND t.in_progress_at IS NOT NULL
          AND t.max_tokens_budget IS NOT NULL
          AND t.max_tokens_budget > 0
        LIMIT 200
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    updated = 0
    try:
        conn = sqlite3.connect(
            f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False,
        )
    except Exception:
        return 0
    try:
        for tid, aid, since_dt in rows:
            since_iso = since_dt.isoformat() if since_dt else None
            if not since_iso:
                continue
            tot = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM flows "
                "WHERE agent_id = ? AND ts >= ?",
                (aid, since_iso),
            ).fetchone()[0]
            if tot and tot > 0:
                await db.execute(text(
                    f"UPDATE tasks SET tokens_used = :n WHERE {_legacy_where()} AND id = :tid"
                ), {"n": int(tot), "tid": str(tid)})
                updated += 1
        await db.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return updated


async def _enforce_task_budgets(db: AsyncSession) -> int:
    """Enforce wall-clock execution deadlines, not token/spending budgets.

    Token limits remain legacy metadata/telemetry; operator disables providers.
    On deadline expiry: drop `assigned_agent_id`, revert status to `inbox`,
    stamp `budget_exhausted_at`, post `[watchdog-budget-exhausted]` marker
    naming the reason. Routing picks the next pool member next tick.
    """
    from sqlalchemy import text
    res = await db.execute(text(f"""
        SELECT
            t.id,
            t.title,
            t.board_id,
            t.assigned_agent_id,
            t.max_seconds_budget,
            t.max_tokens_budget,
            t.tokens_used,
            EXTRACT(EPOCH FROM (NOW() - t.in_progress_at))::int AS elapsed_sec,
            CASE
                WHEN t.max_seconds_budget IS NOT NULL
                 AND t.in_progress_at IS NOT NULL
                 AND (NOW() - t.in_progress_at) > make_interval(secs => t.max_seconds_budget)
                THEN 'wall-clock'
                ELSE NULL
            END AS trigger,
            t.updated_at
        FROM tasks t
        WHERE t.status = 'in_progress'
          -- Native deadlines/leases belong to team_cycle, not legacy requeue;
          -- delegated parent execution finished, but its children may take days.
          AND {_legacy_where('t')}
          AND (
            (t.max_seconds_budget IS NOT NULL AND t.max_seconds_budget > 0
              AND t.in_progress_at IS NOT NULL
              AND (NOW() - t.in_progress_at) > make_interval(secs => t.max_seconds_budget))
          )
        LIMIT 50
    """))
    rows = res.fetchall()
    if not rows:
        return 0
    kicked = 0
    for tid, title, bid, agent_id, sec_budget, tok_budget, tok_used, elapsed, trigger, stamp in rows:
        try:
            task = await _lock_legacy_task(db, str(tid), stamp, "in_progress")
            if task is None:
                continue
            # Read telemetry/metadata under original-generation authority too.
            agent_id, bid = task.assigned_agent_id, task.board_id
            sec_budget, tok_budget, tok_used = task.max_seconds_budget, task.max_tokens_budget, task.tokens_used
            # T104 (2026-05-23): "auto-finalize" path. Previously every
            # budget-exhausted task was reverted to inbox so a new pool
            # member could try. That works for stuck-volunteer tasks but
            # for `parent_task_id IS NULL` top-level tasks that already
            # had an assignee + accumulated tokens, looping inbox→agent→
            # exhaustion in perpetuity wastes capacity. Instead: stamp
            # `[watchdog-finalize]` + move to review so a human or QA
            # reviewer can decide what to keep. Children + tasks without
            # an assignee still get the requeue path.
            is_top_level = task.parent_task_id is None and agent_id is not None

            if is_top_level:
                # A wall-clock finalize that burned ZERO tokens means no model
                # ever ran: the agent was never reachable, or never replied.
                # Sending that to `review` let the PM sign off on work nobody
                # did -- 68 of 68 finalized cycles in the week to 2026-09-28
                # were tokens_used=0 and every one was accepted. Same rule as
                # the non-functional-assignee path: BLOCKED for owner triage,
                # never a status that can be mistaken for completed work.
                # `tasks.tokens_used` only tracks the gateway-agent lane. The
                # coding-harness runner reports its usage to the ranking
                # sidecar instead, so a task it genuinely worked still reads
                # zero here -- blocking those would bury real work. Its result
                # comment is the proof that a model ran.
                ran_nothing = not tok_used and not await _harness_worked(
                    db, str(tid)
                )
                next_status = "blocked" if ran_nothing else "review"
                changed = await db.execute(text(
                    "UPDATE tasks SET status=:st, budget_exhausted_at = NOW(), "
                    f"updated_at = NOW() WHERE {_legacy_where()} AND id = :tid AND status = 'in_progress' "
                    "AND max_seconds_budget > 0 AND in_progress_at IS NOT NULL "
                    "AND (NOW() - in_progress_at) > make_interval(secs => max_seconds_budget)"
                ), {"tid": str(tid), "st": next_status})
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                detail = (
                    f"reason={trigger} tokens_used={tok_used} elapsed={elapsed}s "
                    f"budgets=tok:{tok_budget}/sec:{sec_budget}"
                )
                note = (
                    "No tokens were consumed, so no work was performed. "
                    "Marked BLOCKED for owner triage -- check that the assignee "
                    "is reachable (gateway pairing/scopes) before reassigning."
                ) if ran_nothing else (
                    "Task moved to review on budget exhaustion. Assignee should "
                    "post a final summary then mark done, or owner can reassign."
                )
                if not await _post_comment(
                    db, str(tid), str(bid) if bid else "",
                    f"[watchdog-finalize] {detail} notes=\"{note}\"", commit=False
                ):
                    await db.rollback()
                    continue
            else:
                changed = await db.execute(text(f"""
                    UPDATE tasks
                    SET status='inbox',
                        assigned_agent_id = NULL,
                        in_progress_at = NULL,
                        previous_in_progress_at = in_progress_at,
                        budget_exhausted_at = NOW(),
                        updated_at = NOW()
                    WHERE {_legacy_where()} AND id = :tid AND status = 'in_progress'
                      AND max_seconds_budget > 0 AND in_progress_at IS NOT NULL
                      AND (NOW() - in_progress_at) > make_interval(secs => max_seconds_budget)
                """), {"tid": str(tid)})
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                detail = (
                    f"reason={trigger} tokens_used={tok_used} budget=tok:{tok_budget}/sec:{sec_budget}"
                )
                if not await _post_comment(
                    db, str(tid), str(bid) if bid else "",
                    f"[watchdog-budget-exhausted] {detail} "
                    f"prior_agent={str(agent_id)[:8] if agent_id else 'unassigned'} "
                    f"notes=\"Task reverted to inbox; routing will pick next pool member.\"", commit=False
                ):
                    await db.rollback()
                    continue
            await db.commit()
            kicked += 1
            logger.info(
                "watchdog.budget_%s task=%s trigger=%s elapsed=%ds tokens=%d",
                "finalized" if is_top_level else "exhausted",
                str(tid)[:8], trigger, elapsed or 0, tok_used or 0,
            )
        except Exception as exc:
            await db.rollback()
            logger.warning("watchdog.budget.kick_failed task=%s err=%s", tid, exc)
    return kicked


# Patch 1 (2026-07-16): tasks with NULL/0 `max_seconds_budget` never trip the
# wall-clock trigger above (by design — NULL = no cap, models/tasks.py). This
# makes genuinely-forgotten long runners invisible. Flag, don't finalize.
_UNBUDGETED_FLAG_HOURS = int(os.environ.get("WATCHDOG_UNBUDGETED_FLAG_HOURS", "24"))


async def _flag_unbudgeted_long_runners(db) -> int:
    """Report-only visibility for `in_progress` tasks older than
    `_UNBUDGETED_FLAG_HOURS` that have no wall-clock budget (NULL or 0
    `max_seconds_budget`). Posts ONE `[watchdog-stalled] reason=no-budget`
    marker per cycle (idempotent: marker newer than `in_progress_at` counts
    as done). Deliberately NO status change, NO reassignment, NO default
    budget — a global default would replay the finalize-empty-work failure
    mode on legitimate long runners. Returns markers posted.
    """
    rows = (await db.execute(text(f"""
        SELECT t.id, t.board_id,
               EXTRACT(EPOCH FROM (NOW() - t.in_progress_at))::int AS elapsed_sec
        FROM tasks t
        WHERE t.status = 'in_progress'
          AND (t.max_seconds_budget IS NULL OR t.max_seconds_budget = 0)
          AND t.in_progress_at IS NOT NULL
          AND t.in_progress_at < NOW() - INTERVAL '{_UNBUDGETED_FLAG_HOURS} hours'
          AND NOT EXISTS (
              SELECT 1 FROM activity_events ae
              WHERE ae.task_id = t.id AND ae.event_type = 'task.comment'
                AND ae.message LIKE '[watchdog-stalled] reason=no-budget%'
                AND ae.created_at > t.in_progress_at
          )
        LIMIT 20
    """))).fetchall()
    posted = 0
    for tid, bid, elapsed_sec in rows:
        ok = await _post_comment(
            db, str(tid), str(bid) if bid else "",
            f"[watchdog-stalled] reason=no-budget elapsed={round((elapsed_sec or 0) / 3600, 1)}h "
            f"notes=\"in_progress > {_UNBUDGETED_FLAG_HOURS}h with no max_seconds_budget — "
            f"set a budget or review manually (report-only, no state change)\"",
        )
        if ok:
            posted += 1
            logger.warning(
                "watchdog.unbudgeted_long_runner task=%s elapsed=%dh",
                str(tid)[:8], int((elapsed_sec or 0) / 3600),
            )
    return posted


async def _materialize_dag_from_pm_plan(db: AsyncSession) -> int:
    """E1 — Subtask DAG materialization (2026-05-14).

    Finds in_progress tasks with a recent `[pm-plan]` marker containing
    `subtasks='[...]'` JSON, where the parent has not yet been materialized
    (split_count == 0). Parses + validates the DAG and atomically creates
    child Task rows + TaskDependency edges.

    Posts `[watchdog-dag-materialized]` or `[watchdog-dag-rejected]` marker
    on the parent after the attempt.

    Returns count of parents materialized.
    """
    from sqlalchemy import text
    from app.models.tasks import Task
    from app.services.dag_materialize import (
        DagValidationError,
        materialize_dag,
        parse_subtasks_payload,
        validate_subtasks,
    )

    # Candidates: in_progress, has children-capable schema, never materialized.
    res = await db.execute(text(f"""
        SELECT t.id, t.board_id, t.updated_at
        FROM tasks t
        WHERE t.status = 'in_progress'
          AND t.parent_task_id IS NULL
          AND COALESCE(t.split_count, 0) = 0
          AND {_legacy_where('t')}
          AND t.board_id IS NOT NULL
        ORDER BY t.updated_at DESC
        LIMIT 50
    """))
    candidates = res.fetchall()
    if not candidates:
        return 0

    materialized = 0
    for tid, bid, stamp in candidates:
        tid_s = str(tid)
        bid_s = str(bid)
        try:
            markers = await _task_markers(db, tid_s, since_current_status=True)
            pm_plan = _latest_marker(markers, "pm-plan")
            if not pm_plan:
                continue
            raw = pm_plan.get("kv", {}).get("subtasks")
            if not raw:
                continue
            # Skip if a prior tick already emitted any DAG marker on this task.
            if _latest_marker(markers, "watchdog-dag-materialized") or \
               _latest_marker(markers, "watchdog-dag-rejected"):
                continue

            parent = await _lock_legacy_task(db, tid_s, stamp, "in_progress")
            if parent is None:
                continue
            try:
                items = parse_subtasks_payload(raw)
                validate_subtasks(items)
            except DagValidationError as exc:
                await _post_comment(
                    db, tid_s, bid_s,
                    f"[watchdog-dag-rejected] reason={type(exc).__name__} "
                    f"notes=\"{str(exc)[:200]}\"",
                )
                await db.commit()
                logger.warning("watchdog.dag.rejected task=%s err=%s", tid_s, exc)
                continue

            summary = await materialize_dag(db, parent, items)
            await _post_comment(
                db, tid_s, bid_s,
                f"[watchdog-dag-materialized] children={summary['created']} "
                f"edges={summary['edges']}",
            )
            await db.commit()
            materialized += 1
            logger.info(
                "watchdog.dag.materialized parent=%s children=%d edges=%d",
                tid_s, summary["created"], summary["edges"],
            )
        except Exception as exc:
            await db.rollback()
            logger.warning("watchdog.dag.materialize_failed task=%s err=%s", tid_s, exc)
    return materialized


def _agent_recently_throttled(agent_name: str, window_seconds: int = 90) -> bool:
    """T104 (2026-05-23): return True if the agent's most recent LLM call
    failed with 429 / 504 within the past `window_seconds`.

    Owner saw watchdog re-notify Atlas every minute while vllm was already
    returning Semaphore timeout for Atlas's previous request — adds load
    instead of pausing. With sniffed `agent_name` from system prompt
    (T104 traffic identity fix) we can pause nudges for one tick.
    """
    if not agent_name:
        return False
    try:
        import os, sqlite3
        from pathlib import Path
        db_path = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
        if not db_path.exists():
            return False
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            from datetime import datetime, timezone, timedelta
            since = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
            r = conn.execute(
                "SELECT status_code FROM flows "
                "WHERE agent_name = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (agent_name, since),
            ).fetchone()
            if r and r[0] in (429, 504, 502, 503):
                return True
        finally:
            conn.close()
    except Exception:
        return False
    return False


async def _recover_stuck_claimed_inbox(db) -> int:
    """Inbox tasks that have an assignee but never moved to in_progress.

    Stage 1 (>10 min): re-emit gateway notification.
    Stage 2 (>30 min): rotate assignee to next least-busy pool agent that
    matches the original role; reset the timer.
    Returns number of recovery actions taken (re-notify or reassign).
    """
    res = await db.execute(text(f"""
        SELECT t.id, t.title, t.description, t.board_id, t.assigned_agent_id,
               a.name AS agent_name, t.updated_at
        FROM tasks t JOIN agents a ON a.id = t.assigned_agent_id
        WHERE t.status = 'inbox'
          AND t.assigned_agent_id IS NOT NULL
          AND t.updated_at < NOW() - INTERVAL '{_STUCK_INBOX_RENOTIFY_SECONDS} seconds'
          AND {_legacy_where('t')}
          AND NOT EXISTS (
              SELECT 1 FROM activity_events ae
              WHERE ae.task_id = t.id AND ae.event_type = 'task.assignee_notify_failed'
                AND ae.created_at >= t.updated_at
                AND ae.created_at > NOW() - INTERVAL '{_STUCK_INBOX_RENOTIFY_SECONDS} seconds'
          )
        ORDER BY t.updated_at ASC
        LIMIT 50
    """))
    rows = res.fetchall()
    if not rows:
        return 0

    actions = 0
    now_ts = datetime.now(timezone.utc).timestamp()
    for row in rows:
        tid = str(row[0])
        title = row[1] or ""
        desc = row[2] or ""
        board_id = str(row[3]) if row[3] else ""
        agent_id = str(row[4])
        agent_name = row[5] or ""
        upd_ts = _iso_to_ts(row[6]) or now_ts
        age = now_ts - upd_ts

        try:
            if age > _STUCK_INBOX_REASSIGN_SECONDS:
                # Per-task rotation cap: if we've already rotated this task
                # 3+ times and it still hasn't started, the gateway or pool
                # is broken. Stop looping and post a single stalled marker
                # so the owner can intervene.
                rotation_count = await _count_comments_this_cycle(
                    db, tid, "**Stuck-inbox rotation**"
                )
                if rotation_count >= 3:
                    stalled_already = await _count_comments_this_cycle(
                        db, tid, "[watchdog-stalled]"
                    )
                    if not stalled_already:
                        await _post_comment(
                            db, tid, board_id,
                            "[watchdog-stalled] reason=rotation_cap "
                            f"rotations={rotation_count} "
                            "notes=\"Task rotated 3+ times without progress. "
                            "Likely gateway down or entire role pool offline. "
                            "Owner intervention required.\""
                        )
                        await db.commit()
                    continue

                # Pick a different pool agent.
                # Strip variant suffixes to recover the role keyword.
                role = agent_name
                for sfx in (" Senior", " Beta", " Gamma", " Delta",
                            "-vllm", "-ollama", " [Clone-1]", " [Clone-2]"):
                    if role.endswith(sfx):
                        role = role[: -len(sfx)]
                role = role.strip()

                new_aid = await _pick_worker_for_role(db, role, board_id) if board_id else None
                if new_aid and new_aid != agent_id:
                    changed = await db.execute(text(f"""
                        UPDATE tasks SET assigned_agent_id = :a, updated_at = NOW()
                        WHERE {_legacy_where()} AND id = :t
                          AND status = 'inbox' AND updated_at = :stamp
                        RETURNING updated_at
                    """), {"a": new_aid, "t": tid, "stamp": row[6]})
                    assignment_stamp = changed.scalar_one_or_none()
                    if assignment_stamp is None:
                        await db.rollback()
                        continue  # owner pause/edit won; never dispatch this stale candidate
                    await _post_comment(
                        db, tid, board_id,
                        f"{_NUDGE_TAG} **Stuck-inbox rotation** — original "
                        f"assignee did not start within {int(age/60)} min. "
                        f"Reassigned to next pool member.", commit=False
                    )
                    await db.commit()
                    actions += 1
                    # Notify new assignee.
                    try:
                        from uuid import UUID as _UUID
                        from app.models.agents import Agent
                        from app.models.boards import Board
                        from app.api.tasks import notify_agent_on_task_assign
                        a_obj = await db.get(Agent, _UUID(new_aid))
                        b_obj = await db.get(Board, _UUID(board_id)) if board_id else None
                        t_obj = await _lock_legacy_task(db, tid, assignment_stamp, "inbox")
                        if a_obj and b_obj and t_obj:
                            await notify_agent_on_task_assign(
                                session=db, board=b_obj, task=t_obj, agent=a_obj,
                            )
                    except Exception as e:
                        logger.warning("watchdog.stuck_inbox.notify_failed task=%s err=%s", tid, e)
                    continue

            # Stage 1: re-notify same assignee.
            # Count how many times we've already re-notified this task
            # so we can auto-close it after N cycles if the agent is
            # non-functional. Previously updated_at was reset here, which
            # made stage 2 (30 min rotation) unreachable.
            renotify_count = await _count_comments_this_cycle(
                db, tid, "**Stuck-inbox re-notify**"
            )
            # Rate-limit on our OWN last nudge, not on `tasks.updated_at`.
            # This stage deliberately leaves `updated_at` alone so the 30-min
            # rotation stage stays reachable -- but `updated_at` was also the
            # only thing throttling the query above, so stage 1 fired on every
            # watchdog tick (~72s). Each wake message restarts the assignee's
            # turn, and a turn takes minutes on the shared local model, so the
            # agent could never finish: a livelock that looked like a dead
            # agent (observed at "cycle 56/3").
            since_last_nudge = await _seconds_since_comment_matching(
                db, tid, "**Stuck-inbox re-notify**"
            )
            if (
                since_last_nudge is not None
                and since_last_nudge < _STUCK_INBOX_RENOTIFY_SECONDS
            ):
                continue
            # Only auto-close/BLOCK after the 30-min rotation stage (above) has
            # had its chance — require BOTH >=3 nudges AND age past the reassign
            # threshold. Previously this fired on nudge-count alone (~16 min),
            # so rotation never ran and tasks were blocked prematurely.
            if renotify_count >= 3 and age > _STUCK_INBOX_REASSIGN_SECONDS:
                # Agent non-functional after 3 nudges + past the reassign
                # threshold → mark BLOCKED (not done). Marking done was a
                # false-success bug: a task stuck in *inbox* was never picked
                # up, so no work happened this cycle, yet the old repeat branch
                # closed it `done` "whether agent replied or not" — using a
                # lifetime (not current-cycle) reply count, so a stale comment
                # from a prior cycle read as completion. `blocked` is the
                # honest signal and surfaces in /review for owner triage.
                # Repeat scheduling still works: `_process_interval_repeats`
                # re-fires `blocked` repeating tasks at their interval.
                changed = await db.execute(text(
                    f"UPDATE tasks SET status='blocked', updated_at=NOW() WHERE {_legacy_where()} AND id = :t "
                    "AND status = 'inbox' AND updated_at = :stamp"
                ), {"t": tid, "stamp": row[6]})
                if changed.rowcount != 1:
                    continue  # keep Paused/Cancelled and owner edits unchanged
                await _post_comment(
                    db, tid, board_id,
                    f"{_NUDGE_TAG} **Auto-blocked** — stuck in inbox for "
                    f"{int(age/60)} min after {renotify_count} re-notify "
                    f"cycles. Assignee {agent_name} has not picked up this task. "
                    f"Marked BLOCKED — no work was performed. "
                    f"(Repeating tasks re-fire from blocked on their next interval.)"
                )
                await db.commit()
                actions += 1
                continue

            # T104 throttle-awareness: pause nudging when the agent's
            # provider is already returning 429/504 — re-notifying just
            # piles more requests on a saturated semaphore.
            if _agent_recently_throttled(agent_name):
                logger.info(
                    "watchdog.nudge.skip_throttled task=%s agent=%s",
                    tid[:8], agent_name,
                )
                continue

            from uuid import UUID as _UUID
            from app.models.agents import Agent
            from app.models.boards import Board
            from app.api.tasks import notify_agent_on_task_assign
            a_obj = await db.get(Agent, _UUID(agent_id))
            b_obj = await db.get(Board, _UUID(board_id)) if board_id else None
            t_obj = await _lock_legacy_task(db, tid, row[6], "inbox")
            if a_obj and b_obj and t_obj:
                delivered = await notify_agent_on_task_assign(
                    session=db, board=b_obj, task=t_obj, agent=a_obj,
                )
                if not delivered:
                    continue  # Failure activity is already durable; no false success/nudge.
                if await _lock_legacy_task(db, tid, row[6], "inbox") is None:
                    continue  # Committed wake lost original authority; no stale success marker.
                await _post_comment(
                    db, tid, board_id,
                    f"{_NUDGE_TAG} **Stuck-inbox re-notify** — assignee "
                    f"{agent_name} did not start within {int(age/60)} min. "
                    f"Wake message re-sent. (cycle {renotify_count + 1}/3)"
                )
                await db.commit()
                actions += 1
        except Exception as e:
            await db.rollback()
            logger.warning("watchdog.stuck_inbox.failed task=%s err=%s", tid, e)
    return actions


async def _is_valid_role_pin(db, hint: str, board_id: str) -> bool:
    """Patch 1 (2026-07-16): validate a `;role=` hint against the roles the
    system actually knows — the `_ROUTE_KEYWORDS` role set plus any live
    agent-name prefix (the same prefix match `_pick_worker_for_role` uses).
    Derived from existing data, not a new hand-maintained list.
    """
    if hint in {role for _, role in _ROUTE_KEYWORDS}:
        return True
    row = (await db.execute(text(
        "SELECT 1 FROM agents "
        "WHERE board_id = :bid AND (name = :h OR name LIKE :p) "
        "  AND status NOT IN ('retired', 'deleting', 'updating') "
        "LIMIT 1"
    ), {"bid": board_id, "h": hint, "p": f"{hint}%"})).first()
    return row is not None


def _compose_routing_comment(label: str, reason: str, pin_note: str = "") -> str:
    """Truthful routing marker (Patch 1). The old text always claimed
    'role-matched from task title' even when the task fell through to the
    QA default or the lead — misleading during triage.
    """
    return (
        f"[watchdog-inbox] **Routed to {label}** — reason={reason}.{pin_note} "
        f"Auto-triage by pipeline watchdog."
    )


async def _route_manual_inbox_tasks(db) -> int:
    """Route unassigned inbox tasks by matching role keywords from title/description.
    Falls to Atlas (board lead) when no role matches. Returns count of triaged tasks.
    """
    try:
        # Fetch unassigned inbox tasks created by humans (not system/harness).
        # NULL created_by_type = legacy/owner-created via API (Postgres NULL NOT IN
        # returns NULL, not TRUE — must be handled explicitly).
        # 5-min grace period: agents may self-volunteer first; watchdog only routes
        # tasks that nobody has picked up after _INBOX_VOLUNTEER_GRACE_SECONDS.
        # Dependency gate (E1 foundation, 2026-05-14): skip tasks whose
        # task_dependencies edges have any depends_on_task in a non-done state.
        # Once deps complete, next tick will route the dependent automatically.
        # T91 (2026-05-16): subtasks bypass the 5-min volunteer grace.
        # Parent already established intent at create-time; children are
        # split off WHEN parent is moved to in_progress, so making them
        # wait another 5 min before any agent picks them up serializes
        # the split unnecessarily. `parent_task_id IS NOT NULL` → route
        # immediately on next tick.
        inbox_res = await db.execute(text(f"""
            SELECT t.id, t.title, t.description, t.board_id, t.auto_reason, t.updated_at
            FROM tasks t
            WHERE t.status = 'inbox'
              AND t.assigned_agent_id IS NULL
              -- Coding-harness tasks belong to mc-claude-runner, not gateway
              -- triage: assigning them strands the runner's inbox-only claim.
              -- `execution_lane` is the explicit selector; the title prefix is
              -- the legacy convention and only decides when the lane is unset.
              AND {_legacy_where('t')}
              AND (
                t.parent_task_id IS NOT NULL
                OR t.created_at < NOW() - INTERVAL '{_INBOX_VOLUNTEER_GRACE_SECONDS} seconds'
              )
              AND (t.created_by_type IS NULL
                   OR t.created_by_type NOT IN ('system', 'autonomy-harness', 'tests-harness'))
              AND NOT EXISTS (
                  SELECT 1
                  FROM task_dependencies td
                  JOIN tasks dep_t ON dep_t.id = td.depends_on_task_id
                  WHERE td.task_id = t.id
                    AND dep_t.status <> 'done'
              )
              -- Recurring-task cooldown: a repeating task that just finished a
              -- cycle rests in `inbox` with a `[cycle-complete]` marker. Don't
              -- re-assign it until its interval has elapsed (else it would run
              -- back-to-back instead of on schedule).
              AND NOT (
                COALESCE(t.repeat_enabled, false) = true
                AND EXISTS (
                  SELECT 1 FROM activity_events cc
                  WHERE cc.task_id = t.id AND cc.event_type = 'task.comment'
                    AND cc.message LIKE '[cycle-complete]%'
                    AND cc.created_at > NOW()
                        - (COALESCE(t.repeat_interval_minutes, 60) || ' minutes')::interval
                )
              )
            ORDER BY t.created_at ASC
            LIMIT 50
        """))
        rows = inbox_res.fetchall()
        if not rows:
            return 0

        triaged = 0
        for row in rows:
            task_id = str(row[0])
            title = row[1] or ""
            desc = row[2] or ""
            task_board_id = str(row[3]) if row[3] else ""
            auto_reason = row[4] or ""
            if not task_board_id:
                continue
            lead_row = (await db.execute(text("""
                SELECT id FROM agents
                WHERE board_id = :bid AND is_board_lead = true
                  AND status NOT IN ('retired', 'deleting', 'updating')
                LIMIT 1
            """), {"bid": task_board_id})).first()
            if not lead_row:
                logger.warning("watchdog.inbox_triage.no_board_lead task=%s board=%s",
                               task_id, task_board_id)
                continue
            atlas_id = str(lead_row[0])

            # T104 (2026-05-23): honor splitter's `;role=<Role Name>` hint
            # in auto_reason so children fan out to distinct specs instead
            # of all collapsing back to parent's assignee. Strong signal —
            # skip keyword + LLM steps when the hint is present.
            hint_role = None
            if ";role=" in auto_reason:
                hint = auto_reason.split(";role=", 1)[1].strip()
                if hint:
                    # `;role=<Role>;artifact=...` — the pin ends at the next `;`.
                    hint_role = hint.split(";", 1)[0].strip() or None

            # Patch 1 (2026-07-16): validate the pin against known roles /
            # live agent pools. Invalid pins keep TODAY's fallback behavior
            # (picker miss → QA default → lead) but become visible instead
            # of silently degrading.
            pin_note = ""
            if hint_role is not None and not await _is_valid_role_pin(db, hint_role, task_board_id):
                pin_note = (
                    f" note=invalid role pin {hint_role!r} "
                    f"(no matching role or agent pool)."
                )
                logger.warning(
                    "watchdog.routing.invalid_role_pin task=%s hint=%r",
                    task_id[:8], hint_role,
                )

            # T65 (2026-05-15): primary path is LLM evaluation. Keyword
            # match is the cheap pre-filter (avoid burning an LLM call on
            # tasks the keyword path already classifies confidently) and
            # the offline fallback (LLM down → keep current behaviour).
            body_lower = (title + " " + desc).lower()
            matched_role = hint_role
            route_source = "role-pin" if hint_role is not None else None
            if matched_role is None:
                for kw, role in _ROUTE_KEYWORDS:
                    if kw in body_lower:
                        matched_role = role
                        route_source = "keyword-match"
                        break

            # Optionally consult the LLM to refine the keyword match. Gated OFF
            # by default (WATCHDOG_ROUTING_LLM_ENABLED) to minimize LLM calls:
            # keyword match + QA-Engineer fallback route every task correctly
            # without a per-task classification round-trip (the LLM usually
            # returned None anyway → keyword result stood). Set the env to "1"
            # to re-enable LLM-assisted role routing.
            # P3 Slice 1 (2026-09-01): read the flag at call-time via
            # runtime_settings so it flips live (no restart). Falls back to the
            # module-import env default when the sidecar/env is unset.
            from app.services.runtime_settings import get_setting as _get_setting
            _routing_llm_on = _get_setting(
                "WATCHDOG_ROUTING_LLM_ENABLED", _ROUTING_LLM_ENABLED
            )
            if hint_role is None and _routing_llm_on:
                try:
                    from app.services.llm_evaluator import pick_role_for_task
                    candidates = sorted({role for _, role in _ROUTE_KEYWORDS})
                    # Avoid the LLM call on truly empty titles — nothing to classify.
                    if (title or desc).strip():
                        llm_role = await pick_role_for_task(title, desc, candidates)
                        if llm_role:
                            if matched_role and llm_role != matched_role:
                                logger.info(
                                    "watchdog.routing.llm_override task=%s kw=%s llm=%s",
                                    task_id[:8], matched_role, llm_role,
                                )
                            matched_role = llm_role
                            route_source = "llm-classifier"
                except Exception as exc:
                    logger.warning("watchdog.routing.llm_eval_failed err=%s", exc)

            # Pool router: pick least-busy online agent in the role pool
            # that still has provider capacity. Falls back to QA Engineer
            # worker pool (T64) when no role matches — Atlas is a PM/lead
            # and doesn't post task comments. Last resort: Atlas, only if
            # even the QA pool is empty.
            picked: str | None = None
            if matched_role:
                picked = await _pick_worker_for_role(db, matched_role, task_board_id)
            if not picked:
                picked = await _pick_worker_for_role(db, "QA Engineer", task_board_id)
                if picked:
                    if matched_role:
                        route_source = (
                            f"qa-default (no worker available in "
                            f"'{matched_role}' pool)"
                        )
                    else:
                        route_source = "qa-default (no role matched)"
                    matched_role = "QA Engineer (default worker)"
            if picked:
                new_agent_id = picked
            else:
                new_agent_id = atlas_id
                matched_role = None  # truly no worker available — give to lead
                route_source = "lead-last-resort (no worker available in any pool)"

            # PM mutex — only one PM (or watchdog acting on a PM's behalf)
            # processes the task at a time. STATE.md §3 invariant 2.
            pm_id = await _pick_pm_for_board(db, task_board_id) or atlas_id
            if not await _pm_acquire_lock(db, task_id, pm_id):
                continue  # another PM already handling this task

            # Skip if already assigned (race with another watchdog tick)
            try:
                current = await db.execute(text("""
                    SELECT assigned_agent_id FROM tasks WHERE id = :tid
                """), {"tid": task_id})
                current_row = current.first()
                if current_row and current_row[0] is not None:
                    await _pm_release_lock(db, task_id, pm_id)
                    await db.commit()
                    continue

                changed = await db.execute(text(f"""
                    UPDATE tasks SET assigned_agent_id = :aid, updated_at = NOW()
                    WHERE {_legacy_where()} AND id = :tid AND assigned_agent_id IS NULL
                      AND status = 'inbox' AND updated_at = :stamp
                    RETURNING updated_at
                """), {"aid": new_agent_id, "tid": task_id, "stamp": row[5]})
                assignment_stamp = changed.scalar_one_or_none()
                if assignment_stamp is None:
                    await _pm_release_lock(db, task_id, pm_id)
                    await db.commit()
                    continue
                await _post_comment(
                    db, task_id, task_board_id,
                    _compose_routing_comment(
                        matched_role or "Lead (Atlas)",
                        route_source or "unrouted",
                        pin_note,
                    ), commit=False
                )
                # Release lock once routed — worker now owns the task via assigned_agent_id.
                await _pm_release_lock(db, task_id, pm_id)
                await db.commit()

                # Wake the assignee via gateway so they actually start work.
                # Without this, the agent only finds out on next heartbeat.
                try:
                    from uuid import UUID as _UUID
                    from app.models.agents import Agent
                    from app.models.boards import Board
                    from app.api.tasks import notify_agent_on_task_assign
                    agent_obj = await db.get(Agent, _UUID(new_agent_id))
                    board_obj = await db.get(Board, _UUID(task_board_id))
                    task_obj = await _lock_legacy_task(db, task_id, assignment_stamp, "inbox")
                    if agent_obj and board_obj and task_obj:
                        await notify_agent_on_task_assign(
                            session=db, board=board_obj, task=task_obj, agent=agent_obj,
                        )
                except Exception as e:
                    logger.warning(
                        "watchdog.inbox_triage.notify_failed task=%s err=%s",
                        task_id, e,
                    )

                triaged += 1
            except Exception as e:
                await db.rollback()
                logger.warning(
                    "watchdog.inbox_triage.route_failed task=%s err=%s", task_id, e
                )

        return triaged
    except Exception as e:
        logger.warning("watchdog.inbox_triage_failed err=%s", e)
        return 0


async def _reassign_task(db, task_id: str, new_agent_id: str, stamp, comment: str) -> bool:
    """Original-generation assignment and its marker are one effect, or neither."""
    try:
        task = await _lock_legacy_task(db, task_id, stamp, "in_progress")
        if task is None:
            return False
        changed = await db.execute(
            text(
                "UPDATE tasks SET assigned_agent_id = :aid, updated_at = NOW() "
                f"WHERE {_legacy_where()} AND id = :tid AND status = 'in_progress' "
                "AND updated_at = :stamp"
            ),
            {"aid": new_agent_id, "tid": task_id, "stamp": task.updated_at},
        )
        if changed.rowcount != 1:
            await db.rollback()
            return False
        await _post_comment(db, task_id, task.board_id.hex if task.board_id else "",
                            comment, commit=False)
        await db.commit()
        return True
    except Exception as e:
        logger.warning("watchdog.reassign_failed task=%s err=%s", task_id, e)
        await db.rollback()
        return False


def _cleanup_harness_items() -> int:
    """Remove stale test-harness / anonymous review items older than 10 min
    via owner_reviews table (T18). Returns count of rows removed.
    """
    from app.services.owner_reviews import cleanup_harness_items
    n = cleanup_harness_items(age_sec=600)
    if n > 0:
        logger.info("watchdog.harness_cleanup removed=%d", n)
    return n


async def _reconcile_resolved_reviews() -> int:
    """Auto-close pending watchdog review items whose underlying task is now done/closed
    (T18 — single Postgres UPDATE replaces the prior file-scan loop).
    """
    from app.services.owner_reviews import reconcile_resolved_with_tasks
    return reconcile_resolved_with_tasks()


def _auto_resolve_stale_reviews() -> int:
    """Expire pending watchdog review items >4h old (T18 Postgres-backed).

    The task keeps its status: closing it as `done` reported unreviewed and
    undeployed work as finished. Re-escalation is already capped by the 24h
    `task_recently_escalated` cooldown.
    """
    from app.services.owner_reviews import auto_resolve_stale
    task_ids = auto_resolve_stale(stale_sec=4 * 3600)
    if task_ids:
        logger.info("watchdog.auto_resolved_stale count=%d", len(task_ids))
    return len(task_ids)


_SOUL_REFRESH_AGE_DAYS = float(os.environ.get("WATCHDOG_SOUL_REFRESH_AGE_DAYS", "25"))


async def _refresh_stale_souls(db) -> int:
    """SOUL keep-alive: re-sync workspace SOUL.md from the DB `soul_template`
    for live (non-retired) agents whose file is older than
    `_SOUL_REFRESH_AGE_DAYS`. The canonical SOUL lives in the DB; the
    workspace file is a materialized copy that otherwise never gets re-stamped
    for idle-but-online agents, so `docs.soul_freshness` would eventually fail
    even though the agent is active. Retired agents + orphan dirs are left
    alone (decommissioned — not gated by the freshness invariant). No LLM call.
    Self-limiting: after a refresh the file is <cutoff so it won't rewrite next
    tick. Returns count refreshed.
    """
    home = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
    now = time.time()
    cutoff = _SOUL_REFRESH_AGE_DAYS * 86400
    refreshed = 0
    try:
        rows = await db.execute(
            text("SELECT id, COALESCE(soul_template, '') FROM agents WHERE status != 'retired'")
        )
        agents = rows.fetchall()
    except Exception as e:
        logger.debug("watchdog.soul_refresh.query_failed err=%s", e)
        return 0
    for aid, soul_template in agents:
        ws = home / f"workspace-mc-{aid}"
        if not ws.is_dir():
            continue  # workspace not materialized — nothing to keep fresh
        soul = ws / "SOUL.md"
        try:
            if soul.exists() and (now - soul.stat().st_mtime) < cutoff:
                continue  # still fresh
            content = (soul_template or "").strip()
            if content:
                soul.write_text(content + "\n", encoding="utf-8")  # re-sync from DB
            elif soul.exists():
                os.utime(soul, None)  # keep-alive touch; preserve existing content
            else:
                continue  # no template and no file — skip
            refreshed += 1
        except Exception as e:
            logger.debug("watchdog.soul_refresh.write_failed agent=%s err=%s", aid, e)
    if refreshed:
        logger.info("watchdog.soul_refresh refreshed=%d", refreshed)
    return refreshed


async def _ensure_capacity_owner_review(db) -> int:
    """Capacity primitive (test `capacity.queue_depth_alert`): when the task
    inbox backlog exceeds 2× the recently-active agent count, raise a
    P11.1-compliant `type='capacity'` Owner Review so the owner can add
    headcount/concurrency. Idempotent — skips when a pending capacity review
    already exists. Returns 1 if created, else 0.
    """
    try:
        inbox = (
            await db.execute(text("SELECT COUNT(*) FROM tasks WHERE status='inbox' AND execution_mode IS DISTINCT FROM 'team'"))
        ).scalar() or 0
        active = max(
            (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM agents "
                        "WHERE last_seen_at > NOW() - INTERVAL '15 minutes'"
                    )
                )
            ).scalar()
            or 1,
            1,
        )
    except Exception as e:
        logger.debug("watchdog.capacity_review.query_failed err=%s", e)
        return 0
    if inbox <= 2 * active:
        return 0
    try:
        from app.services.owner_reviews import insert_sync, list_pending_sync

        for i in list_pending_sync():
            if "capacity" in (i.get("title", "") + (i.get("reason") or "")).lower():
                return 0  # already raised — don't duplicate
        ok = insert_sync(
            title=f"CAPACITY: inbox backlog {inbox} > 2× active agents ({active})",
            type_="capacity",
            summary=(
                f"Task inbox depth {inbox} exceeds 2× the {active} agents "
                f"active in the last 15 minutes."
            ),
            reason=(
                f"Queue depth {inbox} > 2*active({active}). Tasks are arriving "
                f"faster than the active fleet can claim them."
            ),
            proposed_action=(
                "Bring additional worker agents online (or raise per-agent "
                "concurrency) until inbox ≤ 2× active."
            ),
            submitted_by="Autonomy Watchdog",
        )
        if ok:
            logger.info(
                "watchdog.capacity_review.created inbox=%d active=%d", inbox, active
            )
            return 1
    except Exception as e:
        logger.warning("watchdog.capacity_review.failed err=%s", e)
    return 0


async def _ensure_lead_election_ballot(db) -> int:
    """Hierarchy primitive (test `hierarchy.lead_election_ready`): when the
    lead agent (Atlas) has been unseen >4h while the board still has active
    work, open a `lead-election` ballot so the fleet can elect an interim
    lead. Idempotent — skips when the board is idle or an open lead-election
    ballot already exists. Returns 1 if opened, else 0.
    """
    try:
        secs = (
            await db.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - last_seen_at)) "
                    "FROM agents WHERE name='Atlas'"
                )
            )
        ).scalar()
        if secs is None or secs < 4 * 3600:
            return 0
        active = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE status IN ('inbox','in_progress','review')"
                )
            )
        ).scalar() or 0
        if active == 0:
            return 0  # board idle — no lead needed
    except Exception as e:
        logger.debug("watchdog.lead_election.query_failed err=%s", e)
        return 0
    try:
        from datetime import timedelta

        conn = sqlite3.connect("/traffic-data/votes.db", timeout=5)
        try:
            if conn.execute(
                "SELECT 1 FROM ballots WHERE topic LIKE '%lead-election%' "
                "AND status='open' LIMIT 1"
            ).fetchone():
                return 0  # election already in progress
            bid = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat()
            expires = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
            conn.execute(
                """
                INSERT INTO ballots
                (id, topic, description, proposed_action, target_task_id, target_board_id,
                 required_quorum, veto_roles, status, created_by, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    bid,
                    "lead-election",
                    f"Atlas (lead) unseen for {int(secs / 3600)}h while {active} "
                    f"tasks are active. Elect an interim lead.",
                    "Nominate + vote an interim lead to keep coordination alive "
                    "until Atlas returns.",
                    None,
                    None,
                    2,
                    "",
                    "open",
                    "Autonomy Watchdog",
                    now_iso,
                    expires,
                ),
            )
            conn.commit()
            logger.info(
                "watchdog.lead_election.opened ballot=%s atlas_stale_h=%d active=%d",
                bid[:8],
                int(secs / 3600),
                active,
            )
            return 1
        finally:
            conn.close()
    except Exception as e:
        logger.warning("watchdog.lead_election.failed err=%s", e)
    return 0


async def _trigger_test_run() -> int:
    """Trigger autonomy test run if last run > debounce interval ago.

    T46 (2026-05-14): debounce window now configurable via
    `WATCHDOG_TEST_TRIGGER_DEBOUNCE_SECONDS` (default 1800 = 30 min).

    Returns 1 if triggered, 0 if skipped.
    """
    LOCAL_AUTH = os.getenv("LOCAL_AUTH_TOKEN", "")
    if not LOCAL_AUTH:
        return 0
    debounce_sec = int(os.environ.get("WATCHDOG_TEST_TRIGGER_DEBOUNCE_SECONDS", "1800"))

    try:
        conn = sqlite3.connect(_TESTS_DB, timeout=2)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS watchdog_test_trigger (id INTEGER PRIMARY KEY, triggered_at TEXT)")
        except Exception:
            pass
        row = conn.execute("SELECT triggered_at FROM watchdog_test_trigger ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            last = row[0]
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < debounce_sec:
                    conn.close()
                    return 0
            except Exception:
                pass
        conn.execute("INSERT INTO watchdog_test_trigger (triggered_at) VALUES (?)", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
    except Exception:
        return 0

    try:
        req = urllib.request.Request(
            "http://localhost:8000/api/v1/tests/run",
            method="POST",
            headers={"Authorization": f"Bearer {LOCAL_AUTH}"},
        )
        # run_all executes the full ~60-test suite synchronously, several of
        # which proxy LLM calls through OmniRoute. Under OR/vLLM latency spikes
        # a 30s ceiling timed out (`test_trigger_failed`), starving the
        # remediation auto-close loop. 180s tolerates the slow path; the
        # trigger is debounced 30 min so a long run is harmless.
        trigger_timeout = int(os.environ.get("WATCHDOG_TEST_TRIGGER_TIMEOUT_SECONDS", "180"))
        # 2026-10-07: the blocking call ran on the event loop of the very process it
        # calls (single uvicorn worker), so the backend froze for the full timeout and
        # the trigger always failed. Previous lines, kept for reference:
        # resp = urllib.request.urlopen(req, timeout=trigger_timeout)
        # data = json.loads(resp.read().decode())
        def _fetch() -> dict:
            with urllib.request.urlopen(req, timeout=trigger_timeout) as resp:
                return json.loads(resp.read().decode())

        data = await asyncio.to_thread(_fetch)
        npass = data.get("summary", {}).get("pass", 0)
        nfail = data.get("summary", {}).get("fail", 0)
        nerr = data.get("summary", {}).get("error", 0)
        npend = data.get("summary", {}).get("pending", 0)
        logger.info("watchdog.test_trigger pass=%d fail=%d error=%d pending=%d", npass, nfail, nerr, npend)

        if nfail > 0 or nerr > 0:
            row = sqlite3.connect(_TESTS_DB).execute(
                "SELECT COUNT(*) FROM watchdog_test_trigger WHERE id > (SELECT COALESCE(MAX(id)-2, 0) FROM watchdog_test_trigger)"
            ).fetchone()
            recent_fails = (row[0] if row else 0)
            if recent_fails >= 2:
                logger.warning("watchdog.test_degraded consecutive_failures=%d", recent_fails)
        return 1
    except Exception as e:
        logger.warning("watchdog.test_trigger_failed err=%s", e)
        return 0


async def _detect_qa_bypass() -> int:
    """Disabled per owner directive (2026-05-08): once a task is `done` it stays
    done. No auto-reopen, no flag-comment, no second-guessing. Owner manually
    moves the task back to `inbox` / `in_progress` if a re-review is wanted.

    Kept as a no-op so the watchdog tick stats still emit `qa_bypass=0` and any
    monitoring of the field doesn't break.
    """
    return 0
    # — Legacy implementation below preserved for reference only —
    detected = 0  # type: ignore[unreachable]
    async with async_session_maker() as db:
        try:
            result = await db.execute(
                text("""
                SELECT t.id, t.title, t.assigned_agent_id, t.updated_at,
                       a.name AS agent_name, t.board_id
                FROM tasks t
                LEFT JOIN agents a ON a.id = t.assigned_agent_id
                WHERE t.status = 'done'
                  AND t.updated_at > NOW() - INTERVAL '30 minutes'
            """)
            )
            rows = result.fetchall()
        except Exception as e:
            logger.warning("watchdog.qa_bypass.scan_failed err=%s", e)
            return 0

        for r in rows:
            tid = str(r[0])
            try:
                # Check for QA verdict OR prior watchdog auto-close in comments.
                # Without the auto-close skip, qa-bypass repeatedly re-opens
                # tasks that the watchdog itself just closed, creating a loop.
                vresult = await db.execute(
                    text("""
                    SELECT message FROM activity_events
                    WHERE task_id = :tid AND event_type = 'task.comment'
                      AND (LOWER(message) LIKE '%qa verdict: pass%'
                        OR LOWER(message) LIKE '%qa-verdict: pass%'
                        OR LOWER(message) LIKE '%[qa-verdict]%result=pass%'
                        OR LOWER(message) LIKE '%auto-closed to `done`%')
                """),
                    {"tid": tid},
                )
                if vresult.first():
                    continue  # has QA pass or watchdog auto-close

                # Check we haven't already flagged this task
                existing = await db.execute(
                    text("""
                    SELECT 1 FROM activity_events
                    WHERE task_id = :tid AND event_type = 'task.comment'
                      AND message LIKE :pat
                """),
                    {"tid": tid, "pat": f"%{_NUDGE_TAG} **QA-bypass detected**%"},
                )
                if existing.first():
                    continue

                title = r[1] or "?"
                board_id = str(r[5]) if r[5] else ""

                # Skip meta-tasks — auto-resolved by other paths.
                if any(title.startswith(p) for p in _META_PREFIXES):
                    continue

                # Tier 2: re-open task and reassign to Senior QA variant for re-verification
                # rather than escalating to Owner Review.
                qa_senior = await _find_senior_variant(db, "QA Engineer", None)
                if qa_senior:
                    try:
                        await db.execute(text(
                            "UPDATE tasks SET status='review', assigned_agent_id=:aid, "
                            f"updated_at=NOW() WHERE {_legacy_where()} AND id=:tid AND status='done'"
                        ), {"aid": qa_senior, "tid": tid})
                        await db.commit()
                        await _post_comment(
                            db, tid, board_id,
                            f"{_NUDGE_TAG} **QA-bypass auto-recovery** — task closed without QA Verdict. "
                            f"Re-opened to `review`, routed to QA Engineer Senior "
                            f"(DB id {qa_senior[:12]}) for verdict per POLICY P11.2."
                        )
                        detected += 1
                        continue
                    except Exception as e:
                        logger.warning("watchdog.qa_bypass.reopen_failed task=%s err=%s", tid, e)

                # Fallback: comment-only flag (don't escalate to Owner Review)
                await _post_comment(
                    db, tid, board_id,
                    f"{_NUDGE_TAG} **QA-bypass flagged** — task closed without QA Verdict, "
                    f"no QA Senior available to re-verify. Atlas, please audit."
                )
                continue
            except Exception as e:
                logger.warning("watchdog.qa_bypass.task_failed task=%s err=%s", tid, e)
    return detected


# Auto-close review tasks with approved high-confidence approvals
_AUTO_CLOSE_CONFIDENCE_THRESHOLD = float(
    get_setting("WATCHDOG_AUTO_CLOSE_CONFIDENCE")
)


async def _task_markers(db, task_id: str, since_current_status: bool = True) -> list[dict[str, Any]]:
    """Return canonical markers seen on a task, newest comment last.

    `since_current_status=True` (default): only markers posted after the most
    recent `task.status_changed` event. Prevents stale markers from a previous
    review cycle (e.g. an old `[pm-signoff] verdict=reject`) from poisoning a
    fresh review.
    """
    where = "WHERE task_id = :tid AND event_type = 'task.comment'"
    if since_current_status:
        where += """
            AND created_at >= COALESCE(
                (SELECT MAX(created_at) FROM activity_events
                 WHERE task_id = :tid AND event_type = 'task.status_changed'),
                '1970-01-01'::timestamp
            )
        """
    res = await db.execute(text(f"""
        SELECT message FROM activity_events
        {where}
        ORDER BY created_at ASC
    """), {"tid": task_id})
    out: list[dict[str, Any]] = []
    for (msg,) in res.fetchall():
        out.extend(_parse_markers(msg))
    return out


def _latest_marker(markers: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    for m in reversed(markers):
        if m["tag"] == tag:
            return m
    return None


async def _auto_approve_high_confidence_approvals(db) -> int:
    """Auto-approve pending approvals whose confidence meets the threshold.

    No marker gates, no task status checks — high confidence IS the verdict.
    This unblocks the status gates so tasks can proceed to done naturally or
    via the existing auto-close machinery in ``_auto_close_review_tasks``.

    2026-10-05: while owner-authorized autonomous acceptance is ENABLED this
    path stands down — confidence is never acceptance evidence, and the
    dedicated read-only reviewer owns the delegated Done decision.

    Returns count of newly-approved rows.
    """
    try:
        from app.services.owner_reviews import automation_enabled

        result = await db.execute(
            text(f"""
                SELECT a.id, a.task_id, a.confidence, a.board_id, t.title, b.organization_id, t.updated_at, t.status
                FROM approvals a
                LEFT JOIN tasks t ON t.id = a.task_id
                LEFT JOIN boards b ON b.id = COALESCE(a.board_id, t.board_id)
                WHERE a.status = 'pending'
                  AND {_legacy_where('t')}
                  AND a.confidence >= :threshold
                ORDER BY a.created_at ASC
            """),
            {"threshold": _AUTO_CLOSE_CONFIDENCE_THRESHOLD},
        )
        rows = result.fetchall()
        if not rows:
            return 0

        approved = 0
        for aid, tid, conf, bid, title, org_id, stamp, status in rows:
            if automation_enabled(org_id):
                continue
            try:
                if tid and await _lock_legacy_task(db, str(tid), stamp, status) is None:
                    continue
                changed = await db.execute(
                    text("""
                        UPDATE approvals
                        SET status = 'approved', resolved_at = NOW()
                        WHERE id = :aid AND status = 'pending'
                    """),
                    {"aid": aid},
                )
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                task_ref = title or (str(tid)[:8] if tid else "unknown")
                if tid and bid:
                    await _post_comment(
                        db, str(tid), str(bid),
                        f"**Auto-approved** — approval (confidence {conf}%) met "
                        f"threshold ({_AUTO_CLOSE_CONFIDENCE_THRESHOLD}%). "
                        f"Status: `pending` → `approved`.",
                        commit=False,
                    )
                await db.commit()
                approved += 1
                logger.info(
                    "watchdog.auto_approved approval=%s task=%s confidence=%.1f",
                    str(aid)[:8], task_ref, conf,
                )
            except Exception as e:
                logger.warning(
                    "watchdog.auto_approve_failed approval=%s err=%s",
                    str(aid)[:8], e,
                )
            finally:
                await db.rollback()
        await db.commit()
        return approved
    except Exception as e:
        logger.warning("watchdog.auto_approve_all_failed err=%s", e)
        return 0


# T84 (2026-05-16): completion-signal patterns. When ≥2 distinct agents
# emit comments containing one of these on the same task in `review`,
# the watchdog treats it as quorum and auto-posts `[qa-verdict]` (and
# `[pm-signoff]` for late-reassess L4+) so the task can self-close
# without owner intervention.
import re as _re_t84
_COMPLETION_RE = _re_t84.compile(
    r"(\[ready-to-close\]|\[qa-verdict\]|FINAL\s+VERDICT|"
    r"password\s*[:=]|answer\s*[:=]|result\s*[:=]\s*pass|"
    r"flag\s*[:=]|key\s*[:=]\s*[A-Za-z0-9_]{4,})",
    _re_t84.IGNORECASE,
)


def _extract_completion_artifact(message: str) -> str | None:
    """Pull a short canonical artifact (Password: foo / Flag: bar / ...).

    Used as a cheap consensus check: two agents that report `Password:
    nDoEiA` agree even if their surrounding analyses differ.
    """
    m = _re_t84.search(
        r"(?:password|answer|flag|key)\s*[:=]\s*[`\"']?([A-Za-z0-9_\-]{3,64})[`\"']?",
        message or "", _re_t84.IGNORECASE,
    )
    if m:
        return m.group(1).strip().lower()
    return None


async def _seed_kb_from_consensus(
    db,
    task_id: str,
    title: str,
    reason: str,
    level: int,
    artifact: str | None,
) -> None:
    """T99 (2026-05-17): persist a KB entry per consensus pass so future
    tasks with similar titles can pull the prior decision via the FTS
    endpoint. Topic prefixed `consensus:` so they're easy to filter.
    """
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timezone as _tz
    import uuid as _uuid
    kb_path = os.environ.get("TRAFFIC_DB_FILE") or os.environ.get("KB_DB_FILE")
    if not kb_path:
        kb_path = "/traffic-data/kb.db"
    # Reuse the same path the kb router uses.
    try:
        from app.api.kb import _DB as _kb_db_path
        kb_path = str(_kb_db_path)
    except Exception:
        pass
    try:
        conn = _sqlite3.connect(kb_path)
        # Dedup by (task_id) so we don't seed twice for the same task.
        cur = conn.execute(
            "SELECT 1 FROM entries WHERE topic LIKE ? LIMIT 1",
            (f"consensus:{task_id[:12]}%",),
        )
        if cur.fetchone():
            conn.close()
            return
        rid = str(_uuid.uuid4())
        topic = f"consensus:{task_id[:12]} {title[:80]}"
        decision = (
            f"Legacy task L{level} received consensus review markers (not accepted Done or native QA). "
            f"Reason: {reason}. "
            f"Artifact: {artifact or 'n/a'}."
        )
        rationale = (
            f"≥2 distinct agents converged on the same completion signal. "
            f"Auto-emitted `[qa-verdict] result=pass` per T84."
        )
        tags = f"consensus,L{level},auto"
        conn.execute("""
            INSERT INTO entries
              (id, topic, decision, rationale, tags, agent_id, board_id,
               created_at, references_json)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, '[]')
        """, (rid, topic, decision, rationale, tags, _dt.now(_tz.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("kb_seed.exec_failed err=%s", exc)


_CONSENSUS_SOLICIT_MARKER = "[consensus-solicit]"
_CONSENSUS_MIN_AGENTS = int(os.environ.get("CONSENSUS_MIN_AGENTS", "2"))
_CONSENSUS_FANOUT = int(os.environ.get("CONSENSUS_FANOUT", "3"))
_STATIC_RE_TITLE_SIGNALS = (
    "crackme", "reverse engineer", "reverse-engineer", "binary analysis",
    "malware analysis", "firmware analysis",
)


def _is_static_re_task(title: str) -> bool:
    """Only static RE jobs belong to the independent analyst quorum."""
    title = title.lower().strip()
    return not title.startswith(("[agent]", "[claude]", "[codex]")) and any(
        signal in title for signal in _STATIC_RE_TITLE_SIGNALS
    )


_RE_CONSENSUS_POOL = (
    "Reverse Engineer Senior", "Reverse Engineer", "Reverse Engineer-vllm",
    "Binary Analyst", "Binary Analyst Senior", "Security Architect",
)


async def _solicit_consensus_for_tool_tasks(db) -> int:
    """T108 (2026-05-30): consensus-by-independent-analysis for tasks that
    need REAL tools but can only be confirmed via STATIC reasoning (remote
    MCP / IDA pseudocode — agents cannot execute the binary).

    Owner model: ≥2 RE agents independently static-analyze the same task,
    each post `Password: <value>` (+ reasoning); when ≥2 converge on the
    SAME value, `_auto_emit_consensus_verdict` (T84) marks it pass. This
    REPLACES PEV's text-only fabricating executor for such tasks.

    This step: for an inbox tool-task on an autonomous board not yet
    solicited, wake N distinct online RE-pool agents onto the SAME task
    with the static-analysis protocol, set in_progress, drop the marker.
    Returns count of tasks solicited.
    """
    try:
        from app.services.pev_loop import _task_requires_real_tools
    except Exception:
        return 0
    rows = (await db.execute(text(f"""
        SELECT t.id, t.title, t.description, t.board_id, t.updated_at
        FROM tasks t JOIN boards b ON b.id = t.board_id
        WHERE b.autonomous_enabled = true
          AND t.status = 'inbox'
          AND {_legacy_where('t')}
          AND t.assigned_agent_id IS NULL
          AND COALESCE(t.auto_reason,'') NOT IN
              ('tests-autonomy-remediation','tests-harness','autonomy-harness',
               'autonomous_idle_improvement')
        ORDER BY t.updated_at ASC
        LIMIT 20
    """))).fetchall()
    solicited = 0
    for row in rows:
        tid, title, desc, board_id = str(row[0]), row[1] or "", row[2] or "", str(row[3]) if row[3] else ""
        if not _is_static_re_task(title) or not _task_requires_real_tools(title, desc):
            continue
        # Already solicited?
        if await _count_comments_matching(db, tid, _CONSENSUS_SOLICIT_MARKER) > 0:
            continue
        # Pick distinct ONLINE agents from the RE pool, in priority order.
        picked: list[tuple[str, str]] = []  # (agent_id, name)
        seen_names: set[str] = set()
        for role in _RE_CONSENSUS_POOL:
            if len(picked) >= _CONSENSUS_FANOUT:
                break
            r = await db.execute(text("""
                SELECT id, name FROM agents
                WHERE name = :n AND status = 'online'
                  AND COALESCE(is_board_lead,false) = false
                LIMIT 1
            """), {"n": role})
            ar = r.first()
            if ar and ar[1] not in seen_names:
                picked.append((str(ar[0]), ar[1]))
                seen_names.add(ar[1])
        if len(picked) < _CONSENSUS_MIN_AGENTS:
            # Not enough RE agents online to form consensus — leave inbox,
            # log once, owner can bring agents online.
            logger.info(
                "watchdog.consensus_solicit.insufficient_agents task=%s online=%d need=%d",
                tid[:8], len(picked), _CONSENSUS_MIN_AGENTS,
            )
            continue
        names = ", ".join(f"@{n}" for _id, n in picked)
        protocol = (
            f"{_CONSENSUS_SOLICIT_MARKER} **Independent multi-agent analysis requested.**\n\n"
            f"Assigned analysts: {names}\n\n"
            f"**Protocol (static analysis only — DO NOT execute the binary):**\n"
            f"1. Use the remote MCP tools (ida-pro-mcp): decompile / disassemble / read pseudocode.\n"
            f"2. Independently derive the password / result from the logic. Do not coordinate.\n"
            f"3. Post exactly one line `Password: <value>` (or `Result: <value>`), then your reasoning "
            f"(key function, comparison, transform).\n"
            f"4. Confirmation = ≥{_CONSENSUS_MIN_AGENTS} analysts converge on the SAME value. The system "
            f"auto-marks the task passed on agreement — no execution needed.\n"
        )
        # Recheck the current lane/status before effects; owner conversion to
        # native mode after the candidate scan must not be overwritten.
        first_id = picked[0][0]
        t_obj = await _lock_legacy_task(db, tid, row[4], "inbox")
        if t_obj is None:
            continue
        assigned = await db.execute(text(
            "UPDATE tasks SET assigned_agent_id = :a, status='in_progress', updated_at=NOW() "
            "WHERE id = :t AND status='inbox' AND assigned_agent_id IS NULL "
            f"AND {_legacy_where()}"
        ), {"a": first_id, "t": tid})
        if not assigned.rowcount:
            await db.rollback()
            continue
        await db.refresh(t_obj)
        stamp = t_obj.updated_at  # Capture our assignment generation BEFORE protocol commits release its lock.
        if not await _post_comment(db, tid, board_id, protocol):
            continue
        await db.commit()
        # Wake every analyst onto this task.
        try:
            from uuid import UUID as _UUID
            from app.models.agents import Agent
            from app.models.boards import Board
            from app.api.tasks import notify_agent_on_task_assign
            b_obj = await db.get(Board, _UUID(board_id)) if board_id else None
            for aid, _name in picked:
                t_obj = await _lock_legacy_task(db, tid, stamp, "in_progress")
                if t_obj is None:
                    break  # Preserve original solicitation authority across committed wakes.
                a_obj = await db.get(Agent, _UUID(aid))
                if a_obj and b_obj and t_obj:
                    await notify_agent_on_task_assign(session=db, board=b_obj, task=t_obj, agent=a_obj)
        except Exception as exc:
            logger.warning("watchdog.consensus_solicit.wake_failed task=%s err=%s", tid[:8], exc)
        solicited += 1
        logger.info(
            "watchdog.consensus_solicit task=%s analysts=%s",
            tid[:8], seen_names,
        )
    return solicited


async def _auto_emit_consensus_verdict(db) -> int:
    """T84 — when multiple agents converge on the same artifact / completion
    signal on a `review` task, auto-emit the `[qa-verdict] result=pass` (and
    `[pm-signoff] verdict=accept` for L4+ tasks whose complexity was raised
    mid-flight) so the existing `_auto_close_review_tasks` step can close
    it. Increases autonomy: no owner intervention needed for tasks where the
    crowd of agents already agrees.

    Gate (all must hold):
      * Task is in `review`.
      * No `[qa-verdict]` marker yet on the task.
      * ≥2 DISTINCT agents posted comments containing a completion signal.
      * At minimum, ≥2 agents either share an extracted artifact (password,
        flag, key) OR both posted `[ready-to-close]` / `FINAL VERDICT`.

    Auto-emitted comments carry `[auto-consensus]` tag so they're visibly
    different from agent-authored verdicts.
    """
    try:
        # T108: also consider `in_progress` tasks that were solicited for
        # consensus (RE/tool tasks confirmed by independent static analysis,
        # not execution). On agreement they're moved to `review` below so
        # the standard close path finishes them.
        rows = (await db.execute(text(f"""
            SELECT DISTINCT t.id, t.title, t.board_id, t.complexity, t.status, t.updated_at
            FROM tasks t
            WHERE {_legacy_where('t')}
              AND (t.status = 'review'
               OR (t.status = 'in_progress' AND EXISTS (
                     SELECT 1 FROM activity_events ae
                     WHERE ae.task_id = t.id
                       AND ae.event_type = 'task.comment'
                       AND ae.message LIKE '%[consensus-solicit]%'
                   )))
        """))).fetchall()
    except Exception as exc:
        logger.warning("auto_consensus_verdict.list_failed err=%s", exc)
        return 0

    emitted = 0
    for row in rows:
        tid = str(row[0])
        title = row[1] or ""
        bid = str(row[2]) if row[2] else ""
        level = int(row[3] or 0)
        task_status = row[4] if len(row) > 4 else "review"
        task_emitted = 0

        try:
            if await _lock_legacy_task(db, tid, row[5], task_status) is None:
                continue  # current human/native decision supersedes the scan
            markers = await _task_markers(db, tid)
            has_qa = _latest_marker(markers, "qa-verdict") is not None
            has_signoff = _latest_marker(markers, "pm-signoff") is not None
            # If qa-verdict exists but pm-signoff is missing for an L4+
            # task that has consensus, fall through to the pm-signoff
            # auto-emit below — we still need to unblock the close gate.
            if has_qa and (level < 4 or has_signoff):
                continue  # nothing more to do

            # Pull every comment on this task with agent_id; group by agent.
            comments = (await db.execute(text("""
                SELECT agent_id, message, created_at
                FROM activity_events
                WHERE task_id = :tid
                  AND event_type = 'task.comment'
                  AND agent_id IS NOT NULL
                ORDER BY created_at ASC
            """), {"tid": tid})).fetchall()
            if not comments:
                continue

            agents_with_signal: set[str] = set()
            artifacts_by_agent: dict[str, set[str]] = {}
            ready_or_verdict_agents: set[str] = set()

            for aid, msg, _ts in comments:
                aid_s = str(aid)
                msg_s = msg or ""
                if not _COMPLETION_RE.search(msg_s):
                    continue
                agents_with_signal.add(aid_s)
                art = _extract_completion_artifact(msg_s)
                if art:
                    artifacts_by_agent.setdefault(aid_s, set()).add(art)
                if "[ready-to-close]" in msg_s.lower() or "final verdict" in msg_s.lower():
                    ready_or_verdict_agents.add(aid_s)

            if len(agents_with_signal) < 2:
                continue  # need ≥2 distinct agents to consider consensus

            # Shared-artifact consensus: any artifact appears in ≥2 agents.
            artifact_consensus: str | None = None
            counter: dict[str, int] = {}
            for arts in artifacts_by_agent.values():
                for a in arts:
                    counter[a] = counter.get(a, 0) + 1
            for a, n in counter.items():
                if n >= 2:
                    artifact_consensus = a
                    break

            # Or "≥2 agents both flagged ready-to-close / FINAL VERDICT".
            ready_consensus = len(ready_or_verdict_agents) >= 2

            if not (artifact_consensus or ready_consensus):
                continue

            reason = (
                f"artifact={artifact_consensus}" if artifact_consensus
                else "≥2 agents posted ready-to-close / FINAL VERDICT"
            )

            # T108: a solicited in_progress task that just hit consensus must
            # move to `review` first so `_auto_close_review_tasks` can finish it.
            if task_status == "in_progress":
                await db.execute(text(
                    "UPDATE tasks SET status='review', updated_at=NOW() "
                    f"WHERE {_legacy_where()} AND id=:t AND status='in_progress'"
                ), {"t": tid})
                logger.info("auto_consensus_verdict.moved_to_review task=%s (solicited consensus)", tid[:8])

            if not has_qa:
                await _post_comment(
                    db, tid, bid,
                    f"[qa-verdict] result=pass confidence=90 [auto-consensus] reason=\"{reason}\"\n"
                    f"[watchdog-nudge] Auto-emitted `[qa-verdict] result=pass` because "
                    f"{len(agents_with_signal)} distinct agents converged on a completion signal. "
                    f"This unblocks the L{level} auto-close path.",
                    commit=False,
                )
                task_emitted += 1
                logger.info(
                    "auto_consensus_verdict.emitted task=%s level=%d agents=%d reason=%s",
                    tid, level, len(agents_with_signal), reason,
                )

            # For L4+ tasks: when consensus exists, also auto-emit the
            # pm-signoff. Rationale: the WHOLE point of consensus is "≥2
            # independent agents agree this is done"; a PM signoff at that
            # point is redundant ceremony. The task can be stuck forever
            # otherwise because the PM lock may never be claimed (or the
            # complexity was raised mid-flight + no PM was ever assigned).
            if level >= 4 and not has_signoff:
                await _post_comment(
                    db, tid, bid,
                    f"[pm-signoff] verdict=accept [auto-consensus]\n"
                    f"[watchdog-nudge] L{level} pm-signoff auto-accepted because "
                    f"{len(agents_with_signal)} distinct agents converged on the "
                    f"same completion artifact. PM ceremony bypassed — the "
                    f"agent quorum IS the verdict.",
                    commit=False,
                )
                task_emitted += 1
                logger.info(
                    "auto_consensus_verdict.pm_signoff_emitted task=%s level=%d",
                    tid, level,
                )
            await db.commit()  # promotion and all synthesized markers together
            emitted += task_emitted
            if not has_qa:
                # Separate SQLite KB is best-effort AFTER committed evidence,
                # not falsely advertised as atomic with PostgreSQL.
                try:
                    await _seed_kb_from_consensus(db, tid, title, reason, level, artifact_consensus)
                except Exception as exc:
                    logger.debug("kb_seed.failed task=%s err=%s", tid, exc)
        except Exception as exc:
            logger.warning("auto_consensus_verdict.task_failed task=%s err=%s", tid, exc)
            try:
                await db.rollback()
            except Exception:
                pass
        finally:
            await db.rollback()  # skipped candidates must not retain row locks

    return emitted


async def _auto_close_review_tasks(db) -> int:
    """Auto-close review tasks whose highest-confidence approval >= threshold.

    Confidence (>= 80%) is the baseline gate. Per-level extra gates (STATE.md §4):
      L1: never reaches review path (workers self-close).
      L2/L3: needs `[ready-to-close]` marker.
      L4/L5: needs `[ready-to-close]` AND `[pm-signoff] verdict=accept`.
    A `[pm-signoff] verdict=reject` rebounds the task to `in_progress` instead.

    2026-10-05: while owner-authorized autonomous acceptance is ENABLED this
    path stands down — a confidence number is not acceptance evidence, and
    the delegated Done decision belongs to the dedicated read-only reviewer
    (POST /api/v1/review/decisions) or a human.
    """
    try:
        from app.services.owner_reviews import automation_enabled

        # Two confidence sources accepted: an `approvals` row OR a
        # `[qa-verdict] result=pass confidence=N` comment. Either ≥ threshold.
        result = await db.execute(text(f"""
            SELECT DISTINCT t.id, t.title, t.board_id, t.complexity, b.organization_id, t.updated_at
            FROM tasks t
            JOIN boards b ON b.id = t.board_id
            WHERE t.status = 'review'
              AND {_legacy_where('t')}
              AND (
                EXISTS (
                  SELECT 1 FROM approvals a
                  WHERE a.task_id = t.id
                    AND a.status IN ('pending','approved')
                    AND a.confidence >= :threshold
                    -- Only this work round: a recurring task kept being closed
                    -- by one approval from its first cycle, months earlier.
                    AND a.created_at >= COALESCE(t.previous_in_progress_at, '-infinity'::timestamp)
                )
                OR EXISTS (
                  SELECT 1 FROM activity_events ae
                  WHERE ae.task_id = t.id AND ae.event_type = 'task.comment'
                    AND ae.message ILIKE '%[qa-verdict]%result=pass%'
                )
              )
        """), {"threshold": _AUTO_CLOSE_CONFIDENCE_THRESHOLD})
        rows = result.fetchall()
        if not rows:
            return 0

        closed = 0
        for row in rows:
            if automation_enabled(row[4]):
                continue
            tid_s = str(row[0])
            title = row[1] or "unknown"
            board_id_s = str(row[2]) if row[2] else ""
            level = int(row[3]) if row[3] is not None else 0
            try:
                # Double-check not already moved (race with lead/agent)
                check = await db.execute(
                    text("SELECT status FROM tasks WHERE id = :tid"),
                    {"tid": tid_s},
                )
                check_row = check.first()
                if not check_row or check_row[0] != "review":
                    continue

                # Per-level marker gate.
                markers = await _task_markers(db, tid_s)
                if await _lock_legacy_task(db, tid_s, row[5], "review") is None:
                    continue
                ready = _latest_marker(markers, "ready-to-close") is not None
                signoff = _latest_marker(markers, "pm-signoff")

                # If the only confidence source is a `[qa-verdict]` marker,
                # parse it and verify ≥ threshold here.
                qa_verdict = _latest_marker(markers, "qa-verdict")
                # Check for an ALREADY-APPROVED high-confidence approval —
                # this is a stronger signal than markers and should bypass
                # the per-level marker gates.
                approved_approval = await db.execute(text("""
                    SELECT 1 FROM approvals a JOIN tasks t ON t.id = a.task_id
                    WHERE a.task_id = :t
                      AND a.status = 'approved'
                      AND a.confidence >= :thr
                      AND a.created_at >= COALESCE(t.previous_in_progress_at, '-infinity'::timestamp)
                    LIMIT 1
                """), {"t": tid_s, "thr": _AUTO_CLOSE_CONFIDENCE_THRESHOLD})
                has_approved_approval = approved_approval.first() is not None
                # Also check for any approval at all (pending or approved)
                approval_check = await db.execute(text("""
                    SELECT 1 FROM approvals a JOIN tasks t ON t.id = a.task_id
                    WHERE a.task_id = :t AND a.status IN ('pending','approved')
                      AND a.confidence >= :thr
                      AND a.created_at >= COALESCE(t.previous_in_progress_at, '-infinity'::timestamp)
                    LIMIT 1
                """), {"t": tid_s, "thr": _AUTO_CLOSE_CONFIDENCE_THRESHOLD})
                has_approval = approval_check.first() is not None
                if not has_approval:
                    if not qa_verdict or qa_verdict.get("kv", {}).get("result") != "pass":
                        continue
                    try:
                        qa_conf = float(qa_verdict.get("kv", {}).get("confidence", "0"))
                    except (TypeError, ValueError):
                        qa_conf = 0.0
                    if qa_conf < _AUTO_CLOSE_CONFIDENCE_THRESHOLD:
                        continue

                # Reject takes precedence — bounce back to in_progress.
                if signoff and signoff.get("kv", {}).get("verdict") == "reject":
                    await db.execute(text(f"""
                        UPDATE tasks SET status='in_progress', updated_at=NOW()
                        WHERE {_legacy_where()} AND id = :tid AND status='review'
                    """), {"tid": tid_s})
                    notes = signoff.get("kv", {}).get("notes", "")
                    await _post_comment(
                        db, tid_s, board_id_s,
                        f"{_NUDGE_TAG} **PM rejected sign-off** — task returned to "
                        f"`in_progress`. {f'Reason: {notes}' if notes else ''}".strip(),
                        commit=False,
                    )
                    await db.commit()
                    continue

                # Confidence already verified by SQL filter; check level gates.
                # If there's an APPROVED high-confidence approval, bypass the
                # marker gates — the approval IS the verdict signal.
                if not has_approved_approval:
                    if level >= 2 and not ready:
                        continue  # waiting on QA / last reviewer marker
                    if level >= 4 and not (signoff and signoff.get("kv", {}).get("verdict") == "accept"):
                        continue  # waiting on PM sign-off

                changed = await db.execute(
                    text(f"""
                        UPDATE tasks SET status='done', updated_at=NOW()
                        WHERE {_legacy_where()} AND id = :tid AND status='review'
                    """),
                    {"tid": tid_s},
                )
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                # Auto-resolve any pending approvals that triggered the close.
                await db.execute(
                    text("""
                        UPDATE approvals
                        SET status='approved', resolved_at=NOW()
                        WHERE task_id = :tid
                          AND status='pending'
                          AND confidence >= :threshold
                    """),
                    {"tid": tid_s, "threshold": _AUTO_CLOSE_CONFIDENCE_THRESHOLD},
                )
                confidence_val = await db.execute(
                    text("""
                        SELECT confidence FROM approvals
                        WHERE task_id = :tid
                          AND confidence >= :threshold
                        ORDER BY confidence DESC LIMIT 1
                    """),
                    {"tid": tid_s, "threshold": _AUTO_CLOSE_CONFIDENCE_THRESHOLD},
                )
                conf_row = confidence_val.first()
                conf_display = round(conf_row[0], 1) if conf_row else ""
                await _post_comment(
                    db, tid_s, board_id_s,
                    f"{_NUDGE_TAG} **Auto-closed to `done`** — approved approval "
                    f"with confidence {conf_display}% met threshold "
                    f"({_AUTO_CLOSE_CONFIDENCE_THRESHOLD}%). "
                    f"Per POLICY: high-confidence approved review → automatic done.",
                    commit=False,
                )
                await db.commit()
                closed += 1
                logger.info(
                    "watchdog.auto_closed task=%s title=%s confidence=%s",
                    tid_s[:8], title[:40], conf_display,
                )
            except Exception as e:
                await db.rollback()
                logger.warning(
                    "watchdog.auto_close_failed task=%s err=%s", tid_s, e
                )
            finally:
                await db.rollback()
        return closed
    except Exception as e:
        logger.warning("watchdog.auto_close_failed_all err=%s", e)
        return 0


_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_TOKEN_BURN_MARKERS = (
    "token-burning",
    "token-burn",
    "**Standing down**",
    "TOKEN-BURNING",
)


_HEARTBEAT_RATE_WINDOW_SECONDS = int(
    get_setting("WATCHDOG_HEARTBEAT_RATE_WINDOW_SECONDS"),
)
# T54c (2026-05-14): bumped from 2 → 12. proxy_vllm now throttles identical
# heartbeat polls to ~1/min upstream — a healthy agent will still record up
# to 5 heartbeat flows per 300 s window through unrelated content drift.
# Threshold of 2 was killing every heartbeating agent's session every cycle,
# breaking task delivery. 12 catches genuinely runaway poll loops (>2/min
# sustained) without false-positives on normal operation.
_HEARTBEAT_RATE_MAX_CALLS = int(
    get_setting("WATCHDOG_HEARTBEAT_RATE_MAX_CALLS"),
)

_AGENT_SESSION_ABORT_AGE_SECONDS = int(
    os.environ.get("WATCHDOG_AGENT_SESSION_ABORT_SECONDS", "900"),
)
_AGENT_SESSION_IDLE_THRESHOLD_SECONDS = int(
    get_setting("WATCHDOG_AGENT_SESSION_IDLE_SECONDS"),
)


# T104 (2026-05-24): openclaw owns the per-session truth at
# `$OPENCLAW_HOME/agents/<agent-dir>/sessions/sessions.json`. MC's
# `agents.status` is a CACHE — when openclaw marks a session
# `timeout/failed`, MC kept showing `online` and routed work to a wedged
# agent. Owner saw Atlas's review-task escalation never happen because
# Atlas's session had `status:"timeout"` for 50+ min, but MC said online.
#
# This reconciler reads each `sessions.json`, looks up the openclaw
# `status`, and forces MC's DB to match:
#   openclaw `timeout`/`failed`/`error` + MC `online` → MC `offline` +
#     clear `openclaw_session_id` so next assign provisions a fresh one
#   openclaw `done`/`running` + MC `offline` → MC `online` (ready)
#
# Reading `sessions.json` is cheap (single JSON parse, ~300KB max).
# No write to openclaw files.
_OPENCLAW_DEAD_STATES = {"timeout", "failed", "error", "aborted"}
_OPENCLAW_READY_STATES = {"done", "idle"}


def _read_openclaw_session_status(session_key: str) -> str | None:
    """Walk every agent dir's `sessions.json` and return the status for `session_key`.
    Returns None if not found or any error."""
    if not session_key:
        return None
    import json as _json
    from pathlib import Path
    agents_dir = Path(_OPENCLAW_HOME) / "agents"
    if not agents_dir.exists():
        return None
    # Scan only dirs whose name appears in the session_key (fast pre-filter).
    # session_key examples:
    #   agent:lead-0000a01c-...:main → dir = "lead-0000a01c-..."
    #   agent:mc-0000a010-...:main   → dir = "mc-0000a010-..."
    parts = session_key.split(":")
    if len(parts) < 2:
        return None
    agent_dir_name = parts[1]  # "lead-..." or "mc-..."
    sj = agents_dir / agent_dir_name / "sessions" / "sessions.json"
    if not sj.exists():
        return None
    try:
        data = _json.loads(sj.read_text())
        entry = data.get(session_key)
        if isinstance(entry, dict):
            st = entry.get("status")
            if isinstance(st, str):
                return st.lower()
    except Exception:
        return None
    return None


def _is_probeable_local_url(url: str) -> bool:
    """Patch 1.2 (2026-07-16): the autorecover step may only probe/flip
    LOCAL providers. The old heuristic ("no api_key = free/local") also
    matched CLOUD endpoints that need no stored key (observed: `kilocode`
    at api.kilo.ai kept getting flipped online against the owner's
    all-cloud-off intent). A provider is probeable only when its base_url
    host is loopback, RFC-1918 private, or explicitly allowlisted via
    WATCHDOG_AUTORECOVER_EXTRA_HOSTS (comma-separated hostnames).
    Everything else stays owner-managed, same as paid providers.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    extra = {
        h.strip().lower()
        for h in os.environ.get("WATCHDOG_AUTORECOVER_EXTRA_HOSTS", "").split(",")
        if h.strip()
    }
    if host in extra:
        return True
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    try:
        if ipaddress.ip_address(host).is_private:
            return True
    except ValueError:
        pass
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    return False


def _classify_models_probe(
    status_code: int | None,
    body_text: str | None,
    exc: Exception | None,
) -> tuple[bool, str | None]:
    """Patch 1 (2026-07-16): typed classification of a `<base_url>/models`
    probe result → (alive, typed_error). Alive semantics are UNCHANGED from
    the original probe (reachable with HTTP < 500 counts as alive); this
    only makes `last_error` actionable instead of one generic string.
    typed_error is None only for a fully-healthy response.
    NOTE: a per-provider expected-model check is not possible yet — the
    `provider_capacity` row has no expected-model column; `empty-model-list`
    (server up, nothing loaded) is the closest derivable signal.
    """
    if exc is not None:
        return False, f"conn-fail: {type(exc).__name__}"
    if status_code is None:
        return False, "conn-fail: no response"
    if status_code >= 500:
        return False, f"http-{status_code}"
    if status_code in (401, 403):
        return True, f"auth-{status_code} (unexpected on a free/local provider)"
    if status_code == 429:
        return True, "quota-429 (rate limited)"
    if status_code != 200:
        return True, f"http-{status_code}"
    try:
        payload = json.loads(body_text or "")
        models = payload.get("data") or []
    except Exception:
        return True, "malformed-response (/models returned non-JSON)"
    if not models:
        return True, "empty-model-list (server up, no model loaded)"
    return True, None


async def _sync_vllm_omniroute_combo() -> int:
    """Keep OmniRoute's `omniroute-auto` combo aligned with the model vllm
    CURRENTLY serves (resolved live from vllm `/v1/models`). The combo pins a
    concrete `<provider>/<model>`; on a model swap it goes stale and
    `omniroute-auto` 404s (breaking gateway agents + the tier degraded
    fallback). Updates via the OmniRoute mgmt API (self-minted JWT) only when
    drifted. Returns 1 if it updated the combo, else 0."""
    import httpx

    from app.services.traffic_etl import _MGMT_URL, _mgmt_token
    from app.services.vllm_model import current_vllm_context_length, current_vllm_model

    live = current_vllm_model()
    if not live:
        return 0
    live_ctx = current_vllm_context_length()
    # OPERATIONAL cap: the gateway sizes agent compaction from the combo's
    # context_length. Default to the current Jetson vLLM max_model_len; tune via
    # VLLM_OPERATIONAL_CONTEXT if prefill latency becomes the bottleneck.
    _op_cap = int(os.environ.get("VLLM_OPERATIONAL_CONTEXT", "262144"))
    if live_ctx:
        live_ctx = min(live_ctx, _op_cap)
    elif _op_cap:
        live_ctx = _op_cap
    try:
        # Cookie-only (NO Origin/Referer): OmniRoute's WRITE endpoints reject the
        # localhost-rewritten Origin that `_mgmt_headers()` adds (403
        # INVALID_ORIGIN) — that origin is only fine for the GET/read path.
        headers = {"Cookie": f"auth_token={_mgmt_token()}"}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{_MGMT_URL}/api/combos", headers=headers)
            if r.status_code != 200:
                return 0
            combo = next(
                (c for c in (r.json().get("combos") or []) if c.get("name") == "omniroute-auto"),
                None,
            )
            if not combo or not combo.get("id"):
                return 0
            changed = False
            for m in combo.get("models") or []:
                mstr = m.get("model")
                if isinstance(mstr, str) and "/" in mstr:
                    prov, _, name = mstr.partition("/")
                    # MULTI-PROVIDER (2026-08-25): only rewrite the vllm member to
                    # the live vllm model. Other providers in the combo (e.g. the
                    # llamacpp load-split member) serve a DIFFERENT model name and
                    # must NOT be clobbered with vllm's. Identify the vllm member
                    # by its label or its provider-node id prefix.
                    _is_vllm_member = (
                        (m.get("label") or "").lower() == "vllm"
                        or prov.startswith("openai-compatible-chat-0c37650d")
                    )
                    if _is_vllm_member and name != live:
                        m["model"] = f"{prov}/{live}"
                        changed = True
            # Keep the combo's advertised context window = vllm's real
            # max_model_len, so the gateway compacts agent contexts to fit the
            # CURRENT model (a smaller-window swap otherwise 400s / slow-504s).
            if live_ctx and combo.get("context_length") != live_ctx:
                combo["context_length"] = live_ctx
                changed = True
            # Membership, strategy and capacity belong to OmniRoute; only track
            # the Jetson's live model/context here, without reverting dashboard edits.
            # THE gateway reads its compaction budget from openclaw.json
            # `contextWindow`, NOT the OR combo — sync it ALWAYS (even when the
            # combo is unchanged), to the same capped `live_ctx`. Idempotent
            # (no write → no gateway reload if already correct). Without this
            # the operational cap never reaches the gateway and agents send
            # oversized contexts → 504/429 storm.
            oj_changed = False
            if live_ctx:
                try:
                    from app.services.omniroute_combo_sync import (
                        _sync_openclaw_json_context_window,
                    )
                    oj_changed = _sync_openclaw_json_context_window(live_ctx)
                except Exception as e:  # noqa: BLE001
                    logger.debug("watchdog.openclaw_ctx_sync_err err=%s", e)
            if not changed:
                if oj_changed:
                    logger.info("watchdog.omniroute_gateway_ctx_synced ctx=%s", live_ctx)
                    return 1
                return 0
            pr = await client.put(
                f"{_MGMT_URL}/api/combos/{combo['id']}", headers=headers, json=combo
            )
            if pr.status_code == 200:
                logger.info("watchdog.omniroute_combo_synced model=%s ctx=%s", live, live_ctx)
                return 1
            logger.warning(
                "watchdog.omniroute_combo_sync_http status=%d body=%s",
                pr.status_code, pr.text[:120],
            )
    except Exception as e:  # noqa: BLE001 — best-effort self-heal
        logger.debug("watchdog.omniroute_combo_sync_err err=%s", e)
    return 0


async def _gate_failing_providers() -> list[str]:
    """T111 (2026-09-16): flip providers with catastrophic recent error
    rates offline + mirror isActive=false into OmniRoute (combo stops
    routing to them). Ground truth = flows sqlite, not probes: a broken
    model server can still answer GET /models with 200 while every
    completion fails (observed: llamacpp 2343x 429 semaphore timeouts in
    7d while `online`). Cooldown keeps _autorecover_providers from
    flapping it back for PROVIDER_GATE_COOLDOWN_SECONDS. Returns the
    list of providers gated this tick."""
    from app.services import provider_failure_gate

    from sqlalchemy import select as sa_select

    from app.models.provider_capacity import ProviderCapacity

    rates = provider_failure_gate.provider_failure_rates()
    if not rates:
        return []
    gated: list[str] = []
    try:
        async with async_session_maker() as db:
            rows = (
                await db.execute(sa_select(ProviderCapacity))
            ).scalars().all()
            by_name = {p.provider_name: p for p in rows}
            for name, p in by_name.items():
                if p.status != "online":
                    continue
                if provider_failure_gate.in_gate_cooldown(name):
                    continue
                should, reason = provider_failure_gate.gate_decision(name, rates)
                if not should:
                    continue
                logger.warning(
                    "provider_gate.flip provider=%s offline reason=%s", name, reason,
                )
                p.status = "offline"
                p.last_error = reason
                gated.append(name)
                provider_failure_gate.mark_gated(name)
            if gated:
                await db.commit()
    except Exception as exc:
        logger.warning("provider_gate.db_failed err=%s", exc)
        return gated

    # Mirror offline state into OmniRoute connections so the round-robin
    # combo stops admitting requests to the gated provider. Reuses the
    # same forward-sync the /providers UI mutations use.
    if gated:
        try:
            from app.api.provider_capacity import _omniroute_sync_provider_upsert
            for name in gated:
                p = by_name.get(name)
                if p is None:
                    continue
                await _omniroute_sync_provider_upsert(
                    name, p.base_url, p.api_key, "offline",
                    max_concurrent=p.max_concurrent, priority=p.priority,
                )
        except Exception as exc:
            # Sync failure is non-fatal: MC-side offline already stops
            # routing; OR combo may retry the member until next tick.
            logger.warning("provider_gate.or_sync_failed err=%s", exc)
    return gated


async def _autorecover_providers() -> int:
    """Set each FREE provider's `provider_capacity.status` to match ACTUAL
    reachability (probe `<base_url>/models`). Auto-recovers vllm when the host's
    network/route changes — e.g. an office LAN and a home VPN both reach the local
    inference host, but a switch can
    briefly drop it offline; without this it stays stuck offline and routing
    dumps to the lead. Paid providers (api_key set) stay owner-managed. Runs
    each main tick (~60s). Returns count of status flips.

    Patch 1 (2026-07-16): probe results are now classified into typed
    `last_error` states (conn-fail / http-<code> / auth / quota / malformed /
    empty-model-list) instead of one generic string. Online/offline semantics
    and paid-provider handling are unchanged.
    """
    import httpx
    from sqlalchemy import select as sa_select

    from app.models.provider_capacity import ProviderCapacity

    async def _probe(url: str) -> tuple[bool, str | None]:
        result: tuple[bool, str | None] = (False, "conn-fail: no attempt")
        for _ in range(2):  # 2 attempts — tolerate a cold VPN/route resync
            try:
                async with httpx.AsyncClient(timeout=6.0) as c:
                    r = await c.get(url)
                result = _classify_models_probe(r.status_code, r.text, None)
            except Exception as probe_exc:
                result = _classify_models_probe(None, None, probe_exc)
            alive, _ = result
            if alive:
                return result
        return result

    flips = 0
    try:
        async with async_session_maker() as db:
            rows = (await db.execute(sa_select(ProviderCapacity))).scalars().all()
            for p in rows:
                if p.status == "disabled" or not p.base_url or p.api_key:
                    continue  # owner-disabled, unconfigured, and paid providers are not auto-recovered
                if not _is_probeable_local_url(p.base_url):
                    # Patch 1.2: keyless CLOUD endpoints (e.g. kilocode) are
                    # owner-managed too — never auto-flip them.
                    continue
                # T111: providers gated offline by traffic errors stay
                # offline during the gate cooldown even if /models probes
                # fine (probe-pass ≠ completion-works).
                try:
                    from app.services import provider_failure_gate
                    if p.status == "offline" and provider_failure_gate.in_gate_cooldown(p.provider_name):
                        continue
                except Exception:
                    pass
                alive, typed_error = await _probe(p.base_url.rstrip("/") + "/models")
                new_status = "online" if alive else "offline"
                if p.status != new_status:
                    logger.info(
                        "provider_autorecover.flip provider=%s %s->%s err=%s",
                        p.provider_name, p.status, new_status, typed_error,
                    )
                    p.status = new_status
                    p.last_error = typed_error
                    flips += 1
                elif alive and typed_error and p.last_error != typed_error:
                    # Online-stable but the probe found something actionable
                    # (quota/auth/empty list) — record the MORE specific state.
                    # Never overwrites offline-state errors set by the owner
                    # or benchmark loop (those providers don't reach here
                    # without a flip), and never clears an existing error
                    # with a generic one.
                    p.last_error = typed_error
            await db.commit()
    except Exception as exc:
        logger.warning("provider_autorecover.failed err=%s", exc)
    return flips


async def _flag_session_key_drift(
    db,
    *,
    agent_id,
    agent_name: str,
    session_key: str,
    is_board_lead: bool,
    board_id,
) -> int:
    """Report-only session-key audit (Patch 1, 2026-07-16). Emits a
    deduplicated `agent.session_key_drift` activity event when a stored
    `openclaw_session_id` is either gateway-unparseable (<3 colon parts —
    refused by openclaw >= v2026.6.11) or claims a canonical MC/lead form
    but disagrees with the `internal/session_keys.py` builders. NEVER
    rewrites the stored key (Patch 2 decision). Returns 1 when flagged.
    """
    from sqlalchemy import text

    from app.services.openclaw.internal.session_keys import (
        canonical_session_key_mismatch,
        is_gateway_parseable_session_key,
    )

    problem: str | None = None
    if not is_gateway_parseable_session_key(session_key):
        problem = (
            f"stored key {session_key!r} is not gateway-parseable "
            "(needs >=3 colon-separated parts)"
        )
    else:
        try:
            expected = canonical_session_key_mismatch(
                session_key,
                agent_id=agent_id,
                board_id=board_id,
                is_board_lead=is_board_lead,
            )
        except Exception:
            expected = None
        if expected is not None:
            problem = (
                f"stored key {session_key!r} drifts from canonical "
                f"builder output {expected!r}"
            )
    if problem is None:
        return 0
    # Dedup: one event per agent per 7 days keeps the signal observable
    # without flooding activity_events every 60s tick.
    dup = await db.execute(text(
        "SELECT 1 FROM activity_events "
        "WHERE event_type = 'agent.session_key_drift' "
        "  AND agent_id = :aid "
        "  AND created_at > NOW() - INTERVAL '7 days' "
        "LIMIT 1"
    ), {"aid": str(agent_id)})
    if dup.first():
        return 0
    await db.execute(text(
        "INSERT INTO activity_events (id, event_type, message, agent_id, created_at) "
        "VALUES (:id, 'agent.session_key_drift', :msg, :aid, NOW())"
    ), {
        "id": str(uuid.uuid4()),
        "msg": f"{agent_name}: {problem}",
        "aid": str(agent_id),
    })
    await db.commit()
    logger.warning(
        "watchdog.session_key_drift agent=%s %s", agent_name, problem,
    )
    return 1


async def _reconcile_openclaw_session_status(db) -> dict[str, int]:
    """Walk all non-retired agents with sessions, read their openclaw status,
    update `agents.status` to match. Returns counts."""
    from sqlalchemy import text
    stats = {"set_offline": 0, "set_online": 0, "checked": 0, "key_drift": 0}
    res = await db.execute(text(
        "SELECT id, name, status, openclaw_session_id, is_board_lead, board_id "
        "FROM agents "
        "WHERE openclaw_session_id IS NOT NULL "
        "  AND openclaw_session_id != '' "
        "  AND status NOT IN ('retired')"
    ))
    rows = res.fetchall()
    for r in rows:
        stats["checked"] += 1
        aid, name, mc_status, sess_key, is_lead, board_id = (
            r[0], r[1], r[2], r[3], r[4], r[5],
        )
        stats["key_drift"] += await _flag_session_key_drift(
            db,
            agent_id=aid,
            agent_name=name,
            session_key=sess_key,
            is_board_lead=bool(is_lead),
            board_id=board_id,
        )
        oc_status = _read_openclaw_session_status(sess_key)
        if oc_status is None:
            continue
        if oc_status in _OPENCLAW_DEAD_STATES and mc_status in ("online", "healthy"):
            # Force offline, but KEEP openclaw_session_id. The key is a
            # deterministic delivery address (agent:mc-<id>:main / agent:lead-
            # <board>:main); NULLing it makes `_notify_agent_on_task_assign`
            # silently early-return (`if not agent.openclaw_session_id: return`)
            # so the task is never delivered AND never re-provisions — the exact
            # delivery bug from 2026-07-02. A dead session is re-instantiated by
            # the wake/ensure_session path on the next assignment; the key must
            # survive for that to reach the agent.
            await db.execute(text(
                "UPDATE agents SET status='offline', updated_at = NOW() "
                "WHERE id = :aid"
            ), {"aid": str(aid)})
            await db.commit()
            stats["set_offline"] += 1
            logger.info(
                "watchdog.openclaw_reconcile.set_offline agent=%s mc=%s openclaw=%s lead=%s",
                name, mc_status, oc_status, is_lead,
            )
        elif oc_status in _OPENCLAW_READY_STATES and mc_status == "offline":
            await db.execute(text(
                "UPDATE agents SET status='online', last_seen_at = NOW(), "
                "  updated_at = NOW() WHERE id = :aid"
            ), {"aid": str(aid)})
            await db.commit()
            stats["set_online"] += 1
            logger.info(
                "watchdog.openclaw_reconcile.set_online agent=%s openclaw=%s",
                name, oc_status,
            )
        elif oc_status in _OPENCLAW_READY_STATES and mc_status in ("online", "healthy"):
            # T104 round-3b (2026-05-24): refresh last_seen_at when openclaw
            # confirms the session is healthy/idle. Without this, MC's
            # `_abort_stuck_sessions` aborts perfectly-healthy idle agents
            # on next tick because the openclaw heartbeat path is decoupled
            # from MC's `last_seen_at` (only updates on agent task interaction).
            # Owner saw 20 agents aborted in one tick post-reconcile because
            # MC's last_seen_at was 6+ hours stale while openclaw said `done`.
            await db.execute(text(
                "UPDATE agents SET last_seen_at = NOW() WHERE id = :aid "
                "  AND last_seen_at < NOW() - INTERVAL '300 seconds'"
            ), {"aid": str(aid)})
            await db.commit()
    return stats


async def _auto_reprovision_session_dropped() -> int:
    """T104 round-4 (2026-05-24) — auto-reprovision agents whose session was
    dropped by the openclaw-status reconciler (status=offline AND
    openclaw_session_id IS NULL) AND that are needed for an active task.

    Without this, the reconciler correctly detects a wedged session, but
    the agent stays offline indefinitely because session_id=NULL fails the
    `_recover_offline_agents` gate. Owner had to manually re-provision via
    `/agents` UI. Now the watchdog does it on demand — only when a real
    task needs this specific agent.

    Cap: 3 reprovisions per tick (heavy gateway round-trip; throttle).
    """
    from sqlalchemy import text as _t
    from app.db.session import async_session_maker
    from app.models.agents import Agent
    from app.models.boards import Board
    from app.services.openclaw.gateway_resolver import get_gateway_for_board
    from app.services.openclaw.provisioning_db import AgentLifecycleService
    from uuid import UUID as _UUID

    reprovisioned = 0
    async with async_session_maker() as db:
        res = await db.execute(_t("""
            SELECT DISTINCT a.id, a.name, a.board_id
            FROM agents a
            JOIN tasks t ON t.assigned_agent_id = a.id
            WHERE a.status = 'offline'
              AND (a.openclaw_session_id IS NULL OR a.openclaw_session_id = '')
              AND a.board_id IS NOT NULL
              AND t.status IN ('inbox', 'in_progress', 'review')
              AND a.last_seen_at > NOW() - INTERVAL '7 days'
            LIMIT 3
        """))
        rows = res.fetchall()
        if not rows:
            return 0
        for aid, name, bid in rows:
            try:
                agent = await db.get(Agent, _UUID(str(aid)))
                board = await db.get(Board, _UUID(str(bid))) if bid else None
                if not agent or not board:
                    continue
                gateway = await get_gateway_for_board(db, board)
                if gateway is None:
                    logger.debug(
                        "watchdog.auto_reprovision.no_gateway agent=%s", name,
                    )
                    continue
                svc = AgentLifecycleService(db)
                await svc.provision_new_agent(
                    agent=agent,
                    board=board,
                    gateway=gateway,
                    auth_token="",  # blank → run_lifecycle mints via mint_agent_token
                    user=None,      # board is set → user lookup falls back to org owner
                    force_bootstrap=False,
                )
                reprovisioned += 1
                logger.info(
                    "watchdog.auto_reprovision.success agent=%s board=%s",
                    name, str(bid)[:8],
                )
            except Exception as e:
                logger.warning(
                    "watchdog.auto_reprovision.failed agent=%s err=%s", name, e,
                )
    return reprovisioned


async def _recover_offline_agents() -> int:
    """Re-online agents offline > 30 min with valid sessions."""
    from sqlalchemy import text
    recovered = 0
    try:
        from app.db.session import async_session_maker
        async with async_session_maker() as db:
            result = await db.execute(text("""
                UPDATE agents
                SET status = 'online',
                    last_seen_at = NOW(),
                    updated_at = NOW()
                WHERE id IN (
                    SELECT id FROM agents
                    WHERE status = 'offline'
                      AND openclaw_session_id IS NOT NULL
                      AND status != 'retired'
                      AND (last_seen_at IS NULL
                           OR last_seen_at < NOW() - INTERVAL '15 minutes')
                    LIMIT 10
                )
                RETURNING id, name
            """))
            rows = result.fetchall()
            recovered = len(rows)
            if rows:
                logger.info(
                    "watchdog.offline_recovery recovered=%d agents=%s",
                    recovered,
                    ", ".join(row.name for row in rows),
                )
            await db.commit()
    except Exception as e:
        logger.warning("watchdog.offline_recovery.failed err=%s", e)
    return recovered


async def _abort_stuck_sessions(db) -> int:
    """Abort gateway sessions for agents stuck in processing loops.

    Criteria:
    1. Agent has an active gateway session (openclaw_session_id).
    2. Agent's last_seen_at > ABORT_AGE_SECONDS (e.g. 15 min) ago —
       indicates the gateway session is still alive but the agent isn't
       checking in (stuck in model_call or tool_call loop).
    3. Agent has zero active tasks (status in inbox/in_progress/review)
       assigned to it — no legitimate work to be doing.
    4. Agent is NOT a lead or PM (those have triage authority and may
       legitimately run long analyses).

    Action: calls gateway RPC ``chat.abort`` on the agent's session,
    then sets agent status to ``offline``.
    """
    # The production gateway rejects control RPC without a secure device
    # identity. Opt in only after that identity is provisioned; retrying every
    # watchdog tick creates noise and never recovers a session.
    if os.environ.get("WATCHDOG_GATEWAY_ABORT_ENABLED", "0") != "1":
        return 0
    try:
        # T103 v33 (2026-05-20): catch BOTH classic-idle (last_seen >
        # idle_threshold) AND runaway-loop (online + 0 open tasks but
        # constantly checking in via tool calls — those never trip the
        # idle threshold because last_seen_at updates each iteration).
        #
        # T103 v33 (2026-05-20): original two-pronged detection
        # T104 (2026-05-23): drop the `updated_at < 300s` clause (killed idle workers)
        # T104 round-3 (2026-05-24): drop `is_board_lead/is_project_manager = false`.
        # The exemption meant a board lead's wedged session could never be
        # recovered. Now: lead+PM also eligible BUT only when the openclaw
        # `sessions.json` actually reports a dead status — the reconciler
        # step (Step 0a7b) does the read and forces MC offline pre-empt,
        # so this query mostly only fires on the rare "idle 30 min,
        # openclaw says alive but no work" tail case.
        result = await db.execute(
            text(f"""
                SELECT a.id, a.name, a.openclaw_session_id,
                       a.board_id, a.status,
                       EXTRACT(EPOCH FROM NOW() - a.last_seen_at)::int AS idle_seconds,
                       a.is_board_lead, a.is_project_manager
                FROM agents a
                WHERE a.status IN ('online', 'healthy')
                  AND a.openclaw_session_id IS NOT NULL
                  AND a.last_seen_at > NOW() - INTERVAL '7 days'
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks t
                      WHERE t.board_id = a.board_id
                        AND t.assigned_agent_id = a.id
                        AND t.status IN ('inbox', 'in_progress', 'review')
                  )
                  AND a.last_seen_at < NOW() - INTERVAL '{_AGENT_SESSION_IDLE_THRESHOLD_SECONDS} seconds'
                ORDER BY a.last_seen_at ASC
                LIMIT 20
            """),
        )
        rows = result.fetchall()
        if not rows:
            return 0

        aborted = 0
        for aid_u, name, session_key, bid_u, status, idle_secs, _is_lead, _is_pm in rows:
            aid = str(aid_u)
            if not session_key:
                continue

            # Resolve gateway config for the agent's board
            try:
                from app.models.boards import Board
                import sqlmodel
                stmt = sqlmodel.select(Board).where(Board.id == bid_u)
                board_res = await db.exec(stmt)
                board_obj = board_res.first()
                if not board_obj:
                    # Board-less agent (e.g. Gateway Agent) — still abort the
                    # session and force offline to stop the spam loop.
                    logger.warning(
                        "watchdog.stuck_session.no_board agent=%s bid=%s — forcing offline",
                        name, bid_u,
                    )
                    await db.execute(
                        text("UPDATE agents SET status = 'offline', updated_at = NOW() WHERE id = :aid"),
                        {"aid": aid},
                    )
                    await db.commit()
                    aborted += 1
                    continue

                from app.services.openclaw.gateway_resolver import (
                    optional_gateway_client_config,
                    get_gateway_for_board,
                )
                gateway = await get_gateway_for_board(db, board_obj)
                config = optional_gateway_client_config(gateway)
                if not config:
                    continue

                from app.services.openclaw.gateway_rpc import abort_session
                await abort_session(session_key, config=config)
                logger.info(
                    "watchdog.stuck_session.aborted agent=%s session=%s idle=%ss",
                    name, session_key[:40], idle_secs,
                )

                # Set agent offline
                await db.execute(
                    text("UPDATE agents SET status = 'offline', updated_at = NOW() WHERE id = :aid"),
                    {"aid": aid},
                )
                await db.commit()
                aborted += 1
            except Exception as e:
                logger.warning(
                    "watchdog.stuck_session.abort_failed agent=%s err=%s",
                    name, e,
                )
        return aborted
    except Exception as e:
        logger.warning("watchdog.stuck_session.abort_all_failed err=%s", e)
        return 0


_TRAFFIC_DB_PATH = "/traffic-data/llm_flows.db"


async def _abort_rapid_heartbeat_sessions(db) -> int:
    """Abort gateway sessions making excessive heartbeat calls.

    If an agent makes >_HEARTBEAT_RATE_MAX_CALLS heartbeat calls in
    _HEARTBEAT_RATE_WINDOW_SECONDS, its session is stuck in rapid poll.
    Skips agents currently assigned to an active task (see T54c).
    """
    if os.environ.get("WATCHDOG_GATEWAY_ABORT_ENABLED", "0") != "1":
        return 0
    try:
        import sqlite3
        from pathlib import Path as _Path
        from app.models.boards import Board
        import sqlmodel
        from app.services.openclaw.gateway_resolver import (
            optional_gateway_client_config,
            get_gateway_for_board,
        )
        from app.services.openclaw.gateway_rpc import abort_session

        if not _Path(_TRAFFIC_DB_PATH).exists():
            return 0
        conn = sqlite3.connect(
            f"file:{_TRAFFIC_DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
        try:
            rows = conn.execute(
                """
                SELECT agent_name, COUNT(*) as n
                FROM flows
                WHERE ts > datetime('now', '-' || ? || ' seconds')
                  AND agent_name IS NOT NULL
                  AND last_user_message LIKE '%heartbeat%'
                GROUP BY agent_name
                HAVING n > ?
                """,
                (_HEARTBEAT_RATE_WINDOW_SECONDS, _HEARTBEAT_RATE_MAX_CALLS),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return 0
        aborted = 0
        for agent_name, count in rows:
            result = await db.execute(
                text(
                    "SELECT id, openclaw_session_id, board_id "
                    "FROM agents WHERE name = :name AND openclaw_session_id IS NOT NULL"
                ),
                {"name": agent_name},
            )
            agent_row = result.first()
            if not agent_row:
                continue
            aid = str(agent_row[0])
            session_key = agent_row[1]
            bid = str(agent_row[2])

            # T54c (2026-05-14): never abort an agent currently assigned to
            # an active task — killing the session strands the task with no
            # one to work it. Watchdog can rotate later if needed.
            active_task = await db.execute(
                text(
                    "SELECT id FROM tasks "
                    "WHERE assigned_agent_id = :aid "
                    "AND status IN ('inbox','in_progress','review') "
                    "LIMIT 1"
                ),
                {"aid": aid},
            )
            if active_task.first():
                logger.info(
                    "watchdog.rapid_heartbeat.skipped agent=%s count=%d/%ds reason=active_task",
                    agent_name, count, _HEARTBEAT_RATE_WINDOW_SECONDS,
                )
                continue
            try:
                stmt = sqlmodel.select(Board).where(Board.id == bid)
                board_res = await db.exec(stmt)
                board_obj = board_res.first()
                if not board_obj:
                    await db.execute(
                        text(
                            "UPDATE agents SET status='offline', "
                            "updated_at=NOW() WHERE id=:aid"
                        ),
                        {"aid": aid},
                    )
                    await db.commit()
                    aborted += 1
                    continue
                gateway = await get_gateway_for_board(db, board_obj)
                config = optional_gateway_client_config(gateway)
                if not config:
                    continue
                await abort_session(session_key, config=config)
                logger.info(
                    "watchdog.rapid_heartbeat.aborted agent=%s count=%d/%ds",
                    agent_name, count, _HEARTBEAT_RATE_WINDOW_SECONDS,
                )
                await db.execute(
                    text(
                        "UPDATE agents SET status='offline', "
                        "updated_at=NOW() WHERE id=:aid"
                    ),
                    {"aid": aid},
                )
                await db.commit()
                aborted += 1
            except Exception as e:
                logger.warning(
                    "watchdog.rapid_heartbeat.abort_failed agent=%s err=%s",
                    agent_name, e,
                )
        return aborted
    except Exception as e:
        logger.warning("watchdog.rapid_heartbeat.failed err=%s", e)
        return 0


def _detect_token_burn_agents() -> dict[str, int]:
    """Scan agent workspace memory files for token-burn markers.
    For each agent showing the pattern, extend their heartbeat to 2h in openclaw.json
    so the gateway stops waking them every 10min.
    Recovery: if an agent was throttled to 2h but its workspace memory has
    NO burn markers for > 6h, restore original heartbeat.
    Returns {"throttled": N, "recovered": N}.
    """
    cfg_path = _OPENCLAW_HOME / "openclaw.json"
    if not cfg_path.exists():
        return {"throttled": 0, "recovered": 0}
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        return {"throttled": 0, "recovered": 0}
    throttled = 0
    recovered = 0
    agents = cfg.get("agents", {}).get("list", [])
    for agent in agents:
        if targets_private_lead({"agentId": agent.get("id")}):
            continue
        ws = agent.get("workspace")
        if not ws:
            continue
        from pathlib import Path as _P

        parts = _P(ws).parts
        try:
            idx = parts.index(".openclaw")
            ws_path = _OPENCLAW_HOME / _P(*parts[idx + 1 :])
        except (ValueError, IndexError):
            continue
        mem_dir = ws_path / "memory"
        if not mem_dir.exists():
            continue
        try:
            mem_files = sorted(mem_dir.glob("*.md"), key=lambda f: f.stat().st_mtime)
        except Exception:
            continue
        if not mem_files:
            continue
        try:
            latest = mem_files[-1].read_text(errors="replace")
        except Exception:
            continue
        latest_lower = latest.lower()
        has_burn = any(m.lower() in latest_lower for m in _TOKEN_BURN_MARKERS[:2])

        hb = agent.get("heartbeat", {})
        every = (hb.get("every") or "").lower()
        # Strip any legacy `original_every` — gateway schema rejects it and rolls
        # back the entire openclaw.json to last-known-good (undoing cloud toggle,
        # task assignments, etc).
        if "original_every" in hb:
            del hb["original_every"]

        if has_burn:
            if every in ("2h", "4h", "8h", "1d"):
                continue  # already throttled
            agent.setdefault("heartbeat", {})["every"] = "2h"
            throttled += 1
            agent_name = agent.get("name") or ""
            logger.info(
                "watchdog.token_burn.throttled agent=%s old_hb=%s new_hb=2h",
                agent_name,
                every,
            )
            # T23b (2026-05-14): repeat-burn escalation — if this agent has
            # been throttled >=1 time already in the last 24h, file an Owner
            # Review item. Record this throttle as an activity_event so the
            # next watchdog tick can count history.
            try:
                from app.db.session import sync_session_maker
                from app.services.owner_reviews import insert_sync as _or_insert
                with sync_session_maker() as _sdb:
                    from sqlalchemy import text as _text
                    r = _sdb.execute(_text(
                        "SELECT COUNT(*) FROM activity_events "
                        "WHERE event_type = 'watchdog.token_burn_throttled' "
                        "AND message LIKE :pat "
                        "AND created_at > NOW() - INTERVAL '24 hours'"
                    ), {"pat": f"%agent={agent_name}%"})
                    prior_24h = r.scalar() or 0
                    _sdb.execute(_text(
                        "INSERT INTO activity_events (id, event_type, message, created_at) "
                        "VALUES (:id, 'watchdog.token_burn_throttled', :msg, NOW())"
                    ), {
                        "id": str(uuid.uuid4()),
                        "msg": f"agent={agent_name} old_hb={every} new_hb=2h",
                    })
                    _sdb.commit()
                if prior_24h >= 1:
                    _or_insert(
                        title=f"WATCHDOG: repeat token-burn — {agent_name[:60]}",
                        type_="other",
                        summary=(
                            f"Agent {agent_name} has been throttled for token-burn "
                            f"{prior_24h + 1} times in the past 24h. Heartbeat "
                            f"pinned to 2h. Owner: investigate root cause "
                            f"(infinite loop? bad prompt template? broken skill?)."
                        ),
                        reason="repeat-token-burn",
                        proposed_action=(
                            "Investigate agent workspace memory + recent task history. "
                            "Possibly retire the agent OR rewrite its identity_profile."
                        ),
                        submitted_by="Pipeline Watchdog (token-burn)",
                    )
                    logger.warning(
                        "watchdog.token_burn.repeat_escalated agent=%s prior_24h=%d",
                        agent_name, prior_24h,
                    )
            except Exception as exc:
                logger.warning(
                    "watchdog.token_burn.repeat_check_failed agent=%s err=%s",
                    agent_name, exc,
                )
        elif every == "2h":
            latest_mtime = mem_files[-1].stat().st_mtime
            age_seconds = time.time() - latest_mtime
            if age_seconds > 21600 and not has_burn:
                # Default restore — agent specifics not retained (gateway schema
                # forbids extra keys, so we can't stash original).
                restore = "30m"
                agent.setdefault("heartbeat", {})["every"] = restore
                recovered += 1
                logger.info(
                    "watchdog.token_burn.recovered agent=%s restored_hb=%s clean_hours=%d",
                    agent.get("name"),
                    restore,
                    age_seconds // 3600,
                )

    if throttled > 0 or recovered > 0:
        try:
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
            os.chmod(cfg_path, 0o666)
        except Exception as e:
            logger.warning("watchdog.token_burn.write_failed err=%s", e)
            return {"throttled": 0, "recovered": 0}
    return {"throttled": throttled, "recovered": recovered}


def _cron_matches(cron_expr: str) -> bool:
    """Check if a crontab expression matches the current time.

    T23f (2026-05-14): switched primary parser to `croniter` so we support
    `?`, `L`, `#`, `W`, `step/range/list` combinations + standard crontab
    nicknames (@hourly etc.). Falls back to the in-house parser if croniter
    rejects the expression (forward-compat with non-standard syntax).
    """
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        return False
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # croniter primary path
    try:
        from croniter import croniter as _ci
        if _ci.is_valid(cron_expr):
            it = _ci(cron_expr, now)
            # Step back one minute and compute next match; if it equals `now`
            # we are on a trigger boundary.
            from datetime import timedelta as _td
            prev_minute = now - _td(minutes=1)
            it2 = _ci(cron_expr, prev_minute)
            return it2.get_next(datetime) == now
    except Exception:
        pass
    # Legacy in-house parser fallback (preserved for non-standard inputs).
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    minute, hour, day, month, weekday = now.minute, now.hour, now.day, now.month, now.weekday()

    def _matches(field: str, value: int) -> bool:
        if field == "*":
            return True
        for segment in field.split(","):
            segment = segment.strip()
            if segment == str(value):
                return True
            if "/" in segment:
                base, step = segment.split("/")
                base = 0 if base == "*" else int(base)
                step = int(step)
                if (value - base) % step == 0:
                    return True
            if "-" in segment:
                lo, hi = segment.split("-")
                if int(lo) <= value <= int(hi):
                    return True
        return False

    return (
        _matches(parts[0], minute)
        and _matches(parts[1], hour)
        and _matches(parts[2], day)
        and _matches(parts[3], month)
        and _matches(parts[4], weekday)
    )


async def _process_cron_repeats() -> int:
    """Reset done cron-repeating tasks back to inbox when their schedule triggers."""
    from app.db.session import async_session_maker
    async with async_session_maker() as db:
        # Check board toggle
        br = await db.execute(text("SELECT id, cron_tasks_enabled FROM boards LIMIT 1"))
        b_row = br.first()
        if not b_row or not b_row[1]:
            return 0

        r = await db.execute(text("""
            SELECT id, title, description, repeat_cron, priority,
                   assigned_agent_id, board_id, updated_at
            FROM tasks
            WHERE status='done' AND repeat_enabled=true AND repeat_cron IS NOT NULL AND repeat_cron != ''
        """))
        tasks_to_repeat = r.fetchall()
        if not tasks_to_repeat:
            return 0

        repeated = 0
        for row in tasks_to_repeat:
            tid, title, desc, cron_expr, priority, aid, board_id, stamp = row
            if _cron_matches(cron_expr):
                task = await _lock_legacy_task(db, str(tid), stamp, "done")
                if task is None or not task.repeat_enabled:
                    continue
                _new_id = _make_uuid()
                await db.execute(text("""
                    INSERT INTO tasks (id, board_id, title, description, status, priority,
                                       assigned_agent_id, repeat_cron, repeat_enabled,
                                       auto_created, created_by_type, created_by_name, created_at, updated_at)
                    VALUES (:id, :bid, :title, :desc, 'inbox', :pri, :aid, :cron, true,
                            true, 'system', 'Cron Repeater', NOW(), NOW())
                """), {
                    "id": _new_id, "bid": str(board_id),
                    "title": title, "desc": desc or f"Repeated cron task: {title}",
                    "pri": priority or "medium", "aid": str(aid) if aid else None,
                    "cron": cron_expr,
                })
                repeated += 1
                await db.commit()  # keep earlier repeats if a later stale candidate rolls back

        if repeated:
            await db.commit()
        return repeated


async def _process_interval_repeats() -> int:
    """Reset done repeatable tasks back to inbox when interval elapses.

    Instead of cloning (which creates duplicate rows), we move the SAME task
    back to inbox. This preserves the task ID for history/comments and avoids
    the clone cascade problem.
    """
    from app.db.session import async_session_maker
    async with async_session_maker() as db:
        br = await db.execute(text("""
            SELECT id FROM boards WHERE cron_tasks_enabled = true LIMIT 1
        """))
        b_row = br.first()
        if not b_row:
            return 0

        r = await db.execute(text("""
            SELECT id, title, assigned_agent_id, board_id,
                   repeat_interval_minutes, updated_at, status
            FROM tasks
            -- Repeating tasks: `review`/`blocked` = a stuck cycle → re-fire at
            -- interval. `done` = the OWNER explicitly ended the recurrence
            -- A repeat-enabled task that reached `done` did so via an AUTO path
            -- (owner stop sets repeat_enabled=false, so it is filtered out here)
            -- → recycle it to inbox-cooldown IMMEDIATELY so it never lingers in
            -- `done`. `review`/`blocked` = a stuck cycle → recycle at interval.
            WHERE status IN ('done', 'review')
              -- Explicit Blocked/Cancelled are owner stops, not repeat retries.
              -- Native team success requeues itself; preserve review/credential stops.
              AND execution_mode != 'team'
              AND repeat_enabled = true
              AND repeat_interval_minutes IS NOT NULL
              AND repeat_interval_minutes > 0
        """))
        tasks_to_repeat = r.fetchall()
        logger.info("interval_repeats.candidates count=%d", len(tasks_to_repeat))
        if not tasks_to_repeat:
            return 0

        now = utcnow().replace(tzinfo=timezone.utc)
        repeated = 0
        for row in tasks_to_repeat:
            tid, title, aid, bid, interval_mins, upd_at, status_val = row
            if upd_at is None:
                continue
            task = await _lock_legacy_task(db, str(tid), upd_at, status_val)
            if task is None or not task.repeat_enabled:
                continue
            # AUTO cycle-end reached `done` (some auto path not covered by
            # `_settle_repeating_cycle`) → recycle to inbox-cooldown NOW so the
            # recurring task never sits in `done`. The `[cycle-complete]` marker
            # makes the router hold it for the interval before the next run.
            if status_val == "done":
                await db.execute(text(f"""
                    UPDATE tasks
                    SET status='inbox', assigned_agent_id=NULL, in_progress_at=NULL,
                        previous_in_progress_at=NULL, tokens_used=0,
                        budget_exhausted_at=NULL, updated_at=NOW()
                    WHERE {_legacy_where()} AND id = :tid
                """), {"tid": tid})
                await _post_comment(
                    db, str(tid), str(bid) if bid else "",
                    f"[cycle-complete] Recurring task cycle finished — next run in "
                    f"~{int(interval_mins)} min. (To stop: move to done, or comment "
                    f"that it's finished.)",
                )
                await db.commit()
                repeated += 1
                continue
            # PostgreSQL returns naive datetime without tzinfo. Make both aware.
            if upd_at.tzinfo is None:
                upd_at = upd_at.replace(tzinfo=timezone.utc)
            elapsed = (now - upd_at).total_seconds() / 60.0
            logger.info(
                "interval_repeats.reset task=%s elapsed=%.1f interval=%d status=%s",
                str(tid)[:8], elapsed, interval_mins, status_val,
            )
            if elapsed >= interval_mins:
                # Move the SAME task back to inbox — no cloning.
                # Clear assignee so auto-assignment picks a fresh agent.
                # This prevents stale assignments to offline/retired agents.
                # T63 (2026-05-15): also reset cycle-specific counters so the
                # budget bar starts fresh each cycle and a previous budget
                # exhaustion doesn't carry over.
                await db.execute(
                    text(f"""
                        UPDATE tasks
                        SET status = 'inbox',
                            assigned_agent_id = NULL,
                            in_progress_at = NULL,
                            previous_in_progress_at = NULL,
                            tokens_used = 0,
                            budget_exhausted_at = NULL,
                            updated_at = NOW()
                        WHERE {_legacy_where()} AND id = :tid
                    """),
                    {"tid": tid},
                )
                # T68 (2026-05-15): also record a stage_timings transition.
                # The watchdog bypasses the `update_task` API path that
                # normally calls `_record_stage_transition`, which meant
                # repeat-reset cycles never closed the previous `done` row
                # nor opened a fresh `inbox` row. The card's "Last cycle"
                # aggregation then summed every cycle since task creation
                # ("121m 7s" instead of the latest cycle's duration).
                await db.execute(
                    text("""
                        UPDATE task_stage_timings
                        SET exited_at = NOW(),
                            duration_seconds = GREATEST(
                                0,
                                EXTRACT(EPOCH FROM (NOW() - entered_at))::int
                            )
                        WHERE task_id = :tid AND exited_at IS NULL
                    """),
                    {"tid": tid},
                )
                await db.execute(
                    text("""
                        INSERT INTO task_stage_timings
                        (id, task_id, status, entered_at, created_at)
                        VALUES (:sid, :tid, 'inbox', NOW(), NOW())
                    """),
                    {"sid": _make_uuid(), "tid": tid},
                )
                # Post a canonical [reset-cycle] system comment that tells
                # the agent to ignore previous verdicts and start fresh.
                # Without this, agents see old [qa-verdict] markers and
                # assume the task is already done — skipping actual work.
                cycle_num = (elapsed // interval_mins) + 1
                reset_msg = (
                    f"**[repeat-reset cycle={cycle_num}]** Task '{title}' reset "
                    f"to inbox (interval={interval_mins}min). Previous cycle "
                    f"completed {int(elapsed)} minutes ago. **Start fresh** — "
                    f"ignore markers, comments, and verdicts from previous "
                    f"cycles. Treat this as a brand new task. Previous results "
                    f"are archived for reference only."
                )
                await db.execute(
                    text("""
                        INSERT INTO activity_events 
                        (id, event_type, agent_id, task_id, message, created_at)
                        VALUES (:id, :type, NULL, :tid, :msg, NOW())
                    """),
                    {
                        "id": _make_uuid(),
                        "type": "task.repeat_reset",
                        "tid": tid,
                        "msg": reset_msg,
                    },
                )
                repeated += 1
                await db.commit()  # task, timing and event form one durable repeat

        if repeated:
            await db.commit()
        return repeated


_AUTONOMOUS_IDLE_MINUTES = int(get_setting("WATCHDOG_AUTONOMOUS_IDLE_MINUTES"))


async def _autonomous_idle_improvements() -> int:
    """Detect idle boards with autonomous mode ON and create improvement tasks.

    When a board has zero active tasks (inbox/in_progress/review) for
    _AUTONOMOUS_IDLE_MINUTES and autonomous_tasks_enabled=true, create
    self-improvement tasks: test-case creation, system auditing, UI fixes.

    T107 round-6 (2026-05-27): GATED behind env flag, default OFF. These
    "[AUTO] Audit / Review / Refresh" titles all require real tool access
    (git, file system, REST APIs) that PEV's LLM-only executor doesn't
    have. Every wave produced 3 tasks that ran through PEV, got verifier-
    rejected for incomplete evidence, escalated to owner-review, then
    auto-spawned 30 min later. Pure noise generator.

    Re-enable when agents gain tool access: set
    `AUTONOMOUS_IDLE_IMPROVEMENTS_ENABLED=1` in .env.
    """
    if os.environ.get("AUTONOMOUS_IDLE_IMPROVEMENTS_ENABLED", "0").lower() not in ("1", "true", "yes"):
        return 0
    from app.db.session import async_session_maker
    async with async_session_maker() as db:
        # Find boards with autonomous mode on
        boards_r = await db.execute(text("""
            SELECT id, name FROM boards
            WHERE autonomous_tasks_enabled = true
        """))
        boards = boards_r.fetchall()
        if not boards:
            return 0

        created = 0
        now = datetime.now(timezone.utc)
        for bid, bname in boards:
            # Check if board is idle (no active tasks)
            active_r = await db.execute(text("""
                SELECT COUNT(*) FROM tasks
                WHERE board_id = :bid
                  AND status IN ('inbox', 'in_progress', 'review')
            """), {"bid": bid})
            active_count = active_r.scalar() or 0
            if active_count > 0:
                continue

            # Check last improvement task created — debounce 30 min
            last_imp_r = await db.execute(text("""
                SELECT MAX(created_at) FROM tasks
                WHERE board_id = :bid
                  AND auto_created = true
                  AND auto_reason = 'autonomous_idle_improvement'
            """), {"bid": bid})
            last_imp = last_imp_r.scalar()
            if last_imp is not None and last_imp.tzinfo is None:
                # Postgres returns naive datetimes by default — normalize to UTC.
                last_imp = last_imp.replace(tzinfo=timezone.utc)
            if last_imp and (now - last_imp).total_seconds() < 1800:
                continue  # debounce: no more than 1 improvement wave per 30 min

            import random as _random
            import uuid as _uuid
            # T21 (2026-05-14): expanded pool. Watchdog picks 3 random templates
            # each idle wave so the same 3 tasks don't dominate over time. Pool
            # ordering: prefer technical-debt + reliability checks over greenfield.
            _IMPROVEMENT_POOL = [
                {
                    "title": "[AUTO] Run system health test suite",
                    "desc": "## Instructions\n\nRun the complete test suite. Report pass/fail. "
                            "If any test fails, create a task to fix it.\n\n## Acceptance\n"
                            "- All tests pass OR failing tests have child fix tasks\n"
                            "- Report posted as comment",
                    "complexity": 2,
                },
                {
                    "title": "[AUTO] Audit recent changes for wiki parity",
                    "desc": "## Instructions\n\nAudit the last 50 code changes against wiki docs. "
                            "Ensure 1:1 parity. Create tasks for any missing documentation.\n\n"
                            "## Acceptance\n- All recent code changes documented in wiki\n"
                            "- Missing docs have child tasks",
                    "complexity": 3,
                },
                {
                    "title": "[AUTO] Review board tasks for stuck items",
                    "desc": "## Instructions\n\nScan all tasks on this board. Identify stuck, "
                            "stale, or abandoned tasks. Re-assign or escalate as needed.\n\n"
                            "## Acceptance\n- All stuck tasks have actions\n- Report posted",
                    "complexity": 2,
                },
                {
                    "title": "[AUTO] Triage improvements-todo HIGH items",
                    "desc": "## Instructions\n\nRead `synthesis/improvements-todo.md`. Pick the "
                            "top HIGH-priority unresolved item. Write a mini-plan in synthesis/ "
                            "with goal, files, risks, test plan. Do NOT implement yet.\n\n## Acceptance\n"
                            "- One new `plan-<feature>-<date>.md` page in synthesis/\n"
                            "- Item linked from the plan page back to improvements-todo",
                    "complexity": 3,
                },
                {
                    "title": "[AUTO] Verify provider registry health",
                    "desc": "## Instructions\n\nList all providers in the database. For each, "
                            "verify the configured `base_url` reaches a healthy LLM endpoint. "
                            "Open an Owner Review item for any provider that fails the probe.\n\n## Acceptance\n"
                            "- All online providers verified OR Owner Review item filed\n"
                            "- Status posted as comment",
                    "complexity": 2,
                },
                {
                    "title": "[AUTO] Scan agent memory for token-burn drift",
                    "desc": "## Instructions\n\nRead `memory/*.md` under each agent's workspace. "
                            "Flag agents whose latest entry mentions 'token-burning' or 'looping'. "
                            "Confirm watchdog throttle is active; nudge owner if not.\n\n## Acceptance\n"
                            "- All token-burn flags verified\n- Report posted",
                    "complexity": 2,
                },
                {
                    "title": "[AUTO] Audit done-tasks for unresolved peer-review markers",
                    "desc": "## Instructions\n\nFind tasks in `done` whose last `[peer-review]` "
                            "marker is `result=fail`. Investigate whether the fail was resolved "
                            "before close. If not, reopen via owner notification.\n\n## Acceptance\n"
                            "- All flagged tasks reviewed\n- Owner alerted on unresolved fails",
                    "complexity": 3,
                },
                {
                    "title": "[AUTO] Refresh role-pool capacity baseline",
                    "desc": "## Instructions\n\nRecord baseline metrics: agents online per role, "
                            "average wait-time per role, recent saturation events. Post to "
                            "`synthesis/role-pool-snapshot-<date>.md` for trend analysis.\n\n## Acceptance\n"
                            "- Snapshot page written\n- Wide deviations from prior snapshot flagged",
                    "complexity": 2,
                },
            ]
            improvement_tasks = _random.sample(
                _IMPROVEMENT_POOL,
                k=min(3, len(_IMPROVEMENT_POOL)),
            )
            for imp in improvement_tasks:
                tid = _uuid.uuid4()
                await db.execute(text("""
                    INSERT INTO tasks (id, board_id, title, description, status, priority,
                                       complexity, auto_created, auto_reason,
                                       created_at, updated_at)
                    VALUES (:id, :bid, :title, :desc, 'inbox', 'low', :cplx,
                            true, 'autonomous_idle_improvement', NOW(), NOW())
                """), {
                    "id": str(tid),
                    "bid": bid,
                    "title": imp["title"],
                    "desc": imp["desc"],
                    "cplx": imp["complexity"],
                })
                created += 1

            await db.commit()
            logger.info(
                "watchdog.autonomous_improvement board=%s tasks=%d",
                bname, created,
            )
        return created


async def _process_scheduled_activation() -> int:
    """Activate inbox tasks whose scheduled_at has passed."""
    from app.db.session import async_session_maker
    async with async_session_maker() as db:
        r = await db.execute(text("""
            SELECT id FROM tasks
            WHERE status = 'inbox'
              AND scheduled_at IS NOT NULL
              AND scheduled_at <= NOW()
            LIMIT 50
        """))
        rows = r.fetchall()
        if not rows:
            return 0

        activated = 0
        for row in rows:
            await db.execute(text(f"""
                UPDATE tasks SET scheduled_at = NULL, updated_at = NOW()
                WHERE {_legacy_where()} AND id = :tid
            """),
            {"tid": str(row[0])})
            activated += 1

        if activated:
            await db.commit()
        return activated


# ── UUID helper ──────────────────────────────────────────────────────────────
def _make_uuid() -> str:
    import uuid as _uuid
    return str(_uuid.uuid4())


async def _pev_autostart_candidates(db, assigned_guard: bool):
    """Only the PEV switch authorizes unattended model execution."""
    return (await db.execute(text("""
        SELECT t.id, t.title, t.description
        FROM tasks t
        JOIN boards b ON b.id = t.board_id
        LEFT JOIN task_pev_state s ON s.task_id = t.id
        WHERE b.autonomous_enabled = true
          AND t.status = 'inbox'
          AND s.task_id IS NULL
          AND COALESCE(t.execution_lane, '') <> 'harness'
          AND NOT (t.execution_lane IS NULL AND t.title ~ '^\\[(agent|claude|codex)\\]')
          AND (:assigned_guard = false OR t.assigned_agent_id IS NULL)
          AND COALESCE(t.auto_reason, '') NOT IN
              ('tests-autonomy-remediation', 'tests-harness',
               'autonomy-harness', 'autonomous_idle_improvement')
        ORDER BY t.updated_at ASC
        LIMIT 5
    """), {"assigned_guard": assigned_guard})).fetchall()


async def _process_once() -> dict[str, int]:
    global _TICK_COUNT
    _TICK_COUNT += 1
    stats = {
        "scanned": 0,
        "nudged": 0,
        "escalated": 0,
        "skipped_cooldown": 0,
        "reassigned": 0,
        "reconciled": 0,
        "qa_bypass": 0,
        "owner_questions_escalated": 0,
        "openclaw_reconciled_offline": 0,
        "openclaw_reconciled_online": 0,
        "auto_reprovisioned": 0,
        "pev_advanced": 0,
        "token_burn_throttled": 0,
        "token_burn_recovered": 0,
        "ballots_opened": 0,
        "ballots_closed": 0,
        "harness_cleaned": 0,
        "souls_refreshed": 0,
        "lead_election_opened": 0,
        "capacity_review_opened": 0,
        "tests_triggered": 0,
        "cron_repeated": 0,
        "interval_repeated": 0,
        "scheduled_activated": 0,
        "autonomous_improvements": 0,
        "inbox_triaged": 0,
        "stuck_inbox_recovered": 0,
        "owner_directives_applied": 0,
        "freeform_promoted": 0,
        "auto_closed": 0,
        "auto_approved": 0,
        "stuck_sessions_aborted": 0,
        "rapid_heartbeat_aborted": 0,
        "split_parents_completed": 0,
        "gateway_down": 0,
        "offline_recovered": 0,
        "provider_mapping_issues": 0,
        "dag_materialized": 0,
        "dag_cancelled": 0,
        "budget_exhausted": 0,
    }

    # Hard gate: if openclaw-gateway is down, mission-control must NOT drive
    # any task/agent actions. Owner halted the platform; respect that.
    # We still emit a tick so liveness checks succeed.
    alive = _gateway_alive()
    # T90 (2026-05-16): write gateway-liveness heartbeat file so a host-side
    # watchdog script can auto-restart the systemd-user service when this
    # flips false. The script reads `~/.openclaw/heartbeat.gateway` and
    # invokes `systemctl --user restart openclaw-gateway.service` after N
    # consecutive `alive=false` reports. Backend writing is best-effort.
    try:
        _write_gateway_heartbeat(alive)
    except Exception as exc:
        logger.debug("gateway_heartbeat.write_failed err=%s", exc)
    if not alive:
        stats["gateway_down"] = 1
        logger.info("watchdog.skipped reason=gateway_down host=%s:%s",
                    _GATEWAY_HOST, _GATEWAY_PORT)
        return stats

    # Step -2: auto-recover agents stuck offline for > 30 min.
    # Stuck-session abort can force agents offline permanently. Without
    # recovery, the worker pool atrophies until 0 agents are online.
    # Recovery: if agent has been offline > 30 min, has an openclaw session,
    # and the gateway is reachable — bring it back online.
    try:
        recovered = await _recover_offline_agents()
        if recovered:
            logger.info("watchdog.offline_recovered agents=%d", recovered)
        stats["offline_recovered"] = recovered
    except Exception as e:
        logger.warning("watchdog.offline_recovery_failed err=%s", e)

    # T104 round-4 (2026-05-24): auto-reprovision session-dropped agents
    # that are needed for an active task. Runs AFTER `_recover_offline_agents`
    # since that path only handles agents with intact session_id; this one
    # handles the harder case where the reconciler nulled session_id.
    try:
        reprov = await _auto_reprovision_session_dropped()
        if reprov:
            logger.info("watchdog.auto_reprovision count=%d", reprov)
        stats["auto_reprovisioned"] = reprov
    except Exception as e:
        logger.warning("watchdog.auto_reprovision.failed err=%s", e)

    # Step -1: remove stale test-harness / anonymous items (anti-spam)
    try:
        stats["harness_cleaned"] = _cleanup_harness_items()
    except Exception as e:
        logger.warning("watchdog.harness_cleanup_failed err=%s", e)

    # Step -1b (T92, 2026-05-17): sync `provider_capacity.current_usage`
    # from live counts so the UI dashboard reflects reality. Routing
    # still uses the live SQL (`_provider_live_usage`); this is purely
    # for visibility.
    try:
        async with async_session_maker() as _db:
            live = await _provider_live_usage(_db)
            await _db.execute(text("UPDATE provider_capacity SET current_usage = 0"))
            for prov_name, n in live.items():
                await _db.execute(
                    text("UPDATE provider_capacity SET current_usage = :n WHERE provider_name = :p"),
                    {"n": int(n), "p": prov_name},
                )
            await _db.commit()
    except Exception as e:
        logger.debug("watchdog.usage_sync_failed err=%s", e)

    # Step 0a-pre: autonomy primitives that keep self-tests green under load,
    # run before the test trigger so the harness sees the corrected state:
    #   - SOUL keep-alive: re-sync live agents' SOUL.md before the 30d cliff
    #   - lead-election: open ballot when Atlas stale >4h + board active
    #   - capacity: raise Owner Review when inbox > 2× active agents
    try:
        async with async_session_maker() as db:
            stats["souls_refreshed"] = await _refresh_stale_souls(db)
            stats["lead_election_opened"] = await _ensure_lead_election_ballot(db)
            stats["capacity_review_opened"] = await _ensure_capacity_owner_review(db)
    except Exception as e:
        logger.warning("watchdog.autonomy_primitives_failed err=%s", e)

    # Step 0a: trigger autonomy test run (skip if last run < 300s ago)
    try:
        stats["tests_triggered"] = await _trigger_test_run()
    except Exception as e:
        logger.warning("watchdog.test_trigger_failed err=%s", e)

    # Step 0a0: auto-recover provider reachability BEFORE routing, so the worker
    # picker sees vllm's true up/down state after any network/route change.
    try:
        flips = await _autorecover_providers()
        if flips:
            logger.info("watchdog.provider_autorecover flips=%d", flips)
        stats["provider_flips"] = flips
    except Exception as e:
        logger.warning("watchdog.provider_autorecover_failed err=%s", e)

    # Step 0a0b (T111): traffic-error provider gate. Probes say a broken
    # upstream is fine (llamacpp /models 200 while every completion fails)
    # — recent flows are ground truth. Gate high-failure providers offline
    # and mirror isActive=false into OmniRoute so the combo stops routing
    # to them. Recovery stays with _autorecover_providers, gated by a
    # cooldown to prevent flap.
    try:
        gated = await _gate_failing_providers()
        if gated:
            logger.info("watchdog.provider_gate gated=%s", gated)
        stats["provider_gated"] = len(gated)
    except Exception as e:
        logger.warning("watchdog.provider_gate_failed err=%s", e)

    # Step 0a0b: keep OmniRoute's `omniroute-auto` combo pointed at whatever
    # model vllm currently serves. The combo pins <provider>/<model>; when the
    # owner swaps the vllm model it goes stale → omniroute-auto 404s + breaks
    # every gateway agent. This self-heals it automatically each tick.
    try:
        synced = await _sync_vllm_omniroute_combo()
        if synced:
            stats["omniroute_combo_synced"] = synced
    except Exception as e:
        logger.debug("watchdog.omniroute_combo_sync_failed err=%s", e)

    # Step 0a1: route unassigned manual inbox tasks created by owner
    try:
        async with async_session_maker() as db:
            triaged = await _route_manual_inbox_tasks(db)
        if triaged:
            logger.info("watchdog.inbox_triaged tasks=%d", triaged)
        stats["inbox_triaged"] = triaged
    except Exception as e:
        logger.warning("watchdog.inbox_triage_failed err=%s", e)

    # Step 0a2: stuck-claimed-inbox recovery (assigned but never started)
    try:
        async with async_session_maker() as db:
            stuck = await _recover_stuck_claimed_inbox(db)
        if stuck:
            logger.info("watchdog.stuck_inbox.recovered actions=%d", stuck)
        stats["stuck_inbox_recovered"] = stuck
    except Exception as e:
        logger.warning("watchdog.stuck_inbox.failed_all err=%s", e)

    # Step 0a2b: T61 — auto-promote inbox→in_progress when assignee posts
    # a comment. Must run before freeform-completion detection, which
    # only scans `in_progress` tasks.
    try:
        async with async_session_maker() as db:
            n = await _auto_promote_inbox_on_assignee_activity(db)
        if n:
            logger.info("watchdog.inbox_to_in_progress.auto_promoted tasks=%d", n)
        stats["inbox_to_in_progress_promoted"] = n
    except Exception as e:
        logger.warning("watchdog.inbox_to_in_progress.failed err=%s", e)

    # Step 0a2b: owner freeform comment → LLM-evaluate as a goal directive →
    # rewrite task description. One LLM call per new owner comment.
    try:
        async with async_session_maker() as db:
            directed = await _apply_owner_directives(db)
        if directed:
            logger.info("watchdog.owner_directive.applied_count=%d", directed)
        stats["owner_directives_applied"] = directed
    except Exception as e:
        logger.warning("watchdog.owner_directive.failed_all err=%s", e)

    # Patch 1 (2026-07-16): generic declared-artifact reconciliation
    # (report-only) for tasks carrying `;artifact=` in auto_reason.
    try:
        async with async_session_maker() as db:
            artifact_marked = await _reconcile_expected_artifacts(db)
        if artifact_marked:
            logger.info("watchdog.artifact_reconcile.posted=%d", artifact_marked)
        stats["artifact_verdicts"] = artifact_marked
    except Exception as e:
        logger.warning("watchdog.artifact_reconcile.failed err=%s", e)

    # Patch 1 (2026-07-16): flag unbudgeted long-running in_progress tasks
    # (report-only marker; the budget enforcer cannot see NULL budgets).
    try:
        async with async_session_maker() as db:
            unbudgeted = await _flag_unbudgeted_long_runners(db)
        if unbudgeted:
            logger.info("watchdog.unbudgeted_flagged=%d", unbudgeted)
        stats["unbudgeted_flagged"] = unbudgeted
    except Exception as e:
        logger.warning("watchdog.unbudgeted_flag.failed err=%s", e)

    # Step 0a3: detect free-text completion → synth marker + promote to review
    try:
        async with async_session_maker() as db:
            promoted = await _detect_freeform_completion(db)
        if promoted:
            logger.info("watchdog.freeform_completion.promoted tasks=%d", promoted)
        stats["freeform_promoted"] = promoted
    except Exception as e:
        logger.warning("watchdog.freeform_completion.failed_all err=%s", e)

    # Step 0a3b: LLM-evaluated completion detection (T65 2026-05-15).
    # Catches assignee comments that don't match the SQL fast-path but
    # semantically indicate completion. Capped at 5 tasks/tick.
    # Gated OFF by default (WATCHDOG_COMPLETION_LLM_ENABLED) to minimize LLM
    # calls: canonical completion markers (COMPLETED:/DONE:/TASK COMPLETE/##
    # Result…, which agents post) already promote via the no-LLM SQL fast-path
    # in `_detect_freeform_completion`; ambiguous freeform comments fall back to
    # the wall-clock finalize. This LLM disambiguation ran up to 5 calls/tick.
    try:
        llm_flagged = 0
        if _COMPLETION_LLM_ENABLED:
            async with async_session_maker() as db:
                llm_flagged = await _detect_llm_completion(db)
            if llm_flagged:
                logger.info("watchdog.llm_completion.flagged tasks=%d", llm_flagged)
        stats["llm_completion_flagged"] = llm_flagged
    except Exception as e:
        logger.warning("watchdog.llm_completion.failed_all err=%s", e)

    # Step 0a4: detect split parents whose children are all done
    try:
        async with async_session_maker() as db:
            completed = await _detect_split_parent_completion(db)
        if completed:
            logger.info("watchdog.split_parents.completed tasks=%d", completed)
        stats["split_parents_completed"] = completed
    except Exception as e:
        logger.warning("watchdog.split_parents.failed_all err=%s", e)

    # Step 0a4b (T97, 2026-05-17): close parent on MAJORITY done if
    # laggard child(ren) are stuck.
    try:
        async with async_session_maker() as db:
            maj = await _detect_split_parent_majority_completion(db)
        if maj:
            logger.info("watchdog.split_parents.majority_completed=%d", maj)
        stats["split_parents_majority"] = maj
    except Exception as e:
        logger.warning("watchdog.split_parents.majority_failed err=%s", e)

    # Step 0a4c (T98, 2026-05-17): reassign stuck subtasks to a fresh
    # agent in the role pool. Saves the parent from blocking on a single
    # wedged worker.
    try:
        async with async_session_maker() as db:
            rea = await _reassign_stuck_subtasks(db)
        if rea:
            logger.info("watchdog.subtasks_reassigned=%d", rea)
        stats["subtasks_reassigned"] = rea
    except Exception as e:
        logger.warning("watchdog.subtasks_reassign_failed err=%s", e)

    # Step 0a5: materialize subtask DAGs declared in [pm-plan] markers (E1)
    try:
        async with async_session_maker() as db:
            n = await _materialize_dag_from_pm_plan(db)
        if n:
            logger.info("watchdog.dag.materialized_count=%d", n)
        stats["dag_materialized"] = n
    except Exception as e:
        logger.warning("watchdog.dag.materialize_failed_all err=%s", e)

    # Step 0a6a: aggregate per-task token usage from traffic sqlite (E4 Phase 2, T26)
    try:
        async with async_session_maker() as db:
            await _aggregate_token_usage(db)
    except Exception as e:
        logger.warning("watchdog.tokens.aggregate_failed err=%s", e)

    # Step 0a6: per-task budget enforcement — wall-clock + token (E4 T22+T26 2026-05-14)
    try:
        async with async_session_maker() as db:
            n = await _enforce_task_budgets(db)
        if n:
            logger.info("watchdog.budget.exhausted_count=%d", n)
        stats["budget_exhausted"] = n
    except Exception as e:
        logger.warning("watchdog.budget.enforce_failed_all err=%s", e)

    # Step 0a7: DAG cancellation propagation (E1 Phase 2, T24b 2026-05-14)
    try:
        async with async_session_maker() as db:
            n = await _propagate_dag_cancellations(db)
        if n:
            logger.info("watchdog.dag_cancel.count=%d", n)
        stats["dag_cancelled"] = n
    except Exception as e:
        logger.warning("watchdog.dag_cancel.failed_all err=%s", e)

    # Step 0a7b (T104, 2026-05-24): reconcile MC agent status with openclaw's
    # per-session sessions.json. Root cause of the "agent online but does
    # nothing" pattern owner observed: openclaw timed out the session +
    # MC kept routing tasks to it because MC's `agents.status` cache was
    # never updated. Now we read the truth from sessions.json each tick.
    try:
        async with async_session_maker() as db:
            recon = await _reconcile_openclaw_session_status(db)
        if recon.get("set_offline") or recon.get("set_online"):
            logger.info(
                "watchdog.openclaw_reconcile checked=%d offlined=%d onlined=%d",
                recon["checked"], recon["set_offline"], recon["set_online"],
            )
        stats["openclaw_reconciled_offline"] = recon.get("set_offline", 0)
        stats["openclaw_reconciled_online"] = recon.get("set_online", 0)
    except Exception as e:
        logger.warning("watchdog.openclaw_reconcile.failed err=%s", e)

    # Step 0a8 (T104, 2026-05-24): owner-question detector. Scans recent
    # comments for patterns like "Prompt to user", "Please confirm", etc.
    # Escalates the task to /review immediately so the owner sees it
    # without waiting for the 20-min stale threshold.
    try:
        async with async_session_maker() as db:
            n = await _detect_owner_questions(db)
        if n:
            logger.info("watchdog.owner_question.escalated=%d", n)
        stats["owner_questions_escalated"] = n
    except Exception as e:
        logger.warning("watchdog.owner_question.failed err=%s", e)

    # Step 0a7 (T107, 2026-05-26): journal auto-writer — append activity
    # events into the daily diary file under wiki-vault/journal/. Owner
    # expects every day to have a structured log without manual edits.
    try:
        from app.services.journal_writer import tick as _jw_tick, write_daily_summary
        from datetime import datetime as _dt
        async with async_session_maker() as db:
            jw = await _jw_tick(db)
        stats["journal_appended"] = jw.get("appended", 0)

        # Nightly daily-summary: write once per day, prefer the first tick
        # after midnight UTC. Idempotent (regex strips the prior auto
        # summary block before re-writing), so safe to run multiple times.
        now = _dt.now()
        last_summary_date = getattr(_process_once, "_last_summary_date", None)
        if last_summary_date != now.strftime("%Y-%m-%d") and now.hour == 0:
            async with async_session_maker() as db:
                # Summarize YESTERDAY at the start of today's first tick.
                from datetime import timedelta as _td
                yday = (now - _td(days=1)).strftime("%Y-%m-%d")
                await write_daily_summary(db, yday)
            setattr(_process_once, "_last_summary_date", now.strftime("%Y-%m-%d"))
    except Exception as e:
        logger.warning("watchdog.journal_writer_failed err=%s", e)

    # Step 0a8a (T107, 2026-05-26): reconcile MC sidecar curated models →
    # OmniRoute curated catalog. Sidecar (`openclaw.mc.json`) is the
    # source of truth; OR catalog can drift after OR rebuilds/clears.
    # Forward-only sync — sidecar additions only, never delete from OR.
    try:
        import json as _json
        from pathlib import Path as _Path
        from app.services.omniroute_sync import (
            find_node_id_by_name,
            list_models_for_provider,
            add_model_for_provider,
        )
        mc_json = _Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")) / "openclaw.mc.json"
        added_models = 0
        if mc_json.exists():
            try:
                sc = _json.loads(mc_json.read_text())
            except Exception:
                sc = {}
            for prov, m_dict in (sc.get("models") or {}).items():
                if not isinstance(m_dict, dict):
                    continue
                nid = await find_node_id_by_name(prov)
                if not nid:
                    continue
                try:
                    cur = await list_models_for_provider(prov, include_available=False)
                except Exception:
                    cur = []
                existing = {(m.get("modelId") or m.get("id")) for m in cur if isinstance(m, dict)}
                for mid in m_dict.keys():
                    if mid in existing:
                        continue
                    ok, _ = await add_model_for_provider(prov, mid)
                    if ok:
                        added_models += 1
                        logger.info("watchdog.or_catalog_reconciled provider=%s model=%s", prov, mid)
        stats["or_catalog_added"] = added_models
    except Exception as e:
        logger.warning("watchdog.or_catalog_reconcile_failed err=%s", e)

    # Step 0a7b (T107 round-5, 2026-05-27): detect OmniRoute semaphore
    # leak + file recovery signal. Symptom: 429 "Semaphore timeout after
    # 30000ms" from OR even though direct upstream is healthy. Caused by
    # OR's per-connection slot counter incrementing on call-start but
    # never decrementing when streaming responses abort. Repeated
    # timeouts permanently leak all slots → pipeline dies silently.
    #
    # Detection: ≥5 semaphore-timeout strikes within 10 min.
    # Action: write `~/.openclaw/heartbeat.omniroute_restart_request`
    # marker. Host-side `omniroute-watchdog.sh` (analogous to the
    # gateway-watchdog from T90 B2.b) tail-monitors this file and
    # restarts OR. Then watchdog clears its strike counter + marker.
    # Throttled to one signal per 5 min to avoid restart-storm.
    try:
        from app.services.llm_evaluator import (
            get_or_semaphore_strikes, reset_or_semaphore_strikes,
        )
        import time as _t
        strikes = get_or_semaphore_strikes()
        now = _t.time()
        # Drop older than 10 min for the threshold check.
        recent = [s for s in strikes if now - s < 600]
        # ALSO detect the leak from GATEWAY-AGENT traffic (the traffic-DB mirror
        # of OR call_logs). MC's own LLM call volume is ~0 (routing/completion
        # LLM disabled), so the in-process strike counter rarely trips — but the
        # leak is driven by agent 429s. Count recent semaphore-timeout 429s by
        # rowid (clock-skew-safe: no datetime('now')). This is the key
        # durability fix — otherwise OR is never auto-restarted on a real leak.
        agent_sem_429 = 0
        try:
            import sqlite3 as _sq
            _tdb = os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db")
            _c = _sq.connect(f"file:{_tdb}?mode=ro", uri=True)
            agent_sem_429 = _c.execute(
                "SELECT COUNT(*) FROM (SELECT status_code,res_body FROM flows "
                "ORDER BY id DESC LIMIT 80) WHERE status_code=429 "
                "AND res_body LIKE '%Semaphore timeout%'"
            ).fetchone()[0]
            _c.close()
        except Exception:
            pass
        last_signal = getattr(_process_once, "_last_or_restart_signal", 0.0)
        if (len(recent) >= 5 or agent_sem_429 >= 5) and (now - last_signal) > 300:
            marker = _OPENCLAW_HOME / "heartbeat.omniroute_restart_request"
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                import json as _json
                marker.write_text(_json.dumps({
                    "timestamp": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now)),
                    "strike_count_10min": len(recent),
                    "agent_semaphore_429_recent": agent_sem_429,
                    "reason": "or_semaphore_leak",
                }) + "\n", encoding="utf-8")
                setattr(_process_once, "_last_or_restart_signal", now)
                logger.warning(
                    "watchdog.omniroute_restart_signal_filed mc_strikes=%d agent_429=%d marker=%s",
                    len(recent), agent_sem_429, str(marker),
                )
                reset_or_semaphore_strikes()
                stats["omniroute_restart_requested"] = 1
            except Exception as exc:
                logger.warning("watchdog.or_restart_marker_write_failed err=%s", exc)
    except Exception as e:
        logger.warning("watchdog.or_semaphore_detect_failed err=%s", e)

    # Step 0a8a3 (T107 round-4, 2026-05-26): reconcile phantom in_progress.
    # Tasks where PEV terminated (escalated / rolled_back) but task.status
    # was never reset → dashboard shows "in_progress" with 0% LLM usage
    # because PEV won't pick them up again (and shouldn't). Move to
    # `blocked` so the count reflects reality.
    try:
        async with async_session_maker() as db:
            r = await db.execute(text(f"""
                UPDATE tasks t
                SET status='blocked', updated_at=NOW()
                FROM task_pev_state s
                WHERE {_legacy_where('t')} AND s.task_id = t.id
                  AND s.status IN ('escalated','rolled_back')
                  AND t.status IN ('in_progress','review')
                  -- Only a PEV verdict NEWER than the task's own last
                  -- transition is a phantom. Without this the escalated row
                  -- is permanent: every later legitimate move to `review`
                  -- gets dragged back to `blocked` on the next tick, and the
                  -- task can never be finished again.
                  AND s.updated_at > t.updated_at
                RETURNING t.id, t.board_id
            """))
            rows = r.fetchall()
            # Every blocked task must carry an explanation (checked by the
            # self-heal tick). Without this comment the board shows a task
            # silently flipped to `blocked` with no reason a human can read.
            for row in rows:
                await _post_comment(
                    db, str(row[0]), str(row[1]) if row[1] else None,
                    "[watchdog-pev-phantom-reset] PEV terminated "
                    "(escalated/rolled_back) with a verdict newer than the "
                    "task's last transition, but task.status was never reset. "
                    "Moved to blocked so the board reflects reality — owner "
                    "triage required.",
                )
            n = len(rows)
            await db.commit()
            if n:
                logger.info("watchdog.pev.phantom_in_progress_reset count=%d", n)
            stats["pev_phantom_reset"] = n
    except Exception as e:
        logger.warning("watchdog.pev.phantom_reset_failed err=%s", e)

    # Step 0a8a2 (T107, 2026-05-26): reconcile PEV-terminal tasks that
    # are still sitting in `review` because they completed under the OLD
    # PEV code (pre-autonomous-auto-close). Catches any task where:
    #   - task_pev_state.status='done' + last_verdict_pass=true
    #   - tasks.status='review'
    #   - board.autonomous_enabled=true
    # → close to `done`, insert approval row, emit close markers.
    try:
        import uuid as _uuid_mod
        async with async_session_maker() as db:
            stuck = (await db.execute(text(f"""
                SELECT t.id, t.board_id, s.verifier_model, t.updated_at
                FROM task_pev_state s
                JOIN tasks t ON t.id = s.task_id
                JOIN boards b ON b.id = t.board_id
                WHERE s.status = 'done'
                  AND s.last_verdict_pass = true
                  AND t.status = 'review'
                  AND {_legacy_where('t')}
                  AND b.autonomous_enabled = true
                LIMIT 10
            """))).fetchall()
            closed = 0
            for row in stuck:
                tid = str(row[0])
                bid = str(row[1]) if row[1] else None
                vm = row[2] or 'unknown'
                try:
                    if await _lock_legacy_task(db, tid, row[3], "review") is None:
                        continue
                    changed = await db.execute(text(
                        "UPDATE tasks SET status='done', updated_at=NOW() "
                        f"WHERE id=:tid AND status='review' AND {_legacy_where()}"
                    ), {"tid": tid})
                    if changed.rowcount != 1:
                        await db.rollback()
                        continue
                    # Insert approval row to satisfy require_approval_for_done.
                    await db.execute(text("""
                        INSERT INTO approvals
                            (id, board_id, task_id, action_type, payload,
                             confidence, status, created_at, resolved_at)
                        VALUES (:id, :bid, :tid, 'pev_autonomous_signoff', :payload,
                                0.95, 'approved', NOW(), NOW())
                    """), {
                        "id": str(_uuid_mod.uuid4()), "bid": bid, "tid": tid,
                        "payload": (
                            '{"source":"pev_reconciliation",'
                            f'"verifier_model":"{vm}","verdict":"pass"}}'
                        ),
                    })
                    await _post_comment(
                        db, tid, bid or "",
                        "[ready-to-close] [pm-signoff] verdict=accept confidence=95 [auto-pev-reconcile]",
                        commit=False,
                    )
                    await db.commit()
                    closed += 1
                    logger.info("watchdog.pev.reconciled_to_done task=%s verifier=%s", tid[:8], vm)
                except Exception as exc:
                    await db.rollback()
                    logger.warning("watchdog.pev.reconcile_failed task=%s err=%s", tid[:8], exc)
                finally:
                    await db.rollback()
            stats["pev_reconciled_done"] = closed
    except Exception as e:
        logger.warning("watchdog.pev.reconcile_step_failed err=%s", e)

    # Step 0a8a (P3, 2026-09-01): UNDERSTAND + RESEARCH enrichment stages.
    # Gated OFF by default; autonomous boards only; additive markers.
    try:
        stats["pipeline_stage_markers"] = await _run_pipeline_stages()
    except Exception as e:
        logger.warning("watchdog.pipeline_stages_step_failed err=%s", e)

    # Step 0a8b (T107, 2026-05-26): auto-start PEV for stuck inbox+blocked
    # tasks on autonomous-enabled boards. Without this, autonomous_enabled
    # was decorative — the toggle did not actually drive any execution.
    # T107 round-2 (2026-05-26): cap reduced from 3 → 1 per tick. Combined
    # with step-0a9 advance cap (also reduced below) keeps concurrent calls
    # under ollama's maxConcurrent=3 so we don't burst-fire into 429s.
    # T107 round-3 (2026-05-26): EXCLUDE `tests-autonomy-remediation` +
    # `tests-harness` tasks. Their fixes require actual code-execution
    # tools (shell, file system, db migrations) which the LLM-only PEV
    # executor cannot provide. Owner sees these as escalations with
    # "executor returned no diff, just describes inability to access". The
    # right answer is to leave them inbox-blocked and surface only ONE
    # umbrella owner-review when many accumulate, not PEV-loop each.
    try:
        from app.services.pev_loop import start_pev, _task_requires_real_tools
        from app.services.runtime_settings import get_setting as _gs_pev
        pev_started = 0
        # P3 Slice 5 (2026-09-01): when tool-route is enabled, PEV must NOT grab a
        # task that inbox-triage already assigned to a (tool-capable) gateway agent
        # — that agent owns it and can use real tools; PEV's text-only executor
        # would double-execute + fabricate. Restrict auto-start to UNASSIGNED tasks
        # so assigned (incl. tool-requiring, triage-routed) tasks flow to their
        # agent. Reversible: flag OFF → prior behaviour (grab regardless of assignee).
        async with async_session_maker() as db:
            rows = await _pev_autostart_candidates(
                db, bool(_gs_pev("WATCHDOG_TOOL_ROUTE_ENABLED", False)),
            )
            for r in rows:
                from uuid import UUID as _UUID
                # T108: skip tasks needing real tools — PEV's text-only
                # executor fabricates on those. They route to the assigned
                # tooled gateway agent instead.
                if _task_requires_real_tools(r[1] or "", r[2] or ""):
                    continue
                tid = _UUID(str(r[0]))
                try:
                    started = await start_pev(db, tid)
                    if started.get("ok"):
                        pev_started += 1
                        # Move task to in_progress so the standard pipeline sees it as live.
                        await db.execute(text(f"""
                            UPDATE tasks SET status='in_progress', updated_at=NOW()
                            WHERE {_legacy_where()} AND id=:tid AND status = 'inbox'
                        """), {"tid": str(tid)})
                        await db.commit()
                        break  # T108: cap at 1 PEV start/tick (concurrency); LIMIT 5 only widens the skip pool
                except Exception as exc:
                    logger.warning("pev.autostart.failed task=%s err=%s", str(tid)[:8], exc)
        if pev_started:
            logger.info("watchdog.pev.auto_started count=%d", pev_started)
        stats["pev_auto_started"] = pev_started
    except Exception as e:
        logger.warning("watchdog.pev.auto_start_failed err=%s", e)

    # Step 0a9 (T105 Phase 3, 2026-05-25): drive Planner-Executor-Verifier
    # loops for every task with active PEV state. Each call advances ONE
    # phase (plan / execute+verify) so the loop respects tick cadence.
    # T107 round-2: cap reduced from 5 → 2 to match the typical provider
    # concurrency limit (ollama maxConcurrent=3). Bursting 5 advances when
    # the pool only has 3 slots caused 429-storms (5 calls fight for 3
    # slots → 2 lose → 429 → exp backoff → tier exhausts → repeat).
    # Also: if ALL chain models are currently rate-limited (cache check),
    # skip the whole step instead of burning calls into a wall.
    try:
        from app.services.pev_loop import list_active_pev_tasks, advance_pev
        from app.services.model_router import _is_model_rate_limited
        from app.services.runtime_settings import get_setting
        pev_advanced = 0
        # Pre-flight: planner+executor+verifier chains all rate-limited?
        all_chains_blocked = True
        for key in ("TIER_PLANNER_MODELS", "TIER_EXECUTOR_MODELS", "TIER_VERIFIER_MODELS"):
            chain = [m.strip() for m in (get_setting(key, "") or "").split(",") if m.strip()]
            if any(not _is_model_rate_limited(m) for m in chain):
                all_chains_blocked = False
                break
        if all_chains_blocked:
            logger.info("watchdog.pev.skip_all_chains_rate_limited")
            stats["pev_advanced"] = 0
            # T107 round-2: file ONE owner-review item if all tiers have
            # been blocked >10 min. De-duped via submitter+title check.
            # Avoids the per-tick 429 storm flooding logs with no progress.
            try:
                from datetime import datetime as _dt, timedelta as _td
                first_seen = getattr(_process_once, "_all_chains_blocked_since", None)
                now_dt = _dt.now()
                if first_seen is None:
                    setattr(_process_once, "_all_chains_blocked_since", now_dt)
                elif now_dt - first_seen > _td(minutes=10):
                    from app.services.owner_reviews import insert_sync
                    async with async_session_maker() as db:
                        existing = await db.execute(text("""
                            SELECT id FROM owner_reviews
                            WHERE status='pending'
                              AND submitted_by='Pipeline Watchdog (tier-exhausted)'
                            LIMIT 1
                        """))
                        if not existing.first():
                            insert_sync(**{
                                "title": "Tier exhausted — all PEV-chain models rate-limited",
                                "type_": "other",
                                "summary": (
                                    "Every model in planner/executor/verifier "
                                    "chains has been rate-limited for >10 min. "
                                    "PEV loop is paused — no autonomous progress. "
                                    "Check OmniRoute provider state + quota."
                                ),
                                "reason": "all_tier_chains_blocked",
                                "proposed_action": (
                                    "Owner to: (a) wait for upstream quota reset, "
                                    "(b) add another model to runtime_settings.json "
                                    "chain, (c) enable a paid provider, "
                                    "(d) reduce PEV active task count."
                                ),
                                "submitted_by": "Pipeline Watchdog (tier-exhausted)",
                                "task_id": None,
                            })
                            logger.warning("watchdog.pev.tier_exhausted_escalated")
            except Exception as exc:
                logger.warning("watchdog.pev.tier_exhaust_escalate_failed err=%s", exc)
        else:
            # Reset the "all blocked since" marker when at least one model is back.
            try:
                if getattr(_process_once, "_all_chains_blocked_since", None) is not None:
                    setattr(_process_once, "_all_chains_blocked_since", None)
            except Exception:
                pass
            async with async_session_maker() as db:
                actives = await list_active_pev_tasks(db, limit=2)
                for row in actives:
                    from uuid import UUID as _UUID
                    try:
                        r = await advance_pev(db, _UUID(str(row["task_id"])))
                        if r.get("ok") and not r.get("terminal"):
                            pev_advanced += 1
                    except Exception as exc:
                        logger.warning(
                            "pev.advance.failed task=%s err=%s",
                            str(row["task_id"])[:8], exc,
                        )
            if pev_advanced:
                logger.info("watchdog.pev.advanced count=%d", pev_advanced)
            stats["pev_advanced"] = pev_advanced
    except Exception as e:
        logger.warning("watchdog.pev.failed err=%s", e)

    # Step 0b: detect tasks closed without QA verdict
    try:
        stats["qa_bypass"] = await _detect_qa_bypass()
    except Exception as e:
        logger.warning("watchdog.qa_bypass_failed err=%s", e)

    # Step 0c: detect token-burning agents and throttle their heartbeats
    try:
        burn_result = _detect_token_burn_agents()
        stats["token_burn_throttled"] = burn_result["throttled"]
        stats["token_burn_recovered"] = burn_result["recovered"]
    except Exception as e:
        logger.warning("watchdog.token_burn_failed err=%s", e)

    # Step 0d: probe LLM provider health, file/close infra incident
    # T40f (2026-05-14): run on independent cadence (default 60s) so a slow
    # provider probe doesn't dominate the 180s main tick. Skipped on ticks
    # where the cadence hasn't elapsed yet.
    if _provider_health_due():
        try:
            await _check_provider_health()
        except Exception as e:
            logger.warning("watchdog.provider_health_failed err=%s", e)

    # Step 0d1 (T103 v17, 2026-05-19): OR→MC sync is now EVENT-DRIVEN.
    #
    # OmniRoute (patched local build `omniroute:patched-t103v17`) fires
    # webhook events on every provider/node/connection mutation:
    #   provider.{created,updated,deleted}
    #   node.{created,updated,deleted}
    # MC receiver `/api/v1/internal/omniroute-webhook` reconciles MC DB
    # on each event. Median latency observed: ~120ms.
    #
    # This tick-level call is a 10-min FAILSAFE — covers boot-window drift
    # before `ensure_omniroute_webhook_registered()` completes, webhook
    # delivery failures, or future OR upgrades that drop the patched
    # build. Set `WATCHDOG_OMNIROUTE_REVERSE_SYNC_FAILSAFE_SECONDS=0` to
    # disable entirely once confidence in the event path is established.
    global _orsync_failsafe_last_run
    _now_fs = time.monotonic()
    if _ORSYNC_FAILSAFE_INTERVAL > 0 and (_now_fs - _orsync_failsafe_last_run) >= _ORSYNC_FAILSAFE_INTERVAL:
        _orsync_failsafe_last_run = _now_fs
        try:
            from app.services import omniroute_sync as _orsync
            rows = await _orsync.reverse_sync_to_mc_db()
            if rows:
                logger.info("watchdog.omniroute_reverse_sync_failsafe rows=%d", rows)
        except Exception as e:
            logger.warning("watchdog.omniroute_reverse_sync_failsafe_err err=%s", e)

    # OmniRoute owns combo membership. The former LAN-only rebuild erased
    # subscription/cloud members and defeated its configured load balancing.

    # Step 0d2: validate openclaw.json agent.model.* refs against
    # provider_capacity registry. Throttled to every Nth tick (default 10 ≈ 30 min)
    # and suppresses provider_offline noise that's expected during cloud-toggle OFF.
    if _TICK_COUNT % _PROVIDER_VALIDATOR_EVERY == 0:
        try:
            from app.services.provider_registry_validator import warn_on_mapping_issues
            async with async_session_maker() as db:
                stats["provider_mapping_issues"] = await warn_on_mapping_issues(
                    db, suppress_offline=True,
                )
        except Exception as e:
            logger.warning("watchdog.provider_validator_failed err=%s", e)

    # Step 0e: detect contested QA verdicts → open ballot (Tier 3)
    try:
        async with async_session_maker() as db:
            stats["ballots_opened"] = await _detect_contested_verdict(db)
    except Exception as e:
        logger.warning("watchdog.contested_verdict_failed err=%s", e)

    # Step 0f: close expired ballots (Tier 4 fallback to Atlas)
    try:
        stats["ballots_closed"] = await _close_expired_ballots()
    except Exception as e:
        logger.warning("watchdog.ballot_close_failed err=%s", e)

    # Step 0g: auto-approve high-confidence pending approvals (unblocks status gates)
    try:
        async with async_session_maker() as db:
            stats["auto_approved"] = await _auto_approve_high_confidence_approvals(db)
        if stats["auto_approved"]:
            logger.info("watchdog.auto_approved approvals=%d", stats["auto_approved"])
    except Exception as e:
        logger.warning("watchdog.auto_approve_failed err=%s", e)

    # Step 0g1.4 (T108, 2026-05-30): solicit independent multi-agent static
    # analysis for tool/RE tasks (crackme, IDA, MCP). These can't be PEV-
    # executed (text-only fabricates) nor binary-executed (remote MCP =
    # static only). Confirmation = ≥2 RE agents independently derive the
    # SAME `Password:` → consensus (step 0g1.5 below closes on agreement).
    try:
        async with async_session_maker() as db:
            stats["consensus_solicited"] = await _solicit_consensus_for_tool_tasks(db)
        if stats.get("consensus_solicited"):
            logger.info("watchdog.consensus_solicited count=%d", stats["consensus_solicited"])
    except Exception as e:
        logger.warning("watchdog.consensus_solicit_failed err=%s", e)

    # Step 0g1.5 (T84): auto-emit `[qa-verdict] result=pass` when ≥2 agents
    # converged on a completion artifact. Lets the next step close the
    # task without owner intervention. Must run BEFORE `_auto_close_review_tasks`.
    try:
        async with async_session_maker() as db:
            stats["auto_consensus"] = await _auto_emit_consensus_verdict(db)
        if stats["auto_consensus"]:
            logger.info("watchdog.auto_consensus emitted=%d", stats["auto_consensus"])
    except Exception as e:
        logger.warning("watchdog.auto_consensus_failed err=%s", e)

    # Step 0g2: auto-close review tasks with approved high-confidence approvals
    try:
        async with async_session_maker() as db:
            stats["auto_closed"] = await _auto_close_review_tasks(db)
        if stats["auto_closed"]:
            logger.info("watchdog.auto_closed tasks=%d", stats["auto_closed"])
    except Exception as e:
        logger.warning("watchdog.auto_close_failed err=%s", e)

    # Step 0g3: reconcile pending review items whose underlying task resolved
    try:
        stats["reconciled"] = await _reconcile_resolved_reviews()
        if stats["reconciled"]:
            logger.info("watchdog.reconciled items=%d", stats["reconciled"])
    except Exception as e:
        logger.warning("watchdog.reconcile_failed err=%s", e)

    # Step 0g4: auto-resolve stale pending review items (underlying task stuck,
    # review item >4h old, owner hasn't acted). Close the review item as
    # approved and auto-close the underlying task to prevent infinite re-escalation.
    try:
        stats["reconciled"] += _auto_resolve_stale_reviews()
    except Exception as e:
        logger.warning("watchdog.auto_resolve_stale_failed err=%s", e)

    # Step 0g5: abort stuck agent sessions that have been processing >15 min
    # with no active tasks assigned. Prevents infinite LLM→tool→LLM loops.
    try:
        async with async_session_maker() as db:
            stats["stuck_sessions_aborted"] = await _abort_stuck_sessions(db)
            stats["rapid_heartbeat_aborted"] = await _abort_rapid_heartbeat_sessions(db)
        if stats["stuck_sessions_aborted"]:
            logger.info("watchdog.stuck_sessions_aborted=%d", stats["stuck_sessions_aborted"])
    except Exception as e:
        logger.warning("watchdog.stuck_sessions_abort_failed err=%s", e)

    # Step 0h: activate inbox tasks whose scheduled_at has passed
    try:
        stats["scheduled_activated"] = await _process_scheduled_activation()
        if stats["scheduled_activated"]:
            logger.info("watchdog.scheduled.activated tasks=%d", stats["scheduled_activated"])
    except Exception as e:
        logger.warning("watchdog.scheduled_activation_failed err=%s", e)

    # Step 0h2: autonomous idle board self-improvement
    try:
        stats["autonomous_improvements"] = await _autonomous_idle_improvements()
        if stats["autonomous_improvements"]:
            logger.info("watchdog.autonomous_improvements tasks=%d",
                        stats["autonomous_improvements"])
    except Exception as e:
        logger.warning("watchdog.autonomous_improvement_failed err=%s", e)

    # Step 0i: clone done tasks with elapsed repeat_interval_minutes
    try:
        stats["interval_repeated"] = await _process_interval_repeats()
        if stats["interval_repeated"]:
            logger.info("watchdog.interval_repeated tasks=%d", stats["interval_repeated"])
    except Exception as e:
        logger.warning("watchdog.interval_repeat_failed err=%s", e)

    # Step 0j: clone done tasks whose repeat_cron schedule triggers
    try:
        stats["cron_repeated"] = await _process_cron_repeats()
        if stats["cron_repeated"]:
            logger.info("watchdog.cron_repeated tasks=%d", stats["cron_repeated"])
    except Exception as e:
        logger.warning("watchdog.cron_repeat_failed err=%s", e)

    async with async_session_maker() as db:
        try:
            stale = await _stale_tasks(db)
        except Exception as e:
            logger.warning("watchdog.scan_failed err=%s", e)
            return stats
        stats["scanned"] = len(stale)
        now_ts = datetime.now(timezone.utc).timestamp()

        for task in stale:
            try:
                count, latest_nudge_ts = await _existing_nudge_count(db, task["id"])
                if await _lock_legacy_task(db, task["id"], task["updated_at"], task["status"]) is None:
                    continue

                # Step A0: Hard-stale recovery — task >12h with no progress.
                # Tier 2 escalation: try Senior (ollama) variant of same role first,
                # then any other clone. Meta-tasks auto-resolved (test runner re-runs).
                if task["status"] == "in_progress" and task["age_hours"] > 12 and task.get("agent_name"):
                    role = task["agent_name"]
                    # Strip variant suffixes to find role keyword
                    for suffix in (
                        " Senior", " Beta", " Gamma", " Delta",
                        " [Clone-1]", " [Clone-2]",
                    ):
                        if role.endswith(suffix):
                            role = role[: -len(suffix)]
                    role = role.strip()

                    # Try Senior variant first (Tier 2)
                    new_agent = await _find_senior_variant(db, role, task.get("assigned_agent_id"))
                    if not new_agent:
                        new_agent = await _find_idle_clone(db, role)

                    if new_agent and str(new_agent) != task.get("assigned_agent_id"):
                        body = (
                            f"{_NUDGE_TAG} **Tier 2 — Senior reassignment** — task stuck "
                            f"{task['age_hours']}h, routing to {role} Senior variant "
                            f"(DB id {str(new_agent)[:12]}) per POLICY P11.2."
                        )
                        if await _reassign_task(db, task["id"], new_agent, task["updated_at"], body):
                            stats["reassigned"] += 1
                        continue  # Refusal is not permission for another stale effect.

                    # Meta-tasks: auto-done with note (test/autonomy harness re-runs anyway)
                    if _is_meta_task(task) and task["age_hours"] > 18:
                        try:
                            changed = await db.execute(text(
                                "UPDATE tasks SET status='done', updated_at=NOW() "
                                f"WHERE {_legacy_where()} AND id = :tid AND status = 'in_progress'"
                            ), {"tid": task["id"]})
                            if changed.rowcount != 1:
                                await db.rollback()
                                continue
                            await _post_comment(
                                db, task["id"], task["board_id"] or "",
                                f"{_NUDGE_TAG} **Meta-task auto-resolved** — stalled "
                                f"{task['age_hours']}h with no Senior variant available. "
                                f"Test/autonomy harness will retrigger if needed.", commit=False,
                            )
                            await db.commit()
                            stats["reassigned"] += 1
                            continue
                        except Exception as e:
                            await db.rollback()
                            logger.warning("watchdog.meta_auto_done_failed task=%s err=%s",
                                           task["id"], e)
                            continue
                    # No clone available — drop assignee + revert to inbox so it
                    # gets out of in_progress backlog. Schema allows only
                    # inbox/in_progress/review/done, so we use 'inbox' + marker.
                    if task["age_hours"] > 24:
                        try:
                            changed = await db.execute(
                                text(
                                    "UPDATE tasks SET status='inbox', "
                                    "assigned_agent_id=NULL, updated_at=NOW() "
                                    f"WHERE {_legacy_where()} AND id = :tid AND status = 'in_progress'"
                                ),
                                {"tid": task["id"]},
                            )
                            if changed.rowcount != 1:
                                await db.rollback()
                                continue
                            await _post_comment(
                                db,
                                task["id"],
                                task["board_id"] or "",
                                f"{_NUDGE_TAG} **Auto-released** — stuck {task['age_hours']}h, "
                                f"no fresh clone available. Returned to inbox unassigned for "
                                f"next-cycle pickup. Owner can intervene if recurring.", commit=False,
                            )
                            await db.commit()
                            stats["reassigned"] += 1
                            continue
                        except Exception as e:
                            await db.rollback()
                            logger.warning(
                                "watchdog.auto_release_failed task=%s err=%s",
                                task["id"],
                                e,
                            )
                            continue

                # Step A: Try active reassignment for stale in_progress before escalating.
                # Only after 1 nudge to give original assignee a chance to respond.
                if (
                    task["status"] == "in_progress"
                    and count == 1
                    and task.get("agent_name")
                    and latest_nudge_ts
                    and (now_ts - latest_nudge_ts) > _NUDGE_COOLDOWN
                ):
                    # Find role keyword (strip Beta/Gamma/Clone suffixes)
                    role = task["agent_name"]
                    for suffix in (
                        " Beta",
                        " Gamma",
                        " Delta",
                        " [Clone-1]",
                        " [Clone-2]",
                    ):
                        if role.endswith(suffix):
                            role = role[: -len(suffix)]
                    role = role.strip()

                    new_agent = await _find_idle_clone(db, role)
                    if new_agent and str(new_agent) != task.get("assigned_agent_id"):
                        body = (
                            f"{_NUDGE_TAG} **Auto-reassigned** to clone (DB id "
                            f"{str(new_agent)[:12]}) — original assignee unresponsive "
                            f"after {count} nudge(s). Stale {task['age_hours']}h."
                        )
                        if await _reassign_task(db, task["id"], new_agent, task["updated_at"], body):
                            stats["reassigned"] += 1
                        continue

                if count >= _MAX_NUDGES_BEFORE_ESCALATE:
                    # Pass severity='stale' — _escalate_to_owner_review will suppress
                    # if any open infra incident is currently filed.
                    if _escalate_to_owner_review(task, severity="stale"):
                        body = (
                            f"{_NUDGE_TAG} **Escalated to Owner Review** — "
                            f"{count} nudges sent without progress. "
                            f"Stale {task['age_hours']}h. See review queue."
                        )
                        await _post_comment(
                            db, task["id"], task["board_id"] or "", body
                        )
                        stats["escalated"] += 1
                    await db.rollback()
                    continue

                if latest_nudge_ts and (now_ts - latest_nudge_ts) < _NUDGE_COOLDOWN:
                    stats["skipped_cooldown"] += 1
                    await db.rollback()
                    continue

                assignee = task.get("agent_name") or "lead"
                msg = (
                    f"{_NUDGE_TAG} @{assignee}: task stuck in `{task['status']}` "
                    f"for {task['age_hours']}h. "
                    f"Reply with status update or transition to `review`/`blocked`. "
                    f"Per POLICY.md P10.{1 if task['status'] == 'in_progress' else 3}."
                )
                if await _post_comment(db, task["id"], task["board_id"] or "", msg):
                    stats["nudged"] += 1
            except Exception as e:
                logger.warning("watchdog.task_failed task=%s err=%s", task["id"], e)
    return stats


_REPEAT_RESET_TICK_SECONDS = int(
    os.environ.get("WATCHDOG_REPEAT_RESET_TICK_SECONDS", "30")
)


_ACTIVITY_PRUNE_TELEMETRY_DAYS = int(os.environ.get("ACTIVITY_PRUNE_TELEMETRY_DAYS", "3"))
_ACTIVITY_PRUNE_MARKER_DAYS = int(os.environ.get("ACTIVITY_PRUNE_MARKER_DAYS", "7"))
_last_activity_prune_ts: float = 0.0


async def _prune_activity_events() -> int:
    """Bound `activity_events` growth. High-volume telemetry (test.run,
    watchdog.tick, heartbeats, provision/wakeup) and old bracketed system
    markers accumulate to 100k+ rows and bloat the DB + activity feed. Keep a
    few days for debugging/liveness, drop the rest. Real task history
    (task.comment, task.status_changed, task.updated) is never pruned.
    Runs at most hourly (gated in `repeat_reset_loop`).
    """
    from app.db.session import async_session_maker
    deleted = 0
    async with async_session_maker() as db:
        r = await db.execute(text(f"""
            DELETE FROM activity_events WHERE id IN (
              SELECT id FROM activity_events
              WHERE (
                (event_type IN ('test.run','watchdog.tick','agent.heartbeat',
                                'agent.wakeup.sent','agent.provision.failed',
                                'agent.provision.direct')
                 AND created_at < NOW() - INTERVAL '{_ACTIVITY_PRUNE_TELEMETRY_DAYS} days')
                OR
                (event_type = 'task.comment' AND agent_id IS NULL
                 AND (message LIKE '[%' OR message LIKE '**[%')
                 AND created_at < NOW() - INTERVAL '{_ACTIVITY_PRUNE_MARKER_DAYS} days')
              )
              LIMIT 20000
            )
        """))
        deleted = r.rowcount or 0
        await db.commit()
    return deleted


async def repeat_reset_loop() -> None:
    """T66 (2026-05-15): dedicated fast-tick loop for `_process_interval_repeats`.

    The main `watchdog_loop` runs every 180s (or 600s when idle). That
    latency was acceptable for nudges + escalations, but felt slow for
    short-interval repeat tasks: a 5-min cycle could see 0-3 min lag past
    the interval boundary because the next tick wasn't due yet. This loop
    polls every `WATCHDOG_REPEAT_RESET_TICK_SECONDS` (default 30s) and
    ONLY calls `_process_interval_repeats`. Cheap — a single indexed
    SELECT followed by a per-row UPDATE.
    """
    if not _ENABLED:
        return
    logger.info(
        "watchdog.repeat_reset_loop.started tick=%ss",
        _REPEAT_RESET_TICK_SECONDS,
    )
    # Stagger so we don't fire simultaneously with the main tick.
    await asyncio.sleep(15)
    global _last_activity_prune_ts
    while True:
        try:
            n = await _process_interval_repeats()
            if n:
                logger.info("watchdog.repeat_reset_loop.repeated tasks=%d", n)
            # Hourly activity_events retention prune (piggybacks this cheap loop).
            nowp = time.perf_counter()
            if nowp - _last_activity_prune_ts > 3600:
                _last_activity_prune_ts = nowp
                pruned = await _prune_activity_events()
                if pruned:
                    logger.info("watchdog.activity_prune.deleted rows=%d", pruned)
        except asyncio.CancelledError:
            logger.info("watchdog.repeat_reset_loop.cancelled")
            raise
        except Exception as e:
            logger.warning("watchdog.repeat_reset_loop.failed err=%s", e)
        await asyncio.sleep(_REPEAT_RESET_TICK_SECONDS)


async def _heartbeat_only_loop() -> None:
    """Paused watchdog: keep the gateway-liveness heartbeat, take no task/agent action.

    The heartbeat is an observation that the host gateway watchdog and the hourly
    health report read. Pausing automation used to stop it too, so the report raised
    a false "critical" every hour and the gateway lost its restart guard (2026-10-06).
    """
    while True:
        try:
            alive = await asyncio.to_thread(_gateway_alive)
            await asyncio.to_thread(_write_gateway_heartbeat, alive)  # file I/O off the loop
        except Exception as exc:
            logger.debug("gateway_heartbeat.write_failed err=%s", exc)
        await asyncio.sleep(_INTERVAL_SECONDS)


async def watchdog_loop() -> None:
    """Long-running task — kicked off in app lifespan."""
    if not _ENABLED:
        logger.info("watchdog.disabled; gateway heartbeat continues")
        await _heartbeat_only_loop()
        return
    logger.info("watchdog.started interval=%ss idle_interval=%ss", _INTERVAL_SECONDS, _IDLE_INTERVAL_SECONDS)
    await asyncio.sleep(60)
    while True:
        try:
            stats = await _process_once()
            # Persist tick event so tests can verify watchdog liveness
            try:
                async with async_session_maker() as db:
                    tick_id = str(uuid.uuid4())
                    await db.execute(
                        text("INSERT INTO activity_events (id, event_type, agent_id, task_id, message, created_at) VALUES (:id, :type, NULL, NULL, :msg, NOW())"),
                        {"id": tick_id, "type": "watchdog.tick", "msg": json.dumps(stats)[:500]}
                    )
                    await db.commit()
            except Exception:
                pass
            if any(stats.values()):
                logger.info(
                    "watchdog.tick scanned=%d nudged=%d escalated=%d reassigned=%d reconciled=%d qa_bypass=%d token_burn=%d ballots_opened=%d ballots_closed=%d skipped_cooldown=%d harness_cleaned=%d tests_triggered=%d auto_approved=%d auto_closed=%d inbox_triaged=%d stuck_aborted=%d interval_repeated=%d cron_repeated=%d",
                    stats["scanned"],
                    stats["nudged"],
                    stats["escalated"],
                    stats["reassigned"],
                    stats["reconciled"],
                    stats["qa_bypass"],
                    stats["token_burn_throttled"],
                    stats["ballots_opened"],
                    stats["ballots_closed"],
                    stats["skipped_cooldown"],
                    stats["harness_cleaned"],
                    stats["tests_triggered"],
                    stats["auto_approved"],
                    stats["auto_closed"],
                    stats["inbox_triaged"],
                    stats["stuck_sessions_aborted"],
                    stats["interval_repeated"],
                    stats["cron_repeated"],
                )

            # ── Idle backoff: sleep longer when all tick stats are zero ──
            # No active tasks, no nudges, no escalations, no repeats — the
            # board is completely quiet. Don't waste DB/CPU cycles polling
            # every 3 min; check at the reduced idle interval instead.
            total_actions = (
                stats["nudged"] + stats["escalated"] + stats["reassigned"] +
                stats["inbox_triaged"] + stats["stuck_inbox_recovered"] +
                stats["freeform_promoted"] + stats["auto_closed"] +
                stats["auto_approved"] + stats["stuck_sessions_aborted"] + stats["rapid_heartbeat_aborted"] +
                stats["interval_repeated"] + stats["cron_repeated"] +
                stats["scheduled_activated"] + stats["scanned"]
            )
            if total_actions == 0:
                await asyncio.sleep(_idle_interval())
            else:
                await asyncio.sleep(_active_interval())
        except asyncio.CancelledError:
            logger.info("watchdog.cancelled")
            raise
        except Exception as e:
            logger.warning("watchdog.tick_failed err=%s", e)
            await asyncio.sleep(_active_interval())
