"""Voting API — multi-agent ballot with veto roles.

Lightweight SQLite store at /traffic-data/votes.db (avoids alembic migration churn).
Roles with veto power: owner (absolute), Security Architect (security topics),
QA Engineer (review→done quality gate).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user_or_agent

router = APIRouter(prefix="/votes", tags=["votes"])

_DB_FILE = Path(os.environ.get("VOTES_DB_FILE", "/traffic-data/votes.db"))
_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Veto roles — case-insensitive name match
VETO_ROLES = {
    "owner",
    "security architect",
    "qa engineer",
}

_DDL = """
CREATE TABLE IF NOT EXISTS ballots (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    description     TEXT,
    proposed_action TEXT NOT NULL,
    target_task_id  TEXT,
    target_board_id TEXT,
    required_quorum INTEGER NOT NULL DEFAULT 2,
    veto_roles      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    outcome         TEXT,
    outcome_reason  TEXT,
    created_by      TEXT,
    created_at      TEXT NOT NULL,
    closed_at       TEXT,
    expires_at      TEXT
);
CREATE TABLE IF NOT EXISTS votes (
    ballot_id   TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    reason      TEXT,
    is_veto     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (ballot_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_votes_ballot ON votes(ballot_id);
CREATE INDEX IF NOT EXISTS idx_ballots_status ON ballots(status);
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _db_lock:
            if _conn is None:
                _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
                _conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                for stmt in _DDL.strip().split(";"):
                    s = stmt.strip()
                    if s:
                        _conn.execute(s)
                _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── models ─────────────────────────────────────────────────────────────────


class CreateBallot(BaseModel):
    topic: str
    description: str | None = None
    proposed_action: str
    target_task_id: str | None = None
    target_board_id: str | None = None
    required_quorum: int = 2
    veto_roles: list[str] = []
    created_by: str = "agent"
    expires_in_seconds: int | None = 86400  # T86: default 24h expiry prevents stale ballots; tiebreaker test requires max age <= 24h


class CastVote(BaseModel):
    agent_name: str
    verdict: Literal["approve", "reject", "abstain"]
    reason: str | None = None


class OwnerVeto(BaseModel):
    reason: str
    actor: str = "owner"


# ── helpers ────────────────────────────────────────────────────────────────


def _ballot_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["veto_roles"] = [r for r in (d.get("veto_roles") or "").split(",") if r]
    return d


def _tally(conn: sqlite3.Connection, ballot_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT verdict, COUNT(*) FROM votes WHERE ballot_id=? GROUP BY verdict",
        (ballot_id,),
    ).fetchall()
    counts = {"approve": 0, "reject": 0, "abstain": 0}
    for r in rows:
        counts[r[0]] = r[1]
    total = sum(counts.values())
    voters = [
        dict(r)
        for r in conn.execute(
            "SELECT agent_name, verdict, reason, is_veto, created_at "
            "FROM votes WHERE ballot_id=? ORDER BY created_at",
            (ballot_id,),
        ).fetchall()
    ]
    return {**counts, "total": total, "voters": voters}


def _evaluate_and_close(
    conn: sqlite3.Connection, ballot_id: str
) -> dict[str, Any] | None:
    """Check if ballot can be closed (veto, quorum reached, expired). Returns updated row dict or None."""
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
    if not row or row["status"] != "open":
        return _ballot_to_dict(row) if row else None

    # Veto check — any vote with is_veto=1 and verdict='reject' kills ballot
    veto_row = conn.execute(
        "SELECT agent_name, reason FROM votes WHERE ballot_id=? AND is_veto=1 AND verdict='reject' LIMIT 1",
        (ballot_id,),
    ).fetchone()
    if veto_row:
        with _db_lock:
            conn.execute(
                "UPDATE ballots SET status='closed', outcome='vetoed', "
                "outcome_reason=?, closed_at=? WHERE id=?",
                (
                    f"Vetoed by {veto_row['agent_name']}: {veto_row['reason'] or 'no reason'}",
                    _now(),
                    ballot_id,
                ),
            )
            conn.commit()
        return _ballot_to_dict(
            conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        )

    # Expiration
    if row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"]).timestamp()
            if time.time() > exp:
                with _db_lock:
                    conn.execute(
                        "UPDATE ballots SET status='closed', outcome='expired', closed_at=? WHERE id=?",
                        (_now(), ballot_id),
                    )
                    conn.commit()
                return _ballot_to_dict(
                    conn.execute(
                        "SELECT * FROM ballots WHERE id=?", (ballot_id,)
                    ).fetchone()
                )
        except Exception:
            pass

    # Quorum check
    tally = _tally(conn, ballot_id)
    if tally["total"] >= row["required_quorum"]:
        if tally["approve"] > tally["reject"]:
            outcome, reason = (
                "approved",
                f"{tally['approve']} approve / {tally['reject']} reject",
            )
        elif tally["reject"] > tally["approve"]:
            outcome, reason = (
                "rejected",
                f"{tally['reject']} reject / {tally['approve']} approve",
            )
        else:
            return _ballot_to_dict(row)  # tie — wait for tiebreaker
        with _db_lock:
            conn.execute(
                "UPDATE ballots SET status='closed', outcome=?, outcome_reason=?, closed_at=? WHERE id=?",
                (outcome, reason, _now(), ballot_id),
            )
            conn.commit()
        return _ballot_to_dict(
            conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
        )

    return _ballot_to_dict(row)


# ── endpoints ──────────────────────────────────────────────────────────────


@router.get(
    "",
    summary="List ballots",
    dependencies=[Depends(require_user_or_agent)],
)
async def list_ballots(
    status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    conn = _db()
    if status:
        rows = conn.execute(
            "SELECT * FROM ballots WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ballots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        b = _ballot_to_dict(r)
        b["tally"] = _tally(conn, b["id"])
        out.append(b)
    return out


@router.post(
    "",
    summary="Create a new ballot",
    dependencies=[Depends(require_user_or_agent)],
)
async def create_ballot(payload: CreateBallot) -> dict[str, Any]:
    conn = _db()
    bid = str(uuid.uuid4())
    expires_at = None
    if payload.expires_in_seconds:
        expires_at = datetime.fromtimestamp(
            time.time() + payload.expires_in_seconds, tz=timezone.utc
        ).isoformat()

    # Auto-inject Security Architect veto for security-related topics
    veto_roles = [r.lower() for r in payload.veto_roles]
    if any(
        kw in (payload.topic or "").lower()
        for kw in ("auth", "secret", "token", "security", "password")
    ):
        if "security architect" not in veto_roles:
            veto_roles.append("security architect")

    with _db_lock:
        conn.execute(
            """INSERT INTO ballots
            (id, topic, description, proposed_action, target_task_id, target_board_id,
             required_quorum, veto_roles, status, created_by, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bid,
                payload.topic,
                payload.description,
                payload.proposed_action,
                payload.target_task_id,
                payload.target_board_id,
                payload.required_quorum,
                ",".join(veto_roles),
                "open",
                payload.created_by,
                _now(),
                expires_at,
            ),
        )
        conn.commit()
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (bid,)).fetchone()
    return _ballot_to_dict(row)


@router.get(
    "/{ballot_id}",
    summary="Get ballot detail with tally",
    dependencies=[Depends(require_user_or_agent)],
)
async def get_ballot(ballot_id: str) -> dict[str, Any]:
    conn = _db()
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "ballot not found")
    b = _ballot_to_dict(row)
    b["tally"] = _tally(conn, ballot_id)
    return b


@router.post(
    "/{ballot_id}/cast",
    summary="Cast a vote on a ballot",
    dependencies=[Depends(require_user_or_agent)],
)
async def cast_vote(ballot_id: str, payload: CastVote) -> dict[str, Any]:
    conn = _db()
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "ballot not found")
    if row["status"] != "open":
        raise HTTPException(409, f"ballot already closed: {row['outcome']}")

    veto_roles_csv = (row["veto_roles"] or "").lower()
    is_veto = payload.agent_name.lower() in veto_roles_csv or any(
        role in veto_roles_csv
        for role in VETO_ROLES
        if role in payload.agent_name.lower()
    )

    with _db_lock:
        # Upsert (agent can change vote until ballot closes)
        conn.execute(
            """INSERT INTO votes (ballot_id, agent_name, verdict, reason, is_veto, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(ballot_id, agent_name) DO UPDATE SET
                   verdict=excluded.verdict,
                   reason=excluded.reason,
                   is_veto=excluded.is_veto,
                   created_at=excluded.created_at""",
            (
                ballot_id,
                payload.agent_name,
                payload.verdict,
                payload.reason,
                1 if is_veto else 0,
                _now(),
            ),
        )
        conn.commit()

    updated = _evaluate_and_close(conn, ballot_id)
    if updated:
        updated["tally"] = _tally(conn, ballot_id)
    return updated or {}


@router.post(
    "/{ballot_id}/owner_veto",
    summary="Owner absolute veto — close ballot immediately",
    dependencies=[Depends(require_user_or_agent)],
)
async def owner_veto(ballot_id: str, payload: OwnerVeto) -> dict[str, Any]:
    conn = _db()
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "ballot not found")
    with _db_lock:
        conn.execute(
            "UPDATE ballots SET status='closed', outcome='vetoed', "
            "outcome_reason=?, closed_at=? WHERE id=?",
            (f"Owner veto: {payload.reason}", _now(), ballot_id),
        )
        conn.execute(
            """INSERT INTO votes (ballot_id, agent_name, verdict, reason, is_veto, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(ballot_id, agent_name) DO UPDATE SET
                   verdict=excluded.verdict, reason=excluded.reason,
                   is_veto=excluded.is_veto, created_at=excluded.created_at""",
            (ballot_id, payload.actor, "reject", payload.reason, 1, _now()),
        )
        conn.commit()
    row = conn.execute("SELECT * FROM ballots WHERE id=?", (ballot_id,)).fetchone()
    out = _ballot_to_dict(row)
    out["tally"] = _tally(conn, ballot_id)
    return out
