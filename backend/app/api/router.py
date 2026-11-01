"""Complexity-based task router.

Heuristic scoring decides whether an inbox task goes to vllm-variant agent
(simple/free) or Senior ollama variant (complex/paid).

Endpoint: POST /api/v1/router/route_task
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.db.session import get_session
from app.services.task_complexity_rules import HARD_KEYWORDS, SIMPLE_KEYWORDS, _complexity_score

router = APIRouter(prefix="/router", tags=["router"])

ROLES = {
    "security": "Security Architect",
    "auth": "Security Architect",
    "cryptography": "Security Architect",
    "binary": "Binary Analyst",
    "reverse": "Reverse Engineer",
    "crack": "Binary Analyst",
    "malware": "Binary Analyst",
    "test": "QA Engineer",
    "qa": "QA Engineer",
    "debug": "QA Engineer",
    "deploy": "DevOps Engineer",
    "pipelines": "DevOps Engineer",
    "ci-cd": "DevOps Engineer",
    "multi": "Software Architect",
    "distributed": "Software Architect",
    "complex": "Software Architect",
    "system": "Software Architect",
    "infrastructure": "Software Architect",
    "architecture": "Software Architect",
    "red": "Red Team Operator",
    "attack": "Red Team Operator",
    "defense": "Red Team Operator",
}


def _role_keyword_for_task(title: str, description: str) -> str | None:
    """Best-effort role match from task title/description."""
    body = (title + " " + description).lower()
    candidates = [
        # T104 (2026-05-23): more RE/binary keywords. Owner's "eval 1"
        # task mentioned "crackme" + "ida pro" + "disassembly" but
        # somehow QA Engineer was selected. Strengthen the match list.
        ("crackme", "Reverse Engineer"),
        ("ida pro", "Reverse Engineer"),
        ("ida-pro", "Reverse Engineer"),
        ("ghidra", "Reverse Engineer"),
        ("radare", "Reverse Engineer"),
        ("disassembl", "Reverse Engineer"),
        ("decompil", "Reverse Engineer"),
        ("reverse engineer", "Reverse Engineer"),
        ("malware", "Binary Analyst"),
        ("binary forensic", "Binary Analyst"),
        ("elf binary", "Binary Analyst"),
        ("pe binary", "Binary Analyst"),
        ("pentest", "Red Team Operator"),
        ("exploit", "Red Team Operator"),
        ("att&ck", "Red Team Operator"),
        ("security architect", "Security Architect"),
        ("threat model", "Security Architect"),
        ("compliance", "Security Architect"),
        ("infra", "DevOps Engineer"),
        ("docker", "DevOps Engineer"),
        ("ci/cd", "DevOps Engineer"),
        ("deploy", "DevOps Engineer"),
        ("qa", "QA Engineer"),
        ("test", "QA Engineer"),
        ("research", "Software Architect"),
        ("architecture", "Software Architect"),
        ("design", "Software Architect"),
    ]
    for kw, role in candidates:
        if kw in body:
            return role
    return None


class RouteRequest(BaseModel):
    task_id: str
    apply: bool = False  # if True, PATCH assigned_agent_id; else just return suggestion


@router.post(
    "/route_task",
    summary="Score a task by complexity and recommend (or apply) an agent assignment",
    dependencies=[Depends(require_user_or_agent)],
)
async def route_task(
    payload: RouteRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # Fetch task
    res = await db.execute(text(
        "SELECT id, title, description, board_id, status, assigned_agent_id "
        "FROM tasks WHERE id = :tid"
    ), {"tid": payload.task_id})
    row = res.first()
    if not row:
        raise HTTPException(404, "task not found")

    title = row[1] or ""
    desc = row[2] or ""
    score = _complexity_score(title, desc)
    role = _role_keyword_for_task(title, desc)

    if not role:
        return {
            "task_id": payload.task_id,
            "complexity": score,
            "matched_role": None,
            "suggested_agent": None,
            "reason": "No role match; Atlas should triage manually.",
        }

    target_name = f"{role} Senior" if score["is_complex"] else role

    # Look up that agent
    res = await db.execute(text(
        "SELECT id, name FROM agents WHERE name = :n AND status != 'retired' LIMIT 1"
    ), {"n": target_name})
    agent = res.first()

    # Fallback: if Senior variant doesn't exist for complex, use vllm variant + flag
    fallback_used = False
    if not agent and score["is_complex"]:
        res = await db.execute(text(
            "SELECT id, name FROM agents WHERE name = :n AND status != 'retired' LIMIT 1"
        ), {"n": role})
        agent = res.first()
        fallback_used = True

    if not agent:
        return {
            "task_id": payload.task_id,
            "complexity": score,
            "matched_role": role,
            "suggested_agent": None,
            "reason": f"No active agent named '{target_name}' or '{role}'.",
        }

    suggested_id = str(agent[0])
    suggested_name = agent[1]

    if payload.apply:
        # Only apply if status is 'inbox' (safe state to reassign)
        if row[4] != "inbox":
            raise HTTPException(409, f"task is in '{row[4]}' status; only 'inbox' is safe to reroute")
        await db.execute(text(
            "UPDATE tasks SET assigned_agent_id = :aid, updated_at = NOW() WHERE id = :tid"
        ), {"aid": suggested_id, "tid": payload.task_id})
        await db.commit()

    return {
        "task_id": payload.task_id,
        "complexity": score,
        "matched_role": role,
        "target_variant": "Senior (ollama)" if score["is_complex"] else "vllm",
        "suggested_agent_id": suggested_id,
        "suggested_agent_name": suggested_name,
        "fallback_used": fallback_used,
        "applied": payload.apply,
    }


@router.get(
    "/complexity_rationale",
    summary="T82 — Rebuild the human-readable breakdown of how a task's complexity was scored",
    dependencies=[Depends(require_user_or_agent)],
)
async def complexity_rationale(
    task_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Returns the per-factor breakdown of how the task got its level.

    T83 (2026-05-16) reshape:
      * If the task carries a persisted `complexity_rationale` (keyword
        breakdown plus optional LLM verdict), return that.
      * Otherwise fall back to a fresh keyword-only recompute.
    """
    import json as _json

    res = await db.execute(text(
        "SELECT title, description, complexity, complexity_rationale "
        "FROM tasks WHERE id = :tid"
    ), {"tid": task_id})
    row = res.first()
    if not row:
        raise HTTPException(404, "task not found")

    title = row[0] or ""
    desc = row[1] or ""
    stored_level = row[2]
    persisted_blob = row[3]

    # ── Path A: return persisted verdict and its actual source ───────
    if persisted_blob:
        try:
            persisted = _json.loads(persisted_blob)
        except Exception:
            persisted = None
        if isinstance(persisted, dict):
            return {
                "task_id": task_id,
                "title": title,
                "schema": "complexity_rationale.v1",
                "method": (
                    "llm_assessor_with_keyword_floor"
                    if persisted.get("llm_level") is not None else (
                        "keyword_history_fallback"
                        if any("Historical:" in str(n) for n in persisted.get("notes") or [])
                        else "keyword_fallback"
                    )
                ),
                "final_level": persisted.get("level"),
                "stored_level": stored_level,
                "llm_verdict": {
                    "level": persisted.get("llm_level"),
                    "reasoning": persisted.get("llm_reasoning"),
                    "model": persisted.get("llm_model"),
                    "provider": persisted.get("llm_provider"),
                },
                "keyword_floor": {
                    "level": persisted.get("keyword_floor"),
                    "score": persisted.get("keyword_score"),
                    "hard_hits": persisted.get("keyword_hard_hits") or [],
                    "simple_hits": persisted.get("keyword_simple_hits") or [],
                },
                "level_thresholds": {
                    "L1": "trivial / one-shot",
                    "L2": "simple / single-file",
                    "L3": "moderate / multi-file",
                    "L4": "complex / cross-subsystem",
                    "L5": "major / architecture-scale",
                },
                "notes": persisted.get("notes") or [],
            }

    # ── Path B: legacy fallback (no LLM data, recompute keyword) ────

    # Reproduce the per-factor scoring step-by-step.
    title_l = title.lower()
    desc_l = desc.lower()
    body_lower = title_l + " " + desc_l

    length_points = min(len(desc), 2000) // 200
    hard_hits = [k for k in HARD_KEYWORDS if k in body_lower]
    simple_hits = [k for k in SIMPLE_KEYWORDS if k in body_lower]
    hard_points = 2 * len(hard_hits)
    # T82: simple keywords skipped entirely when hard signals fire — see
    # `_complexity_score` for the same gating logic.
    simple_points = -len(simple_hits) if not hard_hits else 0

    has_dep = (
        "depends on:" in desc_l
        or "depends_on:" in desc_l
        or "blocked by" in desc_l
    )
    dep_points = 3 if has_dep else 0

    crackme_stage = bool(
        "crackme" in desc_l and re.search(r"(level|stage)\s*[3-9]", desc_l)
    )
    crackme_points = 2 if crackme_stage else 0

    score = length_points + hard_points + simple_points + dep_points + crackme_points
    if score <= 0:
        derived_level = 1
    elif score <= 3:
        derived_level = 2
    elif score <= 6:
        derived_level = 3
    elif score <= 10:
        derived_level = 4
    else:
        derived_level = 5

    factors: list[dict[str, Any]] = []
    factors.append({
        "label": "Description length",
        "points": length_points,
        "detail": f"{len(desc)} chars · +1 per 200 chars (capped at 2000)",
    })
    factors.append({
        "label": "Hard / complex keywords",
        "points": hard_points,
        "detail": (
            f"{len(hard_hits)} hit(s): " + ", ".join(hard_hits) if hard_hits
            else "none"
        ),
    })
    factors.append({
        "label": "Simple / trivial keywords",
        "points": simple_points,
        "detail": (
            f"{len(simple_hits)} hit(s) (ignored — hard signal present): " + ", ".join(simple_hits)
            if simple_hits and hard_hits
            else f"{len(simple_hits)} hit(s): " + ", ".join(simple_hits)
            if simple_hits
            else "none"
        ),
    })
    factors.append({
        "label": "Has cross-task dependencies",
        "points": dep_points,
        "detail": "found 'Depends on:' / 'blocked by' marker" if has_dep else "no dependencies declared",
    })
    factors.append({
        "label": "Crackme level 3+",
        "points": crackme_points,
        "detail": "matched 'crackme … level/stage ≥3'" if crackme_stage else "not a crackme task",
    })

    return {
        "task_id": task_id,
        "title": title,
        "score": score,
        "derived_level": derived_level,
        "stored_level": stored_level,
        "level_thresholds": {
            "L1": "score ≤ 0",
            "L2": "score 1-3",
            "L3": "score 4-6",
            "L4": "score 7-10",
            "L5": "score 11+",
        },
        "factors": factors,
        "note": (
            "Owner can override via the Complexity dropdown when creating the "
            "task. The breakdown above is what the auto-assessor computed; "
            "the stored level is what the task currently carries (may differ "
            "if the owner manually changed it later)."
        ),
    }


@router.post(
    "/reassess_complexity",
    summary="T83 — Re-run LLM-driven complexity assessor on an existing task",
    dependencies=[Depends(require_user_or_agent)],
)
async def reassess_complexity(
    task_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Owner-triggered re-evaluation. Useful when the scorer has been
    improved, when the task description has been edited, or when the
    original LLM call failed silently. Updates `complexity` +
    `complexity_rationale` atomically.
    """
    from app.services.complexity_assessor import assess_complexity

    res = await db.execute(text(
        "SELECT title, description, complexity FROM tasks WHERE id = :tid"
    ), {"tid": task_id})
    row = res.first()
    if not row:
        raise HTTPException(404, "task not found")

    title = row[0] or ""
    desc = row[1] or ""
    prev_level = row[2]

    verdict = await assess_complexity(db, title, desc)

    await db.execute(text(
        "UPDATE tasks SET complexity = :lvl, complexity_rationale = :r WHERE id = :tid"
    ), {
        "lvl": verdict.level,
        "r": verdict.to_json(),
        "tid": task_id,
    })
    await db.commit()

    return {
        "task_id": task_id,
        "previous_level": prev_level,
        "new_level": verdict.level,
        "llm_verdict": verdict.llm_level,
        "llm_reasoning": verdict.llm_reasoning,
        "llm_model": f"{verdict.llm_provider}/{verdict.llm_model}" if verdict.llm_model else None,
        "keyword_floor": verdict.keyword_floor,
        "keyword_score": verdict.keyword_score,
        "notes": verdict.notes,
    }


@router.post(
    "/triage_inbox",
    summary="Score + apply routing for all inbox tasks on a board",
    dependencies=[Depends(require_user_or_agent)],
)
async def triage_inbox(
    board_id: str,
    apply: bool = False,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    res = await db.execute(text(
        "SELECT id FROM tasks WHERE board_id = :bid AND status = 'inbox' "
        "AND assigned_agent_id IS NULL ORDER BY created_at LIMIT 50"
    ), {"bid": board_id})
    task_ids = [str(r[0]) for r in res.fetchall()]

    results = []
    for tid in task_ids:
        try:
            r = await route_task(RouteRequest(task_id=tid, apply=apply), db=db)
            results.append(r)
        except Exception as e:
            results.append({"task_id": tid, "error": str(e)[:200]})

    return {"board_id": board_id, "triaged": len(results), "applied": apply, "results": results}
