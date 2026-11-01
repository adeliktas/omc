"""Knowledge Base — FTS5-backed durable store of agent decisions and retros.

Agents POST entries on closure; query before complex actions.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user_or_agent

router = APIRouter(prefix="/kb", tags=["kb"])

_DB_FILE = Path(os.environ.get("KB_DB_FILE", "/traffic-data/kb.db"))
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_DDL = """
CREATE TABLE IF NOT EXISTS entries (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    decision      TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    related_task_id TEXT,
    related_board_id TEXT,
    submitted_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    topic, decision, rationale, tags,
    content='entries', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, topic, decision, rationale, tags)
    VALUES (new.rowid, new.topic, new.decision, new.rationale, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, topic, decision, rationale, tags)
    VALUES ('delete', old.rowid, old.topic, old.decision, old.rationale, old.tags);
END;
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
                _conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                # executescript handles BEGIN..END; trigger blocks correctly
                _conn.executescript(_DDL)
                _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── models ──

class CreateEntry(BaseModel):
    topic: str
    decision: str
    rationale: str
    tags: list[str] = []
    related_task_id: str | None = None
    related_board_id: str | None = None
    submitted_by: str = "agent"


# ── endpoints ──

@router.post(
    "/entries",
    summary="Add a KB entry (decision + rationale + tags)",
    dependencies=[Depends(require_user_or_agent)],
)
async def create_entry(payload: CreateEntry) -> dict[str, Any]:
    conn = _db()
    eid = str(uuid.uuid4())
    tags_str = ",".join(payload.tags)
    with _lock:
        conn.execute("""
            INSERT INTO entries (id, topic, decision, rationale, tags,
                                 related_task_id, related_board_id, submitted_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            eid, payload.topic, payload.decision, payload.rationale, tags_str,
            payload.related_task_id, payload.related_board_id, payload.submitted_by, _now(),
        ))
        conn.commit()
    return {"id": eid, "created_at": _now()}


@router.get(
    "/entries",
    summary="List KB entries (most recent first)",
    dependencies=[Depends(require_user_or_agent)],
)
async def list_entries(limit: int = 20) -> list[dict[str, Any]]:
    conn = _db()
    rows = conn.execute(
        "SELECT id, topic, decision, rationale, tags, related_task_id, related_board_id, "
        "submitted_by, created_at FROM entries ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        out.append(d)
    return out


@router.get(
    "/entries/{entry_id}",
    summary="Get a single KB entry by id",
    dependencies=[Depends(require_user_or_agent)],
)
async def get_entry(entry_id: str) -> dict[str, Any]:
    conn = _db()
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "entry not found")
    d = dict(row)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return d


@router.delete(
    "/entries/{entry_id}",
    summary="Delete a KB entry",
    dependencies=[Depends(require_user_or_agent)],
)
async def delete_entry(entry_id: str) -> dict[str, Any]:
    conn = _db()
    with _lock:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "entry not found")
    return {"deleted": entry_id}


@router.get(
    "/search",
    summary="Full-text search KB entries (FTS5, ranked by relevance + recency)",
    dependencies=[Depends(require_user_or_agent)],
)
async def search(q: str, limit: int = 10) -> list[dict[str, Any]]:
    conn = _db()
    if not q or not q.strip():
        return []
    # FTS5 query — escape user input by quoting tokens
    safe = " ".join(f'"{tok}"' for tok in q.split() if tok.strip())
    if not safe:
        return []
    try:
        rows = conn.execute("""
            SELECT e.id, e.topic, e.decision, e.rationale, e.tags,
                   e.related_task_id, e.related_board_id, e.submitted_by, e.created_at,
                   bm25(entries_fts) AS rank
            FROM entries_fts
            JOIN entries e ON e.rowid = entries_fts.rowid
            WHERE entries_fts MATCH ?
            ORDER BY rank, e.created_at DESC
            LIMIT ?
        """, (safe, limit)).fetchall()
    except sqlite3.OperationalError:
        safe_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute("""
            SELECT id, topic, decision, rationale, tags, related_task_id,
                   related_board_id, submitted_by, created_at, 0.0 AS rank
            FROM entries
            WHERE topic LIKE ? OR decision LIKE ? OR rationale LIKE ? OR tags LIKE ?
            ORDER BY created_at DESC LIMIT ?
        """, (f"%{safe_q}%", f"%{safe_q}%", f"%{safe_q}%", f"%{safe_q}%", limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        out.append(d)
    return out
