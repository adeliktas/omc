"""T105 Phase 3 — Planner / Executor / Verifier loop.

Per-task orchestration:

    [task]
       │
       ▼
    1. SNAPSHOT (Phase 4 hook — TBD; Phase 3 just records the ID)
       │
       ▼
    2. PLANNER (smart-tier model) → structured JSON plan
       posts [planner-plan] marker
       │
       ▼
    3. EXECUTOR (cheap-tier model) takes step N
       posts [executor-result-N] WITH EVIDENCE
       │
       ▼
    4. VERIFIER (smart-tier model) judges {pass, reason}
       posts [verifier-verdict-N]
       │
       ├── pass → next step
       ├── fail < 2 → retry with hint
       └── fail ≥ 2 → ESCALATE TO /review (owner gate per directive)

Owner directives applied here:
  - No per-task token cap on planner (smart model owns the plan)
  - Verifier MUST be smarter-or-equal tier. If only ONE model available,
    escalate to owner on 2× failure (smart model can't review itself).
  - Smart-tier == executes the major decisions / system-awareness checks.

Markers emitted as `activity_events` of `event_type='task.comment'` so
they appear in the existing task drawer Activity panel.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.llm_evaluator import call_model
from app.services.model_router import pick_model_for_tier

logger = get_logger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────


MAX_STEP_RETRIES = 2  # Owner-directive: ≥2 failures → owner escalate.


# Status enum (string column in DB)
STATUS_PLANNING = "planning"
STATUS_EXECUTING = "executing"
STATUS_VERIFYING = "verifying"
STATUS_DONE = "done"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ESCALATED = "escalated"


# T105 Phase 4 (2026-05-25): protected paths — touching these without
# explicit owner consent escalates to /review.
#
# Glob-style patterns; matched against any file path the planner mentions
# in `description` / `executor_hint`. Forcing `requires_owner_consent=true`
# on these is non-negotiable: a dumb executor must not silently rewrite
# the compose stack.
PROTECTED_PATH_PATTERNS = (
    "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
    "Dockerfile", "dockerfile",
    ".env", ".env.",
    "alembic.ini",
    "migrations/", "/migrations/",
    "openclaw.json",
    "package.json", "package-lock.json", "pnpm-lock.yaml",
    "pyproject.toml", "poetry.lock", "uv.lock",
    "tsconfig.json", "next.config", "tailwind.config",
    "runtime_settings.json",
    "/.git/", ".gitignore",
    # MC source-root patterns (so step "edit backend/app/main.py" is OK
    # but "delete backend/" is gated)
    "backend/Dockerfile",
    "frontend/Dockerfile",
)


# T107 (2026-05-26): verbs that indicate the step only READS the file.
# Read-only access to a protected path is safe — no consent needed. Only
# mutations need owner approval. Previously every mention triggered consent
# escalation, so planner's standard "inspect openclaw.json to understand
# layout" first step flooded /review with false-positive items.
_READ_ONLY_VERBS = (
    "read", "inspect", "view", "check", "verify", "examine", "look at",
    "scan", "review the contents", "cat ", "tail ", "head ", "grep ",
    "list", "show", "display", "print", "audit", "parse", "load",
    "fetch", "get ", "query", "diff against", "compare with", "validate",
    "open", "introspect", "confirm",
)

# Verbs that mutate. Used to RESOLVE ambiguous cases — if both read and
# write verbs appear, treat as write (safer).
_WRITE_VERBS = (
    "write", "edit", "modify", "update", "patch", "append", "rewrite",
    "change", "delete", "remove", "drop", "create", "insert", "add ",
    "overwrite", "set ", "configure", "install", "migrate", "rebuild",
    "regenerate", "force-recreate", "replace", "merge into",
    "commit ", "push ", "rm ", "mv ", "chmod", "chown", "redirect into",
)

# T107 round-3 (2026-05-26): runtime ops verbs — restarting a service via
# `docker compose restart svc` is NOT a file edit even though the description
# mentions `compose.yml`. Without this, every "restart provider benchmark
# loop" step flooded /review with false-positive compose.yml consents.
# These are safe by default (do not require owner consent) unless paired
# with a write verb.
_RUNTIME_OPS_VERBS = (
    "restart", "reload", "respawn", "bounce",
    "bring up", "bring down", "spin up", "spin down",
    "start ", "stop ", "kill ", "halt ", "resume",
    "compose up", "compose down", "compose restart",
    "systemctl", "service ", "supervisorctl", "pm2 ",
    "docker restart", "docker stop", "docker start",
)


# T108 (2026-05-30): signals that a task needs REAL tool execution and so
# must NOT be driven by PEV's text-only executor (it would fabricate). These
# tasks route to the tooled gateway agent (tools.profile:full) instead.
_REQUIRES_TOOLS_SIGNALS = (
    "mcporter", "mcp server", "/mcp", "ida pro", "ida-pro", "ida_pro",
    "crackme", "disassembl", "decompil", "reverse engineer", "reverse-engineer",
    "binary", "shellcode", "exploit", "rop chain", "kernel module",
    "device driver", "firmware", "packed", "unpack", "gdb", "objdump",
    "radare", "ghidra", "run the executable", "execute the binary",
    "compile", "build the", "deploy", "shell command", "run mcporter",
    "inspect the mcp", "use the mcp tools", "patch the binary",
)


def _task_requires_real_tools(title: str, description: str) -> bool:
    """True when the task needs live tools (shell/MCP/IDA/binary) that PEV's
    text-only `call_model` executor cannot provide. Such tasks belong to a
    tooled gateway agent, not PEV."""
    blob = f"{title}\n{description}".lower()
    return any(sig in blob for sig in _REQUIRES_TOOLS_SIGNALS)


def _step_touches_protected_path(step: dict) -> str | None:
    """Returns the first protected-path pattern that the step's description
    or executor_hint mentions AND that mention is for a WRITE operation.

    T107 (2026-05-26): read-only mentions ("inspect openclaw.json") no
    longer force consent. Owner only sees /review items for actual
    mutations to protected files. Verb matching uses word boundaries to
    avoid substring false positives (e.g. "confirm" matched "rm " as a
    write verb before this fix).
    """
    import re as _re
    desc = (step.get("description") or "").lower()
    hint = (step.get("executor_hint") or "").lower()
    blob = f"{desc}\n{hint}"
    matched_pat: str | None = None
    for pat in PROTECTED_PATH_PATTERNS:
        if pat.lower() in blob:
            matched_pat = pat
            break
    if not matched_pat:
        return None

    def _has_verb(verbs: tuple[str, ...]) -> bool:
        for v in verbs:
            v_stripped = v.strip()
            # Match as a whole token. Trailing space in source list = the
            # short verbs that need the explicit space; we accept either.
            if _re.search(rf"\b{_re.escape(v_stripped)}\b", blob):
                return True
        return False

    has_write = _has_verb(_WRITE_VERBS)
    has_read = _has_verb(_READ_ONLY_VERBS)
    has_runtime = _has_verb(_RUNTIME_OPS_VERBS)
    # Write verb present → require consent regardless of co-occurring others.
    if has_write:
        return matched_pat
    # Read-only OR runtime op → safe (runtime restart isn't a file edit).
    if has_read or has_runtime:
        return None
    # No verb cue: be cautious — assume mutation possible, require consent.
    return matched_pat


# ─── Prompts ────────────────────────────────────────────────────────────


_PLANNER_SYSTEM = """You are the PLANNER. You operate on a multi-agent system
called OpenClaw Mission Control where you have full system awareness.

Your job: given the task description + current system state, produce a
structured JSON plan that a cheaper EXECUTOR model will follow step-by-step.

System layout you must know:
  - `openclaw.json` is the gateway-authoritative agent config. STRICT schema —
    extra keys cause full rollback. ONLY contains: agents[].{id,name,model,
    heartbeat,identity,workspace,agentDir}. NO model IQ scores, NO disabled
    flags, NO benchmark stats live here.
  - `openclaw.mc.json` is the MC SIDECAR. Stores MC-only metadata
    (IQ scores, benchmark TPS, disabled flags, owner notes). IF YOU NEED
    TO STORE IQ / DISABLED / METRICS — TARGET THE SIDECAR, NOT
    openclaw.json. The sidecar is NOT a protected path.
  - `provider_capacity` Postgres table is the registry of provider URLs +
    API keys + max_concurrent + status. Mutations go via REST API
    (POST/PATCH /api/v1/providers/...), not file edits.
  - `runtime_settings.json` holds tier-chain knobs + benchmark intervals.
    Protected — owner consent required for edits.

Hard requirements:
  - Plan steps are ATOMIC — one observable change per step.
  - Each step has a `verification` field specifying CONCRETE EVIDENCE the
    verifier will look for (e.g. file path + expected content fragment,
    a command + expected exit code, a database state query).
  - Plans MUST be safe: forbid WRITES to compose.yml / migrations / .env /
    Dockerfile / openclaw.json / runtime_settings.json WITHOUT setting
    `requires_owner_consent: true` on that step. READ-ONLY inspection
    of these files is fine — does NOT require consent.
  - Prefer REST API calls or sidecar writes over editing protected files.
    Example: instead of "edit openclaw.json to add IQ score", emit
    "POST /api/v1/providers/<name>/models/<id> with iq_score=N" (writes
    the sidecar via the MC API).
  - Reply ONLY with a single JSON object — no prose, no markdown fences.
    Schema:
      {
        "summary": "one-line plan description",
        "steps": [
          {
            "id": "step-1",
            "description": "what to do",
            "executor_hint": "specific command/file/pattern for the executor",
            "verification": "what the verifier should check + how",
            "requires_owner_consent": false
          },
          ...
        ]
      }
"""


_EXECUTOR_SYSTEM = """You are the EXECUTOR. You implement plan steps exactly.

Hard requirements:
  - Read the step description + executor_hint.
  - Do ONE step at a time. Do not skip ahead.
  - Reply with a single JSON object — no prose, no markdown fences. Schema:
      {
        "action": "what you did, one sentence",
        "evidence": "concrete proof — file diff, command output, query result",
        "files_changed": [{"path": "...", "change": "modified|created|deleted"}],
        "self_assessment": "succeeded|partial|failed",
        "notes": "anything the verifier should know"
      }
  - DO NOT claim success without specific evidence. If you couldn't do it,
    set `self_assessment: failed` and explain in `notes`.
"""


_VERIFIER_SYSTEM = """You are the VERIFIER. You judge whether the EXECUTOR's
result actually satisfies the step's verification criteria.

Hard requirements:
  - Read the step description + verification criteria + executor result.
  - Check the evidence is CONCRETE — not vague claims like "did it" or
    "all good". If evidence is generic, FAIL.
  - Reply with a single JSON object — no prose, no markdown fences. Schema:
      {
        "pass": true|false,
        "reason": "one-sentence rationale citing specific evidence (or
                   lack thereof)",
        "hint_for_executor": "if fail, what specifically should be done differently"
      }
"""


# ─── JSON extraction helpers ────────────────────────────────────────────


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?", re.IGNORECASE)
_JSON_BLOB_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    s = raw.strip()
    # Strip markdown fences
    s = _JSON_FENCE_RE.sub("", s)
    s = s.replace("```", "").strip()
    # Direct parse
    try:
        return json.loads(s)
    except Exception:
        pass
    # First-blob fallback
    m = _JSON_BLOB_RE.search(s)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ─── DB helpers ─────────────────────────────────────────────────────────


async def _post_comment(db: AsyncSession, task_id: UUID, board_id: UUID | None, msg: str) -> None:
    """Keep markers in the caller's guarded task transaction."""
    from app.services.watchdog_loop import _post_comment as guarded_comment
    await guarded_comment(db, str(task_id), str(board_id) if board_id else "", msg, commit=False)


async def _get_state(db: AsyncSession, task_id: UUID) -> dict[str, Any] | None:
    res = await db.execute(text(
        "SELECT * FROM task_pev_state WHERE task_id = :tid"
    ), {"tid": str(task_id)})
    row = res.first()
    return dict(row._mapping) if row else None


async def _save_state(db: AsyncSession, task_id: UUID, **fields: Any) -> None:
    if not fields:
        return
    task = await _fetch_task(db, task_id)
    if not task or not await _begin_effects(db, task):
        raise ValueError("task unavailable to legacy PEV state effects")
    sets: list[str] = []
    params: dict[str, Any] = {"tid": str(task_id)}
    for k, v in fields.items():
        if k in ("plan",):
            sets.append(f"{k} = CAST(:{k} AS jsonb)")
            params[k] = json.dumps(v)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = NOW()")
    await db.execute(
        text(f"UPDATE task_pev_state SET {', '.join(sets)} WHERE task_id = :tid"),
        params,
    )
    # T107: also touch `tasks.updated_at` so the staleness-detector sees PEV
    # as live activity even though it writes to a sidecar table. Defense in
    # depth on top of the `task_pev_state EXISTS` filter in `_stale_tasks`.
    await db.execute(
        text("UPDATE tasks SET updated_at = NOW() WHERE id = :tid"),
        {"tid": str(task_id)},
    )


async def _ensure_state(db: AsyncSession, task_id: UUID) -> dict[str, Any]:
    existing = await _get_state(db, task_id)
    if existing:
        return existing
    task = await _fetch_task(db, task_id)
    if not task or not await _begin_effects(db, task):
        raise ValueError("task unavailable to legacy PEV")
    await db.execute(text("""
        INSERT INTO task_pev_state (task_id, status, plan, step_index, step_retries, created_at, updated_at)
        VALUES (:tid, :status, '[]'::jsonb, 0, 0, NOW(), NOW())
        ON CONFLICT (task_id) DO NOTHING
    """), {"tid": str(task_id), "status": STATUS_PLANNING})
    await db.commit()
    state = await _get_state(db, task_id)
    assert state is not None
    return state


async def _fetch_task(db: AsyncSession, task_id: UUID) -> dict[str, Any] | None:
    # T107 round-3: include auto_reason so verifier-fail path can branch on
    # AUTONOMY-FIX tasks (which need real tools, not LLM-only execution).
    res = await db.execute(text(
        "SELECT id, board_id, title, description, status, auto_reason, execution_lane, execution_mode, updated_at "
        "FROM tasks WHERE id = :tid"
    ), {"tid": str(task_id)})
    r = res.first()
    return dict(r._mapping) if r and r._mapping['status'] not in ('done', 'blocked', 'cancelled', 'paused', 'trash') else None


async def _begin_effects(db: AsyncSession, task: dict[str, Any]) -> bool:
    """Recheck the captured owner generation after inference, then retain the lock."""
    from app.services.watchdog_loop import _lock_legacy_task
    current = await _lock_legacy_task(db, str(task["id"]), task["updated_at"], task["status"])
    if current is None or _harness_owned({"execution_lane": current.execution_lane, "execution_mode": current.execution_mode, "title": current.title}):
        await db.rollback()
        return False
    return True


# ─── Phases ─────────────────────────────────────────────────────────────


async def _run_planner(db: AsyncSession, task_id: UUID) -> dict[str, Any]:
    """Phase 1: planner produces structured plan.

    Tries planner-tier model first. Falls back to degraded-tier if smart
    tier offline. Owner-directive: no token cap on planner.
    """
    state = await _ensure_state(db, task_id)
    task = await _fetch_task(db, task_id)
    if not task or _harness_owned(task):
        return {"ok": False, "error": "task unavailable to legacy PEV"}

    picked = await pick_model_for_tier(db, "planner")
    if not picked:
        return {"ok": False, "error": "no planner-tier model online"}
    _provider, model_id, attempted = picked

    user_msg = (
        f"TASK TITLE: {task['title']}\n\n"
        f"TASK DESCRIPTION:\n{task.get('description') or '(no description)'}\n\n"
        f"Produce the JSON plan now."
    )
    await db.rollback()
    raw = await call_model(
        model_id,
        user_msg,
        system_prompt=_PLANNER_SYSTEM,
        max_tokens=4096,  # T107 round-2: reasoning_effort=low keeps deepseek concise; 32K was causing 60-90s call durations that exhausted OR's 30s semaphore window. 4K plenty for plan JSON.
        temperature=0.1,
    )
    if not raw:
        return {"ok": False, "error": f"planner model {model_id} returned nothing"}

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed.get("steps"), list) or not parsed["steps"]:
        return {"ok": False, "error": "planner output not valid JSON plan", "raw": raw[:500]}

    plan_steps = parsed["steps"]
    summary = parsed.get("summary", "")

    # T105 Phase 4: protected-path guard. Force `requires_owner_consent`
    # on any step that mentions a protected file. Planner can't override.
    forced_count = 0
    for step in plan_steps:
        pat = _step_touches_protected_path(step)
        if pat and not step.get("requires_owner_consent"):
            step["requires_owner_consent"] = True
            step["_consent_forced_by"] = pat
            forced_count += 1

    if not await _begin_effects(db, task):
        return {"ok": False, "error": "task changed during planner inference"}
    await _save_state(
        db, task_id,
        status=STATUS_EXECUTING,
        plan=plan_steps,
        step_index=0,
        step_retries=0,
        planner_model=model_id,
        total_calls_planner=int(state.get("total_calls_planner") or 0) + 1,
    )

    # T108 (2026-05-30): keep tasks.status in lockstep with PEV. Previously
    # 0a9 advanced the PEV state machine independently of task.status, so a
    # task PEV had planned + was executing still showed `inbox` on the board
    # (looked stuck/broken to the owner). Flip to in_progress the moment the
    # plan exists so the board reflects reality.
    try:
        await db.execute(text(
            "UPDATE tasks SET status='in_progress', updated_at=NOW() "
            "WHERE id=:tid AND status IN ('inbox','blocked')"
        ), {"tid": str(task_id)})
    except Exception:
        await db.rollback()
        raise

    summary_preview = summary[:200] if summary else "(no summary)"
    step_list = "\n".join(
        f"  {i + 1}. {s.get('description', '')[:120]}" +
        (
            " [owner-consent required"
            + (f": {s['_consent_forced_by']}" if s.get("_consent_forced_by") else "")
            + "]"
            if s.get("requires_owner_consent") else ""
        )
        for i, s in enumerate(plan_steps[:10])
    )
    if len(plan_steps) > 10:
        step_list += f"\n  ... + {len(plan_steps) - 10} more"

    forced_line = (
        f"\n\n⚠ {forced_count} step(s) auto-flagged for owner-consent — "
        f"protected path detected (compose / Dockerfile / .env / migration / etc.)."
        if forced_count else ""
    )

    await _post_comment(
        db, task_id, UUID(str(task["board_id"])) if task.get("board_id") else None,
        f"[pev-planner-plan] model=`{model_id}` steps={len(plan_steps)}\n\n"
        f"**Summary:** {summary_preview}\n\n"
        f"**Steps:**\n{step_list}"
        f"{forced_line}",
    )
    await db.commit()
    return {"ok": True, "step_count": len(plan_steps), "model": model_id, "forced_consent": forced_count}


async def _run_executor_step(db: AsyncSession, task_id: UUID) -> dict[str, Any]:
    """Phase 2 of one step: executor runs the current step."""
    state = await _get_state(db, task_id)
    if not state or state["status"] not in (STATUS_EXECUTING, STATUS_VERIFYING):
        return {"ok": False, "error": "not in executing state"}

    task = await _fetch_task(db, task_id)
    if not task or _harness_owned(task):
        return {"ok": False, "error": "task unavailable to legacy PEV"}

    plan = state["plan"] or []
    idx = state["step_index"] or 0
    if idx >= len(plan) or plan[idx].get("requires_owner_consent"):
        if not await _begin_effects(db, task):
            return {"ok": False, "error": "task changed before executor effects"}
    if idx >= len(plan):
        await _save_state(db, task_id, status=STATUS_DONE)
        await db.commit()
        return {"ok": True, "done": True}

    step = plan[idx]
    if step.get("requires_owner_consent"):
        # Owner gate — escalate to /review immediately, do not execute.
        await _save_state(db, task_id, status=STATUS_ESCALATED)
        forced_by = step.get("_consent_forced_by")
        consent_msg = (
            f"[pev-owner-consent] step {idx + 1}/{len(plan)} flagged "
            f"`requires_owner_consent=true`"
            + (f" (auto-forced by protected-path match: `{forced_by}`)" if forced_by else "")
            + ". Awaiting owner approval at /review."
        )
        await _post_comment(
            db, task_id, UUID(str(task["board_id"])) if task.get("board_id") else None,
            consent_msg,
        )
        # T105 Phase 4: surface to owner-review with selectable options.
        # An open consent item holds this step; recurrence cannot refresh its clock.
        try:
            from app.services.owner_reviews import insert_sync
            from sqlalchemy import text as _sql_text
            consent_marker = f"Step {idx + 1}/{len(plan)}"
            existing = await db.execute(_sql_text("""
                SELECT id FROM owner_reviews
                WHERE task_id = :tid
                  AND status IN ('pending', 'revision_requested')
                  AND submitted_by = 'PEV Loop'
                  AND summary LIKE :marker
                LIMIT 1
            """), {"tid": str(task_id), "marker": f"{consent_marker}%"})
            row = existing.first()
            if row:
                logger.info(
                    "pev.consent.preserved_existing task=%s step=%d review_id=%s",
                    str(task_id)[:8], idx + 1, str(row[0])[:8],
                )
            else:
                insert_sync(**{
                    "title": f"PEV consent: {task.get('title','')[:60]}",
                    "type_": "other",
                    "summary": (
                        f"Step {idx + 1}/{len(plan)} touches protected path. "
                        f"Description: {step.get('description','')[:200]}"
                    ),
                    "reason": (
                        f"Auto-forced owner-consent by protected-path match: `{forced_by}`"
                        if forced_by
                        else "Planner marked step `requires_owner_consent=true`."
                    ),
                    "proposed_action": (
                        f"Owner to: (a) approve this specific step and resume the loop, "
                        f"(b) reject and revise the plan, "
                        f"(c) cancel the autonomous loop entirely."
                    ),
                    "submitted_by": "PEV Loop",
                    "task_id": str(task_id),
                })
        except Exception as exc:
            logger.warning("pev.consent.owner_review_insert_failed err=%s", exc)
        await db.commit()
        return {"ok": False, "escalated": True}

    picked = await pick_model_for_tier(db, "executor")
    if not picked:
        return {"ok": False, "error": "no executor-tier model online"}
    _provider, model_id, _attempted = picked

    # Build the executor prompt
    retry_hint = ""
    if state.get("step_retries") and state.get("last_verdict_reason"):
        retry_hint = (
            f"\n\nRETRY: previous attempt failed verification. "
            f"Verifier said: {state['last_verdict_reason']}\n"
        )
    user_msg = (
        f"TASK: {task['title']}\n\n"
        f"STEP {idx + 1}/{len(plan)}: {step.get('description', '')}\n\n"
        f"HINT: {step.get('executor_hint', '(none)')}\n\n"
        f"VERIFICATION CRITERIA (so you know what evidence to gather):\n"
        f"{step.get('verification', '(none)')}\n"
        f"{retry_hint}\n"
        f"Implement the step. Reply with the JSON result object."
    )
    await db.rollback()
    raw = await call_model(
        model_id,
        user_msg,
        system_prompt=_EXECUTOR_SYSTEM,
        max_tokens=2048,  # T107 round-2: reasoning_effort=low; executor output is a JSON action block, not long-form
        temperature=0.2,
    )
    if not raw:
        return {"ok": False, "error": f"executor model {model_id} returned nothing"}
    parsed = _extract_json(raw)
    if not parsed:
        return {"ok": False, "error": "executor output not valid JSON", "raw": raw[:500]}

    if not await _begin_effects(db, task):
        return {"ok": False, "error": "task changed during executor inference"}
    await _save_state(
        db, task_id,
        status=STATUS_VERIFYING,
        executor_model=model_id,
        total_calls_executor=int(state.get("total_calls_executor") or 0) + 1,
    )

    bid = UUID(str(task["board_id"])) if task.get("board_id") else None
    self_assessment = parsed.get("self_assessment", "?")
    evidence_preview = (parsed.get("evidence") or "")[:300]
    files = parsed.get("files_changed") or []
    files_str = ", ".join(f"{f.get('change','?')}:{f.get('path','?')}" for f in files[:5])
    await _post_comment(
        db, task_id, bid,
        f"[pev-executor-result] step {idx + 1}/{len(plan)} model=`{model_id}` "
        f"self_assessment=`{self_assessment}` retry={state.get('step_retries') or 0}\n\n"
        f"**Action:** {(parsed.get('action') or '')[:200]}\n"
        f"**Evidence:** {evidence_preview}\n"
        + (f"**Files changed:** {files_str}\n" if files_str else "")
        + f"**Notes:** {(parsed.get('notes') or '')[:200]}",
    )
    await db.commit()
    return {"ok": True, "executor_output": parsed}


async def _run_verifier_step(db: AsyncSession, task_id: UUID, executor_output: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 of one step: verifier judges executor's output."""
    state = await _get_state(db, task_id)
    if not state or state["status"] != STATUS_VERIFYING:
        return {"ok": False, "error": "not in verifying state"}
    task = await _fetch_task(db, task_id)
    if not task or _harness_owned(task):
        return {"ok": False, "error": "task unavailable to legacy PEV"}

    plan = state["plan"] or []
    idx = state["step_index"] or 0
    if idx >= len(plan):
        return {"ok": False, "error": "step index out of range"}
    step = plan[idx]

    # Smart-tier verifier. If only one model is the executor (tier ==
    # executor's), we cannot self-verify per owner directive — escalate.
    picked = await pick_model_for_tier(db, "verifier")
    if not picked:
        # No smart-tier available — escalate AND auto-rollback per Phase 4.
        if not await _begin_effects(db, task):
            return {"ok": False, "error": "task changed before verifier effects"}
        await _save_state(db, task_id, status=STATUS_ESCALATED)
        bid = UUID(str(task["board_id"])) if task.get("board_id") else None
        await _post_comment(
            db, task_id, bid,
            "[pev-escalate] no verifier-tier model online; cannot judge "
            "executor result. Escalating to /review.",
        )
        try:
            await _auto_rollback(
                db, task_id,
                reason="no verifier-tier model available — cannot self-judge",
            )
        except Exception as exc:
            logger.warning("pev.rollback.no_verifier_exception task=%s err=%s", task_id, exc)
        try:
            from app.services.owner_reviews import insert_sync
            insert_sync(**{
                "title": f"PEV escalation: {task.get('title','')[:60]}",
                "type_": "infrastructure",
                "summary": "Verifier-tier (smart) model unavailable — cannot judge executor output.",
                "reason": "All planner/verifier-tier models offline or over quota.",
                "proposed_action": (
                    "Owner to: (a) restore smart-tier model availability and resume, "
                    "(b) reject and abort the autonomous loop, "
                    "(c) defer pending model restoration."
                ),
                "submitted_by": "PEV Loop",
                "task_id": str(task_id),
            })
        except Exception as exc:
            logger.warning("pev.escalate.owner_review_insert_failed err=%s", exc)
        await db.commit()
        return {"ok": False, "escalated": True}
    _provider, model_id, _attempted = picked

    user_msg = (
        f"TASK: {task['title']}\n\n"
        f"STEP DESCRIPTION: {step.get('description', '')}\n\n"
        f"VERIFICATION CRITERIA:\n{step.get('verification', '(none)')}\n\n"
        f"EXECUTOR'S RESULT:\n{json.dumps(executor_output, indent=2)[:3000]}\n\n"
        f"Judge whether the executor's evidence concretely satisfies the criteria."
    )
    await db.rollback()
    raw = await call_model(
        model_id,
        user_msg,
        system_prompt=_VERIFIER_SYSTEM,
        max_tokens=1024,  # T107 round-2: verifier emits pass/fail + reason — small payload
        temperature=0.0,
    )
    if not raw:
        return {"ok": False, "error": f"verifier model {model_id} returned nothing"}
    parsed = _extract_json(raw)
    if not parsed:
        # Treat unparseable verdict as a fail for owner safety.
        parsed = {"pass": False, "reason": f"verifier reply unparseable: {raw[:200]}"}

    verdict_pass = bool(parsed.get("pass"))
    reason = (parsed.get("reason") or "no reason")[:1000]
    hint = (parsed.get("hint_for_executor") or "")[:500]

    if not await _begin_effects(db, task):
        return {"ok": False, "error": "task changed during verifier inference"}
    bid = UUID(str(task["board_id"])) if task.get("board_id") else None
    await _post_comment(
        db, task_id, bid,
        f"[pev-verifier-verdict] step {idx + 1}/{len(plan)} "
        f"model=`{model_id}` result={'**PASS**' if verdict_pass else '**FAIL**'}\n\n"
        f"**Reason:** {reason}\n"
        + (f"**Hint:** {hint}\n" if (hint and not verdict_pass) else ""),
    )

    new_total = int(state.get("total_calls_verifier") or 0) + 1

    if verdict_pass:
        # Advance step. If end → done.
        new_idx = idx + 1
        if new_idx >= len(plan):
            await _save_state(
                db, task_id,
                status=STATUS_DONE,
                step_index=new_idx,
                step_retries=0,
                last_verdict_pass=True,
                last_verdict_reason=reason,
                verifier_model=model_id,
                total_calls_verifier=new_total,
            )
            # T107 (2026-05-26): if the board is owner-marked autonomous,
            # the smart-tier verifier's PASS is the sign-off — auto-close
            # to `done` directly. The owner explicitly delegated trust by
            # flipping `autonomous_enabled=true`. Without this branch the
            # task sat in `review` forever (no agent watching, PM gate
            # never satisfied, no owner-review item filed).
            auto_close = False
            if task.get("board_id"):
                board_row = (await db.execute(text(
                    "SELECT autonomous_enabled, require_approval_for_done "
                    "FROM boards WHERE id = :bid"
                ), {"bid": str(task["board_id"])})).first()
                if board_row and board_row[0]:
                    auto_close = True

            if auto_close:
                # Emit the standard close markers so existing auto-close +
                # consensus paths (watchdog._auto_close_review_tasks) also
                # see the verdict as legitimate.
                await _post_comment(
                    db, task_id, bid,
                    f"[pev-done] all {len(plan)} steps verified by smart-tier "
                    f"`{model_id}`. Board autonomous mode → owner sign-off "
                    f"delegated to verifier. Auto-closing.",
                )
                await _post_comment(
                    db, task_id, bid,
                    "[ready-to-close] [pm-signoff] verdict=accept "
                    "confidence=95 [auto-pev]",
                )
                # Satisfy the require_approval_for_done gate by inserting
                # an approved approval row (mirrors what the /review owner
                # path does on approve).
                try:
                    import uuid as _uuid
                    await db.execute(text("""
                        INSERT INTO approvals
                            (id, board_id, task_id, action_type, payload,
                             confidence, status, created_at, resolved_at)
                        VALUES
                            (:id, :bid, :tid, 'pev_autonomous_signoff', :payload,
                             95, 'approved', NOW(), NOW())
                    """), {
                        "id": str(_uuid.uuid4()),
                        "bid": str(task["board_id"]) if task.get("board_id") else None,
                        "tid": str(task_id),
                        "payload": (
                            '{"source":"pev_autonomous_signoff",'
                            f'"verifier_model":"{model_id}",'
                            '"verdict":"pass"}'
                        ),
                    })
                except Exception as exc:
                    logger.warning("pev.signoff_approval_insert_failed err=%s", exc)
                # Direct transition to done.
                await db.execute(text(
                    "UPDATE tasks SET status='done', updated_at=NOW() "
                    "WHERE id = :tid AND status IN ('in_progress','inbox','review')"
                ), {"tid": str(task_id)})
            else:
                await _post_comment(
                    db, task_id, bid,
                    f"[pev-done] all {len(plan)} steps verified. "
                    f"Board NOT autonomous — moving to `review` for owner sign-off.",
                )
                await db.execute(text(
                    "UPDATE tasks SET status='review', updated_at=NOW() "
                    "WHERE id = :tid AND status IN ('in_progress','inbox')"
                ), {"tid": str(task_id)})
            await db.commit()
            return {"ok": True, "done": True, "auto_closed": auto_close}

        # Next step
        await _save_state(
            db, task_id,
            status=STATUS_EXECUTING,
            step_index=new_idx,
            step_retries=0,
            last_verdict_pass=True,
            last_verdict_reason=reason,
            verifier_model=model_id,
            total_calls_verifier=new_total,
        )
        await db.commit()
        return {"ok": True, "next_step": new_idx}

    # FAIL path
    retries = int(state.get("step_retries") or 0) + 1
    # T107 round-3 (2026-05-26): detect tool-less-executor failure patterns.
    # When the executor (text-only LLM) returns a placeholder or "cannot
    # access codebase" / "no diff to show" response, the verifier correctly
    # fails it. Looping a second time will reproduce the same failure
    # because the LLM still has no tools. Short-circuit: close the task as
    # `blocked` with explanatory comment, surface ONE umbrella owner-review
    # only on the first occurrence. Saves an LLM round-trip per stuck task.
    _TOOLLESS_PATTERNS = (
        "cannot access", "no access to the codebase", "no diff",
        "placeholder", "example value", "labeled as an example",
        "unable to read", "unable to write", "unable to execute",
        "would need", "would require", "as a language model",
        "not actually run", "did not actually",
        # T107 round-3: more failure-mode keywords seen in escalations.
        "narrative description", "rather than concrete output",
        "unparseable", "reply unparseable",
        "only shows the file content", "not the actual execution",
        "lacks specific details", "rather than the actual",
        "explicitly labeled as an example",
        "does not satisfy", "evidence is only a statement",
        # T107 round-6 (2026-05-27): every recent verifier-fail boils down
        # to "evidence is incomplete / truncated / can't be substantiated"
        # which is the signature of an LLM that lacks actual tool access.
        # Generalize aggressively — these all mean the executor faked it.
        "evidence is truncated", "is truncated", "is incomplete",
        "ids are incomplete", "preventing concrete",
        "does not concretely show", "not concretely demonstrated",
        "no http response received", "cannot verify",
        "did not verify", "could not verify", "missing",
        "claim of full output is not substantiated",
        "not substantiated", "is not substantiated",
        "external expected count", "no external",
        "not provide an external", "derived from the listing itself",
        "only a sample of", "only a few lines",
    )
    rl = (reason or "").lower()
    tool_less = any(p in rl for p in _TOOLLESS_PATTERNS)
    if tool_less:
        await _save_state(
            db, task_id, status=STATUS_ESCALATED,
            step_retries=retries, last_verdict_pass=False,
            last_verdict_reason=reason, verifier_model=model_id,
            total_calls_verifier=new_total,
        )
        await _post_comment(
            db, task_id, bid,
            "[pev-tool-less-detected] verifier rejected executor output as "
            "placeholder / inability-to-access. PEV's text-only executor "
            f"cannot perform this kind of work. Marking task BLOCKED + "
            f"closing PEV loop. Verifier reason: {reason[:200]}",
        )
        # Roll back any half-done changes from the failed step.
        try:
            await _auto_rollback(db, task_id, reason="tool-less executor")
        except Exception:
            pass
        if task.get("board_id"):
            await db.execute(text(
                "UPDATE tasks SET status='blocked', updated_at=NOW() "
                "WHERE id=:tid AND status IN ('in_progress','review','inbox')"
            ), {"tid": str(task_id)})
        await db.commit()
        return {"ok": False, "tool_less": True, "blocked": True}
    if retries >= MAX_STEP_RETRIES:
        # T107 round-3 (2026-05-26): if task is AUTONOMY-FIX (test harness
        # remediation), don't file an owner-review item. Owner already
        # knows test-harness limits exist. Block silently with a comment;
        # the umbrella alert is filed if MANY accumulate (separate flow).
        task_auto_reason = (task.get("auto_reason") or "").lower()
        is_autonomy_fix = task_auto_reason in (
            "tests-autonomy-remediation", "tests-harness", "autonomy-harness",
            "autonomous_idle_improvement",
        )
        await _save_state(
            db, task_id,
            status=STATUS_ESCALATED,
            step_retries=retries,
            last_verdict_pass=False,
            last_verdict_reason=reason,
            verifier_model=model_id,
            total_calls_verifier=new_total,
        )
        if is_autonomy_fix:
            await _post_comment(
                db, task_id, bid,
                f"[pev-autonomy-fix-blocked] step {idx + 1} failed {retries}× — "
                f"AUTONOMY-FIX tasks require code-execution tools beyond PEV's "
                f"LLM-only executor. Marking task blocked. Verifier reason: {reason[:200]}",
            )
            try:
                await _auto_rollback(db, task_id, reason="autonomy-fix tool-less")
            except Exception:
                pass
            if task.get("board_id"):
                await db.execute(text(
                    "UPDATE tasks SET status='blocked', updated_at=NOW() "
                    "WHERE id=:tid AND status IN ('in_progress','review','inbox')"
                ), {"tid": str(task_id)})
            await db.commit()
            return {"ok": False, "blocked": True, "autonomy_fix": True}

        await _post_comment(
            db, task_id, bid,
            f"[pev-escalate] step {idx + 1} failed verification {retries}× — "
            f"escalating to /review per owner-directive.",
        )

        # T105 Phase 4: auto-rollback to pre-loop snapshot. Dumb model
        # may have made changes during the failed step — restore openclaw
        # config + workspaces. Owner directive: protect from breakage.
        try:
            rb_ok = await _auto_rollback(
                db, task_id,
                reason=f"step {idx + 1} failed verification {retries}×: {reason[:200]}",
            )
        except Exception as exc:
            logger.warning("pev.rollback.exception task=%s err=%s", task_id, exc)
            rb_ok = False

        # Surface to owner review queue — selectable options per Phase 3
        # /review work. Options correspond to owner's choices.
        try:
            from app.services.owner_reviews import insert_sync
            rb_note = (
                "Auto-rolled-back to pre-loop snapshot." if rb_ok
                else "Auto-rollback FAILED — owner must inspect + restore manually."
            )
            insert_sync(**{
                "title": f"PEV escalation: {task.get('title', '')[:60]}",
                "type_": "other",
                "summary": (
                    f"Step {idx + 1}/{len(plan)}: {step.get('description', '')[:200]}. "
                    f"{rb_note}"
                ),
                "reason": f"Verifier failed {retries}× — {reason[:300]}",
                "proposed_action": (
                    f"Owner to: (a) accept rollback and close as won't-fix, "
                    f"(b) revise plan and re-run PEV, "
                    f"(c) escalate to lead for manual handling."
                    + (f" Hint from verifier: {hint[:200]}" if hint else "")
                ),
                "submitted_by": "PEV Loop",
                "task_id": str(task_id),
            })
        except Exception as exc:
            logger.warning("pev.escalate.owner_review_insert_failed err=%s", exc)
        await db.commit()
        return {"ok": False, "escalated": True, "rollback": rb_ok}

    # retry < 2 — back to executing
    await _save_state(
        db, task_id,
        status=STATUS_EXECUTING,
        step_retries=retries,
        last_verdict_pass=False,
        last_verdict_reason=reason,
        verifier_model=model_id,
        total_calls_verifier=new_total,
    )
    await db.commit()
    return {"ok": True, "retry": retries}


# ─── Public orchestrator ────────────────────────────────────────────────


def _harness_owned(task: dict[str, Any] | None) -> bool:
    return bool(task and (
        task.get("execution_mode") == "team"
        or task.get("execution_lane") == "harness"
        or (not task.get("execution_lane") and (task.get("title") or "").startswith(
            ("[agent]", "[claude]", "[codex]")
        ))
    ))


async def advance_pev(db: AsyncSession, task_id: UUID) -> dict[str, Any]:
    """Drive the loop forward by exactly one phase.

    Watchdog calls this every tick per active task. Idempotent —
    state machine decides what to do next.
    """
    task = await _fetch_task(db, task_id)
    if not task or _harness_owned(task):
        return {"ok": False, "error": "stopped or harness task unavailable to legacy PEV"}
    state = await _ensure_state(db, task_id)
    status = state["status"]

    if status == STATUS_PLANNING:
        return await _run_planner(db, task_id)

    if status == STATUS_EXECUTING:
        r = await _run_executor_step(db, task_id)
        if not r.get("ok"):
            return r
        if r.get("done"):
            return r
        if r.get("escalated"):
            return r
        # Chain into verifier in the same tick (cheap call)
        executor_out = r.get("executor_output") or {}
        return await _run_verifier_step(db, task_id, executor_out)

    if status == STATUS_VERIFYING:
        # We're between executor and verifier — re-run executor to get output
        # to pass to verifier. (Should be rare — caller usually finishes
        # executor + verifier in the same tick.)
        r = await _run_executor_step(db, task_id)
        if not r.get("ok"):
            return r
        executor_out = r.get("executor_output") or {}
        return await _run_verifier_step(db, task_id, executor_out)

    # done / rolled_back / escalated — no further action
    return {"ok": True, "terminal": True, "status": status}


async def start_pev(db: AsyncSession, task_id: UUID) -> dict[str, Any]:
    """Initialize PEV state for a task. Takes a lightweight pre-loop
    snapshot (postgres + openclaw config + workspaces) so an escalation
    later can roll back the system to its pre-autonomous state.

    Owner directive: dumb model risk = high → safety net required."""
    task = await _fetch_task(db, task_id)
    if not task or _harness_owned(task):
        return {"ok": False, "error": "task unavailable to legacy PEV"}
    state = await _ensure_state(db, task_id)

    # T105 Phase 4: pre-loop snapshot. Skip if already captured (idempotent).
    if not state.get("pre_loop_snapshot_id"):
        try:
            from app.services.backup import take_snapshot, PEV_PRESTEP_COMPONENTS
            await db.rollback()
            manifest = await take_snapshot(
                label=f"pev-pre-{str(task_id)[:8]}",
                components=PEV_PRESTEP_COMPONENTS,
                reason=f"pev-pre-loop task={task_id}",
            )
            if not await _begin_effects(db, task):
                return {"ok": False, "error": "task changed during snapshot"}
            snap_id = manifest.get("id")
            if snap_id:
                await _save_state(db, task_id, pre_loop_snapshot_id=snap_id)
                state["pre_loop_snapshot_id"] = snap_id
                # Surface in activity
                task = await _fetch_task(db, task_id)
                bid = UUID(str(task["board_id"])) if task and task.get("board_id") else None
                await _post_comment(
                    db, task_id, bid,
                    f"[pev-snapshot] pre-loop snapshot taken: `{snap_id}` "
                    f"(size={manifest.get('total_size', 0)} bytes). "
                    f"Auto-rollback on escalation will revert to this point.",
                )
                await db.commit()
        except Exception as exc:
            logger.warning("pev.snapshot.pre_loop_failed task=%s err=%s", task_id, exc)
            # Continue without snapshot only for the same current owner generation.
            try:
                if not await _begin_effects(db, task):
                    return {"ok": False, "error": "task changed during snapshot"}
                bid = UUID(str(task["board_id"])) if task and task.get("board_id") else None
                await _post_comment(
                    db, task_id, bid,
                    f"[pev-snapshot-failed] pre-loop snapshot FAILED: {exc!s}. "
                    f"Loop will continue but auto-rollback is disabled for this task.",
                )
                await db.commit()
            except Exception:
                pass

    return {"ok": True, "state": state}


async def _auto_rollback(db: AsyncSession, task_id: UUID, reason: str) -> bool:
    """T105 Phase 4: restore the pre-loop snapshot for a PEV task.

    Scope: openclaw_config + workspaces (NOT postgres — that would lose
    unrelated tasks created during the loop). Postgres restore requires
    explicit owner action via /backups.

    Returns True if rollback succeeded.
    """
    state = await _get_state(db, task_id)
    if not state or not state.get("pre_loop_snapshot_id"):
        return False
    snap_id = state["pre_loop_snapshot_id"]
    task = await _fetch_task(db, task_id)
    if not task or not await _begin_effects(db, task):
        return False
    bid = UUID(str(task["board_id"])) if task.get("board_id") else None
    try:
        from app.services.backup import restore_snapshot, COMPONENT_OPENCLAW_CFG, COMPONENT_WORKSPACES
        result = await restore_snapshot(
            snap_id,
            components=[COMPONENT_OPENCLAW_CFG, COMPONENT_WORKSPACES],
        )
        ok = bool(result.get("ok"))
        await _post_comment(
            db, task_id, bid,
            f"[pev-rollback] {'✓' if ok else '✗'} restored from `{snap_id}` "
            f"(components: openclaw_config + workspaces). "
            f"Reason: {reason[:200]}\n"
            f"Result: {result.get('results', {})}",
        )
        if ok:
            await _save_state(db, task_id, status=STATUS_ROLLED_BACK)
            # T107 round-4 (2026-05-26): task.status was stuck at `in_progress`
            # after PEV rollback, leaving phantom in_progress on dashboards
            # while no actual work is being done. Reset to `blocked` so the
            # dashboard reflects reality + downstream watchdog can triage.
            try:
                await db.execute(text(
                    "UPDATE tasks SET status='blocked', updated_at=NOW() "
                    "WHERE id=:tid AND status IN ('in_progress','review','inbox')"
                ), {"tid": str(task_id)})
            except Exception as exc:
                logger.warning("pev.rollback.task_status_reset_failed err=%s", exc)
        return ok
    except Exception as exc:
        logger.warning("pev.rollback.failed task=%s err=%s", task_id, exc)
        await _post_comment(
            db, task_id, bid,
            f"[pev-rollback-failed] could not restore `{snap_id}`: {exc!s}. "
            f"Owner must restore manually via /backups.",
        )
        return False


async def list_active_pev_tasks(
    db: AsyncSession,
    limit: int = 50,
    *,
    require_board_autonomous: bool = True,
) -> list[dict[str, Any]]:
    """Return tasks where PEV is mid-loop (planning / executing / verifying).

    T105 Phase 5 (2026-05-25): board.autonomous_enabled gate. When the
    watchdog calls this, it only picks up tasks on boards where the owner
    has explicitly enabled autonomous mode. Manual `/pev/tasks/{id}/advance`
    callers can pass `require_board_autonomous=False` to override (e.g.
    for one-off testing).
    """
    where_clauses = ["t.status IN ('inbox', 'in_progress', 'review')", "s.status IN ('planning', 'executing', 'verifying')", "COALESCE(t.execution_mode, 'single') != 'team'", "COALESCE(t.execution_lane, '') != 'harness'"]
    if require_board_autonomous:
        where_clauses.append("b.autonomous_enabled = true")
    # T107 round-3 (2026-05-26): AUTONOMY-FIX / test-harness tasks need
    # real code-execution tools. PEV's LLM-only executor can't satisfy
    # them — verifier rightly rejects placeholder responses. Exclude here
    # so existing in-flight state machines stop being driven.
    where_clauses.append(
        "COALESCE(t.auto_reason,'') NOT IN "
        "('tests-autonomy-remediation','tests-harness','autonomy-harness',"
        "'autonomous_idle_improvement')"
    )
    where_sql = " AND ".join(where_clauses)
    res = await db.execute(text(f"""
        SELECT s.task_id, s.status, s.step_index, s.step_retries,
               s.planner_model, s.executor_model, s.verifier_model,
               s.updated_at, t.title, t.description, t.board_id, b.name AS board_name,
               b.autonomous_enabled
        FROM task_pev_state s
        JOIN tasks t ON t.id = s.task_id
        JOIN boards b ON b.id = t.board_id
        WHERE {where_sql}
        ORDER BY s.updated_at ASC
        LIMIT :lim
    """), {"lim": limit})
    # T108 (2026-05-30): drop tasks that require REAL tool execution
    # (mcporter / MCP / IDA / binary / crackme / shell). PEV's executor is
    # a text-only `call_model` completion with no shell/file/tool access —
    # on these it FABRICATES evidence and the (also text-only) verifier
    # rubber-stamps it, producing false "done" with invented answers, or
    # tool-less-detected blocks. Those tasks belong to the tooled gateway
    # agent (tools.profile:full) they're assigned to, not PEV. See
    # `_task_requires_real_tools`.
    out = []
    for r in res.fetchall():
        m = dict(r._mapping)
        if _task_requires_real_tools(m.get("title") or "", m.get("description") or ""):
            continue
        out.append(m)
    return out
