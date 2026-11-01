"""Pipeline watchdog — detect stale/stuck tasks, expose autonomy signals.

Readonly analysis. No side-effects. Frontend + Atlas consume the recommendations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.core.logging import get_logger
from app.db.session import get_session

logger = get_logger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))

# Thresholds (seconds). 2026-09-22: owner-editable from /settings/runtime —
# read per request, defaults are the values that used to be compiled in.
_STALE_IN_PROGRESS = 2 * 3600    # 2h without activity
_STALE_REVIEW      = 4 * 3600    # 4h without verdict
# Patch 1 (2026-07-16): mirror of WATCHDOG_UNBUDGETED_FLAG_HOURS — surfaces
# in_progress tasks with no wall-clock budget in the watchdog hazards summary.
_UNBUDGETED_HOURS = int(os.environ.get("WATCHDOG_UNBUDGETED_FLAG_HOURS", "24"))
_ATLAS_OFFLINE     = 1 * 3600    # 1h without LLM call = Atlas may be dead

# WIP limits per role (lead > worker > QA)
_WIP_LIMIT_LEAD   = 5
_WIP_LIMIT_WORKER = 2
_WIP_LIMIT_QA     = 3


def _knob(key: str, fallback: int) -> int:
    """Resolve an owner-editable /pipeline threshold, per request."""
    from app.services.runtime_settings import get_setting

    return int(get_setting(key, fallback))


def _atlas_offline() -> int:
    return _knob("PIPELINE_ATLAS_OFFLINE_SECONDS", _ATLAS_OFFLINE)


# Succession order if Atlas offline — agent name prefixes.
_SUCCESSION = ["Software Architect", "QA Engineer", "DevOps Engineer", "Security Architect"]


# ── traffic helpers ─────────────────────────────────────────────────────────

def _traffic_last_ts() -> dict[str, float]:
    if not _TRAFFIC_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
        rows = conn.execute(
            "SELECT agent_id, MAX(ts) FROM flows WHERE agent_id IS NOT NULL GROUP BY agent_id"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for agent_id, ts in rows:
        if not agent_id or not ts:
            continue
        try:
            out[agent_id] = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return out


def _iso_to_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ── config reader ──────────────────────────────────────────────────────────

async def _agent_name_to_db_id(db: AsyncSession) -> dict[str, str]:
    """Map agent name → DB UUID. Authoritative via agents table."""
    from sqlalchemy import text
    result = await db.execute(text("SELECT id, name FROM agents"))
    out: dict[str, str] = {}
    for row in result.fetchall():
        if row[1]:
            out[row[1]] = str(row[0])
    return out


# ── DB task reader ──────────────────────────────────────────────────────────

async def _fetch_tasks(db: AsyncSession) -> list[dict[str, Any]]:
    # Use direct SQL — avoids coupling to ORM model imports.
    from sqlalchemy import text
    result = await db.execute(text("""
        SELECT
            t.id, t.title, t.status, t.board_id, t.assigned_agent_id,
            t.created_at, t.updated_at,
            a.name AS agent_name,
            b.name AS board_name,
            t.max_seconds_budget, t.in_progress_at
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.assigned_agent_id
        LEFT JOIN boards b ON b.id = t.board_id
        WHERE t.status IN ('inbox','in_progress','review','blocked')
        ORDER BY t.updated_at DESC
        LIMIT 500
    """))
    rows = result.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": str(r[0]),
            "title": r[1],
            "status": r[2],
            "board_id": str(r[3]) if r[3] else None,
            "assigned_agent_id": str(r[4]) if r[4] else None,
            "created_at": r[5].isoformat() if r[5] else None,
            "updated_at": r[6].isoformat() if r[6] else None,
            "agent_name": r[7],
            "board_name": r[8],
            "max_seconds_budget": r[9],
            "in_progress_at": r[10].isoformat() if r[10] else None,
        })
    return out


# ── analysis ────────────────────────────────────────────────────────────────

def _analyze(tasks: list[dict[str, Any]], traffic_ts: dict[str, float]) -> dict[str, Any]:
    now = time.time()
    stale: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    wip_by_agent: dict[str, list[dict[str, Any]]] = {}
    review_queue: list[dict[str, Any]] = []
    unbudgeted_long_runners: list[dict[str, Any]] = []

    for t in tasks:
        updated = _iso_to_ts(t.get("updated_at"))
        age_sec = (now - updated) if updated else 0.0
        assignee = t.get("assigned_agent_id") or ""
        assignee_name = t.get("agent_name") or "UNASSIGNED"
        status = t.get("status")

        # Traffic freshness for assignee (informational only)
        last_traffic = traffic_ts.get(assignee)
        traffic_age = (now - last_traffic) if last_traffic else None

        # 1. Stale in_progress — use task age alone.
        #    Agent traffic ≠ task progress. Task has been in_progress X hours without
        #    comment or status change → it is stuck regardless of agent busy-ness.
        if status == "in_progress":
            if age_sec > _knob("PIPELINE_STALE_INPROGRESS_SECONDS", _STALE_IN_PROGRESS):
                stale.append({
                    **t,
                    "age_hours": round(age_sec / 3600, 1),
                    "assignee_traffic_age_hours": round(traffic_age / 3600, 1) if traffic_age else None,
                    "reason": f"in_progress {round(age_sec/3600, 1)}h without updates",
                    "recommended_action": (
                        "reassign to different worker or escalate to Owner Review"
                        if traffic_age and traffic_age < 3600
                        else "assignee may be offline — reassign"
                    ),
                })
            wip_by_agent.setdefault(assignee_name, []).append(t)
            # Patch 1 (2026-07-16): unbudgeted long runner — invisible to the
            # wall-clock budget enforcer (NULL/0 budget never triggers it).
            ip_ts = _iso_to_ts(t.get("in_progress_at"))
            if (
                not t.get("max_seconds_budget")
                and ip_ts
                and (now - ip_ts) > _UNBUDGETED_HOURS * 3600
            ):
                unbudgeted_long_runners.append({
                    **t,
                    "in_progress_hours": round((now - ip_ts) / 3600, 1),
                    "reason": (
                        f"in_progress {round((now - ip_ts) / 3600, 1)}h with no "
                        f"max_seconds_budget (>{_UNBUDGETED_HOURS}h threshold)"
                    ),
                })

        # 2. Stale review
        if status == "review":
            review_queue.append({**t, "age_hours": round(age_sec / 3600, 1)})
            if age_sec > _knob("PIPELINE_STALE_REVIEW_SECONDS", _STALE_REVIEW):
                stale.append({
                    **t,
                    "age_hours": round(age_sec / 3600, 1),
                    "reason": "review without verdict",
                    "recommended_action": "QA verdict or lead closure",
                })

        # 3. Unassigned inbox
        if status == "inbox" and not assignee:
            unassigned.append({**t, "age_hours": round(age_sec / 3600, 1)})

    # 4. WIP overloads
    wip_violations = []
    for name, items in wip_by_agent.items():
        limit = _knob("PIPELINE_WIP_LIMIT_WORKER", _WIP_LIMIT_WORKER)
        if name.lower() == "atlas":
            limit = _knob("PIPELINE_WIP_LIMIT_LEAD", _WIP_LIMIT_LEAD)
        elif "QA" in name:
            limit = _knob("PIPELINE_WIP_LIMIT_QA", _WIP_LIMIT_QA)
        if len(items) > limit:
            wip_violations.append({
                "agent_name": name,
                "wip_count": len(items),
                "limit": limit,
                "task_ids": [i["id"] for i in items],
            })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stale_tasks": stale,
        "unassigned_inbox": unassigned,
        "review_queue": review_queue,
        "wip_violations": wip_violations,
        "wip_by_agent": {n: len(v) for n, v in wip_by_agent.items()},
        "unbudgeted_long_runners": unbudgeted_long_runners,
        "summary": {
            "stale_count": len(stale),
            "unassigned_count": len(unassigned),
            "review_count": len(review_queue),
            "wip_violation_count": len(wip_violations),
            "unbudgeted_long_runner_count": len(unbudgeted_long_runners),
        },
    }


def _acting_lead_impl(traffic_ts: dict[str, float], name_to_id: dict[str, str]) -> dict[str, Any]:
    now = time.time()

    atlas_id = name_to_id.get("Atlas")
    atlas_last = traffic_ts.get(atlas_id) if atlas_id else None
    atlas_age = (now - atlas_last) if atlas_last else None

    if atlas_last and atlas_age is not None and atlas_age < _atlas_offline():
        return {
            "acting_lead": "Atlas",
            "agent_id": atlas_id,
            "reason": "Atlas traffic fresh",
            "atlas_age_seconds": int(atlas_age),
        }

    # Succession
    for candidate in _SUCCESSION:
        cid = name_to_id.get(candidate)
        if not cid:
            continue
        last = traffic_ts.get(cid)
        if last and (now - last) < _atlas_offline():
            return {
                "acting_lead": candidate,
                "agent_id": cid,
                "reason": f"Atlas stale >{_atlas_offline() // 3600}h, next-fresh successor",
                "atlas_age_seconds": int(atlas_age) if atlas_age else None,
            }

    return {
        "acting_lead": None,
        "agent_id": None,
        "reason": "no fresh candidate in succession",
        "atlas_age_seconds": int(atlas_age) if atlas_age else None,
    }


# ── endpoints ───────────────────────────────────────────────────────────────

_POLICY_FILE = _OPENCLAW_HOME / "POLICY.md"


@router.get(
    "/policy",
    summary="Get current operating policy",
    dependencies=[Depends(require_user_or_agent)],
)
async def get_policy() -> dict[str, Any]:
    if not _POLICY_FILE.exists():
        return {"version": None, "content": "", "exists": False}
    content = _POLICY_FILE.read_text()
    version = ""
    for line in content.splitlines()[:20]:
        if line.lower().startswith("version:"):
            version = line.split(":", 1)[1].strip()
            break
    return {
        "version": version,
        "content": content,
        "exists": True,
        "path": str(_POLICY_FILE),
        "size": len(content),
    }


@router.get(
    "/watchdog",
    summary="Detect stale, stuck, unassigned, and over-WIP tasks",
    dependencies=[Depends(require_user_or_agent)],
)
async def watchdog(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    tasks = await _fetch_tasks(db)
    traffic_ts = _traffic_last_ts()
    return _analyze(tasks, traffic_ts)


@router.get(
    "/acting_lead",
    summary="Who is acting lead right now (Atlas or successor)",
    dependencies=[Depends(require_user_or_agent)],
)
async def acting_lead(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    traffic_ts = _traffic_last_ts()
    name_to_id = await _agent_name_to_db_id(db)
    return _acting_lead_impl(traffic_ts, name_to_id)


@router.get(
    "/scorecard",
    summary="Per-agent activity scorecard (LLM calls, errors, token burn)",
    dependencies=[Depends(require_user_or_agent)],
)
async def scorecard(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    out: list[dict[str, Any]] = []

    # 1. Traffic-derived stats. NOTE: only flows that pass through MC's
    # proxies (vllm + generic) are captured here. Agents whose primary
    # talks directly to a cloud-tier provider (e.g. an ollama-cloud or
    # gemini fallback) without going through proxy_generic produce 0
    # captured flows — see #2 below.
    by_name: dict[str, dict[str, Any]] = {}
    if _TRAFFIC_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
            rows = conn.execute("""
                SELECT
                    agent_name, agent_id,
                    COUNT(*) AS calls,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    ROUND(AVG(latency_ms)) AS avg_latency_ms,
                    SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS errors,
                    MAX(ts) AS last_ts,
                    MIN(ts) AS first_ts
                FROM flows
                WHERE agent_name IS NOT NULL
                GROUP BY agent_name
            """).fetchall()
            conn.close()
            for r in rows:
                by_name[r[0]] = {
                    "agent_name": r[0],
                    "agent_id": r[1],
                    "calls": r[2],
                    "total_tokens": r[3],
                    "avg_latency_ms": int(r[4]) if r[4] else None,
                    "errors": r[5],
                    "error_rate_pct": round(100 * r[5] / r[2], 2) if r[2] else 0.0,
                    "last_ts": r[6],
                    "first_ts": r[7],
                    "source": "traffic",
                }
        except Exception:
            logger.exception("scorecard traffic query failed")
            raise HTTPException(status_code=500, detail="Internal error")

    # 2. DB heartbeat fallback for agents without captured traffic (any
    # cloud-tier agent whose flows bypass MC's proxies).
    from sqlalchemy import text
    result = await db.execute(text("""
        SELECT name, last_seen_at,
               COALESCE(identity_profile->>'model', '') AS model
        FROM agents WHERE last_seen_at IS NOT NULL
    """))
    for row in result.fetchall():
        name = row[0]
        if name in by_name:
            continue  # already counted from traffic
        by_name[name] = {
            "agent_name": name,
            "agent_id": None,
            "calls": 0,
            "total_tokens": 0,
            "avg_latency_ms": None,
            "errors": 0,
            "error_rate_pct": 0.0,
            "last_ts": row[1].isoformat() if row[1] else None,
            "first_ts": None,
            "source": "db-heartbeat",
            "model": row[2],
        }

    out = sorted(by_name.values(), key=lambda x: x["total_tokens"], reverse=True)
    return {"agents": out, "total_agents": len(out)}
