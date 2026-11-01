"""Projects API — rolled-up view over board_groups (and orphan boards).

Each "project" = one board_group (or a synthetic "ungrouped" wrapper for
boards with no group). Surfaces task progress + status + priority + last
agent that worked on it, all derived from existing tables. No new schema.

T104 (2026-05-24).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent
from app.core.auth import SESSION_DEP

router = APIRouter(prefix="/projects", tags=["projects"])


_PRIORITY_ORDER = {"high": 3, "medium": 2, "normal": 1, "low": 0}


def _derive_status(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total == 0:
        return "planning"
    done = counts.get("done", 0)
    in_progress = counts.get("in_progress", 0)
    review = counts.get("review", 0)
    inbox = counts.get("inbox", 0)
    if done == total:
        return "complete"
    if in_progress > 0 or review > 0:
        return "active"
    if inbox == total:
        return "planning"
    return "active"


def _max_priority(prios: list[str]) -> str | None:
    if not prios:
        return None
    return max(prios, key=lambda p: _PRIORITY_ORDER.get(p or "", -1))


def _progress_pct(done: int, total: int) -> int:
    """Floor to 99 unless every task is done, so a near-finished board never reads 100%."""
    if total <= 0:
        return 0
    if done >= total:
        return 100
    return min(99, int(done / total * 100))


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectFromTemplate(BaseModel):
    """T106 (2026-05-25): scaffold a project from a built-in template."""

    name: str
    template_id: str
    description: str | None = None


class ProjectFromIntent(BaseModel):
    """T106 (2026-05-25): scaffold a project from owner free-text intent.
    Smart-tier planner decomposes into boards + tasks + cross-board deps."""

    name: str
    intent: str


class ProjectRefine(BaseModel):
    """T107 (2026-05-26): owner-supplied new info → planner re-analysis →
    ADDITIVE boards + tasks delta (no removals/renames)."""

    additional_intent: str


@router.get("", summary="List projects with rolled-up stats")
async def list_projects(
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> list[dict[str, Any]]:
    """List `board_groups` + a synthetic 'Ungrouped boards' bucket.

    For each project: total task count, status breakdown, progress %,
    derived status, max priority across tasks, and last agent that
    posted a comment / event on any task in any board of the project.
    """
    # 1) board_groups
    groups_res = await session.execute(
        text(
            "SELECT id, name, description, created_at, updated_at "
            "FROM board_groups ORDER BY name"
        )
    )
    groups = [dict(r._mapping) for r in groups_res.fetchall()]

    # 2) boards (grouped + ungrouped)
    boards_res = await session.execute(
        text("SELECT id, name, description, board_group_id, autonomous_enabled FROM boards")
    )
    boards_by_group: dict[str | None, list[dict[str, Any]]] = {}
    boards_by_id: dict[str, dict[str, Any]] = {}
    for r in boards_res.fetchall():
        m = dict(r._mapping)
        bid = str(m["id"])
        m["id"] = bid
        boards_by_id[bid] = m
        gid = str(m["board_group_id"]) if m["board_group_id"] else None
        boards_by_group.setdefault(gid, []).append(m)

    # 3) task stats per board
    task_res = await session.execute(text("""
        SELECT board_id, status, priority, COUNT(*) AS n
        FROM tasks
        WHERE board_id IS NOT NULL
        GROUP BY board_id, status, priority
    """))
    stats_by_board: dict[str, dict[str, Any]] = {}
    for r in task_res.fetchall():
        bid = str(r._mapping["board_id"])
        status = r._mapping["status"] or "inbox"
        prio = r._mapping["priority"] or "normal"
        n = int(r._mapping["n"])
        entry = stats_by_board.setdefault(bid, {"counts": {}, "prios": []})
        entry["counts"][status] = entry["counts"].get(status, 0) + n
        entry["prios"].extend([prio] * n)

    # 4) latest comment/activity per board (to surface "last agent")
    last_res = await session.execute(text("""
        SELECT DISTINCT ON (t.board_id)
            t.board_id, ae.created_at, ae.agent_id, a.name AS agent_name,
            ae.event_type
        FROM activity_events ae
        JOIN tasks t ON t.id = ae.task_id
        LEFT JOIN agents a ON a.id = ae.agent_id
        WHERE t.board_id IS NOT NULL
          AND ae.agent_id IS NOT NULL
          AND ae.event_type IN ('task.comment', 'task.status_changed', 'task.auto_assigned')
        ORDER BY t.board_id, ae.created_at DESC
    """))
    last_by_board: dict[str, dict[str, Any]] = {}
    for r in last_res.fetchall():
        last_by_board[str(r._mapping["board_id"])] = {
            "agent_id": str(r._mapping["agent_id"]) if r._mapping["agent_id"] else None,
            "agent_name": r._mapping["agent_name"],
            "ts": r._mapping["created_at"].isoformat() if r._mapping["created_at"] else None,
            "event_type": r._mapping["event_type"],
        }

    def _rollup(board_ids: list[str]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        prios: list[str] = []
        last: dict[str, Any] | None = None
        for bid in board_ids:
            s = stats_by_board.get(bid)
            if s:
                for k, v in s["counts"].items():
                    counts[k] = counts.get(k, 0) + v
                prios.extend(s["prios"])
            lb = last_by_board.get(bid)
            if lb and (last is None or (lb.get("ts") or "") > (last.get("ts") or "")):
                last = lb
        total = sum(counts.values())
        done = counts.get("done", 0)
        return {
            "total": total,
            "done": done,
            "in_progress": counts.get("in_progress", 0),
            "review": counts.get("review", 0),
            "inbox": counts.get("inbox", 0),
            "cancelled": counts.get("cancelled", 0),
            "blocked": counts.get("blocked", 0),
            "progress_pct": _progress_pct(done, total),
            "status": _derive_status(counts),
            "priority": _max_priority(prios),
            "last_agent": last,
        }

    projects: list[dict[str, Any]] = []
    for g in groups:
        gid = str(g["id"])
        gboards = boards_by_group.get(gid, [])
        roll = _rollup([b["id"] for b in gboards])
        # T105 Phase 5: PEV autonomous = any-board-in-group has it on
        autonomous = any(bool(b.get("autonomous_enabled")) for b in gboards)
        projects.append(
            {
                "id": gid,
                "kind": "group",
                "name": g["name"],
                "description": g["description"] or "",
                "boards": [
                    {
                        "id": b["id"],
                        "name": b["name"],
                        "autonomous_enabled": bool(b.get("autonomous_enabled")),
                    }
                    for b in gboards
                ],
                "board_count": len(gboards),
                "autonomous_enabled": autonomous,
                "created_at": g["created_at"].isoformat() if g["created_at"] else None,
                **roll,
            }
        )

    # Synthetic project per ungrouped board (each board IS a project)
    for b in boards_by_group.get(None, []):
        roll = _rollup([b["id"]])
        projects.append(
            {
                "id": f"board:{b['id']}",
                "kind": "board",
                "name": b["name"],
                "description": (b["description"] or "")[:280],
                "boards": [
                    {
                        "id": b["id"],
                        "name": b["name"],
                        "autonomous_enabled": bool(b.get("autonomous_enabled")),
                    }
                ],
                "board_count": 1,
                "autonomous_enabled": bool(b.get("autonomous_enabled")),
                "created_at": None,
                **roll,
            }
        )

    projects.sort(
        key=lambda p: (
            0 if p["status"] == "active" else (1 if p["status"] == "planning" else 2),
            -p["total"],
            p["name"],
        )
    )
    return projects


@router.post("", summary="Create a new project (board_group)")
async def create_project(
    payload: ProjectCreate,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    # Default to the first organization (single-tenant mode)
    org_res = await session.execute(text("SELECT id FROM organizations LIMIT 1"))
    org = org_res.first()
    if not org:
        raise HTTPException(status_code=400, detail="No organization found")
    slug = payload.name.lower().replace(" ", "-")[:64]
    res = await session.execute(
        text(
            "INSERT INTO board_groups (id, organization_id, name, slug, description, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :oid, :name, :slug, :desc, NOW(), NOW()) "
            "RETURNING id, name, description, created_at"
        ),
        {"oid": str(org[0]), "name": payload.name, "slug": slug, "desc": payload.description},
    )
    row = res.first()
    await session.commit()
    return {
        "id": str(row[0]),
        "kind": "group",
        "name": row[1],
        "description": row[2] or "",
        "created_at": row[3].isoformat() if row[3] else None,
        "total": 0,
        "done": 0,
        "progress_pct": 0,
        "status": "planning",
        "priority": None,
        "last_agent": None,
        "board_count": 0,
        "boards": [],
    }


# T106 (2026-05-25): templated + intent-based project scaffolds.


@router.get("/templates", summary="List built-in project scaffolds")
async def list_project_templates(
    _: None = Depends(require_user_or_agent),
) -> list[dict[str, Any]]:
    from app.services.project_scaffolds import list_templates

    return list_templates()


@router.post(
    "/from-template",
    summary="Scaffold a project from a built-in template "
    "(creates board_group + phase boards + seeded tasks + cross-board deps)",
)
async def create_from_template(
    payload: ProjectFromTemplate,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    from app.services.project_scaffolds import scaffold_from_template

    try:
        return await scaffold_from_template(
            session,
            project_name=payload.name,
            template_id=payload.template_id,
            project_description=payload.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/from-intent",
    summary="Scaffold a project from free-text intent via smart-tier planner",
)
async def create_from_intent(
    payload: ProjectFromIntent,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    from app.services.project_scaffolds import scaffold_from_intent

    try:
        return await scaffold_from_intent(
            session,
            project_name=payload.name,
            intent=payload.intent,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post(
    "/{project_id}/refine",
    summary="Re-analyze project with new owner info (additive boards/tasks)",
)
async def refine_project(
    project_id: UUID,
    payload: ProjectRefine,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    from app.services.project_scaffolds import refine_project_with_intent

    try:
        return await refine_project_with_intent(
            session,
            project_id=project_id,
            additional_intent=payload.additional_intent,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.patch("/{project_id}", summary="Rename / re-describe a project")
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    sets: list[str] = []
    params: dict[str, Any] = {"pid": str(project_id)}
    if payload.name is not None:
        sets.append("name = :name")
        params["name"] = payload.name
    if payload.description is not None:
        sets.append("description = :desc")
        params["desc"] = payload.description
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets.append("updated_at = NOW()")
    await session.execute(
        text(f"UPDATE board_groups SET {', '.join(sets)} WHERE id = :pid"), params
    )
    await session.commit()
    return {"ok": True}


@router.delete("/{project_id}", summary="Delete a project (group)")
async def delete_project(
    project_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    # Don't cascade to boards — just detach.
    await session.execute(
        text("UPDATE boards SET board_group_id = NULL WHERE board_group_id = :pid"),
        {"pid": str(project_id)},
    )
    res = await session.execute(
        text("DELETE FROM board_groups WHERE id = :pid"), {"pid": str(project_id)}
    )
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}


@router.post(
    "/{project_id}/boards/{board_id}",
    summary="Assign a board to a project",
)
async def attach_board(
    project_id: UUID,
    board_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    res = await session.execute(
        text("UPDATE boards SET board_group_id = :pid WHERE id = :bid"),
        {"pid": str(project_id), "bid": str(board_id)},
    )
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Board not found")
    return {"ok": True}
