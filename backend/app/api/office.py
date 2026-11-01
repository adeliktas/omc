"""Office tab — agent workspace state reader from openclaw filesystem + traffic liveness + DB heartbeat fallback."""

from __future__ import annotations

import json

from app.core.logging import get_logger
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.deps import require_user_or_agent
from app.db.session import async_session_maker

router = APIRouter(prefix="/office", tags=["office"])

_OPENCLAW_HOME = Path(
    os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")
)

# Traffic DB provides authoritative agent liveness (most recent LLM call per agent_id).
_TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))


def _traffic_last_ts_by_name() -> dict[str, float]:
    """Return agent_name -> unix-ts of last LLM call. Join by name (authoritative across ID schemes)."""
    if not _TRAFFIC_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
        rows = conn.execute(
            "SELECT agent_name, MAX(ts) FROM flows WHERE agent_name IS NOT NULL GROUP BY agent_name"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for agent_name, ts in rows:
        if not agent_name or not ts:
            continue
        try:
            out[agent_name] = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return out


async def _db_last_seen_by_name() -> dict[str, float]:
    """Fallback liveness — agents.last_seen_at from postgres.
    Catches agents using any cloud-tier provider whose traffic bypasses
    MC's proxy capture (the proxy only sees flows that route through
    `/api/v1/proxy/{vllm,generic}/...`).
    """
    out: dict[str, float] = {}
    try:
        async with async_session_maker() as session:
            result = await session.execute(text(
                "SELECT name, last_seen_at FROM agents WHERE last_seen_at IS NOT NULL"
            ))
            for row in result.fetchall():
                if row[0] and row[1]:
                    out[row[0]] = row[1].timestamp()
    except Exception:
        pass
    return out


async def _db_id_by_name() -> dict[str, str]:
    """Map `agents.name` → `agents.id` (UUID) for DB-known agents.

    Used by the office endpoint's fallback path so each row can carry the
    UUID needed to navigate to `/agents/<uuid>`. Prefers non-retired
    rows when a name is duplicated.
    """
    out: dict[str, str] = {}
    try:
        async with async_session_maker() as session:
            result = await session.execute(text(
                "SELECT id, name, status FROM agents "
                "WHERE name IS NOT NULL "
                "ORDER BY CASE status WHEN 'retired' THEN 1 ELSE 0 END, name"
            ))
            for row in result.fetchall():
                aid, name, _status = row
                if name and name not in out:
                    out[name] = str(aid)
    except Exception:
        pass
    return out

# Agent department mapping — derived from agent role pools and provider assignments.
# Patterns are ordered: more specific first, generic last.
# Matching is case-insensitive substring on agent name.
_AGENT_DEPARTMENT_PATTERNS: list[tuple[str, str]] = [
    # ── Project Management ───────────────────────────────────────────────
    ("project-management", "project manager"),
    ("project-management", "scrum master"),
    ("project-management", "pm lead"),
    ("project-management", "delivery manager"),

    # ── Product ──────────────────────────────────────────────────────────
    # T104 (2026-05-24): product folks — define WHAT to build vs how
    ("product", "product manager"),
    ("product", "product owner"),
    ("product", "business analyst"),

    # ── Development ──────────────────────────────────────────────────────
    ("development", "software architect"),
    ("development", "software engineer"),
    ("development", "software developer"),
    ("development", "backend engineer"),
    ("development", "frontend engineer"),
    ("development", "fullstack engineer"),
    ("development", "full-stack"),
    ("development", "qa engineer"),
    ("development", "qa lead"),
    ("development", "mobile developer"),
    ("development", "data engineer"),

    # ── Operations / SRE / DevOps ────────────────────────────────────────
    # T104 (2026-05-24): operational vs development split. DevOps lived
    # in development historically; now grouped with reliability + network.
    ("operations", "devops"),
    ("operations", "sre"),
    ("operations", "site reliability"),
    ("operations", "reliability engineer"),
    ("operations", "network engineer"),
    ("operations", "platform engineer"),
    ("operations", "cloud engineer"),
    ("operations", "observability engineer"),

    # ── Security / Pentesting ────────────────────────────────────────────
    ("security", "security architect"),
    ("security", "security engineer"),
    ("security", "security analyst"),
    ("security", "security auditor"),
    ("security", "soc analyst"),
    ("security", "red team"),
    ("security", "blue team"),
    ("security", "pentest"),
    ("security", "penetration test"),
    ("security", "incident responder"),
    ("security", "incident response"),

    # ── Research ─────────────────────────────────────────────────────────
    ("research", "research"),
    ("research", "r&d"),
    ("research", "scientist"),
    ("research", "reverse engineer"),
    ("research", "re lead"),
    ("research", "re-alpha"),
    ("research", "re-beta"),
    ("research", "binary analyst"),
    ("research", "malware analyst"),
    ("research", "threat researcher"),
    ("research", "osint"),
    ("research", "intel analyst"),
    ("research", "data analyst"),
    ("research", "data scientist"),

    # ── Documentation / Knowledge ────────────────────────────────────────
    # T104 (2026-05-24): wiki + journal maintenance, owner-facing docs
    ("documentation", "technical writer"),
    ("documentation", "tech writer"),
    ("documentation", "knowledge manager"),
    ("documentation", "documentation"),

    # ── Management (board leads, CTO, director-level) ────────────────────
    ("management", "lead"),
    ("management", "atlas"),
    ("management", "director"),
    ("management", "cto"),
    ("management", "ciso"),
    ("management", "chief"),
    ("management", "head of"),

    # ── System Control (gateway agents, infrastructure) ──────────────────
    ("system", "gateway agent"),
    ("system", "infra agent"),
    ("system", "system agent"),
]

_DEPARTMENTS: dict[str, dict[str, Any]] = {
    "management":          {"label": "Management",           "color": "#3b82f6"},
    "product":             {"label": "Product",              "color": "#06b6d4"},
    "project-management":  {"label": "Project Management",   "color": "#8b5cf6"},
    "development":         {"label": "Development",           "color": "#10b981"},
    "operations":          {"label": "Operations / SRE",     "color": "#0ea5e9"},
    "security":            {"label": "Security / Pentesting", "color": "#ef4444"},
    "research":            {"label": "Research",              "color": "#f59e0b"},
    "documentation":       {"label": "Documentation",         "color": "#a855f7"},
    "system":              {"label": "System Control",        "color": "#6b7280"},
}


def _resolve_agent_department(agent_name: str) -> str:
    """Infer department from agent name patterns instead of hardcoded IDs."""
    name_lower = agent_name.lower()
    for dept, pattern in _AGENT_DEPARTMENT_PATTERNS:
        if pattern in name_lower:
            return dept
    return "development"  # default fallback


def _agent_department(agent_id: str, agent_name: str | None = None) -> str:
    # Prefer name-based pattern matching (agent-agnostic, survives re-provisioning)
    if agent_name:
        return _resolve_agent_department(agent_name)
    # Fallback: legacy ID-prefix matching for agents without names loaded
    name_lower = agent_id.lower()
    for dept, pattern in _AGENT_DEPARTMENT_PATTERNS:
        if pattern in name_lower:
            return dept
    return "system"


def _latest_memory_file(workspace: Path) -> Path | None:
    mem_dir = workspace / "memory"
    if not mem_dir.exists():
        return None
    files = sorted(mem_dir.glob("*.md"), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


_STATE_KEYWORDS: list[tuple[list[str], str, str]] = [
    # (keywords, status, label) — checked in order
    (["token-burning", "requires immediate human action"], "problem", "Critical — Token Burn"),
    (["critical:", "critical —", "40+ hours idle"], "problem", "Critical"),
    (["in_progress"], "working", "Working"),
    (["heartbeat_ok"], "heartbeat", "Idle Loop"),
    (["stood down", "standing down", "stand down"], "standby", "Standby"),
    (["deep standby", "zero tasks", "zero state changes"], "standby", "Standby"),
    (["idle"], "idle", "Idle"),
]


def _sections(content: str) -> list[str]:
    """Split markdown content into chronological sections (by ## heading)."""
    parts = content.split("\n## ")
    if len(parts) <= 1:
        return [content]
    # parts[0] is preamble before first ##; parts[1:] are sections
    # For state-detection we scan from newest (end) to oldest (start).
    return parts


def _detect_state(content: str, mtime: float) -> tuple[str, str]:
    age_min = (time.time() - mtime) / 60
    sections = _sections(content)

    # 1) Scan sections NEWEST → OLDEST.
    #    Each section can override the verdict from an older section.
    #    This lets a recent "Standing down" beat an older "token-burning".
    for section in reversed(sections):
        lower = section.lower()

        for keywords, status, label in _STATE_KEYWORDS:
            if any(kw in lower for kw in keywords):
                if status == "working" and age_min > 60:
                    break
                if status == "problem":
                    return status, label
                if age_min < 1440:
                    return status, label

        # Explicit stand-down overrides prior alerts
        if "standing down" in lower or "stood down" in lower or "alert resolved" in lower:
            if age_min < 60:
                return "active", "Active"
            if age_min < 720:
                return "idle", "Idle"
            return "standby", "Standby"

    # 2) No keywords matched at all — fall through to age-based
    if age_min > 2880:   # > 48h
        return "offline", "Offline"
    if age_min > 720:    # > 12h
        return "standby", "Deep Standby"
    if age_min > 60:     # > 1h
        return "standby", "Standby"
    if age_min < 15:
        return "active", "Active"
    return "idle", "Idle"


def _last_meaningful_line(content: str) -> str:
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.startswith("#")]
    for line in reversed(lines):
        if len(line) > 15:
            return line[:120]
    return ""


def _remap_path(raw: str) -> Path | None:
    """Remap host openclaw workspace path to the container mount point.

    T28 (2026-05-14): test fixtures use a non-dotted `openclaw/` parent, so we
    also accept `openclaw` in addition to the production `.openclaw`. Returns
    None only when neither marker appears.
    """
    if not raw:
        return None
    p = Path(raw)
    parts = p.parts
    for marker in (".openclaw", "openclaw"):
        try:
            idx = next(i for i, part in enumerate(parts) if part == marker)
        except StopIteration:
            continue
        relative = Path(*parts[idx + 1:])
        return _OPENCLAW_HOME / relative
    return None


_ID_PREFIX_TO_DB_ID: dict[str, str] = {
    # workspace config id prefix → DB-level agent uuid (resolve traffic freshness)
    # Populated dynamically from openclaw.json below.
}


def _extract_db_id(agent_cfg: dict[str, Any]) -> str | None:
    """Pull DB-level agent UUID from openclaw.json agent entry if present."""
    # openclaw.json stores DB UUID in agent_cfg.agentDir as /.../agents/<uuid>/agent
    agent_dir = agent_cfg.get("agentDir", "")
    if not agent_dir:
        return None
    parts = Path(agent_dir).parts
    try:
        idx = parts.index("agents")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def _traffic_status(age_sec: float) -> tuple[str, str]:
    """Map traffic-call age to status label."""
    if age_sec < 300:       # < 5min
        return "active", "Active (LLM call <5m)"
    if age_sec < 900:       # < 15min
        return "active", "Active (LLM call <15m)"
    if age_sec < 3600:      # < 1h
        return "idle", "Idle (LLM call <1h)"
    if age_sec < 10800:     # < 3h
        return "standby", "Standby (LLM call <3h)"
    if age_sec < 86400:     # < 24h
        return "standby", "Deep Standby"
    return "offline", "Offline"


async def _read_agents() -> list[dict[str, Any]]:
    cfg_path = _OPENCLAW_HOME / "openclaw.json"
    cfg: dict[str, Any] = {}
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
    except (PermissionError, OSError, json.JSONDecodeError) as e:
        # Permissions can be reset by openclaw gateway rewriting config.
        # Don't crash the endpoint — fall back to DB-only data.
        get_logger(__name__).warning(
            "office.read_config_failed err=%s path=%s", e, cfg_path,
        )
        cfg = {}

    traffic_ts = _traffic_last_ts_by_name()
    db_seen_ts = await _db_last_seen_by_name()
    now = time.time()

    agents_cfg: list[dict[str, Any]] = (cfg.get("agents") or {}).get("list") or []
    result: list[dict[str, Any]] = []

    for agent_cfg in agents_cfg:
        agent_id: str = agent_cfg.get("id", "")
        if not agent_id or agent_id == "main":
            continue

        # T73 (2026-05-15): `.get(key, {})` returns None when the key
        # exists but holds null. openclaw.json frequently has explicit
        # nulls for optional blocks (identity, heartbeat, model). Use
        # `(x or {})` so we still get an empty dict and don't crash the
        # entire endpoint with `AttributeError: 'NoneType' object has no
        # attribute 'get'` — that was leaving the Office tab empty.
        identity_block = agent_cfg.get("identity") or {}
        heartbeat_block = agent_cfg.get("heartbeat") or {}
        model_block = agent_cfg.get("model") or {}

        name: str = agent_cfg.get("name") or identity_block.get("name") or agent_id
        workspace_path = agent_cfg.get("workspace", "")
        workspace = _remap_path(workspace_path)
        db_id = _extract_db_id(agent_cfg) or ""

        status = "offline"
        label = "Offline"
        last_active: float | None = None
        last_message = ""
        heartbeat_interval: str = heartbeat_block.get("every", "?") if isinstance(heartbeat_block, dict) else "?"
        memory_status: str | None = None

        # Step 1: read memory file (gives "what agent was doing" + fallback liveness)
        if workspace and workspace.exists():
            mem_file = _latest_memory_file(workspace)
            if mem_file:
                try:
                    content = mem_file.read_text(errors="replace")
                    mtime = mem_file.stat().st_mtime
                    last_active = mtime
                    status, label = _detect_state(content, mtime)
                    memory_status = status
                    last_message = _last_meaningful_line(content)
                except Exception:
                    pass
            else:
                status = "standby"
                label = "Never run"

        # Step 2: OVERRIDE with traffic freshness when available.
        # Traffic DB is authoritative for vllm flows — agent making LLM calls = alive.
        # Join by agent NAME (stable across workspace-id vs DB-uuid differences).
        if name in traffic_ts:
            traffic_age = now - traffic_ts[name]
            traffic_status, traffic_label = _traffic_status(traffic_age)
            # "problem" (token-burn) from memory always wins — it is an explicit alert.
            if memory_status != "problem":
                status = traffic_status
                label = traffic_label
                if traffic_ts[name] > (last_active or 0):
                    last_active = traffic_ts[name]

        # Step 3: For agents whose primary is any cloud-tier provider
        # whose flows bypass MC's proxies, mitmdump doesn't see flows.
        # Fall back to mission-control DB agents.last_seen_at as a third
        # liveness signal.
        if name in db_seen_ts and memory_status != "problem":
            db_age = now - db_seen_ts[name]
            db_status, db_label = _traffic_status(db_age)
            # Only apply if it's fresher than what we already have
            if db_seen_ts[name] > (last_active or 0):
                status = db_status
                label = db_label.replace("LLM call", "heartbeat")
                last_active = db_seen_ts[name]

        # T73: surface agent specialization metadata so the Office tab
        # can show what each agent is configured for (model, fallback
        # chain, identity tags, custom skills, role description from
        # SOUL.md / identity_profile). UI renders these as chips/tags.
        if isinstance(model_block, dict):
            primary_model = model_block.get("primary") or ""
            fallbacks = model_block.get("fallbacks") or []
            if not isinstance(fallbacks, list):
                fallbacks = []
        else:
            primary_model = str(model_block) if model_block else ""
            fallbacks = []

        # Extract identity-profile-style metadata. openclaw.json shape
        # varies; tolerate either `identity.profile`, top-level `role`,
        # or nothing at all.
        role_hint: str = ""
        skills_list: list[str] = []
        if isinstance(identity_block, dict):
            role_hint = (
                identity_block.get("role")
                or identity_block.get("title")
                or identity_block.get("specialization")
                or ""
            )
            sk = identity_block.get("skills") or identity_block.get("tools")
            if isinstance(sk, list):
                skills_list = [str(s) for s in sk if s][:8]

        # Fallback role derivation from name suffix (-vllm/-cloud → drop
        # to get the base specialization).
        if not role_hint:
            base = name
            for suffix in (" Senior", "-vllm", "-ollama", "-cloud", "-hauhau",
                           "-Alpha", "-Beta", "-Gamma", "-Delta"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            role_hint = base

        result.append({
            "id": agent_id,
            "name": name,
            "department": _agent_department(agent_id, agent_name=name),
            "status": status,
            "label": label,
            "last_active": last_active,
            "last_message": last_message,
            "heartbeat_interval": heartbeat_interval,
            "model": primary_model,
            "fallbacks": fallbacks,
            "role": role_hint,
            "skills": skills_list,
            "workspace": str(workspace) if workspace else "",
            "db_id": db_id,
            # T105 Phase 2: tier from DB will be injected post-loop by
            # `_inject_tier_for_openclaw_agents`.
            "tier": "mixed",
        })

    # T105 Phase 2 (2026-05-25): inject `tier` from DB into the openclaw.json-
    # derived rows above. The DB tier column is authoritative; openclaw.json
    # doesn't carry tier metadata so we map by name → tier.
    try:
        from app.db.session import async_session_maker as _asm
        async with _asm() as _db:
            _res = await _db.execute(text(
                "SELECT name, tier FROM agents WHERE status != 'retired' AND name IS NOT NULL"
            ))
            tier_by_name = {r[0]: (r[1] or "mixed") for r in _res.fetchall()}
        for row in result:
            t = tier_by_name.get(row.get("name") or "")
            if t:
                row["tier"] = t
    except Exception:
        pass

    # T104 (2026-05-24): supplement openclaw.json-derived roster with
    # DB-only agents (those provisioned in `agents` table but not yet in
    # openclaw.json `agents.list`). Previously this only fired on JSON
    # parse failure → seeded roles never appeared in office tab.
    #
    # Dedup: skip names already added from openclaw.json.
    already_added = {(a.get("name") or "").lower() for a in result}
    id_by_name = await _db_id_by_name()
    # Also pull the full agent rows so we can read status/is_board_lead/is_pm
    db_agents: list[dict[str, Any]] = []
    try:
        from app.db.session import async_session_maker as _asm
        async with _asm() as _db:
            _res = await _db.execute(text(
                "SELECT id, name, status, is_board_lead, is_project_manager, "
                "       last_seen_at, tier "
                "FROM agents WHERE status != 'retired' AND name IS NOT NULL "
                "ORDER BY name"
            ))
            for r in _res.fetchall():
                db_agents.append({
                    "id": str(r[0]),
                    "name": r[1],
                    "db_status": r[2],
                    "is_board_lead": bool(r[3]),
                    "is_project_manager": bool(r[4]),
                    "last_seen_at": r[5].timestamp() if r[5] else None,
                    "tier": r[6] or "mixed",
                })
    except Exception:
        pass

    for a in db_agents:
        name = a["name"]
        if name.lower() in already_added:
            continue
        already_added.add(name.lower())
        ts = a["last_seen_at"] or max(
            traffic_ts.get(name, 0), db_seen_ts.get(name, 0), 0,
        )
        age = now - ts if ts else float("inf")
        # T104: when MC says status=offline and there's no live signal,
        # display as standby instead of inferring from age=infinity (offline).
        if a["db_status"] == "online" and ts:
            status, label = _traffic_status(age)
        else:
            status, label = "standby", "Provisioned, awaiting wake"
        result.append({
            "id": f"db:{name}",
            "db_id": a["id"],
            "name": name,
            "department": _resolve_agent_department(name),
            "status": status,
            "label": label,
            "last_active": ts or None,
            "last_message": "DB-provisioned agent — workspace not in openclaw.json yet",
            "heartbeat_interval": "?",
            "model": "",
            "tier": a.get("tier") or "mixed",
        })

    return result


@router.get(
    "/agents",
    summary="Agent office state",
    dependencies=[Depends(require_user_or_agent)],
)
async def get_office_agents() -> dict[str, Any]:
    # Never raise — degrade gracefully so CORS headers attach + UI doesn't break.
    try:
        agents = await _read_agents()
    except Exception as e:
        get_logger(__name__).error("office.read_agents.unexpected err=%s", e, exc_info=True)
        agents = []
    departments = {}
    for dept_id, dept_meta in _DEPARTMENTS.items():
        departments[dept_id] = {
            **dept_meta,
            "agents": [a for a in agents if a["department"] == dept_id],
        }
    return {
        "departments": departments,
        "agents": agents,
        "total": len(agents),
        "problem_count": sum(1 for a in agents if a["status"] == "problem"),
        "active_count": sum(1 for a in agents if a["status"] in ("working", "active")),
    }
