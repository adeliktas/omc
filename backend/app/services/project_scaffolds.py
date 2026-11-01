"""T106 (2026-05-25) — Project scaffolds.

Owner intent: "create project X" → automatically materialize the full
SDLC structure (project = board_group, phase boards, seeded tasks,
cross-board dependencies, optional autonomous-PEV per phase).

Two paths:
  - **Template-based** (`scaffold_from_template`): deterministic, fast,
    no LLM cost. Three built-in templates: software / research / ops.
  - **Intent-based** (`scaffold_from_intent`): smart-tier planner reads
    free-text intent + emits a custom project plan JSON. More flexible,
    smart-tier cost (no per-task cap per owner directive).

Both paths converge on `_materialize_plan` which writes the rows.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)


# ─── Built-in templates ────────────────────────────────────────────────


TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "software",
        "label": "Software Project (SDLC)",
        "description": (
            "Standard software development lifecycle: Requirements → "
            "Implementation → QA → Release. 4 phase-boards with cross-"
            "board dependencies. PEV autonomous mode pre-enabled on "
            "Implementation + QA + Release; Requirements stays manual "
            "(owner discussion phase)."
        ),
        "icon": "💻",
        "boards": [
            {
                "suffix": "Requirements & Design",
                "phase_order": 1,
                "autonomous_enabled": False,
                "description": "Define what to build. Owner-driven phase. Output: MVP scope, user stories, technical sketch.",
                "role_pool_pm": "Product Manager",
                "role_pool_workers": ["Business Analyst", "Software Architect Senior"],
                "tasks": [
                    {"title": "Define MVP scope + acceptance criteria", "complexity": 2, "priority": "high"},
                    {"title": "Write user stories + edge cases", "complexity": 2, "priority": "high"},
                    {"title": "Choose architecture (modules, layers, language idioms)", "complexity": 3, "priority": "medium"},
                    {"title": "Sketch module + class layout", "complexity": 2, "priority": "medium"},
                ],
            },
            {
                "suffix": "Implementation",
                "phase_order": 2,
                "autonomous_enabled": True,
                "description": "Build the agreed design. PEV-driven. Each task = one atomic feature with concrete acceptance evidence.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["Backend Engineer", "Frontend Engineer"],
                "depends_on_suffix": "Requirements & Design",
                "tasks": [
                    {"title": "Scaffold project skeleton (package layout + entry points)", "complexity": 2, "priority": "high"},
                    {"title": "Implement core domain logic per spec", "complexity": 3, "priority": "high"},
                    {"title": "Implement CLI / interface layer", "complexity": 2, "priority": "medium"},
                    {"title": "Implement error handling + input validation", "complexity": 2, "priority": "medium"},
                    {"title": "Implement persistence / state (if required)", "complexity": 3, "priority": "medium"},
                ],
            },
            {
                "suffix": "QA & Testing",
                "phase_order": 3,
                "autonomous_enabled": True,
                "description": "Verify the build. PEV-driven. Coverage target ≥80%. Owner-consent required for breaking-change reports.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["QA Engineer Senior", "QA Engineer"],
                "depends_on_suffix": "Implementation",
                "tasks": [
                    {"title": "Write unit tests for core logic", "complexity": 2, "priority": "high"},
                    {"title": "Edge-case + boundary tests", "complexity": 2, "priority": "high"},
                    {"title": "Property-based / fuzzing tests", "complexity": 3, "priority": "medium"},
                    {"title": "End-to-end / integration tests", "complexity": 3, "priority": "medium"},
                    {"title": "Coverage report ≥ 80%", "complexity": 1, "priority": "medium"},
                ],
            },
            {
                "suffix": "Release",
                "phase_order": 4,
                "autonomous_enabled": True,
                "description": "Ship it. Build artifacts, CI, docs. Protected-path consent for compose / Dockerfile / .env changes.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["DevOps Engineer", "Technical Writer"],
                "depends_on_suffix": "QA & Testing",
                "tasks": [
                    {"title": "Build distributable artifact (wheel / binary / image)", "complexity": 2, "priority": "high"},
                    {"title": "Write README + usage docs", "complexity": 1, "priority": "medium"},
                    {"title": "CI pipeline: build + test on push", "complexity": 3, "priority": "medium"},
                    {"title": "Dry-run publish to artifact registry", "complexity": 2, "priority": "medium"},
                ],
            },
        ],
    },
    {
        "id": "research",
        "label": "Research Project",
        "description": (
            "Discovery → Analysis → Synthesis → Output. For legal-research, "
            "threat-intel, scout, market-discovery. PEV-autonomous from day one "
            "(continuously scans for new sources)."
        ),
        "icon": "🔬",
        "boards": [
            {
                "suffix": "Sources & Discovery",
                "phase_order": 1,
                "autonomous_enabled": True,
                "description": "Find primary sources. Periodic re-scans. OSINT + targeted lookups.",
                "role_pool_pm": "Product Manager",
                "role_pool_workers": ["OSINT Analyst", "Threat Researcher"],
                "tasks": [
                    {"title": "Inventory primary sources (regulations / RSS / APIs)", "complexity": 2, "priority": "high"},
                    {"title": "Set up daily scan job for new entries", "complexity": 2, "priority": "high"},
                    {"title": "Capture initial corpus snapshot in wiki", "complexity": 2, "priority": "medium"},
                ],
            },
            {
                "suffix": "Analysis",
                "phase_order": 2,
                "autonomous_enabled": True,
                "description": "Read + classify discovered sources. Flag changes vs baseline. Extract structured data.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["Data Engineer", "Software Architect Senior"],
                "depends_on_suffix": "Sources & Discovery",
                "tasks": [
                    {"title": "Parse + normalize source content into structured form", "complexity": 3, "priority": "high"},
                    {"title": "Classify by topic + impact (high/med/low)", "complexity": 2, "priority": "high"},
                    {"title": "Detect deltas vs prior snapshot", "complexity": 3, "priority": "medium"},
                ],
            },
            {
                "suffix": "Synthesis & Recommendations",
                "phase_order": 3,
                "autonomous_enabled": True,
                "description": "Roll up findings. Update plans/policies. Recommend owner actions.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["Knowledge Manager", "Technical Writer"],
                "depends_on_suffix": "Analysis",
                "tasks": [
                    {"title": "Synthesize period summary (weekly digest)", "complexity": 2, "priority": "high"},
                    {"title": "Update compliance / strategy plans in wiki", "complexity": 3, "priority": "high"},
                    {"title": "Surface owner-action items to /review", "complexity": 1, "priority": "high"},
                ],
            },
        ],
    },
    {
        "id": "ops",
        "label": "Operations / SRE Project",
        "description": (
            "Inventory → Monitor → Respond. For infrastructure, deployment "
            "tracking, oncall rotation, incident handling. Heavy protected-"
            "path consent gating."
        ),
        "icon": "⚙️",
        "boards": [
            {
                "suffix": "Inventory & Baseline",
                "phase_order": 1,
                "autonomous_enabled": False,
                "description": "Catalog systems + capture baseline. Owner-driven setup; PEV stays off until baseline approved.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["SRE", "Network Engineer"],
                "tasks": [
                    {"title": "Inventory all services + dependencies", "complexity": 2, "priority": "high"},
                    {"title": "Document architecture diagram", "complexity": 2, "priority": "high"},
                    {"title": "Capture baseline metrics (RPS, latency, error rate)", "complexity": 2, "priority": "medium"},
                ],
            },
            {
                "suffix": "Monitoring & Alerts",
                "phase_order": 2,
                "autonomous_enabled": True,
                "description": "Continuous monitoring + alert tuning. PEV-driven. Owner consent for any alert-rule changes that could miss incidents.",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["SRE", "SOC Analyst"],
                "depends_on_suffix": "Inventory & Baseline",
                "tasks": [
                    {"title": "Define SLOs + error budgets", "complexity": 3, "priority": "high"},
                    {"title": "Wire alerts to oncall channels", "complexity": 2, "priority": "high"},
                    {"title": "Tune noisy / silent alerts (weekly)", "complexity": 2, "priority": "medium"},
                ],
            },
            {
                "suffix": "Incident Response",
                "phase_order": 3,
                "autonomous_enabled": True,
                "description": "Triage + remediate incidents. Owner-consent for ALL system-breaking changes (compose / migrations).",
                "role_pool_pm": "Project Manager Senior",
                "role_pool_workers": ["Incident Responder", "SRE"],
                "depends_on_suffix": "Monitoring & Alerts",
                "tasks": [
                    {"title": "Build runbook templates per service", "complexity": 2, "priority": "high"},
                    {"title": "Postmortem template + tracking", "complexity": 1, "priority": "medium"},
                ],
            },
        ],
    },
]


_TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


# ─── Materialization (template + intent share this path) ───────────────


async def _materialize_plan(
    db: AsyncSession,
    project_name: str,
    project_description: str,
    boards_spec: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create board_group + boards + tasks + cross-board deps from a plan.

    Returns a summary with created IDs.
    """
    # Resolve org
    org_res = await db.execute(text("SELECT id FROM organizations LIMIT 1"))
    org = org_res.first()
    if not org:
        raise RuntimeError("no organization configured")
    org_id = org[0]

    # Gateway for board attribution (best-effort; first gateway)
    gw_res = await db.execute(text("SELECT id FROM gateways LIMIT 1"))
    gw_row = gw_res.first()
    if not gw_row:
        raise RuntimeError("no gateway configured")
    gateway_id = gw_row[0]

    # 1. Create board_group
    project_slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")[:64] or "project"
    project_id = uuid4()
    await db.execute(text("""
        INSERT INTO board_groups (id, organization_id, name, slug, description, created_at, updated_at)
        VALUES (:id, :oid, :name, :slug, :desc, NOW(), NOW())
    """), {
        "id": str(project_id), "oid": str(org_id),
        "name": project_name, "slug": project_slug,
        "desc": project_description,
    })

    # 2. Create boards, keyed by suffix for dep wiring
    suffix_to_board_id: dict[str, UUID] = {}
    board_summaries: list[dict[str, Any]] = []
    for spec in boards_spec:
        board_id = uuid4()
        board_name = f"{project_name} — {spec['suffix']}"
        board_slug = re.sub(r"[^a-z0-9]+", "-", board_name.lower()).strip("-")[:64]
        await db.execute(text("""
            INSERT INTO boards
                (id, organization_id, gateway_id, board_group_id, name, slug, description,
                 board_type, board_subtype, board_status,
                 require_approval_for_done, require_review_before_done,
                 comment_required_for_review, block_status_changes_with_pending_approval,
                 only_lead_can_change_status, max_agents,
                 autonomous_tasks_enabled, cron_tasks_enabled, cloud_providers_enabled,
                 autonomous_enabled,
                 created_at, updated_at)
            VALUES
                (:id, :oid, :gw, :pid, :name, :slug, :desc,
                 'task', NULL, 'active',
                 true, true,
                 false, false,
                 false, 4,
                 true, true, true,
                 :auto,
                 NOW(), NOW())
        """), {
            "id": str(board_id), "oid": str(org_id), "gw": str(gateway_id),
            "pid": str(project_id),
            "name": board_name, "slug": board_slug,
            "desc": spec.get("description", ""),
            "auto": bool(spec.get("autonomous_enabled", False)),
        })
        suffix_to_board_id[spec["suffix"]] = board_id
        board_summaries.append({
            "id": str(board_id),
            "name": board_name,
            "autonomous_enabled": bool(spec.get("autonomous_enabled", False)),
            "phase_order": spec.get("phase_order"),
        })

    # 3. Seed tasks per board
    task_count = 0
    first_task_by_board: dict[UUID, UUID] = {}  # for cross-board deps
    last_task_by_board: dict[UUID, UUID] = {}

    # T107 (2026-05-26): pre-resolve role→agent for each board so seeded
    # tasks land directly on a specialized agent instead of sitting in
    # inbox waiting for a watchdog tick to route them. Picks least-busy
    # online agent matching each task's `role` field (planner's free-text
    # role from the JSON plan) or the board's `role_pool_workers` list as
    # fallback. If nothing online matches, leave assigned_agent_id NULL
    # and let the inbox-triage watchdog handle it later.
    async def _resolve_agent_for_role(role_name: str) -> str | None:
        if not role_name:
            return None
        r = await db.execute(text("""
            SELECT a.id,
                   (SELECT COUNT(*) FROM tasks t
                    WHERE t.assigned_agent_id = a.id
                      AND t.status IN ('inbox','in_progress')) AS wip
            FROM agents a
            WHERE a.status = 'online'
              AND a.organization_id = :oid
              AND (a.name = :exact OR a.name LIKE :prefix)
            ORDER BY wip ASC, a.name ASC
            LIMIT 1
        """), {"oid": str(org_id), "exact": role_name, "prefix": f"{role_name}%"})
        row = r.first()
        return str(row[0]) if row else None

    for spec in boards_spec:
        bid = suffix_to_board_id[spec["suffix"]]
        board_workers = spec.get("role_pool_workers") or []
        for task_spec in spec.get("tasks", []):
            task_id = uuid4()
            comp = int(task_spec.get("complexity") or 3)
            budget_table = {1: (600, 20_000), 2: (1200, 40_000), 3: (1800, 80_000), 4: (3600, 200_000), 5: (7200, 500_000)}
            secs, toks = budget_table.get(comp, (1800, 80_000))

            # T107: resolve assignee from task's `role` field or board worker pool.
            assignee_id: str | None = None
            task_role = task_spec.get("role")
            if task_role:
                assignee_id = await _resolve_agent_for_role(task_role)
            if not assignee_id:
                for w in board_workers:
                    assignee_id = await _resolve_agent_for_role(w)
                    if assignee_id:
                        break

            await db.execute(text("""
                INSERT INTO tasks
                    (id, board_id, title, description, status, priority,
                     complexity, created_by_type, created_by_name,
                     assigned_agent_id,
                     max_seconds_budget, max_tokens_budget,
                     created_at, updated_at)
                VALUES
                    (:id, :bid, :title, :desc, 'inbox', :prio,
                     :comp, 'system', 'Project Scaffold',
                     :aid,
                     :secs, :toks,
                     NOW(), NOW())
            """), {
                "id": str(task_id), "bid": str(bid),
                "title": task_spec["title"],
                "desc": task_spec.get("description", task_spec["title"]),
                "prio": task_spec.get("priority", "medium"),
                "comp": comp,
                "aid": assignee_id,
                "secs": secs, "toks": toks,
            })
            if bid not in first_task_by_board:
                first_task_by_board[bid] = task_id
            last_task_by_board[bid] = task_id
            task_count += 1

    # 4. Cross-board phase deps. When phase N depends on phase N-1, every
    # task in phase N gets a dep on the LAST task of phase N-1 (cheap +
    # gates the whole phase). Cross-board deps allowed via task_dependencies
    # table (existing T39).
    cross_dep_count = 0
    for spec in boards_spec:
        dep_suffix = spec.get("depends_on_suffix")
        if not dep_suffix:
            continue
        downstream_bid = suffix_to_board_id[spec["suffix"]]
        upstream_bid = suffix_to_board_id.get(dep_suffix)
        if not upstream_bid:
            continue
        upstream_last = last_task_by_board.get(upstream_bid)
        if not upstream_last:
            continue
        # Add deps from every task in downstream to upstream_last
        ds_tasks = await db.execute(text(
            "SELECT id FROM tasks WHERE board_id = :bid"
        ), {"bid": str(downstream_bid)})
        for r in ds_tasks.fetchall():
            await db.execute(text("""
                INSERT INTO task_dependencies (id, task_id, depends_on_task_id, created_at)
                VALUES (:id, :tid, :dep, NOW())
                ON CONFLICT DO NOTHING
            """), {
                "id": str(uuid4()),
                "tid": str(r[0]),
                "dep": str(upstream_last),
            })
            cross_dep_count += 1

    await db.commit()
    logger.info(
        "project_scaffold.materialized project=%s boards=%d tasks=%d deps=%d",
        project_id, len(boards_spec), task_count, cross_dep_count,
    )
    return {
        "project_id": str(project_id),
        "project_name": project_name,
        "boards": board_summaries,
        "task_count": task_count,
        "cross_dep_count": cross_dep_count,
    }


# ─── Public entry points ───────────────────────────────────────────────


def list_templates() -> list[dict[str, Any]]:
    """Return user-facing template index."""
    return [
        {
            "id": t["id"],
            "label": t["label"],
            "description": t["description"],
            "icon": t.get("icon", "📋"),
            "board_count": len(t["boards"]),
            "task_count": sum(len(b.get("tasks", [])) for b in t["boards"]),
            "phases": [b["suffix"] for b in t["boards"]],
        }
        for t in TEMPLATES
    ]


async def scaffold_from_template(
    db: AsyncSession,
    *,
    project_name: str,
    template_id: str,
    project_description: str | None = None,
) -> dict[str, Any]:
    """Deterministic path — pick a template by id, materialize it."""
    template = _TEMPLATE_BY_ID.get(template_id)
    if not template:
        raise ValueError(f"unknown template: {template_id}")
    desc = project_description or template["description"]
    return await _materialize_plan(
        db,
        project_name=project_name,
        project_description=desc,
        boards_spec=template["boards"],
    )


_PLANNER_SYSTEM = """You are the PROJECT PLANNER. The owner gives you a free-text
intent for a project. Decompose it into a structured SDLC scaffold:
phase-boards, role pools, seeded tasks, cross-board dependencies.

Reply ONLY with a single JSON object — no prose, no markdown fences.

Schema:
{
  "project_name": "owner-given or refined",
  "project_description": "1-2 sentences explaining what the project achieves",
  "boards": [
    {
      "suffix": "phase name (will become 'Project Name — Suffix' on the board)",
      "phase_order": 1,
      "autonomous_enabled": true|false,
      "description": "what this phase covers",
      "role_pool_pm": "Product Manager | Project Manager Senior | ...",
      "role_pool_workers": ["role 1", "role 2"],
      "depends_on_suffix": "previous phase suffix or null for first phase",
      "tasks": [
        {
          "title": "specific task title (verb + object)",
          "description": "longer description — what to do + done criteria",
          "complexity": 1-5,
          "priority": "high|medium|low",
          "role": "specific agent role from role_pool_workers — used for direct assignment at scaffold time. If omitted, falls back to first online role from board's worker pool."
        }
      ]
    }
  ]
}

Rules:
  - 2-5 boards (phases) per project. Fewer = less overhead.
  - 3-7 tasks per board. Each task should be a concrete deliverable.
  - First phase usually `autonomous_enabled=false` (owner discussion).
  - Subsequent phases `true` unless they involve security-critical config.
  - role_pool values must come from this set:
    Product Manager, Business Analyst,
    Project Manager Senior, Scrum Master,
    Software Architect Senior, Backend Engineer, Frontend Engineer, Data Engineer,
    QA Engineer Senior, QA Engineer,
    DevOps Engineer, SRE, Network Engineer,
    Security Architect, SOC Analyst, Incident Responder,
    Reverse Engineer Senior, Binary Analyst, Malware Analyst,
    OSINT Analyst, Threat Researcher,
    Knowledge Manager, Technical Writer
"""


async def scaffold_from_intent(
    db: AsyncSession,
    *,
    project_name: str,
    intent: str,
) -> dict[str, Any]:
    """Smart-tier planner path — free-text intent → custom project plan.

    Per owner directive: no per-task token cap on planning. Uses the
    `planner` tier (deepseek-v4-pro / kimi / glm fallback). If no smart-
    tier model is online, raises so caller can offer template fallback.
    """
    from app.services.model_router import pick_model_for_tier
    from app.services.llm_evaluator import call_model

    picked = await pick_model_for_tier(db, "planner")
    if not picked:
        raise RuntimeError("no planner-tier model online — pick a template instead")
    _provider, model_id, _attempted = picked

    user_msg = (
        f"PROJECT INTENT: {intent}\n\n"
        f"Owner-supplied name (use as-is if no refinement needed): {project_name}\n\n"
        f"Produce the JSON project plan."
    )
    raw = await call_model(
        model_id,
        user_msg,
        system_prompt=_PLANNER_SYSTEM,
        max_tokens=8192,
        temperature=0.2,
    )
    if not raw:
        raise RuntimeError(f"planner {model_id} returned no content")

    # JSON parse with fence stripping
    s = raw.strip()
    s = re.sub(r"^```\w*\n?", "", s)
    s = s.replace("```", "").strip()
    try:
        plan = json.loads(s)
    except Exception:
        # Fallback: pull first {...} blob
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise RuntimeError(f"planner output not parseable: {raw[:200]}")
        plan = json.loads(m.group(0))

    boards = plan.get("boards")
    if not boards or not isinstance(boards, list):
        raise RuntimeError("planner plan missing `boards` array")

    refined_name = (plan.get("project_name") or project_name).strip()
    description = (plan.get("project_description") or f"Project: {refined_name}").strip()

    summary = await _materialize_plan(
        db,
        project_name=refined_name,
        project_description=description,
        boards_spec=boards,
    )
    summary["planner_model"] = model_id
    summary["planner_raw_chars"] = len(raw)
    return summary


_REFINE_SYSTEM = """You are the PROJECT REFINER. The owner has an existing
project with boards + tasks already in place. They've now added more info /
new requirements / scope adjustments. Your job: produce ADDITIVE deltas only.

DO NOT redo the whole plan. DO NOT propose deleting / renaming existing
boards or tasks. ONLY append new boards or new tasks to existing boards.

Reply ONLY with a single JSON object — no prose, no markdown fences.

Schema:
{
  "new_boards": [
    {
      "suffix": "phase name", "phase_order": int,
      "autonomous_enabled": true|false, "description": "...",
      "role_pool_pm": "...", "role_pool_workers": ["..."],
      "depends_on_suffix": "existing-or-new phase suffix or null",
      "tasks": [ {"title":"...","description":"...","complexity":1-5,"priority":"...","role":"..."} ]
    }
  ],
  "added_tasks": [
    {
      "board_suffix": "must match an existing board's suffix exactly",
      "title": "...", "description": "...",
      "complexity": 1-5, "priority": "...",
      "role": "agent role for direct assignment"
    }
  ]
}

If the owner's new info doesn't warrant any changes, return:
{"new_boards": [], "added_tasks": []}

Rules:
  - Same role-pool whitelist as the initial planner.
  - `new_boards[].phase_order` must be greater than the max existing phase_order.
  - Tasks added to existing boards must have a `board_suffix` that exactly
    matches one of the existing project's board suffixes.
"""


async def refine_project_with_intent(
    db: AsyncSession,
    *,
    project_id: UUID,
    additional_intent: str,
) -> dict[str, Any]:
    """T107 (2026-05-26): re-analyze an existing project with new owner info.

    Pulls current boards + tasks, asks planner-tier for ADDITIVE deltas
    only (new boards + new tasks on existing boards). Never removes/renames
    existing rows. Returns counts of added rows + planner model used.
    """
    from app.services.model_router import pick_model_for_tier
    from app.services.llm_evaluator import call_model

    picked = await pick_model_for_tier(db, "planner")
    if not picked:
        raise RuntimeError("no planner-tier model online — refinement unavailable")
    _provider, model_id, _attempted = picked

    proj_row = (await db.execute(text(
        "SELECT id, name, description FROM board_groups WHERE id = :pid"
    ), {"pid": str(project_id)})).first()
    if not proj_row:
        raise RuntimeError(f"project {project_id} not found")
    proj_name = proj_row[1]
    proj_desc = proj_row[2] or ""

    board_rows = (await db.execute(text("""
        SELECT b.id, b.name, COALESCE(REGEXP_REPLACE(b.name, '^.* — ', ''), b.name) AS suffix,
               b.autonomous_enabled
        FROM boards b
        WHERE b.board_group_id = :pid
        ORDER BY b.created_at ASC
    """), {"pid": str(project_id)})).fetchall()
    existing_suffixes = [r[2] for r in board_rows]
    suffix_to_bid = {r[2]: r[0] for r in board_rows}
    max_phase_order = len(board_rows)

    task_rows = (await db.execute(text("""
        SELECT b.name, t.title, t.status, t.priority, t.complexity
        FROM tasks t JOIN boards b ON b.id = t.board_id
        WHERE b.board_group_id = :pid
        ORDER BY t.created_at ASC
        LIMIT 200
    """), {"pid": str(project_id)})).fetchall()

    snapshot = "\n".join(
        f"- [{r[2]}] {r[0]} :: {r[1]} (prio={r[3]}, comp={r[4]})"
        for r in task_rows
    ) or "(no tasks yet)"

    user_msg = (
        f"EXISTING PROJECT: {proj_name}\n"
        f"DESCRIPTION: {proj_desc}\n\n"
        f"EXISTING BOARDS ({len(board_rows)} phases): {', '.join(existing_suffixes) or '(none)'}\n"
        f"MAX PHASE ORDER: {max_phase_order}\n\n"
        f"EXISTING TASKS:\n{snapshot}\n\n"
        f"NEW INFO FROM OWNER:\n{additional_intent}\n\n"
        f"Produce the JSON refinement delta."
    )
    raw = await call_model(
        model_id,
        user_msg,
        system_prompt=_REFINE_SYSTEM,
        max_tokens=8192,
        temperature=0.2,
    )
    if not raw:
        raise RuntimeError(f"refiner {model_id} returned no content")

    s = raw.strip()
    s = re.sub(r"^```\w*\n?", "", s).replace("```", "").strip()
    try:
        plan = json.loads(s)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise RuntimeError(f"refiner output not parseable: {raw[:200]}")
        plan = json.loads(m.group(0))

    new_boards = plan.get("new_boards") or []
    added_tasks = plan.get("added_tasks") or []

    # Materialize new boards (reuse _materialize_plan flow for the new
    # boards only, attaching to the SAME project_id rather than creating
    # a fresh project group).
    added_board_count = 0
    added_task_count = 0
    cross_deps = 0

    if new_boards:
        # Pull org + gateway from existing project's first board.
        meta = (await db.execute(text("""
            SELECT bg.organization_id, b.gateway_id
            FROM board_groups bg JOIN boards b ON b.board_group_id = bg.id
            WHERE bg.id = :pid LIMIT 1
        """), {"pid": str(project_id)})).first()
        if not meta:
            raise RuntimeError("project has no boards — cannot infer org/gateway")
        org_id = meta[0]
        gateway_id = meta[1]

        for spec in new_boards:
            board_id = uuid4()
            suffix = spec.get("suffix", "Phase")
            board_name = f"{proj_name} — {suffix}"
            slug = re.sub(r"[^a-z0-9]+", "-", board_name.lower()).strip("-")[:60]
            await db.execute(text("""
                INSERT INTO boards
                    (id, organization_id, gateway_id, board_group_id, name, slug,
                     description, board_type, target_date, status,
                     goal_confirmed, require_approval_for_done,
                     require_review_before_done, comment_required_for_review,
                     block_status_changes_with_pending_approval, max_agents,
                     autonomous_tasks_enabled, cron_tasks_enabled, cloud_providers_enabled,
                     autonomous_enabled, created_at, updated_at)
                VALUES
                    (:id, :oid, :gw, :pid, :name, :slug, :desc,
                     'task', NULL, 'active',
                     true, true, false, false, false, 4,
                     true, true, true,
                     :auto, NOW(), NOW())
            """), {
                "id": str(board_id), "oid": str(org_id), "gw": str(gateway_id),
                "pid": str(project_id),
                "name": board_name, "slug": slug,
                "desc": spec.get("description", ""),
                "auto": bool(spec.get("autonomous_enabled", False)),
            })
            suffix_to_bid[suffix] = board_id
            added_board_count += 1

            for task_spec in spec.get("tasks", []):
                task_id = uuid4()
                comp = int(task_spec.get("complexity") or 3)
                budget_table = {1: (600, 20_000), 2: (1200, 40_000), 3: (1800, 80_000),
                                4: (3600, 200_000), 5: (7200, 500_000)}
                secs, toks = budget_table.get(comp, (1800, 80_000))
                role = task_spec.get("role") or (spec.get("role_pool_workers") or [None])[0]
                aid = None
                if role:
                    r = await db.execute(text("""
                        SELECT a.id FROM agents a
                        WHERE a.status='online' AND a.organization_id = :oid
                          AND (a.name = :exact OR a.name LIKE :prefix)
                        ORDER BY a.name LIMIT 1
                    """), {"oid": str(org_id), "exact": role, "prefix": f"{role}%"})
                    row = r.first()
                    aid = str(row[0]) if row else None
                await db.execute(text("""
                    INSERT INTO tasks
                        (id, board_id, title, description, status, priority,
                         complexity, created_by_type, created_by_name,
                         assigned_agent_id, max_seconds_budget, max_tokens_budget,
                         created_at, updated_at)
                    VALUES (:id, :bid, :title, :desc, 'inbox', :prio,
                            :comp, 'system', 'Project Refiner',
                            :aid, :secs, :toks, NOW(), NOW())
                """), {
                    "id": str(task_id), "bid": str(board_id),
                    "title": task_spec["title"],
                    "desc": task_spec.get("description", task_spec["title"]),
                    "prio": task_spec.get("priority", "medium"),
                    "comp": comp, "aid": aid, "secs": secs, "toks": toks,
                })
                added_task_count += 1

            dep_suffix = spec.get("depends_on_suffix")
            if dep_suffix and dep_suffix in suffix_to_bid:
                upstream_bid = suffix_to_bid[dep_suffix]
                upstream_last_row = (await db.execute(text("""
                    SELECT id FROM tasks WHERE board_id = :bid
                    ORDER BY created_at DESC LIMIT 1
                """), {"bid": str(upstream_bid)})).first()
                if upstream_last_row:
                    ds = await db.execute(text(
                        "SELECT id FROM tasks WHERE board_id = :bid"
                    ), {"bid": str(board_id)})
                    for r in ds.fetchall():
                        await db.execute(text("""
                            INSERT INTO task_dependencies (id, task_id, depends_on_task_id, created_at)
                            VALUES (:id, :tid, :dep, NOW()) ON CONFLICT DO NOTHING
                        """), {"id": str(uuid4()), "tid": str(r[0]),
                               "dep": str(upstream_last_row[0])})
                        cross_deps += 1

    # Attach added_tasks to existing boards by suffix match
    for ts in added_tasks:
        suffix = ts.get("board_suffix")
        bid = suffix_to_bid.get(suffix)
        if not bid:
            continue
        task_id = uuid4()
        comp = int(ts.get("complexity") or 3)
        budget_table = {1: (600, 20_000), 2: (1200, 40_000), 3: (1800, 80_000),
                        4: (3600, 200_000), 5: (7200, 500_000)}
        secs, toks = budget_table.get(comp, (1800, 80_000))
        role = ts.get("role")
        aid = None
        if role:
            r = await db.execute(text("""
                SELECT a.id FROM agents a
                WHERE a.status='online'
                  AND (a.name = :exact OR a.name LIKE :prefix)
                ORDER BY a.name LIMIT 1
            """), {"exact": role, "prefix": f"{role}%"})
            row = r.first()
            aid = str(row[0]) if row else None
        await db.execute(text("""
            INSERT INTO tasks
                (id, board_id, title, description, status, priority,
                 complexity, created_by_type, created_by_name,
                 assigned_agent_id, max_seconds_budget, max_tokens_budget,
                 created_at, updated_at)
            VALUES (:id, :bid, :title, :desc, 'inbox', :prio,
                    :comp, 'system', 'Project Refiner',
                    :aid, :secs, :toks, NOW(), NOW())
        """), {
            "id": str(task_id), "bid": str(bid),
            "title": ts["title"],
            "desc": ts.get("description", ts["title"]),
            "prio": ts.get("priority", "medium"),
            "comp": comp, "aid": aid, "secs": secs, "toks": toks,
        })
        added_task_count += 1

    await db.commit()
    return {
        "project_id": str(project_id),
        "added_boards": added_board_count,
        "added_tasks": added_task_count,
        "cross_deps": cross_deps,
        "planner_model": model_id,
        "planner_raw_chars": len(raw),
    }
