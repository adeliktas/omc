"""LLM traffic monitoring — SQLite-backed for fast queries and durable storage."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from uuid import UUID
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.api.deps import require_org_member, require_user_or_agent
from app.db.session import get_session
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle, TeamMember
from app.services.organizations import OrganizationContext, list_accessible_board_ids
from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/traffic", tags=["traffic"])


@router.get("/native-attempts")
async def native_attempts(
    board_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    ctx: OrganizationContext = Depends(require_org_member),
) -> list[dict[str, Any]]:
    """Task-bound harness attempts, NOT captured HTTP flows or billed usage."""
    from fastapi import HTTPException

    boards = await list_accessible_board_ids(session, member=ctx.member, write=False)
    if board_id is not None:
        if board_id not in boards:
            raise HTTPException(403)
        boards = [board_id]
    statement = (
        select(TeamAttempt, Task, TeamMember)
        .join(Task, col(TeamAttempt.task_id) == col(Task.id))
        .join(TeamMember, col(TeamAttempt.member_id) == col(TeamMember.id))
        .join(TeamCycle, col(TeamAttempt.cycle_id) == col(TeamCycle.id))
        .where(col(TeamAttempt.board_id).in_(boards),
               col(Task.board_id) == col(TeamAttempt.board_id),
               col(TeamMember.board_id) == col(TeamAttempt.board_id),
               col(TeamCycle.board_id) == col(TeamAttempt.board_id),
               col(TeamCycle.task_id) == col(TeamAttempt.task_id))
    )
    changed_at = func.coalesce(TeamAttempt.finished_at, TeamAttempt.updated_at)
    if since is not None:
        cutoff = since.astimezone(timezone.utc).replace(tzinfo=None) if since.tzinfo else since
        statement = statement.where(changed_at >= cutoff)
    rows = (await session.exec(statement.order_by(changed_at.desc()).limit(limit))).all()
    result = []
    for attempt, task, member in rows:
        try:
            tools = json.loads(attempt.assignment or "{}").get("tool_counts")
        except (ValueError, AttributeError):
            tools = None
        if not isinstance(tools, dict) or not tools or any(
            name not in ("read", "bash", "edit", "write", "grep", "find", "ls")
            or type(count) is not int or not 0 <= count <= 100000
            for name, count in tools.items()
        ):
            tools = None
        result.append({
            "attempt_id": str(attempt.id), "cycle_id": str(attempt.cycle_id),
            "task_id": str(task.id), "board_id": str(task.board_id), "task_title": task.title,
            "member_id": str(attempt.member_id), "member_name": member.name,
            "role_kind": attempt.role_kind, "stage_key": attempt.stage_key,
            "provider": attempt.provider, "model": attempt.model, "status": attempt.status,
            "started_at": attempt.started_at, "finished_at": attempt.finished_at,
            "usage_reported": attempt.usage_reported, "total_tokens": attempt.total_tokens,
            "tool_counts": tools,
        })
    return result

_DB_FILE = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
_LEGACY_FILE = Path(os.environ.get("TRAFFIC_HISTORY_FILE", "/traffic-data/llm_flows.jsonl.gz"))
_MAX_ROWS = int(os.environ.get("TRAFFIC_MAX_ROWS", "50000"))
_STATS_TTL = int(os.environ.get("TRAFFIC_STATS_TTL", "15"))  # cache stats for 15s

# ponytail: one global RLock serializes every reader and writer on the shared
# sqlite connection (readers used to run unlocked and could see a mid-write
# state, e.g. COUNT(*) returning no row). Fine at this traffic volume; move to
# per-request connections (sqlite handles concurrent readers fine under WAL)
# if this lock becomes a throughput ceiling.
_db_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_last_ingest_ts: float = 0.0  # monotonic seconds
_ingest_count_since_check: int = 0  # amortize trim COUNT(*)

# ── Cache for traffic_stats response ──
_stats_cache: dict[str, Any] = {}
_stats_ts: float = 0.0


# ── schema ─────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS flows (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    host             TEXT,
    path             TEXT,
    method           TEXT,
    model            TEXT,
    status_code      INTEGER,
    latency_ms       INTEGER,
    agent_name       TEXT,
    agent_id         TEXT,
    board_id         TEXT,
    messages_count   INTEGER DEFAULT 0,
    has_system       INTEGER DEFAULT 0,
    has_tools        INTEGER DEFAULT 0,
    tools_count      INTEGER DEFAULT 0,
    stream           INTEGER DEFAULT 0,
    temperature      REAL,
    max_tokens       INTEGER,
    last_user_message TEXT DEFAULT '',
    finish_reason    TEXT  DEFAULT '',
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    total_tokens     INTEGER,
    content_preview  TEXT  DEFAULT '',
    reasoning_preview TEXT DEFAULT '',
    has_tool_calls   INTEGER DEFAULT 0,
    tool_calls_count INTEGER DEFAULT 0,
    req_body         TEXT,
    res_body         TEXT
);
CREATE INDEX IF NOT EXISTS idx_flows_ts    ON flows(ts);
CREATE INDEX IF NOT EXISTS idx_flows_agent ON flows(agent_name);
CREATE INDEX IF NOT EXISTS idx_flows_model ON flows(model);
CREATE INDEX IF NOT EXISTS idx_flows_latency
    ON flows(latency_ms) WHERE latency_ms IS NOT NULL;
"""


def _open_db() -> sqlite3.Connection:
    _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-24000")   # 24 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    # T23d (2026-05-14): incremental auto-vacuum reclaims pages from deleted
    # flows without rewriting the entire DB. SQLite docs: must be set before
    # first table creation OR followed by `VACUUM` to take effect. Safe to
    # repeat — pragma is idempotent.
    try:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    except Exception:
        pass
    # Only set mmap_size if file is writable (avoids readonly volume issues)
    try:
        conn.execute("PRAGMA mmap_size=268435456")  # 256 MB memory-mapped I/O
    except Exception:
        pass
    for stmt in _DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                # Table/index may already exist or DB is read-only — skip
                pass
    conn.commit()
    return conn


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _db_lock:
            if _conn is None:
                _conn = _open_db()
                _migrate_legacy()
    return _conn


# ── legacy migration ────────────────────────────────────────────────────────

def _migrate_legacy() -> None:
    if not _LEGACY_FILE.exists():
        return
    try:
        rows: list[dict[str, Any]] = []
        with gzip.open(_LEGACY_FILE, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if rows:
            with _db_lock:
                _insert_many(_conn, rows)  # type: ignore[arg-type]
        _LEGACY_FILE.rename(_LEGACY_FILE.with_suffix(".gz.migrated"))
    except Exception:
        pass


# ── insert helpers ──────────────────────────────────────────────────────────

_COLS = (
    "ts", "host", "path", "method", "model", "status_code", "latency_ms",
    "agent_name", "agent_id", "board_id", "messages_count", "has_system",
    "has_tools", "tools_count", "stream", "temperature", "max_tokens",
    "last_user_message", "finish_reason", "prompt_tokens", "completion_tokens",
    "total_tokens", "content_preview", "reasoning_preview", "has_tool_calls",
    "tool_calls_count", "req_body", "res_body",
)

_INSERT = f"INSERT INTO flows ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})"


def _row_values(flow: dict[str, Any]) -> tuple:
    return tuple(
        json.dumps(flow[c], ensure_ascii=False) if c in ("req_body", "res_body") and isinstance(flow.get(c), dict)
        else flow.get(c)
        for c in _COLS
    )


def _insert_many(conn: sqlite3.Connection, flows: list[dict[str, Any]]) -> None:
    conn.executemany(_INSERT, [_row_values(f) for f in flows])
    conn.commit()


def _trim_if_needed(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
    if count > _MAX_ROWS + 1000:
        conn.execute(
            "DELETE FROM flows WHERE id IN "
            "(SELECT id FROM flows ORDER BY ts ASC LIMIT ?)",
            (count - _MAX_ROWS,),
        )
        conn.commit()


# ── row → dict ──────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col in ("req_body", "res_body"):
        v = d.get(col)
        if isinstance(v, str):
            try:
                d[col] = json.loads(v)
            except Exception:
                pass
    for col in ("has_system", "has_tools", "stream", "has_tool_calls"):
        if col in d:
            d[col] = bool(d[col])
    return d


# ── FastAPI models ──────────────────────────────────────────────────────────

class IngestPayload(BaseModel):
    ts: str
    host: str
    path: str
    method: str
    model: str
    status_code: int
    latency_ms: int | None = None
    agent_name: str | None = None
    agent_id: str | None = None
    board_id: str | None = None
    messages_count: int = 0
    has_system: bool = False
    has_tools: bool = False
    tools_count: int = 0
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    last_user_message: str = ""
    finish_reason: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    content_preview: str = ""
    reasoning_preview: str = ""
    has_tool_calls: bool = False
    tool_calls_count: int = 0
    req_body: dict[str, Any] | None = None
    res_body: dict[str, Any] | None = None


# ── helper: compute WHERE clause ────────────────────────────────────────────

# ── endpoints ───────────────────────────────────────────────────────────────

@router.post(
    "/ingest",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ingest flow from mitmproxy addon",
    include_in_schema=False,
)
def ingest(payload: IngestPayload) -> None:
    global _last_ingest_ts, _ingest_count_since_check
    flow = payload.model_dump()
    conn = _db()
    with _db_lock:
        conn.execute(_INSERT, _row_values(flow))
        conn.commit()
        _last_ingest_ts = time.monotonic()
        _ingest_count_since_check += 1
        # Don't clobber stats cache — let TTL expire naturally
    # Amortize trim check: only every 500 ingests
    if _ingest_count_since_check >= 500:
        with _db_lock:
            _ingest_count_since_check = 0
            count = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
            if count > _MAX_ROWS + 1000:
                _trim_if_needed(conn)


@router.get(
    "/flows",
    summary="List captured LLM flows",
    dependencies=[Depends(require_user_or_agent)],
)
def list_flows(
    limit: int = Query(default=500, ge=1, le=5000),
    since: str | None = None,
    after: str | None = None,
    agent_name: str | None = None,
    model: str | None = None,
    board_id: str | None = None,
) -> list[Any]:
    """List flows with optimized queries using index scans.

    T27a (2026-05-14): added `?agent_name=`, `?model=`, `?board_id=` filters
    so the UI can narrow the flow stream without client-side scanning.
    All filters combine with AND. Empty values are ignored.
    """
    conn = _db()
    cols = (
        "id, ts, host, path, method, model, status_code, latency_ms, "
        "agent_name, agent_id, board_id, messages_count, has_system, has_tools, "
        "tools_count, stream, temperature, max_tokens, last_user_message, "
        "finish_reason, prompt_tokens, completion_tokens, total_tokens, "
        "content_preview, reasoning_preview, has_tool_calls, tool_calls_count"
    )
    where: list[str] = []
    params: list[Any] = []
    if after:
        where.append("ts > ?")
        params.append(after)
    elif since:
        where.append("ts >= ?")
        params.append(since)
    if agent_name:
        where.append("agent_name = ?")
        params.append(agent_name)
    if model:
        where.append("model = ?")
        params.append(model)
    if board_id:
        where.append("board_id = ?")
        params.append(board_id)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT {cols} FROM flows{where_clause} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _db_lock:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get(
    "/stats",
    summary="Aggregate traffic statistics",
    dependencies=[Depends(require_user_or_agent)],
)
def traffic_stats(since: str | None = None) -> dict[str, Any]:
    """Aggregate traffic stats with SQL-native P50 and 10s cache."""
    global _stats_cache, _stats_ts

    cache_key = f"stats:{since or 'all'}"
    now = time.monotonic()
    if _stats_cache and cache_key in _stats_cache and (now - _stats_ts) < _STATS_TTL:
        return _stats_cache[cache_key]

    conn = _db()
    where_clause = "WHERE ts >= ?" if since else ""
    and_ts = "AND ts >= ?" if since else ""
    params: tuple = (since,) if since else ()

    with _db_lock:
        # ── Aggregate stats ──────────────────────────────────────────────
        agg = conn.execute(
            f"""
            SELECT
                COUNT(*)                                             AS flow_count,
                COALESCE(SUM(total_tokens), 0)                      AS total_tokens,
                ROUND(AVG(latency_ms))                              AS avg_latency_ms,
                MAX(ts)                                             AS last_ts,
                SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS errors
            FROM flows {where_clause}
            """,
            params,
        ).fetchone()

        # ── P50 latency — SQL-native percentile via window function ─────
        # Avoids loading all latency values into Python memory
        p50_sql = f"""
            WITH filtered AS (
                SELECT latency_ms
                FROM flows
                WHERE latency_ms IS NOT NULL
                {and_ts}
            ),
            numbered AS (
                SELECT latency_ms,
                       ROW_NUMBER() OVER (ORDER BY latency_ms ASC) as rn,
                       COUNT(*) OVER () as cnt
                FROM filtered
            )
            SELECT latency_ms FROM numbered
            WHERE rn = (cnt + 1) / 2 AND cnt > 0
            LIMIT 1
        """
        p50_row = conn.execute(p50_sql, params).fetchone()
        p50 = p50_row[0] if p50_row else None

        # ── Total count (all time, for "of X total") ────────────────────
        all_count_row = conn.execute("SELECT COUNT(*) FROM flows").fetchone()
        all_count = all_count_row[0] if all_count_row else 0

        # ── Per-agent breakdown ──────────────────────────────────────────
        agent_rows = conn.execute(
            f"""
            SELECT
                agent_name,
                agent_id,
                board_id,
                COUNT(*)                                             AS flow_count,
                COALESCE(SUM(total_tokens), 0)                      AS total_tokens,
                SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
                ROUND(AVG(latency_ms))                              AS avg_latency_ms
            FROM flows {where_clause}
            GROUP BY agent_name
            ORDER BY flow_count DESC
            """,
            params,
        ).fetchall()

        # ── P99 latency ───────────────────────────────────────────────────
        p99_sql = f"""
            WITH filtered AS (
                SELECT latency_ms
                FROM flows
                WHERE latency_ms IS NOT NULL
                {and_ts}
            ),
            numbered AS (
                SELECT latency_ms,
                       ROW_NUMBER() OVER (ORDER BY latency_ms ASC) as rn,
                       COUNT(*) OVER () as cnt
                FROM filtered
            )
            SELECT latency_ms FROM numbered
            WHERE rn = cnt * 99 / 100 AND cnt > 0
            LIMIT 1
        """
        p99_row = conn.execute(p99_sql, params).fetchone()
        p99 = p99_row[0] if p99_row else None

    running = (time.monotonic() - _last_ingest_ts) < 120 if _last_ingest_ts else False

    result = {
        "flow_count": agg["flow_count"],
        "all_flow_count": all_count,
        "total_tokens": agg["total_tokens"] or 0,
        "avg_latency_ms": int(agg["avg_latency_ms"]) if agg["avg_latency_ms"] else None,
        "p50_latency_ms": p50,
        "p99_latency_ms": p99,
        "errors": agg["errors"] or 0,
        "running": running,
        "last_ts": agg["last_ts"],
        "agents": [dict(r) for r in agent_rows],
    }

    _stats_cache[cache_key] = result
    _stats_ts = now
    return result


@router.get(
    "/flows/{flow_id}",
    summary="Get full flow detail incl. req/res bodies",
    dependencies=[Depends(require_user_or_agent)],
)
def get_flow(flow_id: int) -> dict[str, Any]:
    conn = _db()
    with _db_lock:
        row = conn.execute(
            "SELECT * FROM flows WHERE id = ?", (flow_id,)
        ).fetchone()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="flow not found")
    return _row_to_dict(row)


@router.get(
    "/health",
    summary="Traffic system health check",
    include_in_schema=False,
)
def traffic_health() -> dict[str, Any]:
    """Check if traffic ingestion is active and data exists.

    T111 (2026-09-16): also returns a 24h `errors` breakdown (status-code
    counts, top error messages, per-provider error rates) for the /traffic
    health strip.
    """
    conn = _db()
    with _db_lock:
        count_row = conn.execute("SELECT COUNT(*) FROM flows").fetchone()
        latest = conn.execute("SELECT ts FROM flows ORDER BY ts DESC LIMIT 1").fetchone()
    count = count_row[0] if count_row else 0
    out: dict[str, Any] = {
        "enabled": count > 0,
        "flow_count": count,
        "latest_ts": latest[0] if latest else None,
        "stale_seconds": max(0, int(time.time() - datetime.fromisoformat(latest[0]).timestamp()))
        if latest else None,
    }
    try:
        from app.services.provider_failure_gate import error_breakdown
        out["errors"] = error_breakdown()
    except Exception:
        pass  # breakdown is best-effort; base health must stay reliable
    return out
