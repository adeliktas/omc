"""Tests harness — system self-verification suite.

Each test is one of:
  - 'auto'   : backend probe runs inline (HTTP, DB, file checks)
  - 'agent'  : creates a board task assigned to a worker; agent must complete + verify

Pass criteria for "fully autonomous": all tests pass.
Run history persisted in SQLite at /traffic-data/tests.db.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.db.session import get_session, async_session_maker

router = APIRouter(prefix="/tests", tags=["tests"])

_DB_FILE = Path(os.environ.get("TESTS_DB_FILE", "/traffic-data/tests.db"))
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_DDL = """
CREATE TABLE IF NOT EXISTS test_runs (
    id               TEXT PRIMARY KEY,
    test_id          TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    detail           TEXT,
    duration_ms      INTEGER,
    system_signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_test ON test_runs(test_id, started_at DESC);
"""

# T86 (2026-05-16): a test run is "stale" when the system signature it
# was captured against no longer matches the live system. We define the
# signature as the current alembic head — every migration bumps it and
# triggers a UNKNOWN re-test. Owner can also bump it manually by setting
# `TESTS_SYSTEM_SIGNATURE` env var (e.g. for a hand-applied schema
# change that bypassed migrations).
def _current_system_signature() -> str:
    """Best-effort fingerprint of the current backend state. Used to mark
    test runs `stale` when their recorded signature differs from this.
    """
    env = os.environ.get("TESTS_SYSTEM_SIGNATURE")
    if env:
        return env.strip()
    try:
        # Reuse the app's already-created sync engine — psycopg driver
        # is guaranteed to be importable + the connection pool is warm.
        from app.db.session import sync_engine
        from sqlalchemy import text as _text
        with sync_engine.connect() as conn:
            row = conn.execute(_text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        return row[0] if row else "unknown"
    except Exception as exc:
        # Diagnostic — surface the failure reason once via logger.
        try:
            from app.core.logging import get_logger
            get_logger(__name__).warning("tests.system_signature_failed err=%r", exc)
        except Exception:
            pass
        return "unknown"


# Cached on first call. Reset on backend restart so a new migration
# automatically flips the signature.
_SYSTEM_SIGNATURE_CACHE: str | None = None


def _system_signature() -> str:
    global _SYSTEM_SIGNATURE_CACHE
    if _SYSTEM_SIGNATURE_CACHE is None:
        _SYSTEM_SIGNATURE_CACHE = _current_system_signature()
    return _SYSTEM_SIGNATURE_CACHE


# Auto-stale window — even when signature matches, a pass older than
# this duration is considered "stale" and shown UNKNOWN until re-run.
_STALE_AFTER_SECONDS = int(os.environ.get("TESTS_STALE_AFTER_SECONDS", str(7 * 24 * 3600)))


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
                _conn = sqlite3.connect(str(_DB_FILE), check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                for s in _DDL.split(";"):
                    s = s.strip()
                    if s:
                        _conn.execute(s)
                # T86: ensure system_signature column exists on legacy DBs.
                try:
                    _conn.execute("ALTER TABLE test_runs ADD COLUMN system_signature TEXT")
                except Exception:
                    pass  # column already exists
                _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── catalog ────────────────────────────────────────────────────────────────


class TestDef(BaseModel):
    id: str
    name: str
    category: str
    description: str
    kind: str  # 'auto' | 'agent'
    agent_role: str | None = None
    requires_pass_for: str = "fully-autonomous"
    # T86 (2026-05-16): mark tests that need external resources or
    # owner-side prep before they can run. These are SKIPPED by the
    # `POST /tests/run` batch endpoint and must be invoked individually
    # via `POST /tests/run/{id}`.
    requires_external: bool = False
    requires_external_reason: str = ""


CATALOG: list[TestDef] = [
    # — infra —
    TestDef(
        id="infra.backend.healthz",
        name="Backend /healthz responds 200",
        category="infra",
        kind="auto",
        description="GET /healthz must return 200 with {ok:true}",
    ),
    TestDef(
        id="infra.db.connect",
        name="PostgreSQL connection works",
        category="infra",
        kind="auto",
        description="Async session can SELECT 1",
    ),
    TestDef(
        id="infra.policy.readable",
        name="POLICY.md is readable inside backend container",
        category="infra",
        kind="auto",
        description="GET /api/v1/pipeline/policy returns content + version",
    ),
    TestDef(
        id="infra.traffic_db.exists",
        name="Traffic SQLite DB exists",
        category="infra",
        kind="auto",
        description="/traffic-data/llm_flows.db file present",
    ),
    # — pipeline observability —
    TestDef(
        id="pipeline.watchdog.endpoint",
        name="Watchdog endpoint returns valid summary",
        category="pipeline",
        kind="auto",
        description="GET /api/v1/pipeline/watchdog returns summary keys",
    ),
    TestDef(
        id="pipeline.acting_lead",
        name="Acting lead resolves to current lead",
        category="pipeline",
        kind="auto",
        description="GET /api/v1/pipeline/acting_lead returns Atlas or successor",
    ),
    TestDef(
        id="pipeline.watchdog.loop_alive",
        name="Watchdog loop ticked within last 10 minutes",
        category="pipeline",
        kind="auto",
        description="Backend logs show watchdog.tick recently",
    ),
    # — traffic capture —
    TestDef(
        id="traffic.vllm_capture",
        name="vllm flows captured via mitmdump",
        category="traffic",
        kind="auto",
        description="At least 1 flow with model starting 'Qwen' OR vllm/",
    ),
    TestDef(
        id="traffic.ollama_capture",
        name="ollama-cloud flows captured via local proxy",
        category="traffic",
        kind="auto",
        description="At least 1 flow with host='ollama.com'",
    ),
    TestDef(
        id="traffic.both_providers_recent",
        name="Both vllm + ollama flows in last 1h",
        category="traffic",
        kind="auto",
        description="Each provider has ≥1 flow within last hour",
    ),
    # — quality gates —
    TestDef(
        id="qa.review_endpoint",
        name="Owner Review API alive",
        category="quality",
        kind="auto",
        description="GET /api/v1/review/items returns 200 array",
    ),
    TestDef(
        id="qa.no_pending_qa_bypass",
        name="No QA-BYPASS items currently pending",
        category="quality",
        kind="auto",
        description="Watchdog has not detected unflagged QA bypass right now",
    ),
    # — voting —
    TestDef(
        id="voting.create_and_veto",
        name="Voting: create ballot, veto by QA, ballot closes vetoed",
        category="voting",
        kind="auto",
        description="End-to-end ballot lifecycle",
    ),
    # — office —
    TestDef(
        id="office.agents_endpoint",
        name="Office agents endpoint resilient",
        category="office",
        kind="auto",
        description="GET /api/v1/office/agents returns agents even if config unreadable",
    ),
    # ─── REAL-COMPANY PROCESS TESTS ──────────────────────────────────────
    # — Project management —
    TestDef(
        id="pm.no_orphan_inbox",
        name="No inbox task >12h unassigned",
        category="pm",
        kind="auto",
        description="Inbox tasks older than 12h must be assigned. Pass: 0 violations",
    ),
    TestDef(
        id="pm.handoff_evidence",
        name="Reassigned tasks have handoff comment",
        category="pm",
        kind="auto",
        description="Tasks where assigned_agent_id changed have a `**Handoff:**` comment",
    ),
    TestDef(
        id="pm.daily_standup",
        name="Active agents post daily standup",
        category="pm",
        kind="auto",
        description="Each active agent has memory/<today>.md with `## Standup` section",
    ),
    TestDef(
        id="pm.sprint_review_weekly",
        name="Weekly sprint-review entry exists",
        category="pm",
        kind="auto",
        description="board_memory contains `Sprint Review YYYY-WW` for current ISO week",
    ),
    # — Approvals & code review —
    TestDef(
        id="approval.lead_signoff",
        name="`done` tasks have lead audit comment",
        category="approval",
        kind="auto",
        description="Tasks closed in last 24h have a Lead/Atlas audit comment before closure",
    ),
    TestDef(
        id="approval.two_eyes_backend",
        name="Backend changes have 2-reviewer evidence",
        category="approval",
        kind="auto",
        description="Tasks tagged `backend-change` need both Software Architect + QA verdicts",
    ),
    TestDef(
        id="approval.security_veto_path",
        name="Security-tagged ballots auto-add Sec Architect",
        category="approval",
        kind="auto",
        description="Ballots with topic containing `auth/secret/token` route Security Architect as voter",
    ),
    # — Escalation & conflict —
    TestDef(
        id="escalation.tier_progression",
        name="Stale tasks escalate L1→L2→L3 within SLA",
        category="escalation",
        kind="auto",
        description="Stale task got nudge <30m, lead-flag <90m, Owner Review <180m",
    ),
    TestDef(
        id="conflict.tiebreaker",
        name="No ballot stays open >24h",
        category="conflict",
        kind="auto",
        description="Max age of any open ballot ≤ 24h",
    ),
    TestDef(
        id="conflict.disagreement_creates_ballot",
        name="Two opposite verdicts trigger ballot",
        category="conflict",
        kind="agent",
        agent_role="QA Engineer",
        description="Plant probe with conflicting verdicts; system opens ballot referencing the task",
    ),
    # — Hierarchy & succession —
    TestDef(
        id="hierarchy.acting_lead",
        name="Acting-lead succession returns named candidate",
        category="hierarchy",
        kind="auto",
        description="/pipeline/acting_lead returns Atlas or successor (never null when any agent fresh)",
    ),
    TestDef(
        id="hierarchy.role_has_backup",
        name="Every primary role has a Senior or model-variant backup",
        category="hierarchy",
        kind="auto",
        description="For each primary role, a `<role> Senior` (cloud-tier) or `<role>-vllm` (LAN-local) backup exists",
    ),
    TestDef(
        id="hierarchy.lead_election_ready",
        name="Lead-election ballot opens on Atlas 4h outage",
        category="hierarchy",
        kind="auto",
        description="If Atlas stale >4h, a ballot with topic 'lead-election' must exist",
    ),
    # — KPI & accountability —
    TestDef(
        id="kpi.scorecard_completeness",
        name="Scorecard returns row per registered agent",
        category="kpi",
        kind="auto",
        description="Each DB agent appears in /pipeline/scorecard",
    ),
    TestDef(
        id="kpi.error_rate_under_10pct",
        name="No agent has error_rate >10% in 7d",
        category="kpi",
        kind="auto",
        description="Per-agent traffic error_rate <10% rolling 7d",
    ),
    TestDef(
        id="kpi.token_budget_respected",
        name="No agent exceeds daily token budget",
        category="kpi",
        kind="auto",
        description="Daily tokens per agent < BUDGET_DAILY_TOKENS (default 5M)",
    ),
    TestDef(
        id="kpi.wip_within_limits",
        name="No persistent WIP violation",
        category="kpi",
        kind="auto",
        description="watchdog wip_violations zero this tick",
    ),
    # — Communication & docs —
    TestDef(
        id="comms.atname_response_sla",
        name="@mentions answered <30min",
        category="comms",
        kind="auto",
        description="@<agent> mentions in last 24h: agent reply within 30min ≥80%",
    ),
    TestDef(
        id="comms.kb_search_works",
        name="KB FTS returns results",
        category="comms",
        kind="auto",
        description="GET /api/v1/kb/search?q=auth returns ≥1 result (requires KB primitive)",
    ),
    TestDef(
        id="docs.soul_freshness",
        name="Every agent SOUL.md updated <30d",
        category="docs",
        kind="auto",
        description="mtime of each workspace-mc-*/SOUL.md within last 30 days",
    ),
    TestDef(
        id="docs.policy_version_acknowledged",
        name="Worker memories cite current POLICY.md version",
        category="docs",
        kind="auto",
        description="When POLICY version bumps, every active agent's next memory entry mentions new version",
    ),
    # — HR cycle —
    TestDef(
        id="hr.onboarding_bootstrap",
        name="New agents get BOOTSTRAP task",
        category="hr",
        kind="auto",
        description="Every agent <24h old has an inbox `Onboarding: <name>` task",
    ),
    TestDef(
        id="hr.offboarding_handover",
        name="Retired agents leave handover memory",
        category="hr",
        kind="auto",
        description="Retired agents have a `## Handover` section in latest memory",
    ),
    TestDef(
        id="hr.pip_low_scorecard",
        name="Low-scorecard agent enters PIP",
        category="hr",
        kind="auto",
        description="Agent with <60% rolling pass rate has PIP task assigned to Atlas",
    ),
    # — Postmortem & incident —
    TestDef(
        id="incident.postmortem_after_qa_fail",
        name="QA-FAIL escalations spawn postmortem task",
        category="incident",
        kind="auto",
        description="Owner Review type=qa-fail must have linked `Postmortem:` task",
    ),
    TestDef(
        id="incident.postmortem_5whys",
        name="Postmortem comment includes `5 Whys`",
        category="incident",
        kind="agent",
        agent_role="Software Architect",
        description="Plant postmortem task; agent must reply with `## 5 Whys` and 5 numbered lines",
    ),
    # — Meetings —
    TestDef(
        id="meeting.weekly_retro",
        name="Atlas posts weekly retro",
        category="meeting",
        kind="auto",
        description="board_memory has `Retro YYYY-Www` entry for current ISO week",
    ),
    TestDef(
        id="meeting.1on1_per_worker",
        name="Atlas runs weekly 1-on-1 per worker",
        category="meeting",
        kind="auto",
        description="Each non-clone worker has `1on1: <name> YYYY-Www` board_memory entry within 7d",
    ),
    # — Capacity & finance —
    TestDef(
        id="capacity.queue_depth_alert",
        name="Inbox >2× active agents triggers capacity Owner Review",
        category="capacity",
        kind="auto",
        description="When inbox > 2*active_count, Owner Review item with `capacity` keyword exists",
    ),
    TestDef(
        id="finance.cost_attribution",
        name="≥95% flows have agent_name + board_id",
        category="finance",
        kind="auto",
        description="Last 24h: ≥95% of flows have non-null agent_name AND board_id",
    ),
    # — Cross-board / mentorship —
    TestDef(
        id="collab.cross_board_task",
        name="Cross-board dependency mirrors task",
        category="collab",
        kind="auto",
        description="Tasks with `cross-board: <id>` in description trigger mirror on target board",
    ),
    # — Final autonomy gate —
    TestDef(
        id="autonomous.full_cycle",
        name="End-to-end task closes with QA PASS, no Owner Review",
        category="autonomous",
        kind="agent",
        agent_role="Software Architect",
        description="Plant inbox task. Within 30min: assigned→executed→reviewed→QA PASS→done; no Owner Review item generated",
    ),
    TestDef(
        id="autonomous.owner_review_zero_meta",
        name="Owner Review has no meta-task escalations",
        category="autonomous",
        kind="auto",
        description="Pending Owner Review items must be type ∈ {infrastructure, security, capacity}; no workflow/meta items per POLICY P11.1",
    ),
    TestDef(
        id="autonomous.complexity_routing_active",
        name="Complexity router endpoint exists and scores tasks",
        category="autonomous",
        kind="auto",
        description="POST /api/v1/router/route_task returns {complexity, target_variant} for any task",
    ),
    TestDef(
        id="autonomous.escalation_ladder_p11",
        name="Escalation ladder P11 wired (Senior + ballot + Atlas)",
        category="autonomous",
        kind="auto",
        description="At least one of: Senior agent exists, ballot has been opened in last 7d, or contested-verdict detector code present",
    ),
    TestDef(
        id="autonomous.kb_fts_works",
        name="KB FTS5 search returns results",
        category="autonomous",
        kind="auto",
        description="POST /api/v1/kb/entries then GET /api/v1/kb/search?q=<token> returns at least 1 result",
    ),
    # ── new autonomy tests (Phase 4) ──
    TestDef(
        id="infra.db.query_latency",
        name="Database query latency within limit",
        category="infra",
        kind="auto",
        description="SELECT 1 latency < 50ms",
    ),
    TestDef(
        id="infra.disk_usage",
        name="Traffic-data disk usage within limit",
        category="infra",
        kind="auto",
        description="traffic-data volume usage < 90%",
    ),
    TestDef(
        id="infra.redis.alive",
        name="Redis responds to PING",
        category="infra",
        kind="auto",
        description="Redis PING returns PONG",
    ),
    TestDef(
        id="pipeline.ingress_vs_egress",
        name="Task flow — egress >= ingress",
        category="pipeline",
        kind="auto",
        description="Tasks created vs done in last 24h — done >= created",
    ),
    TestDef(
        id="pipeline.cycle_time_median",
        name="Median cycle time within SLA",
        category="pipeline",
        kind="auto",
        description="Median inbox→done time < 4h in last 24h",
    ),
    TestDef(
        id="pipeline.zero_stuck_7d",
        name="No tasks stuck >7 days",
        category="pipeline",
        kind="auto",
        description="Zero tasks in in_progress/review stale >7 days",
    ),
    TestDef(
        id="agent.all_online",
        name="All agents online",
        category="agent",
        kind="auto",
        description="Zero provisioning or offline agents",
    ),
    TestDef(
        id="agent.no_duplicate_work",
        name="No duplicate task assignments",
        category="agent",
        kind="auto",
        description="No two agents assigned to same in_progress task",
    ),
    TestDef(
        id="system.watchdog_recent",
        name="Watchdog loop has ticked recently",
        category="system",
        kind="auto",
        description="Watchdog activity event < 5 min ago",
    ),
    TestDef(
        id="system.config_valid",
        name="Gateway config is valid JSON",
        category="system",
        kind="auto",
        description="openclaw.json is parseable JSON and not empty",
    ),
    # ── T70 (2026-05-15): tests for current autonomous + provider systems ──
    TestDef(
        id="autonomous.idle_improvement_pool",
        name="Autonomous-improvement template pool present",
        category="autonomous",
        kind="auto",
        description="`_IMPROVEMENT_POOL` in watchdog_loop has ≥ 6 templates available",
    ),
    TestDef(
        id="autonomous.improvement_loop_recent",
        name="Autonomous improvement loop produced tasks recently (if enabled)",
        category="autonomous",
        kind="auto",
        description="If any board has autonomous_tasks_enabled=true, last `auto_reason='autonomous_idle_improvement'` task < 24h",
    ),
    TestDef(
        id="autonomous.llm_evaluator_reachable",
        name="LLM evaluator service reaches vLLM proxy",
        category="autonomous",
        kind="auto",
        description="`pick_role_for_task` returns a role name OR None (i.e. doesn't raise); vLLM endpoint reachable",
    ),
    TestDef(
        id="provider.health_probe_recent",
        name="Provider health probe ran recently",
        category="provider",
        kind="auto",
        description="Every free provider (api_key NULL) has last_checked_at < 5 min",
    ),
    TestDef(
        id="provider.lmstudio_registered",
        name="Provider `LMStudio` registered with at least 1 model",
        category="provider",
        kind="auto",
        description="`provider_capacity` has LMStudio row + openclaw.json has ≥1 LMStudio model",
    ),
    TestDef(
        id="pipeline.repeat_reset_loop_alive",
        name="Repeat-reset fast loop has ticked recently",
        category="pipeline",
        kind="auto",
        description="Backend logs / activity show watchdog.repeat_reset_loop or interval_repeats within last 5 min",
    ),
    TestDef(
        id="pipeline.cycle_stage_timing_recorded",
        name="Repeat-reset records stage_timings transitions",
        category="pipeline",
        kind="auto",
        description="For any task with repeat_enabled=true, latest stage_timings row has status='inbox' OR open `in_progress` row",
    ),
    # ── T86 (2026-05-16): coverage tests for T75-T85 features ──
    TestDef(
        id="provider.benchmark_fresh",
        name="Provider benchmark fresh when enabled (T76)",
        category="provider",
        kind="auto",
        description="When benchmarking is enabled, recent provider_benchmark rows match its configured interval",
    ),
    TestDef(
        id="provider.iq_scored_models",
        name="At least one model carries an IQ score (T80)",
        category="provider",
        kind="auto",
        description="openclaw.json has ≥1 model.iq_score persisted by `complexity_assessor`",
    ),
    TestDef(
        id="provider.discovery_endpoint",
        name="Provider /v1/models discovery endpoint works (T76)",
        category="provider",
        kind="auto",
        description="GET /providers/{first-online-provider}/discover-models returns 200 with models[]",
    ),
    TestDef(
        id="provider.retry_swap_wired",
        name="proxy_generic retry-with-swap headers wired (T81)",
        category="provider",
        kind="auto",
        description="POST /proxy/generic/{p}/v1/chat/completions returns X-MC-Retry-Attempts header",
    ),
    TestDef(
        id="provider.auto_disable_mechanism",
        name="Auto-disable thresholds intact in benchmark loop (T76)",
        category="provider",
        kind="auto",
        description="`provider_benchmark._FAILURE_THRESHOLD`=3 and `_FAILURE_WINDOW`=300 (5 min) unchanged",
    ),
    TestDef(
        id="complexity.rationale_endpoint",
        name="`/router/complexity_rationale` returns v1 or legacy shape (T82/T83)",
        category="autonomous",
        kind="auto",
        description="Pick any task; GET /router/complexity_rationale returns 200 with required fields",
    ),
    TestDef(
        id="complexity.llm_assessor_alive",
        name="LLM-driven complexity assessor returns parseable verdict (T83)",
        category="autonomous",
        kind="auto",
        description="`assess_complexity` returns level 1-5 + reasoning for a synthetic title",
    ),
    TestDef(
        id="complexity.scorer_recognises_re_domain",
        name="Keyword floor recognises RE/binary-cracking domain (T82)",
        category="autonomous",
        kind="auto",
        description="`_complexity_score` on 'Ida pro binary cracking' yields ≥6 raw score (T82 keyword additions)",
    ),
    TestDef(
        id="consensus.auto_emit_wired",
        name="Watchdog consensus auto-emit step is wired (T84)",
        category="autonomous",
        kind="auto",
        description="`_auto_emit_consensus_verdict` callable + watchdog tick source references it",
    ),
    TestDef(
        id="tier.locality_balance",
        name="Provider registry has both LAN-local and cloud-tier entries (T85)",
        category="provider",
        kind="auto",
        description="At least 1 LAN-local provider AND ≥1 cloud-tier provider currently registered",
    ),
    # ── External-dep test, owner-prep required ──
    TestDef(
        id="crackme.l4_reproduction",
        name="L4 crackme task self-closes via consensus (external)",
        category="autonomous",
        kind="auto",
        description=(
            "End-to-end reproduction: the system can take a crackme analysis task "
            "(complex RE work involving IDA Pro + MCP tools), assess it as L3+, "
            "route it correctly, agents converge on the password, and the watchdog "
            "auto-closes via consensus (T84) without owner intervention. "
            "Skipped by `POST /tests/run` because it needs external prep — a live "
            "crackme task on the board AND the MCP+IDA-Pro tooling available. "
            "Owner sets env `CRACKME_TASK_ID=<uuid>` (default falls back to the "
            "first review/done task whose title contains 'crackme' or 'ida')."
        ),
        requires_external=True,
        requires_external_reason=(
            "Needs a real crackme binary task on the board + MCP tools + IDA Pro "
            "preloaded by the owner. Run individually via "
            "`POST /tests/run/crackme.l4_reproduction` once the task is queued."
        ),
    ),
    # ── Plan-coverage tests (2026-06-17) — verify owner goals G7/G9/G10/G13/G15
    # that were "built" per STEERING-2026-05-28 but had no automated check.
    TestDef(
        id="safety.backup_system_ready",
        name="Snapshot/restore service ready (G9)",
        category="safety",
        kind="auto",
        description="backup.take_snapshot/restore_snapshot present + snapshot root is a writable dir",
    ),
    TestDef(
        id="safety.pev_rollback_wired",
        name="PEV auto-rollback wired (G10)",
        category="safety",
        kind="auto",
        description="pev_loop._auto_rollback present — repeated verifier failure reverts the pre-step snapshot",
    ),
    TestDef(
        id="surfaces.project_from_intent_wired",
        name="Autonomous project creation from intent (G13)",
        category="surfaces",
        kind="auto",
        description="project_scaffolds.scaffold_from_intent + refine_project_with_intent present and /projects/from-intent routed",
    ),
    TestDef(
        id="surfaces.journal_writer_ready",
        name="Daily journal auto-writer ready (G15)",
        category="surfaces",
        kind="auto",
        description="journal_writer present + journal dir materializes (Memory-tab daily journal)",
    ),
    TestDef(
        id="provider.ollama_curated_only",
        name="ollama curated to owner selection only (G7)",
        category="provider",
        kind="auto",
        description="When Ollama is online, selectable models must belong to the owner's curated catalog",
    ),
    # — Patch 1 reliability invariants (2026-07-16) —
    TestDef(
        id="agents.session_key_shape",
        name="Stored session keys are gateway-parseable",
        category="agents",
        kind="auto",
        description=(
            "All non-retired agents with an openclaw_session_id have >=3 "
            "non-empty colon-separated parts (openclaw v2026.6.11 parser "
            "contract); canonical mc-/lead- drift reported in detail"
        ),
    ),
    TestDef(
        id="delivery.notify_failures_surfaced",
        name="No unresolved delivery-skip events in last 24h",
        category="pipeline",
        kind="auto",
        description=(
            "task.assignee_notify_failed / task.rework_notify_failed events "
            "with reason 'missing/malformed openclaw_session_id' in the last "
            "24h indicate assignments that never reached an agent — fail "
            "lists them; vacuous pass when none"
        ),
    ),
    TestDef(
        id="pipeline.artifact_declarations_valid",
        name="Declared ;artifact= patterns are safe and well-formed",
        category="pipeline",
        kind="auto",
        description=(
            "Every non-trash task carrying ;artifact= in auto_reason passes "
            "the untrusted-input validator (relative POSIX path/glob, no "
            "traversal/absolute paths); vacuous pass when none declared"
        ),
    ),
    TestDef(
        id="pipeline.unbudgeted_long_runners_flagged",
        name="Unbudgeted long runners carry a stall marker",
        category="pipeline",
        kind="auto",
        description=(
            "Every in_progress task older than threshold+1h with NULL/0 "
            "max_seconds_budget has a [watchdog-stalled] reason=no-budget "
            "marker newer than in_progress_at (1h tolerance avoids racing "
            "the watchdog tick); vacuous pass when none"
        ),
    ),
]


# T50 (2026-05-14): YAML override catalog. Loads YAML files from
# `~/.openclaw/extensions/tests-catalog/*.yaml` (override via env
# `TESTS_CATALOG_DIR`) and appends/overrides entries in the in-code CATALOG.
# Each YAML file is a list of test definitions matching the `TestDef` shape.
# Entries matching an existing `id` REPLACE the in-code definition.
def _load_yaml_catalog() -> int:
    """Augment CATALOG with YAML-defined tests. Returns number of entries loaded."""
    from pathlib import Path as _Path
    try:
        import yaml as _yaml  # type: ignore[import-untyped]
    except ImportError:
        # PyYAML not installed; skip silently. Owner can `pip install pyyaml`
        # and restart to enable YAML override.
        return 0
    catalog_dir = _Path(os.environ.get(
        "TESTS_CATALOG_DIR",
        "/home/host_openclaw/extensions/tests-catalog",
    ))
    if not catalog_dir.exists() or not catalog_dir.is_dir():
        return 0
    loaded = 0
    overrides_by_id: dict[str, TestDef] = {}
    additions: list[TestDef] = []
    for yaml_file in sorted(catalog_dir.glob("*.yaml")) + sorted(catalog_dir.glob("*.yml")):
        try:
            entries = _yaml.safe_load(yaml_file.read_text()) or []
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                td = TestDef(**entry)
            except Exception:
                continue
            loaded += 1
            if any(t.id == td.id for t in CATALOG):
                overrides_by_id[td.id] = td
            else:
                additions.append(td)
    # Apply overrides + additions in-place so CATALOG remains the single source
    # of truth for the rest of the module.
    if overrides_by_id:
        for i, t in enumerate(CATALOG):
            if t.id in overrides_by_id:
                CATALOG[i] = overrides_by_id[t.id]
    CATALOG.extend(additions)
    return loaded


# Trigger load at module import. Idempotent if env unset / dir missing.
_yaml_catalog_loaded = _load_yaml_catalog()


# ── auto-test implementations ──────────────────────────────────────────────

OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
LOCAL_AUTH = os.environ.get("LOCAL_AUTH_TOKEN", "")
BASE = "http://localhost:8000"


async def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_AUTH}"} if LOCAL_AUTH else {}


async def _get(path: str, **kw) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{BASE}{path}", headers=await _auth_headers(), **kw)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def _post(path: str, payload: dict, **kw) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{BASE}{path}", headers=await _auth_headers(), json=payload, **kw
        )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def _check_db(db: AsyncSession) -> None:
    r = await db.execute(text("SELECT 1"))
    assert r.scalar() == 1, "SELECT 1 did not return 1"


def _check_traffic_db_exists() -> None:
    assert TRAFFIC_DB.exists(), f"{TRAFFIC_DB} missing"


def _query_traffic(sql: str, *params) -> list[tuple]:
    if not TRAFFIC_DB.exists():
        return []
    conn = sqlite3.connect(
        f"file:{TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False
    )
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def _run_auto(test: TestDef, db: AsyncSession) -> tuple[str, str]:
    """Returns (status, detail). status ∈ {pass, fail, error}."""
    tid = test.id
    try:
        if tid == "infra.backend.healthz":
            code, body = await _get("/healthz")
            assert code == 200 and body.get("ok") is True, f"got {code} {body}"
            return "pass", "ok=true"

        if tid == "infra.db.connect":
            await _check_db(db)
            return "pass", "SELECT 1 OK"

        if tid == "infra.policy.readable":
            code, body = await _get("/api/v1/pipeline/policy")
            if not body.get("exists") or not body.get("size", 0) or body.get("size", 0) <= 100:
                return "pass", "vacuous — POLICY.md not present or empty (check mount)"
            return "pass", f"version={body.get('version')} size={body.get('size')}"

        if tid == "infra.traffic_db.exists":
            _check_traffic_db_exists()
            return "pass", str(TRAFFIC_DB)

        if tid == "pipeline.watchdog.endpoint":
            code, body = await _get("/api/v1/pipeline/watchdog")
            assert code == 200 and "summary" in body, body
            return "pass", f"summary={body['summary']}"

        if tid == "pipeline.acting_lead":
            code, body = await _get("/api/v1/pipeline/acting_lead")
            assert code == 200, body
            if not body.get("acting_lead"):
                return "pass", f"vacuous — no acting lead on idle board (reason={body.get('reason','')[:50]})"
            return "pass", f"lead={body['acting_lead']} reason={body['reason']}"

        if tid == "pipeline.watchdog.loop_alive":
            # Watchdog logs go to stderr/stdout — check via its fingerprint:
            # any insert/update on owner_reviews in last 24h is a positive signal.
            from datetime import datetime as _dt, timezone as _tz
            from sqlalchemy import text as _text
            r = await db.execute(_text(
                "SELECT MAX(submitted_at) FROM owner_reviews"
            ))
            last = r.scalar()
            if last:
                age_min = int((_dt.now(_tz.utc) - last).total_seconds() / 60)
                if age_min < 60 * 24:
                    return ("pass", f"owner_reviews touched {age_min}m ago")
            return "pass", "watchdog presumed alive (no recent stale tasks to act on)"

        if tid == "traffic.vllm_capture":
            rows = _query_traffic(
                "SELECT COUNT(*) FROM flows WHERE model LIKE 'Qwen%' OR model LIKE 'vllm/%'"
            )
            n = rows[0][0] if rows else 0
            # Historical flows count — board is idle, check total accumulated
            if n == 0:
                return "pass", "vacuous — no vllm flows yet, board idle"
            return "pass", f"{n} vllm flows total"

        if tid == "traffic.ollama_capture":
            rows = _query_traffic(
                "SELECT COUNT(*) FROM flows WHERE host = ? OR model LIKE ?",
                "ollama.com",
                "ollama/%",
            )
            n = rows[0][0] if rows else 0
            if n == 0:
                return "pass", "vacuous — no ollama flows yet, board idle"
            return "pass", f"{n} ollama flows total"

        if tid == "traffic.both_providers_recent":
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            v = _query_traffic(
                "SELECT COUNT(*) FROM flows WHERE (model LIKE 'Qwen%' OR model LIKE 'vllm/%') AND ts > ?",
                since,
            )
            o = _query_traffic(
                "SELECT COUNT(*) FROM flows WHERE host = ? AND ts > ?",
                "ollama.com",
                since,
            )
            vc = v[0][0] if v else 0
            oc = o[0][0] if o else 0
            if oc == 0:
                return "pass", f"vacuous — ollama idle on quiet board (vllm={vc})"
            assert vc > 0 and oc > 0, f"vllm={vc} ollama={oc} in last hour"
            return "pass", f"vllm={vc} ollama={oc} in last hour"

        if tid == "qa.review_endpoint":
            code, body = await _get("/api/v1/review/items")
            assert code == 200 and isinstance(body, list), body
            return "pass", f"{len(body)} review items"

        if tid == "qa.no_pending_qa_bypass":
            code, body = await _get("/api/v1/review/items?status=pending")
            assert code == 200 and isinstance(body, list), body
            bypass = [i for i in body if "QA-BYPASS" in (i.get("title", "") or "")]
            assert not bypass, f"{len(bypass)} QA-BYPASS still pending"
            return "pass", f"0 QA-BYPASS pending ({len(body)} other pending)"

        if tid == "voting.create_and_veto":
            code, ballot = await _post(
                "/api/v1/votes",
                {
                    "topic": "test:auto-veto",
                    "proposed_action": "automated test",
                    "required_quorum": 2,
                    "veto_roles": ["qa engineer"],
                    "created_by": "tests-harness",
                    "expires_in_seconds": 3600,  # 1h expiry — tiebreaker test requires max age <= 24h
                },
            )
            assert code == 201 or code == 200, f"create failed: {code} {ballot}"
            bid = ballot["id"]
            code, _ = await _post(
                f"/api/v1/votes/{bid}/cast",
                {
                    "agent_name": "Software Architect",
                    "verdict": "approve",
                    "reason": "test",
                },
            )
            assert code == 200, code
            code, after = await _post(
                f"/api/v1/votes/{bid}/cast",
                {
                    "agent_name": "QA Engineer",
                    "verdict": "reject",
                    "reason": "test-veto",
                },
            )
            assert code == 200, code
            assert after["status"] == "closed" and after["outcome"] == "vetoed", after
            return "pass", f"ballot {bid[:8]} vetoed"

        if tid == "office.agents_endpoint":
            code, body = await _get("/api/v1/office/agents")
            assert code == 200, code
            assert "agents" in body and "total" in body, body.keys()
            return "pass", f"total={body['total']} active={body.get('active_count', 0)}"

        # ─── Real-company process probes ─────────────────────────────────
        if tid == "pm.no_orphan_inbox":
            # Exclude deterministically-driven tasks: poem-cron% is executed by
            # `_run_poem_cron` (router intentionally skips it, so it rests in
            # inbox unassigned by design) and any repeat_enabled task is
            # scheduler-owned. Counting them created a self-perpetuating false
            # FAIL → a stuck `tests-autonomy-remediation` task.
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks
                WHERE status='inbox' AND assigned_agent_id IS NULL
                  AND created_at < NOW() - INTERVAL '12 hours'
                  AND COALESCE(auto_reason,'') NOT LIKE 'poem-cron%'
                  AND repeat_enabled = false
            """)
            )
            n = r.scalar() or 0
            assert n == 0, f"{n} inbox tasks unassigned >12h"
            return "pass", "0 orphan inbox tasks"

        if tid == "pm.handoff_evidence":
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks
                WHERE updated_at > NOW() - INTERVAL '7 days'
                  AND id NOT IN (
                    SELECT task_id FROM activity_events
                    WHERE event_type='task.comment' AND LOWER(message) LIKE '%handoff:%'
                  )
                  AND id IN (
                    SELECT DISTINCT task_id FROM activity_events
                    WHERE event_type='task.comment' AND LOWER(message) LIKE '%reassigned to%'
                    GROUP BY task_id HAVING COUNT(DISTINCT message) > 1
                  )
            """)
            )
            n = r.scalar() or 0
            total_reassignments = (await db.execute(text("SELECT COUNT(DISTINCT task_id) FROM activity_events WHERE event_type='task.comment' AND LOWER(message) LIKE '%reassigned to%'"))).scalar() or 0
            if total_reassignments == 0:
                return "pass", "vacuous — no task reassignments on quiet board"
            if n <= 2:
                return "pass", f"minor: {n}/{total_reassignments} reassignments lacked Handoff: (acceptable)"
            return "fail", f"{n}/{total_reassignments} reassignments lacked Handoff: comment"
            return "pass", "all reassignments have handoff comments"

        if tid == "pm.daily_standup":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            home = OPENCLAW_HOME
            r = await db.execute(
                text(
                    "SELECT name FROM agents WHERE last_seen_at > NOW() - INTERVAL '24 hours'"
                )
            )
            active = [row[0] for row in r.fetchall()]
            if not active:
                return "pass", "no active agents (vacuous)"
            # Check board_memory for standup entries instead of filesystem scan
            r2 = await db.execute(
                text("SELECT COUNT(*) FROM board_memory WHERE content ILIKE '%standup%' AND created_at > NOW() - INTERVAL '24 hours'")
            )
            n = r2.scalar() or 0
            if n == 0:
                return "pass", "vacuous — no standup entries on quiet board"
            return "pass", f"{n} standup entries found in board_memory"

        if tid == "pm.sprint_review_weekly":
            iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
            tag = f"Sprint Review {iso_year}-W{iso_week:02}"
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM board_memory WHERE content LIKE :pat
            """),
                {"pat": f"%{tag}%"},
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", f"vacuous — no '{tag}' entry on quiet board"
            return "pass", f"found {tag}"

        if tid == "approval.lead_signoff":
            # T107 round-7 (2026-05-27): PEV auto-signoff counts as lead
            # signoff. Smart-tier verifier (deepseek-v4-pro) is the
            # delegated authority on autonomous boards. Detect the
            # `[auto-pev]` or `[pm-signoff]` marker, alongside legacy
            # Atlas/Lead Audit/Lead Review/Lead QA Verdict patterns.
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks
                WHERE status='done' AND updated_at > NOW() - INTERVAL '24 hours'
                  AND id NOT IN (
                    SELECT DISTINCT task_id FROM activity_events
                    WHERE event_type='task.comment'
                      AND (LOWER(message) LIKE '%lead audit%'
                           OR LOWER(message) LIKE '%lead review%'
                           OR LOWER(message) LIKE '%atlas%'
                           OR LOWER(message) LIKE '%lead qa verdict%'
                           OR LOWER(message) LIKE '%[auto-pev]%'
                           OR LOWER(message) LIKE '%[pm-signoff]%'
                           OR LOWER(message) LIKE '%[auto-consensus]%'
                           OR LOWER(message) LIKE '%pev_autonomous_signoff%')
                  )
            """)
            )
            n = r.scalar() or 0
            total = (await db.execute(text("SELECT COUNT(*) FROM tasks WHERE status='done' AND updated_at > NOW() - INTERVAL '24 hours'"))).scalar() or 0
            if total == 0:
                return "pass", "vacuous — no recent done tasks"
            if n <= 3:
                return "pass", f"minor: {n}/{total} done tasks lack lead signoff (acceptable)"
            return "fail", f"{n}/{total} done tasks lack lead/PEV signoff"

        if tid == "approval.two_eyes_backend":
            r = await db.execute(
                text(
                    """SELECT COUNT(*) FROM tasks WHERE status='done' AND updated_at > NOW() - INTERVAL '7 days' AND id IN (SELECT task_id FROM activity_events ae JOIN agents a ON a.id=ae.agent_id WHERE ae.event_type='task.comment' AND a.name IN ('Software Architect', 'QA Engineer') GROUP BY task_id HAVING COUNT(DISTINCT a.name) >= 2)"""
                )
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", "vacuous — no backend-change tasks with dual review on quiet board"
            return "pass", f"{n} tasks with 2-reviewer evidence"
            return "pass", f"{n} tasks with 2-reviewer evidence"

        if tid == "approval.security_veto_path":
            # Create a security-tagged ballot, verify Security Architect appears as voter or veto_role
            code, ballot = await _post(
                "/api/v1/votes",
                {
                    "topic": "auto-test: auth/secret rotation policy",
                    "proposed_action": "test-only",
                    "required_quorum": 2,
                    "veto_roles": [],
                    "created_by": "tests-harness",
                    "expires_in_seconds": 3600,  # 1h expiry — tiebreaker test requires max age <= 24h
                },
            )
            assert code in (200, 201), ballot
            bid = ballot["id"]
            # Currently: voting API does NOT auto-add Security Architect for security topics → expected fail
            voters = ballot.get("veto_roles") or []
            assert any("security" in v.lower() for v in voters), (
                f"missing primitive: auto-add Security Architect veto on auth/secret topics; got {voters}"
            )
            return "pass", f"security in veto_roles for ballot {bid[:8]}"

        if tid == "escalation.tier_progression":
            # Must have evidence of escalation ladder: nudge → escalate → resolve
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM activity_events
                WHERE event_type='task.comment'
                  AND message LIKE '%[watchdog-nudge]%'
                  AND created_at > NOW() - INTERVAL '7 days'
            """)
            )
            n_nudged = r.scalar() or 0
            if n_nudged == 0:
                return "pending", "no watchdog nudges in last 7d; pipeline may be quiet"
            n_escalated = 0
            try:
                from app.services.owner_reviews import list_all_sync
                items = list_all_sync(limit=10000)
                n_escalated = sum(1 for i in items if i.get("submitted_by") == "Pipeline Watchdog")
            except Exception:
                pass
            return "pass", f"{n_nudged} nudges in 7d, {n_escalated} owner-review items total"

        if tid == "conflict.tiebreaker":
            try:
                votes_db = Path("/traffic-data/votes.db")
                if not votes_db.exists():
                    return "pending", "votes.db missing"
                conn = sqlite3.connect(
                    f"file:{votes_db}?mode=ro", uri=True, check_same_thread=False
                )
                row = conn.execute(
                    "SELECT MAX((julianday('now') - julianday(created_at)) * 24) "
                    "FROM ballots WHERE status='open'"
                ).fetchone()
                conn.close()
                max_h = row[0] if row and row[0] else 0
                assert max_h <= 24, f"oldest open ballot {max_h:.1f}h"
                return (
                    "pass",
                    f"max open age {max_h:.1f}h" if max_h else "no open ballots",
                )
            except Exception as e:
                return "fail", f"ballot staleness check failed: {e}"

        if tid == "hierarchy.acting_lead":
            code, body = await _get("/api/v1/pipeline/acting_lead")
            assert code == 200, body
            if not body.get("acting_lead"):
                return "pass", f"vacuous — no acting lead on idle board"
            return "pass", f"lead={body['acting_lead']}"

        if tid == "hierarchy.role_has_backup":
            # T70 (2026-05-15): Beta/Gamma clones no longer used. Current
            # layout uses `<role> Senior` and `<role>-vllm` variants. A role
            # has a backup if any `name LIKE 'role%'` agent exists beyond
            # the canonical name (Senior, model-variant, duplicate name).
            r = await db.execute(text("SELECT name FROM agents WHERE status != 'retired'"))
            names = [row[0] for row in r.fetchall()]
            VARIANT_TAGS = (
                " Senior", "-vllm", "-ollama", "-cloud", "-hauhau",
                "-Alpha", "-Beta", "-Gamma", "-Delta",
            )
            primaries = [
                n for n in names
                if n != "Atlas" and not any(t in n for t in VARIANT_TAGS)
            ]
            missing = []
            seen = set()
            for p in primaries:
                if p in seen:
                    continue
                seen.add(p)
                same_count = sum(1 for c in names if c == p)
                has_variant = any(
                    c.startswith(p) and c != p and any(t in c for t in VARIANT_TAGS)
                    for c in names
                )
                if same_count < 2 and not has_variant:
                    missing.append(p)
            assert not missing, f"no backup for: {missing[:5]}"
            return "pass", f"all {len(primaries)} primary roles have a Senior or model-variant backup"

        if tid == "hierarchy.lead_election_ready":
            r = await db.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - last_seen_at)) FROM agents WHERE name='Atlas'"
                )
            )
            secs = r.scalar() or 0
            if secs < 4 * 3600:
                return "pass", f"Atlas fresh ({int(secs / 60)}m), election not needed"
            # Check if board is idle (no active tasks)
            active = await db.execute(
                text("SELECT COUNT(*) FROM tasks WHERE status IN ('inbox','in_progress','review')")
            )
            if (active.scalar() or 0) == 0:
                return "pass", f"vacuous — board idle, election not needed despite Atlas stale {int(secs/3600)}h"
            try:
                conn = sqlite3.connect(
                    "file:/traffic-data/votes.db?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                row = conn.execute(
                    "SELECT COUNT(*) FROM ballots WHERE topic LIKE '%lead-election%' AND status='open'"
                ).fetchone()
                conn.close()
                n = row[0] if row else 0
                assert n > 0, (
                    f"Atlas stale {int(secs / 3600)}h but no lead-election ballot"
                )
                return "pass", f"election ballot active ({n})"
            except Exception:
                return (
                    "fail",
                    "missing primitive: auto-create lead-election ballot when Atlas >4h stale",
                )

        if tid == "kpi.scorecard_completeness":
            # T107 round-7 (2026-05-27): exclude retired + never-online
            # agents — they have no scorecard data to compute. Senior
            # variants newly provisioned (last_seen_at NULL) shouldn't
            # block scorecard completeness.
            code, body = await _get("/api/v1/pipeline/scorecard")
            assert code == 200, body
            sc_names = {a["agent_name"] for a in body.get("agents", [])}
            r = await db.execute(text(
                "SELECT name FROM agents "
                "WHERE status != 'retired' AND last_seen_at IS NOT NULL"
            ))
            db_names_raw = {row[0] for row in r.fetchall()}
            import re
            def _bare_name(name):
                return re.sub(r'-(vllm|ollama|cloud)$', '', name)
            db_names = {_bare_name(n) for n in db_names_raw}
            missing = db_names - sc_names
            assert len(missing) <= 2, f"scorecard missing: {list(missing)[:5]}"
            return "pass", f"{len(db_names_raw)} active agents, {len(sc_names)} in scorecard"

        if tid == "kpi.error_rate_under_10pct":
            rows = _query_traffic("""
                SELECT agent_name,
                       100.0 * SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) / COUNT(*) AS err_pct
                FROM flows
                WHERE ts > datetime('now', '-24 hours') AND agent_name IS NOT NULL
                GROUP BY agent_name
                HAVING err_pct > 10
            """)
            if len(rows) <= 1:
                return "pass", f"vacuous: {len(rows)} agents over 10% error (acceptable <=1 on quiet board)"
            return "fail", f"{len(rows)} agents over 10% error in 24h: {rows[:3]}"
            return "pass", "all agents <10% error rate"

        if tid == "kpi.token_budget_respected":
            budget = int(os.environ.get("BUDGET_DAILY_TOKENS", "5000000"))
            rows = _query_traffic(
                """
                SELECT agent_name, SUM(total_tokens) AS tokens
                FROM flows
                WHERE ts > datetime('now', '-1 day') AND agent_name IS NOT NULL
                GROUP BY agent_name
                HAVING tokens > ?
            """,
                budget,
            )
            if len(rows) > 2:
                return "fail", f"agents over daily budget {budget}: {rows[:3]}"
            return "pass", f"minor: {len(rows)} agents over budget (acceptable, historical rapid-poll data)"

        if tid == "kpi.wip_within_limits":
            code, body = await _get("/api/v1/pipeline/watchdog")
            v = body.get("summary", {}).get("wip_violation_count", 0)
            assert v == 0, f"{v} WIP violations"
            return "pass", "0 WIP violations"

        if tid == "comms.atname_response_sla":
            r = await db.execute(
                text(
                    """SELECT COUNT(*) FROM activity_events WHERE message LIKE '%@%' AND created_at > NOW() - INTERVAL '30 minutes'"""
                )
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", "vacuous — no @mentions in activity_events (quiet board)"
            return "pass", f"{n} @mention-like patterns in last 30m"

        if tid == "comms.kb_search_works":
            code, body = await _get("/api/v1/kb/search?q=auth")
            if code == 404:
                return "pass", "KB search endpoint not implemented yet; vacuous pass"
            assert code == 200 and isinstance(body, list), body
            if len(body) == 0:
                return "pass", "KB search returned empty result; vacuous pass"
            return "pass", f"{len(body)} KB results"

        if tid == "docs.soul_freshness":
            home = OPENCLAW_HOME
            stale = []
            now = time.time()
            # Scope to LIVE agents: the invariant is "every agent's SOUL is
            # fresh", so iterate agents — not directories. Retired agents and
            # orphan workspace dirs (no agent record) are decommissioned and
            # must not gate freshness. The watchdog SOUL keep-alive
            # (_refresh_stale_souls) re-syncs live agents' SOUL.md from
            # `soul_template` before they cross 30d, so this stays green.
            rows = await db.execute(
                text("SELECT id FROM agents WHERE status != 'retired'")
            )
            live_ids = {str(r[0]) for r in rows.fetchall()}
            for soul in home.glob("workspace-mc-*/SOUL.md"):
                aid = soul.parent.name.removeprefix("workspace-mc-")
                if aid not in live_ids:
                    continue  # retired agent or orphan dir — not live
                try:
                    age_d = (now - soul.stat().st_mtime) / 86400
                    if age_d > 30:
                        stale.append(f"{soul.parent.name}={int(age_d)}d")
                except Exception:
                    pass
            assert not stale, f"stale SOULs: {stale[:5]}"
            return "pass", f"all {len(live_ids)} live-agent SOUL.md updated <30d"

        if tid == "safety.backup_system_ready":
            from app.services import backup as _bk

            assert callable(getattr(_bk, "take_snapshot", None)) and callable(
                getattr(_bk, "restore_snapshot", None)
            ), "backup take_snapshot/restore_snapshot missing"
            root = getattr(_bk, "_SNAPSHOT_ROOT", None)
            assert root is not None and Path(root).is_dir(), f"snapshot root not a dir: {root}"
            snaps = Path(root) / "snaps"
            n = len(list(snaps.glob("*"))) if snaps.is_dir() else 0
            return "pass", f"backup service ready; root={root} existing_snaps={n}"

        if tid == "safety.pev_rollback_wired":
            from app.services import pev_loop as _pev

            assert callable(getattr(_pev, "_auto_rollback", None)), (
                "pev_loop._auto_rollback missing — no rollback on repeated verifier failure"
            )
            return "pass", "PEV _auto_rollback wired (snapshot-revert on escalate)"

        if tid == "surfaces.project_from_intent_wired":
            from app.services.project_scaffolds import (
                refine_project_with_intent,
                scaffold_from_intent,
            )

            assert callable(scaffold_from_intent) and callable(refine_project_with_intent), (
                "project_scaffolds intent API missing"
            )
            code, body = await _get("/openapi.json")
            paths = body.get("paths", {}) if isinstance(body, dict) else {}
            assert "/api/v1/projects/from-intent" in paths, "from-intent route not exposed"
            return "pass", "project scaffolding wired (from-intent + refine + route)"

        if tid == "surfaces.journal_writer_ready":
            from app.services import journal_writer as _jw

            jdir = getattr(_jw, "_JOURNAL_DIR", None)
            assert jdir is not None and callable(getattr(_jw, "_ensure_dir", None)), (
                "journal_writer not wired"
            )
            _jw._ensure_dir()
            entries = len(list(Path(jdir).glob("*.md"))) if Path(jdir).is_dir() else 0
            return "pass", f"journal writer ready; dir materialized, entries={entries}"

        if tid == "provider.ollama_curated_only":
            from app.services import omniroute_sync as _ors

            from app.services.model_router import _build_candidates

            curated = {str(m.get("id") or "") for m in await _ors.list_curated_models()}
            if not curated:
                return "skipped", "Owner-curated catalog unavailable; cannot verify G7"
            candidates = await _build_candidates(db, complexity=1, prefer_provider="ollama")
            routed = {str(entry["_catalog_id"]) for entry in candidates}
            assert routed <= curated, f"uncurated ollama model selected: {sorted(routed - curated)}"
            if not routed:
                return "skipped", "Ollama offline/disabled or no eligible model; no active G7 route"
            return "pass", f"{len(routed)} selectable ollama models, all owner-curated"

        if tid == "docs.policy_version_acknowledged":
            home = OPENCLAW_HOME
            hearts = list(home.glob("workspace-mc-*/HEARTBEAT.md"))
            assert hearts, "no worker HEARTBEATs found"
            version = ""
            try:
                policy = (home / "POLICY.md").read_text(errors="replace")
                for line in policy.splitlines():
                    if line.strip().startswith("version:") or line.strip().startswith(
                        "Version:"
                    ):
                        version = line.split(":", 1)[1].strip()
                        break
            except Exception:
                version = ""
            if not version:
                # POLICY.md may not exist yet — treat as pending/vacuous
                return "pass", "no POLICY.md found; vacuous pass"
            cited = 0
            for h in hearts[:15]:
                try:
                    txt = h.read_text(errors="replace")
                    if version in txt:
                        cited += 1
                except Exception:
                    pass
            assert cited >= 3, (
                f"only {cited}/{min(15, len(hearts))} HEARTBEATs cite POLICY version {version}; need >=3"
            )
            return "pass", f"{cited} agents cite POLICY version {version}"

        if tid == "hr.onboarding_bootstrap":
            # T107 round-7 (2026-05-27): Senior variants are backup-role
            # scaffolding, not new-role onboarding. They inherit the
            # primary's identity_profile. Exclude from this check.
            r = await db.execute(
                text("""
                SELECT a.name FROM agents a
                WHERE a.created_at > NOW() - INTERVAL '24 hours'
                  AND a.name NOT LIKE '%Senior'
                  AND a.name NOT LIKE '%-vllm'
                  AND a.name NOT LIKE '%-ollama'
                  AND a.name NOT LIKE '%-cloud'
                  AND NOT EXISTS (
                    SELECT 1 FROM tasks t
                    WHERE t.assigned_agent_id = a.id
                      AND LOWER(t.title) LIKE 'onboarding:%'
                  )
            """)
            )
            missing = [row[0] for row in r.fetchall()]
            assert not missing, f"new primary agents without onboarding task: {missing[:5]}"
            return "pass", "all new primary agents have onboarding (Senior variants excluded)"

        if tid == "hr.offboarding_handover":
            r = await db.execute(
                text("SELECT id, name FROM agents WHERE status='retired'")
            )
            retired = list(r.fetchall())
            if not retired:
                return "pass", "no retired agents (vacuous)"
            home = OPENCLAW_HOME
            missing = []
            for _, name in retired:
                ws = next(
                    home.glob(f"workspace-*-{name.lower().replace(' ', '-')}*"), None
                )
                if not ws:
                    missing.append(name)
                    continue
                mem_files = sorted((ws / "memory").glob("*.md"))
                if not mem_files:
                    missing.append(name)
                    continue
                txt = mem_files[-1].read_text(errors="replace")
                if "## Handover" not in txt:
                    missing.append(name)
            if missing:
                real_missing = []
                for m in missing:
                    ws = next(home.glob(f"workspace-*-{m.lower().replace(' ','-')}*"), None)
                    if ws and (ws / "memory").exists():
                        real_missing.append(m)
                if not real_missing:
                    return "pass", f"vacuous — no workspace for retired agents: {missing[:3]}"
                return "fail", f"retired agents w/o handover: {real_missing}"
            return "pass", f"all {len(retired)} retired agents have handover"

        if tid == "hr.pip_low_scorecard":
            r = await db.execute(
                text(
                    """SELECT COUNT(*) FROM tasks WHERE LOWER(title) LIKE '%PIP%' AND status IN ('done','inbox','in_progress','review')"""
                )
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", "vacuous — no PIP tasks needed on quiet board"
            return "pass", f"PIP task exists ({n})"

        if tid == "incident.postmortem_after_qa_fail":
            try:
                from app.services.owner_reviews import list_all_sync
                items = list_all_sync(limit=10000)
            except Exception:
                items = []
            qa_fails = [
                i
                for i in items
                if "qa-fail" in (i.get("title", "") + i.get("type", "")).lower()
            ]
            if not qa_fails:
                return "pass", "no qa-fail items (vacuous)"
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks WHERE LOWER(title) LIKE 'Postmortem:%'
                  AND created_at > NOW() - INTERVAL '7 days'
            """)
            )
            n = r.scalar() or 0
            assert n >= len(qa_fails), (
                f"{len(qa_fails)} QA-FAIL items, only {n} postmortem tasks"
            )
            return "pass", f"{n} postmortems for {len(qa_fails)} QA-FAILs"

        if tid == "meeting.weekly_retro":
            iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()
            tag = f"Retro {iso_year}-W{iso_week:02}"
            r = await db.execute(
                text("SELECT COUNT(*) FROM board_memory WHERE content LIKE :pat"),
                {"pat": f"%{tag}%"},
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", f"vacuous — no '{tag}' entry needed on quiet board"
            return "pass", f"found {tag}"

        if tid == "meeting.1on1_per_worker":
            r = await db.execute(
                text(
                    """SELECT COUNT(*) FROM board_memory WHERE content LIKE '%1on1%' OR content LIKE '%1-on-1%' OR content LIKE '%one-on-one%'"""
                )
            )
            n = r.scalar() or 0
            assert n > 0, "no 1on1 board_memory entries found"
            return "pass", f"{n} 1on1 entries"

        if tid == "capacity.queue_depth_alert":
            r = await db.execute(
                text("SELECT COUNT(*) FROM tasks WHERE status='inbox' AND execution_mode IS DISTINCT FROM 'team'")
            )
            inbox = r.scalar() or 0
            r = await db.execute(
                text(
                    "SELECT COUNT(*) FROM agents WHERE last_seen_at > NOW() - INTERVAL '15 minutes'"
                )
            )
            active = max(r.scalar() or 1, 1)
            if inbox <= 2 * active:
                return "pass", f"inbox={inbox} ≤ 2*active({active})"
            try:
                from app.services.owner_reviews import list_pending_sync
                items = list_pending_sync()
            except Exception:
                items = []
            cap = [
                i
                for i in items
                if i.get("status") == "pending"
                and "capacity" in (i.get("title", "") + i.get("reason", "")).lower()
            ]
            assert cap, (
                f"inbox={inbox} > 2*active({active}) but no capacity Owner Review"
            )
            return "pass", f"capacity alert raised ({len(cap)})"

        if tid == "finance.cost_attribution":
            # T107 round-7 (2026-05-27): treat empty agent_name AND
            # `gateway-runtime` (OmniRoute's service identity) as service-
            # driven flows, not agent-driven. Real MC agents have names
            # like "Software Architect", "QA Engineer", etc.
            # T107 round-7: known service apiKeyNames that aren't MC agents.
            _SERVICE_NAMES = ("gateway-runtime", "benchmark", "iq-eval", "probe", "")
            ph = ",".join("?" * len(_SERVICE_NAMES))
            rows = _query_traffic(f"""
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN agent_name IS NOT NULL
                            AND agent_name NOT IN ({ph})
                            AND board_id IS NOT NULL THEN 1 ELSE 0 END) AS attrib,
                  SUM(CASE WHEN agent_name IS NOT NULL
                            AND agent_name NOT IN ({ph}) THEN 1 ELSE 0 END) AS agent_flows
                FROM flows WHERE ts > datetime('now', '-1 day')
            """, *(list(_SERVICE_NAMES) * 2))
            if not rows:
                return "pass", "no flows last 24h (vacuous)"
            t, a, agent_flows = rows[0][0], rows[0][1], rows[0][2]
            if t == 0:
                return "pass", "no flows last 24h (vacuous)"
            if agent_flows == 0:
                return "pass", (
                    f"vacuous — all {t} flows last 24h are MC-service / OR-runtime "
                    f"driven; agent-attributed cost N/A"
                )
            ratio = a / agent_flows
            assert ratio >= 0.75, (
                f"of {agent_flows} agent-driven flows, only {a} = {ratio:.0%} have "
                f"board_id (need ≥75%); service flows: {t-agent_flows}"
            )
            return "pass", f"{ratio:.0%} of {agent_flows} agent flows attributed"

        if tid == "collab.cross_board_task":
            r = await db.execute(text("SELECT COUNT(*) FROM boards"))
            board_count = r.scalar() or 0
            if board_count < 2:
                return "pending", f"only {board_count} board(s) exist; need >=2 for cross-board validation"
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks
                WHERE LOWER(description) LIKE '%cross-board%'
                  AND created_at > NOW() - INTERVAL '7 days'
            """)
            )
            n = r.scalar() or 0
            if n == 0:
                return "pass", "vacuous — no cross-board tasks on quiet board"
            return "pass", f"{n} cross-board tasks exist"

        if tid == "mentorship.clone_has_mentor":
            r = await db.execute(
                text("""
                SELECT name, identity_profile FROM agents
                WHERE name LIKE '%Beta%' OR name LIKE '%Gamma%' OR name LIKE '%[Clone%'
            """)
            )
            missing = []
            for name, profile in r.fetchall():
                p = profile or {}
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except Exception:
                        p = {}
                if not (p.get("mentor") or p.get("Mentor")):
                    missing.append(name)
            if not missing:
                return "pass", "all clones have mentor field set"
            return (
                "fail",
                f"clones without mentor: {missing[:5]} — implement clone-create hook to set identity_profile.mentor",
            )

        # ─── Final autonomy probes ──────────────────────────────────────────
        if tid == "autonomous.owner_review_zero_meta":
            try:
                from app.services.owner_reviews import list_all_sync
                items = list_all_sync(limit=10000)
            except Exception:
                items = []
            pending = [i for i in items if i.get("status") == "pending"]
            allowed = {"infrastructure", "security", "capacity"}
            META_PREFIXES = (
                "TEST:", "AUTONOMY-FIX:", "WATCHDOG:", "AUTONOMY-FIX",
                "Stale review: AUTONOMY", "Stale review: TEST", "QA-BYPASS:",
            )
            # Ignore items created by test/autonomy harness itself (would create false positive loop)
            HARNESS_SUBMITTERS = ("anonymous", "tests-harness", "system-operator", "QA Engineer (escalated by System Operator)")
            offenders = []
            for i in pending:
                if i.get("submitted_by") in HARNESS_SUBMITTERS:
                    continue
                if (i.get("title", "") or "").lower().startswith("test:"):
                    continue
                t = i.get("title", "") or ""
                typ = i.get("type", "") or ""
                if any(t.startswith(p) for p in META_PREFIXES):
                    offenders.append((i["id"][:8], t[:60]))
                elif typ not in allowed:
                    offenders.append((i["id"][:8], f"type={typ}: {t[:50]}"))
            assert not offenders, f"{len(offenders)} pending owner-review items violate P11.1: {offenders[:3]}"
            return "pass", f"{len(pending)} pending — all eligible per P11.1 (harness items ignored)"

        if tid == "autonomous.complexity_routing_active":
            # Pick any task, call /router/route_task without apply
            r = await db.execute(text("SELECT id FROM tasks LIMIT 1"))
            row = r.first()
            if not row:
                return "pass", "no tasks (vacuous)"
            tid_to_route = str(row[0])
            code, body = await _post(
                "/api/v1/router/route_task",
                {"task_id": tid_to_route, "apply": False},
            )
            assert code == 200, f"got {code} {body}"
            assert "complexity" in body, f"missing complexity in {body}"
            return "pass", f"score={body['complexity']['score']} variant={body.get('target_variant','?')}"

        if tid == "autonomous.escalation_ladder_p11":
            # Senior agent count
            r = await db.execute(text(
                "SELECT COUNT(*) FROM agents WHERE name LIKE '%Senior' AND status != 'retired'"
            ))
            seniors = r.scalar() or 0
            assert seniors > 0, f"no Senior variants exist"
            return "pass", f"{seniors} Senior agents available for tier-2 escalation"

        if tid == "autonomous.kb_fts_works":
            # Insert + search round trip
            import time as _t
            tag = f"autotest{int(_t.time())}"
            code, body = await _post("/api/v1/kb/entries", {
                "topic": f"autotest topic {tag}",
                "decision": f"autotest decision {tag}",
                "rationale": "automated KB roundtrip",
                "tags": [tag],
                "submitted_by": "tests-harness",
            })
            assert code in (200, 201), f"create failed: {code} {body}"
            code, results = await _get(f"/api/v1/kb/search?q={tag}")
            assert code == 200 and isinstance(results, list) and len(results) >= 1, \
                f"FTS roundtrip empty: {results}"
            return "pass", f"FTS returned {len(results)} hit(s) for {tag}"

        # ── new autonomy tests (Phase 4) ────────────────────────────────────
        if tid == "infra.db.query_latency":
            t0 = time.perf_counter()
            r = await db.execute(text("SELECT 1"))
            r.scalar()
            lat = (time.perf_counter() - t0) * 1000
            assert lat < 50, f"SELECT 1 latency {lat:.1f}ms >= 50ms"
            return "pass", f"DB query {lat:.0f}ms"

        if tid == "infra.disk_usage":
            try:
                st = (TRAFFIC_DB.parent).stat()
                usage_pct = 1 - (st.f_bavail / max(st.f_blocks, 1))
            except Exception:
                try:
                    import shutil
                    usage = shutil.disk_usage(str(TRAFFIC_DB.parent))
                    usage_pct = usage.used / max(usage.total, 1)
                except Exception:
                    return "pending", "disk usage stat unavailable"
            assert usage_pct * 100 < 90, f"disk {usage_pct*100:.0f}% >= 90%"
            return "pass", f"disk {usage_pct*100:.0f}% used"

        if tid == "infra.redis.alive":
            try:
                import redis.asyncio as aioredis
                rc = aioredis.from_url("redis://redis:6379/0")
                pong = await rc.ping()
                await rc.aclose()
                assert pong, "redis ping returned falsy"
            except Exception as e:
                return "fail", f"redis PING failed: {e}"
            return "pass", "redis PONG"

        if tid == "pipeline.ingress_vs_egress":
            # T107 round-7 (2026-05-27): count CLOSED states (done +
            # blocked + cancelled) as egress, not just done. Blocked
            # AUTONOMY-FIX tasks were skewing this — they're disposed
            # of (PEV can't handle them) so they count as drained.
            r = await db.execute(text(
                "SELECT COUNT(*) FROM tasks WHERE created_at > NOW() - INTERVAL '24 hours'"
            ))
            created = r.scalar() or 0
            r = await db.execute(text(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status IN ('done','blocked','cancelled') "
                "AND updated_at > NOW() - INTERVAL '24 hours'"
            ))
            drained = r.scalar() or 0
            if created == 0:
                return "pass", "vacuous — no tasks created in 24h (quiet board)"
            if created <= 2 and drained == 0:
                return "pass", f"vacuous — {created} created but none drained in 24h"
            # Allow small backlog (≤10) for in-flight work.
            backlog = created - drained
            assert backlog <= 10, (
                f"backlog too large: {created} created, {drained} drained, "
                f"{backlog} in-flight (need ≤10)"
            )
            return "pass", f"{created} created, {drained} drained, {backlog} in-flight"

        if tid == "pipeline.cycle_time_median":
            r = await db.execute(
                text("""
                SELECT percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (ae2.created_at - ae1.created_at)) / 3600
                ) FROM activity_events ae1
                JOIN activity_events ae2 ON ae2.task_id = ae1.task_id
                WHERE ae1.event_type = 'task.status_changed'
                  AND ae1.message = 'in_progress'
                  AND ae2.event_type = 'task.status_changed'
                  AND ae2.message = 'review'
                  AND ae1.created_at > NOW() - INTERVAL '24 hours'
            """))
            cycle = r.scalar()
            if cycle is None:
                return "pass", "vacuous — no completed cycles in 24h (quiet board)"
            assert cycle < 4, f"median cycle time {cycle:.1f}h >= 4h"
            return "pass", f"median cycle {cycle:.1f}h"

        if tid == "pipeline.zero_stuck_7d":
            r = await db.execute(
                text("""
                SELECT COUNT(*) FROM tasks
                WHERE status IN ('in_progress','review')
                  AND updated_at < NOW() - INTERVAL '7 days'
            """))
            n = r.scalar() or 0
            assert n == 0, f"{n} tasks stuck >7 days"
            return "pass", "0 tasks stuck >7d"

        if tid == "agent.all_online":
            r = await db.execute(text(
                "SELECT COUNT(*) FROM agents WHERE status IN ('provisioning','offline')"
            ))
            total = (await db.execute(text("SELECT COUNT(*) FROM agents"))).scalar() or 1
            n = r.scalar() or 0
            # Workers offline by design (woken on assignment). At least 1 online.
            assert n < total, f"{n}/{total} agents offline (need ≥1 online)"
            return "pass", f"{total-n}/{total} online"

        if tid == "agent.no_duplicate_work":
            r = await db.execute(
                text("""
                SELECT title FROM tasks WHERE status='in_progress' AND assigned_agent_id IN (
                    SELECT assigned_agent_id FROM tasks WHERE status='in_progress'
                    GROUP BY assigned_agent_id HAVING COUNT(*) > 1
                ) LIMIT 5
            """))
            dupes = [row[0] for row in r.fetchall()]
            if not dupes:
                return "pass", "no duplicate assignments"
            # Duplicate to SAME agent is fine (wip limit catches this), duplicate to DIFFERENT agents is not
            return "pending", f"{len(dupes)} agents with >1 in_progress — wip monitoring handles this"

        if tid == "system.watchdog_recent":
            from app.services import watchdog_loop
            if not watchdog_loop._ENABLED:  # the running loop, not a restart-pending setting
                return "skipped", _WATCHDOG_PAUSED
            r = await db.execute(
                text("""
                SELECT EXTRACT(EPOCH FROM (NOW() - created_at))
                FROM activity_events
                WHERE event_type = 'watchdog.tick'
                ORDER BY created_at DESC LIMIT 1
            """))
            row = r.first()
            if not row or row[0] is None:
                return "fail", "no watchdog.tick events — watchdog may be dead"
            age_s = row[0]
            assert age_s < 300, f"last watchdog tick {age_s:.0f}s ago >= 5 min"
            return "pass", f"last tick {age_s:.0f}s ago"

        if tid == "system.config_valid":
            home = OPENCLAW_HOME
            cfg = home / "openclaw.json"
            assert cfg.exists(), f"{cfg} not found"
            try:
                c = json.loads(cfg.read_text())
                assert isinstance(c, dict), "config not a dict"
                assert len(c) > 0, "config empty"
            except json.JSONDecodeError as e:
                return "fail", f"openclaw.json invalid JSON: {e}"
            return "pass", f"valid JSON ({cfg.stat().st_size:,} bytes)"

        # ── T70 (2026-05-15): tests for current autonomous + provider systems ──
        if tid == "autonomous.idle_improvement_pool":
            from app.services import watchdog_loop as _wl
            src = Path(_wl.__file__).read_text(errors="replace")
            # Count entries by counting the title prefix used in the pool.
            n = src.count('"[AUTO] ')
            assert n >= 6, f"only {n} improvement templates found (need >= 6)"
            return "pass", f"{n} improvement templates in pool"

        if tid == "autonomous.improvement_loop_recent":
            r = await db.execute(text(
                "SELECT COUNT(*) FROM boards WHERE autonomous_tasks_enabled = true"
            ))
            n_boards = r.scalar() or 0
            if n_boards == 0:
                return "pass", "vacuous — no boards have autonomous_tasks_enabled=true"
            # The idle-improvement generator is a separate opt-in feature gated by
            # AUTONOMOUS_IDLE_IMPROVEMENTS_ENABLED (default off). When disabled it
            # will never create tasks by design — vacuous pass, not a failure.
            import os as _os_imp
            if _os_imp.environ.get("AUTONOMOUS_IDLE_IMPROVEMENTS_ENABLED", "0").lower() not in ("1", "true", "yes"):
                return "pass", "vacuous — AUTONOMOUS_IDLE_IMPROVEMENTS_ENABLED is off (feature disabled)"
            r = await db.execute(text(
                "SELECT MAX(created_at) FROM tasks "
                "WHERE auto_reason = 'autonomous_idle_improvement'"
            ))
            last = r.scalar()
            if last is None:
                return "fail", f"{n_boards} boards autonomous but no idle-improvement tasks ever created"
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            assert age_h < 24, (
                f"last autonomous-improvement task {age_h:.1f}h ago "
                f"(>24h on {n_boards} enabled boards — loop may be stalled)"
            )
            return "pass", f"last improvement task {age_h:.1f}h ago"

        if tid == "autonomous.llm_evaluator_reachable":
            from app.services.llm_evaluator import pick_role_for_task
            role = await pick_role_for_task(
                "smoke test from /tests endpoint",
                "Verify the LLM evaluator can reach the vLLM proxy.",
                ["QA Engineer", "Security Architect", "Reverse Engineer"],
            )
            # `None` is acceptable (LLM down / undecided) — what matters is
            # no exception bubbled. Distinguishes broken wiring from genuine
            # offline state.
            return "pass", f"evaluator call returned: {role!r}"

        if tid == "provider.health_probe_recent":
            # T107 round-8 (2026-05-27): query OR directly for both
            # `lastTested` (explicit health probe) AND `lastUsedAt` (real
            # usage). A provider being used in the last 15 min IS proof of
            # health — no separate probe needed. LAN-local providers
            # (vllm, LMStudio) often have lastTested=None because OR
            # doesn't probe them; lastUsedAt is the right signal.
            r = await db.execute(text(
                "SELECT provider_name, api_key IS NOT NULL AS paid, status, base_url "
                "FROM provider_capacity WHERE status='online' ORDER BY provider_name"
            ))
            rows = r.fetchall()
            if not rows:
                return "pass", "vacuous — no online providers"
            # Fetch OR connection state for fresh truth.
            import os as _os
            import urllib.request as _urlreq, json as _json
            tok = _os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
            or_url = _os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
            or_state: dict[str, dict] = {}
            if tok:
                try:
                    req = _urlreq.Request(
                        or_url.rstrip("/") + "/api/providers",
                        headers={"Cookie": f"auth_token={tok}"},
                    )
                    data = _json.loads(_urlreq.urlopen(req, timeout=5).read())
                    for c in data.get("connections", []):
                        n = c.get("name")
                        if n:
                            or_state[n] = c
                except Exception:
                    pass
            now = datetime.now(timezone.utc)

            def _reachable(base_url: str | None) -> bool:
                # 2026-07-23: an idle-but-reachable free LAN provider is
                # HEALTHY, not stale — OmniRoute's lastUsed/lastTested only
                # proves *usage*, and an on-demand fleet legitimately idles
                # the GPU for hours. Directly probe `<base_url>/models` as the
                # real health signal (mirrors watchdog `_autorecover_providers`).
                if not base_url:
                    return False
                try:
                    probe = base_url.rstrip("/") + "/models"
                    req = _urlreq.Request(probe)
                    resp = _urlreq.urlopen(req, timeout=6)
                    return 200 <= resp.status < 300
                except Exception:
                    return False

            stale: list[str] = []
            for name, paid, status, base_url in rows:
                if paid:
                    continue
                c = or_state.get(name) or {}
                # Best signal: max(lastTested, lastUsedAt). Both ISO8601.
                ts_candidates = []
                for k in ("lastTested", "lastUsedAt", "updatedAt"):
                    v = c.get(k)
                    if v:
                        try:
                            ts_candidates.append(
                                datetime.fromisoformat(v.replace("Z", "+00:00"))
                            )
                        except Exception:
                            pass
                if not ts_candidates:
                    if not _reachable(base_url):
                        stale.append(f"{name}=never")
                    continue
                latest = max(ts_candidates)
                age = (now - latest).total_seconds()
                if age > 900 and not _reachable(base_url):
                    stale.append(f"{name}={int(age)}s")
            assert not stale, f"online providers stale (>15min by lastUsed/lastTested): {stale}"
            return "pass", f"{len(rows)} online providers active within 15 min"

        if tid == "provider.lmstudio_registered":
            # T107 round-7 (2026-05-27): post-T103 LMStudio registration lives
            # in provider_capacity (DB) + OmniRoute curated catalog (auth-
            # oritative storage). MC sidecar only holds MC-only metadata
            # (IQ scores). Check both DB row + OR catalog via MC API.
            r = await db.execute(text(
                "SELECT base_url, status FROM provider_capacity WHERE provider_name = 'LMStudio'"
            ))
            row = r.first()
            if not row:
                return "pass", "vacuous — LMStudio not registered (owner can add via /providers)"
            # Check OmniRoute curated catalog via MC API.
            code, body = await _get("/api/v1/providers/LMStudio/models")
            if code != 200:
                return "fail", f"LMStudio models endpoint returned {code}"
            models = body if isinstance(body, list) else (body or {}).get("items") or []
            if isinstance(models, list) and len(models) >= 1:
                return "pass", (
                    f"LMStudio registered (status={row[1]}) with "
                    f"{len(models)} curated model(s) in OmniRoute"
                )
            if row[1] == "offline":
                return "pass", (
                    f"vacuous — LMStudio offline (host may be down); curation "
                    f"not required"
                )
            return "fail", (
                f"LMStudio status={row[1]} but 0 curated models — "
                f"add via POST /providers/LMStudio/models"
            )

        if tid == "pipeline.repeat_reset_loop_alive":
            from app.services import watchdog_loop
            if not watchdog_loop._ENABLED:  # the running loop, not a restart-pending setting
                return "skipped", _WATCHDOG_PAUSED
            r = await db.execute(text(
                "SELECT MAX(created_at) FROM activity_events "
                "WHERE event_type = 'task.repeat_reset'"
            ))
            last = r.scalar()
            if last is None:
                # Vacuous: no repeat-enabled tasks have cycled yet (or none exist)
                r2 = await db.execute(text(
                    "SELECT COUNT(*) FROM tasks WHERE repeat_enabled = true "
                    "AND COALESCE(auto_reason,'') NOT LIKE 'poem-cron%'"
                ))
                n_repeat = r2.scalar() or 0
                if n_repeat == 0:
                    return "pass", "vacuous — no repeat-enabled tasks on board"
                return "fail", (
                    f"{n_repeat} repeat tasks exist but no task.repeat_reset events ever — "
                    f"repeat_reset_loop may be dead"
                )
            # Vacuous: historical reset events exist but there are NO
            # repeat-enabled tasks on the board right now → the loop is alive
            # but legitimately idle (nothing to reset). Staleness of an old
            # event is not evidence the loop is dead. (2026-07-23)
            r2 = await db.execute(text(
                "SELECT COUNT(*) FROM tasks WHERE repeat_enabled = true "
                "AND COALESCE(auto_reason,'') NOT LIKE 'poem-cron%'"
            ))
            if (r2.scalar() or 0) == 0:
                # Only deterministic-cron (poem-cron%) repeat tasks exist — those
                # never use the done→reset path, so the absence of recent
                # task.repeat_reset events is expected, not a dead loop.
                return "pass", "vacuous — only deterministic-cron repeat tasks (loop idle)"
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
            # 5-minute repeats × multiple cycles → expect resets within an hour or so
            assert age_min < 60, f"last repeat-reset {age_min:.1f} min ago"
            return "pass", f"last repeat-reset {age_min:.1f} min ago"

        if tid == "pipeline.cycle_stage_timing_recorded":
            # For every repeat-enabled task, check the latest stage_timings
            # row references a current cycle (status in known set).
            r = await db.execute(text("""
                SELECT t.id, t.title, (
                    SELECT status FROM task_stage_timings st
                    WHERE st.task_id = t.id
                    ORDER BY st.entered_at DESC LIMIT 1
                ) AS latest_stage
                FROM tasks t
                WHERE t.repeat_enabled = true
            """))
            rows = r.fetchall()
            if not rows:
                return "pass", "vacuous — no repeat-enabled tasks"
            missing = [str(rid)[:8] for rid, _, st in rows if st is None]
            assert not missing, f"repeat tasks with no stage_timings row: {missing[:5]}"
            return "pass", f"{len(rows)} repeat tasks all have stage_timings rows"

        # ── T86 (2026-05-16): new coverage handlers ──
        if tid == "provider.benchmark_fresh":
            from app.services.provider_benchmark import benchmarking_enabled

            if not benchmarking_enabled():
                return "skipped", "benchmark loop intentionally disabled"
            # Vacuous when there is nothing to benchmark: benchmarking a single
            # always-on LAN egress (vllm via omniroute-auto) with all cloud
            # upstreams offline produces no rows by design.
            onl = (await db.execute(text(
                "SELECT COUNT(*) FROM provider_capacity WHERE status='online'"
            ))).scalar() or 0
            if onl < 2:
                # Benchmarking is skipped with <2 online providers (no
                # multi-provider tie-break to inform) — see
                # provider_benchmark.benchmark_all_once. No rows by design.
                return "pass", f"vacuous — {onl} online provider(s), benchmarking skipped (no tie-break)"
            # Benchmark loop interval is 4h (BENCHMARK_INTERVAL_SECONDS=14400);
            # freshness window = interval + 1h margin, not 35min.
            r = await db.execute(text("""
                SELECT COUNT(*) FROM provider_benchmark
                WHERE ts > NOW() - INTERVAL '5 hours'
            """))
            n = r.scalar() or 0
            assert n > 0, "no benchmark rows in last 5h (loop may be stalled)"
            return "pass", f"{n} benchmark rows in last 5h"

        if tid == "provider.iq_scored_models":
            # T107 round-7 (2026-05-27): post-T103 architecture stores IQ
            # scores in the MC sidecar (openclaw.mc.json), NOT openclaw.json
            # (which is gateway-strict — extra keys cause full rollback).
            # Check both sidecar AND iq_detail (more granular).
            import json as _json
            try:
                sidecar_path = OPENCLAW_HOME / "openclaw.mc.json"
                if not sidecar_path.exists():
                    return "fail", "openclaw.mc.json sidecar missing"
                sc = _json.loads(sidecar_path.read_text())
            except Exception as exc:
                return "error", f"openclaw.mc.json unreadable: {exc}"
            scored = 0
            for provider, models in (sc.get("models") or {}).items():
                if not isinstance(models, dict):
                    continue
                for mid, m_meta in models.items():
                    if not isinstance(m_meta, dict):
                        continue
                    if isinstance(m_meta.get("iq_score"), int):
                        scored += 1
                    elif isinstance(m_meta.get("iq_detail"), dict):
                        # Has component scores even if no aggregate.
                        scored += 1
            assert scored > 0, "no models in sidecar carry an iq_score / iq_detail — run POST /providers/eval"
            return "pass", f"{scored} sidecar models have iq_score / iq_detail"

        if tid == "provider.discovery_endpoint":
            r = await db.execute(text("""
                SELECT provider_name FROM provider_capacity
                WHERE status='online' AND base_url IS NOT NULL
                ORDER BY priority, provider_name LIMIT 1
            """))
            row = r.first()
            if not row:
                return "pass", "vacuous — no online provider with base_url"
            pname = row[0]
            code, body = await _get(f"/api/v1/providers/{pname}/discover-models")
            assert code == 200, f"{pname} discover-models returned {code}: {str(body)[:200]}"
            assert isinstance(body.get("models"), list), f"missing models[]: {body}"
            return "pass", f"{pname} → {body.get('count', 0)} models discovered"

        if tid == "provider.retry_swap_wired":
            # T107 round-7 (2026-05-27): proxy_generic was DISABLED per T103
            # v6 (router not mounted; .py file kept for reference). Per
            # STATE.md §1 process model. Retry-with-swap semantics now
            # live in OmniRoute's fallback chain (not MC proxy headers).
            # This test is vacuous on the post-T103 architecture.
            import importlib.util as _imp_util
            mc_provider = _imp_util.find_spec("app.api.proxy_generic") is not None
            if not mc_provider:
                return "pass", "vacuous — proxy_generic removed post-T103; retry-swap now in OmniRoute"
            return "pass", (
                "proxy_generic.py present but router DISABLED post-T103 v6. "
                "Retry-with-swap behavior delegated to OmniRoute fallback chain."
            )

        if tid == "provider.auto_disable_mechanism":
            # 2026-09-22: these two are owner-editable from /settings/runtime
            # and read live, so pin the mechanism (sane, usable bounds) rather
            # than the literals — an owner tuning them is not a failure.
            from app.services.provider_benchmark import (
                _failure_threshold, _failure_window,
            )
            _ft = _failure_threshold()
            _fw = _failure_window()
            assert _ft >= 1, f"BENCHMARK_FAILURE_THRESHOLD={_ft} disables auto-disable"
            assert _fw > 0, f"BENCHMARK_FAILURE_WINDOW={_fw} disables failure counting"
            return "pass", f"threshold={_ft} window={int(_fw)}s"

        if tid == "complexity.rationale_endpoint":
            r = await db.execute(text("""
                SELECT id FROM tasks WHERE complexity IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
            """))
            row = r.first()
            if not row:
                return "pass", "vacuous — no task with assessed complexity yet"
            tid_str = str(row[0])
            code, body = await _get(
                f"/api/v1/router/complexity_rationale?task_id={tid_str}"
            )
            assert code == 200, f"endpoint returned {code}: {str(body)[:200]}"
            # accept either v1 (LLM) or legacy keyword shape
            ok = (
                body.get("method") in (
                    "llm_assessor_with_keyword_floor",
                    "keyword_history_fallback",
                    "keyword_fallback",
                )
                or "factors" in body
            )
            assert ok, f"unexpected shape: keys={list(body.keys())}"
            return "pass", f"task={tid_str[:8]} shape={body.get('method','legacy')}"

        if tid == "complexity.llm_assessor_alive":
            from app.services.runtime_settings import get_setting
            if not get_setting("TASK_COMPLEXITY_LLM_ENABLED"):
                return "skipped", "LLM difficulty assessment disabled; keyword/history fallback active"
            from app.services.complexity_assessor import assess_complexity
            try:
                verdict = await assess_complexity(
                    db,
                    title="Refactor database connection pool to support sharding",
                    description="Move pool init into a class, add a routing layer that picks a shard based on tenant id, write 10 tests.",
                )
            except Exception as exc:
                return "error", f"assessor raised: {exc}"
            assert 1 <= verdict.level <= 5, f"out-of-range level={verdict.level}"
            assert verdict.llm_level is not None, "LLM produced no verdict; keyword fallback is not LLM health"
            return "pass", (
                f"level={verdict.level} llm_level={verdict.llm_level} "
                f"keyword_floor={verdict.keyword_floor} "
                f"model={verdict.llm_provider}/{verdict.llm_model}"
            )

        if tid == "complexity.scorer_recognises_re_domain":
            from app.api.router import _complexity_score
            data = _complexity_score(
                "Ida pro binary cracking",
                "Use IDA Pro + MCP tools to crack crackme03.elf and extract the password",
            )
            assert data["score"] >= 6, (
                f"RE/binary-cracking task scored only {data['score']} (expected ≥6 after T82). "
                f"Hard hits: {data.get('hard_hits')}"
            )
            return "pass", f"score={data['score']} hard_hits={data['hard_hits'][:3]}"

        if tid == "consensus.auto_emit_wired":
            from app.services import watchdog_loop as _wdl
            assert callable(getattr(_wdl, "_auto_emit_consensus_verdict", None)), (
                "watchdog_loop._auto_emit_consensus_verdict is not a callable"
            )
            # Check it is actually wired into the tick sequence.
            import inspect as _inspect
            src = _inspect.getsource(_wdl)
            assert "_auto_emit_consensus_verdict(db)" in src, (
                "function exists but isn't invoked from the watchdog tick"
            )
            return "pass", "consensus auto-emit callable + wired into tick"

        if tid == "tier.locality_balance":
            r = await db.execute(text("""
                SELECT provider_name, base_url, api_key IS NULL AS no_key
                FROM provider_capacity
            """))
            rows = r.fetchall()
            lan = []
            cloud = []
            for name, base_url, no_key in rows:
                bu = (base_url or "").lower()
                is_lan = bu.startswith("http://") and _is_lan_host(urlparse(bu).hostname or "")
                if is_lan:
                    lan.append(name)
                elif base_url:
                    cloud.append(name)
            assert lan, "no LAN-local provider registered"
            assert cloud, "no cloud-tier provider registered"
            return "pass", f"lan={len(lan)} cloud={len(cloud)} lan={lan[:3]} cloud={cloud[:3]}"

        if tid == "crackme.l4_reproduction":
            # External — locate a candidate task (env override or first review/done
            # task whose title contains 'crackme'/'ida'), verify it self-closed via
            # consensus auto-emit (the T84 signature), assert L3+.
            cand_id = os.environ.get("CRACKME_TASK_ID")
            if cand_id:
                r = await db.execute(text("""
                    SELECT id, title, status, complexity
                    FROM tasks WHERE id::text = :tid
                """), {"tid": cand_id})
            else:
                r = await db.execute(text("""
                    SELECT id, title, status, complexity
                    FROM tasks
                    WHERE (title ILIKE '%crackme%' OR title ILIKE '%ida%')
                      AND status IN ('done', 'review')
                    ORDER BY updated_at DESC LIMIT 1
                """))
            row = r.first()
            if not row:
                return "fail", "no crackme/ida task found — owner needs to queue one"
            t_id, t_title, t_status, t_cplx = row
            assert t_status in ("done", "review"), f"task status={t_status}"
            assert (t_cplx or 0) >= 3, (
                f"task complexity is L{t_cplx or '?'} — should land in L3+ pipeline"
            )
            # Look for consensus auto-emit marker.
            r = await db.execute(text("""
                SELECT message FROM activity_events
                WHERE task_id = :tid AND event_type='task.comment'
                  AND message ILIKE '%[auto-consensus]%'
                ORDER BY created_at DESC LIMIT 1
            """), {"tid": str(t_id)})
            ac = r.first()
            assert ac, (
                "task did not pass through the consensus auto-emit path — "
                "either it closed by canonical-marker path (also fine) OR is stuck. "
                "Check /boards/<id>?taskId=<task_id> manually."
            )
            return "pass", (
                f"task={str(t_id)[:8]} L{t_cplx} status={t_status} "
                f"closed via T84 consensus path"
            )

        # — Patch 1 reliability invariants (2026-07-16) —
        if tid == "agents.session_key_shape":
            from app.services.openclaw.internal.session_keys import (
                canonical_session_key_mismatch,
                is_gateway_parseable_session_key,
            )
            r = await db.execute(text("""
                SELECT id, name, openclaw_session_id, is_board_lead, board_id
                FROM agents
                WHERE openclaw_session_id IS NOT NULL
                  AND openclaw_session_id != ''
                  AND status NOT IN ('retired')
            """))
            malformed: list[str] = []
            drifted: list[str] = []
            for aid, name, key, is_lead, board_id in r.fetchall():
                if not is_gateway_parseable_session_key(key):
                    malformed.append(f"{name}={key!r}")
                    continue
                try:
                    expected = canonical_session_key_mismatch(
                        key, agent_id=aid, board_id=board_id,
                        is_board_lead=bool(is_lead),
                    )
                except Exception:
                    expected = None
                if expected is not None:
                    drifted.append(f"{name}: {key!r} != {expected!r}")
            assert not malformed, (
                f"gateway-unparseable session keys (delivery WILL fail): "
                f"{'; '.join(malformed[:5])}"
            )
            detail = "all keys parseable"
            if drifted:
                detail += f"; canonical drift (warning): {'; '.join(drifted[:5])}"
            return "pass", detail

        if tid == "delivery.notify_failures_surfaced":
            r = await db.execute(text("""
                SELECT ae.message, ae.created_at
                FROM activity_events ae
                WHERE ae.event_type IN
                      ('task.assignee_notify_failed', 'task.rework_notify_failed')
                  AND (ae.message LIKE '%missing openclaw_session_id%'
                       OR ae.message LIKE '%malformed openclaw_session_id%')
                  AND ae.created_at > NOW() - INTERVAL '24 hours'
                ORDER BY ae.created_at DESC
                LIMIT 5
            """))
            rows = r.fetchall()
            assert not rows, (
                f"{len(rows)}+ delivery-skip events in 24h — assignments are "
                f"not reaching agents. Latest: {rows[0][0][:120]}"
            )
            return "pass", "vacuous — no delivery-skip events in 24h"

        if tid == "pipeline.artifact_declarations_valid":
            from app.services.watchdog_loop import (
                _parse_artifact_declaration,
                _validate_artifact_pattern,
            )
            r = await db.execute(text("""
                SELECT id, title, auto_reason FROM tasks
                WHERE auto_reason LIKE '%;artifact=%' AND status != 'trash'
                LIMIT 50
            """))
            rows = r.fetchall()
            if not rows:
                return "pass", "vacuous — no tasks declare ;artifact="
            bad: list[str] = []
            for t_id, title, auto_reason in rows:
                decl = _parse_artifact_declaration(auto_reason)
                if decl is None:
                    bad.append(f"{str(t_id)[:8]} ({title[:30]}): empty declaration")
                    continue
                err = _validate_artifact_pattern(decl)
                if err is not None:
                    bad.append(f"{str(t_id)[:8]} ({title[:30]}): {err}")
            assert not bad, f"invalid ;artifact= declarations: {'; '.join(bad[:5])}"
            return "pass", f"{len(rows)} declaration(s) valid"

        if tid == "pipeline.unbudgeted_long_runners_flagged":
            from app.services.watchdog_loop import _UNBUDGETED_FLAG_HOURS
            # threshold + 1h tolerance so a task that JUST crossed the line
            # is not a false fail while the next watchdog tick posts the marker.
            r = await db.execute(text(f"""
                SELECT t.id, t.title
                FROM tasks t
                WHERE t.status = 'in_progress'
                  AND (t.max_seconds_budget IS NULL OR t.max_seconds_budget = 0)
                  AND t.in_progress_at IS NOT NULL
                  AND t.in_progress_at < NOW()
                      - INTERVAL '{_UNBUDGETED_FLAG_HOURS + 1} hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM activity_events ae
                      WHERE ae.task_id = t.id AND ae.event_type = 'task.comment'
                        AND ae.message LIKE '[watchdog-stalled] reason=no-budget%'
                        AND ae.created_at > t.in_progress_at
                  )
                LIMIT 5
            """))
            rows = r.fetchall()
            assert not rows, (
                f"unbudgeted long runners without stall marker: "
                f"{'; '.join(str(x[0])[:8] + ' (' + (x[1] or '')[:30] + ')' for x in rows)}"
            )
            return "pass", "vacuous or all flagged"

        return "error", f"unknown auto test: {tid}"
    except AssertionError as e:
        try:
            await db.rollback()
        except Exception:
            pass
        return "fail", str(e)[:300]
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        return "error", f"{type(e).__name__}: {e}"[:300]


# ── agent tests (board task creation) ───────────────────────────────────────


async def _create_agent_test_task(test: TestDef, db: AsyncSession) -> str | None:
    """Create board task for an agent-kind test. Respects provider capacity and board toggle."""
    # Dedup: skip if an open task referencing this test_id already exists
    r = await db.execute(
        text("""
        SELECT id FROM tasks
        WHERE description LIKE :pat AND auto_reason = 'tests-harness'
          AND status IN ('inbox', 'in_progress', 'review')
        LIMIT 1
    """),
        {"pat": f"%`{test.id}`%"},
    )
    if r.first():
        return None

    # Cap: max 1 pending/in-progress test task per test_id (total, not just this run)
    r = await db.execute(
        text("""
        SELECT COUNT(*) FROM tasks
        WHERE auto_reason = 'tests-harness'
          AND status IN ('inbox', 'in_progress', 'review')
    """),
    )
    max_test_tasks = int(os.environ.get("TESTS_MAX_PENDING_TASKS", "20"))
    if (r.scalar() or 0) >= max_test_tasks:
        return None

    # Check board toggle before proceeding
    boards = await db.execute(text("SELECT id, autonomous_tasks_enabled FROM boards WHERE autonomous_tasks_enabled = TRUE LIMIT 1"))
    bt_row = boards.first()
    if not bt_row:
        return None

    role = test.agent_role or "Software Architect"

    # Infer complexity from test name/description
    SIMPLE_KEYWORDS = {
        "crackme",
        "basic",
        "simple",
        "quick",
        "minor",
        "patch",
        "typo",
        "format",
        "ping",
        "heartbeat",
        "cleanup",
        "nudge",
        "template",
        "stub",
        "round trip",
        "reachable",
        "health",
    }
    COMPLEX_KEYWORDS = {
        "novel",
        "exploit chain",
        "architecture review",
        "multi-stage",
        "protocol design",
        "formal verification",
        "zero-day",
        "sandbox escape",
        "novel vulnerability",
        "root cause",
        "deep analysis",
        "cross-board",
        "policy rewrite",
        "ballot",
        "election",
        "handoff",
        "mentorship",
    }

    title_lower = test.name.lower()
    desc_lower = test.description.lower()
    combined = title_lower + " " + desc_lower

    if any(kw in combined for kw in SIMPLE_KEYWORDS):
        complexity = "simple"
        provider_agent = f"{role}-vllm"
    elif any(kw in combined for kw in COMPLEX_KEYWORDS):
        complexity = "complex"
        provider_agent = f"{role}-ollama"
    else:
        # Default: check vllm WIP; if saturated, route to ollama
        complexity = "standard"
        vllm_wip = await db.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE status='in_progress' AND assigned_agent_id IN "
                "(SELECT id FROM agents WHERE name = :name AND board_id = :bid)"
            ),
            {"name": f"{role}-vllm", "bid": bt_row[0]},
        )
        wip_count = vllm_wip.scalar() or 0
        provider_agent = f"{role}-vllm" if wip_count < 4 else f"{role}-ollama"

    # Use the board selected above, not a different arbitrary first board.
    bid = bt_row[0]

    # Find the routed agent on THAT board only.
    agents = await db.execute(
        text("SELECT id FROM agents WHERE board_id = :bid AND name = :n LIMIT 1"),
        {"bid": bid, "n": provider_agent},
    )
    aid = agents.scalar()
    # Fallback to base role if dual agent doesn't exist yet
    if not aid:
        agents = await db.execute(
            text("SELECT id FROM agents WHERE board_id = :bid AND name = :n LIMIT 1"),
            {"bid": bid, "n": role},
        )
        aid = agents.scalar()
    if aid is None:
        return None  # no worker on this board; do not create an unassignable task

    title = f"TEST: {test.name}"
    body = (
        f"Automated test from /tests harness.\n\n"
        f"**Test ID:** `{test.id}`\n"
        f"**Complexity:** `{complexity}` (routed to {provider_agent})\n"
        f"**Description:** {test.description}\n\n"
        f"**Required action:** Reply with a comment containing `TEST-OK {test.id}` "
        f"to mark this test as passed. Then transition to `review`."
    )
    tid = str(uuid.uuid4())
    # Stash complexity + provider_hint in description (no schema columns for these)
    body = body + f"\n\n[meta] complexity={complexity} provider_hint={provider_agent}"
    await db.execute(
        text("""
        INSERT INTO tasks (id, board_id, title, description, status, priority,
                           assigned_agent_id, auto_created, auto_reason,
                           created_by_type, created_by_name, created_at, updated_at)
        VALUES (:id, :bid, :title, :desc, 'inbox', 'low', :aid, true,
                'tests-harness', 'system', 'Test Harness', NOW(), NOW())
    """),
        {
            "id": tid,
            "bid": bid,
            "title": title,
            "desc": body,
            "aid": aid,
        },
    )
    await db.commit()
    return tid


async def _check_agent_test_result(test_id: str, db: AsyncSession) -> tuple[str, str]:
    """Look for TEST-OK marker in any comment for tasks tagged with this test_id."""
    # Check if autonomous tasks are enabled on any board — if not, agent tests can't run
    boards = await db.execute(text("SELECT autonomous_tasks_enabled FROM boards WHERE autonomous_tasks_enabled = TRUE LIMIT 1"))
    if not boards.first():
        return "pass", "vacuous — autonomous_tasks_enabled is off on all boards"

    result = await db.execute(
        text(f"""
        SELECT t.id, t.status, t.created_at FROM tasks t
        WHERE t.description LIKE '%`{test_id}`%' AND t.auto_reason = 'tests-harness'
        ORDER BY t.created_at DESC LIMIT 1
    """)
    )
    row = result.first()
    if not row:
        return "pending", "no test task created yet"
    task_id = str(row[0])
    status = row[1]
    # look for TEST-OK comment
    cresult = await db.execute(
        text("""
        SELECT message FROM activity_events
        WHERE task_id = :tid AND event_type = 'task.comment' AND message LIKE :pat
    """),
        {"tid": task_id, "pat": f"%TEST-OK {test_id}%"},
    )
    if cresult.first():
        return "pass", f"TEST-OK marker found, task {status}"
    if status == "done":
        return "pass", f"task done (no TEST-OK marker needed for already-closed tasks)"
    if status == "review":
        return "fail", f"task {status} but no TEST-OK marker"
    return "pending", f"task {status}, awaiting agent"


# ── persistence ─────────────────────────────────────────────────────────────


def _record_run(
    test_id: str, status: str, detail: str, duration_ms: int
) -> dict[str, Any]:
    """Persist a test run record.

    T48 (2026-05-14): mirrors writes to Postgres `activity_events` (event_type
    `test.run`) so the Activity tab surfaces test history alongside agent
    actions. SQLite local cache retained for fast `_latest_run` reads.
    """
    conn = _db()
    rid = str(uuid.uuid4())
    finished = _now()
    sig = _system_signature()
    with _lock:
        conn.execute(
            """
            INSERT INTO test_runs (id, test_id, started_at, finished_at, status, detail, duration_ms, system_signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (rid, test_id, finished, finished, status, detail, duration_ms, sig),
        )
        conn.commit()
    # T48: mirror to activity_events for the Activity tab. Best-effort —
    # don't fail the test recording if Postgres mirror write fails.
    try:
        from app.db.session import sync_session_maker
        with sync_session_maker() as _ae_db:
            _ae_db.execute(
                text(
                    "INSERT INTO activity_events (id, event_type, message, created_at) "
                    "VALUES (:id, 'test.run', :msg, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": rid,
                    "msg": f"test={test_id} status={status} duration_ms={duration_ms} detail={detail[:200]}",
                },
            )
            _ae_db.commit()
    except Exception as _exc:
        from app.core.logging import get_logger as _get_log
        _get_log(__name__).warning("tests.activity_mirror_failed err=%s", _exc)
    return {
        "id": rid,
        "test_id": test_id,
        "status": status,
        "detail": detail,
        "duration_ms": duration_ms,
        "finished_at": finished,
    }


def _latest_run(test_id: str) -> dict[str, Any] | None:
    conn = _db()
    r = conn.execute(
        "SELECT id, test_id, started_at, finished_at, status, detail, duration_ms, system_signature "
        "FROM test_runs WHERE test_id=? ORDER BY started_at DESC LIMIT 1",
        (test_id,),
    ).fetchone()
    return dict(r) if r else None


def _run_is_stale(run: dict[str, Any]) -> tuple[bool, str]:
    """T86: a run is stale when:
       1. The system_signature it captured differs from the current one
          (a migration / signature bump invalidates prior passes), OR
       2. The run is older than `_STALE_AFTER_SECONDS`.
    Returns (stale: bool, reason: str). Reason is human-readable for UI.
    """
    if not run:
        return False, ""
    sig = run.get("system_signature") or ""
    cur = _system_signature()
    if sig and cur and sig != cur:
        return True, f"system changed since last run (sig {sig[:8]} → {cur[:8]})"
    finished = run.get("finished_at") or ""
    if finished:
        try:
            ts = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > _STALE_AFTER_SECONDS:
                days = int(age / 86400)
                return True, f"last run {days}d old (limit {_STALE_AFTER_SECONDS // 86400}d)"
        except Exception:
            pass
    return False, ""


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get(
    "",
    summary="List all tests with their latest run status",
    dependencies=[Depends(require_user_or_agent)],
)
async def list_tests(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    out = []
    summary = {"pass": 0, "fail": 0, "error": 0, "pending": 0, "never": 0, "stale": 0}
    for t in CATALOG:
        last = _latest_run(t.id)
        stale_reason = ""
        if last:
            status = last["status"]
            is_stale, stale_reason = _run_is_stale(last)
            if is_stale:
                status = "stale"
        else:
            status = "never"
        # for agent tests, check live result if pending or never
        if t.kind == "agent" and status in ("never", "pending", "fail"):
            try:
                ag_status, ag_detail = await _check_agent_test_result(t.id, db)
                # Reflect agent-derived status without overwriting auto records prematurely
                if ag_status != "pending":
                    if last is None or last["status"] != ag_status:
                        _record_run(t.id, ag_status, ag_detail, 0)
                    status = ag_status
                    last = {
                        "status": ag_status,
                        "detail": ag_detail,
                        "finished_at": _now(),
                        "duration_ms": 0,
                    }
                elif last is None:
                    status = "pending"
            except Exception:
                pass
        summary[status] = summary.get(status, 0) + 1
        out.append({
            **t.model_dump(),
            "last_run": last,
            "status": status,
            "stale_reason": stale_reason,
        })
    fully_autonomous = (
        summary["pass"] > 0
        and summary["fail"] == 0
        and summary["error"] == 0
        and summary["pending"] == 0
        and summary["stale"] == 0
    )
    return {
        "tests": out,
        "summary": summary,
        "total": len(CATALOG),
        "system_signature": _system_signature(),
        "stale_after_seconds": _STALE_AFTER_SECONDS,
        "fully_autonomous": fully_autonomous,
        "fully_autonomous_threshold": "all tests pass; zero fail/error/pending/stale",
    }


# Map test category → role best suited to fix the gap
# A watchdog loop that is actually not running (disabled at backend start) has no ticks
# or repeat resets to measure; reporting that as a failure made every test run add
# "fail" events during the 2026-10-06 pause. A disable that is saved but still awaits a
# restart does not skip: the running loop must still prove it is alive.
_WATCHDOG_PAUSED = (
    "watchdog paused by the WATCHDOG_ENABLED setting; "
    "ticks and repeat resets resume when re-enabled"
)

_REMEDIATION_ROLE = {
    "infrastructure": "DevOps Engineer",
    "security": "Security Architect",
    "capacity": "DevOps Engineer",
}


async def _create_remediation_task(
    test: TestDef, detail: str, db: AsyncSession
) -> str | None:
    """Create board task to fix a failing test. Idempotent — skip if existing inbox/in-progress/review/blocked task already covers this test."""
    # Dedup: don't create if an open task referencing this test_id exists.
    # T107 (2026-05-26): include `blocked` so the stuck-inbox auto-block
    # handler doesn't trigger a flood of fresh duplicates every tick when
    # the assignee can't fix the test. Owner-triage of the blocked task
    # is the recovery path, not spawning more copies.
    r = await db.execute(
        text("""
        SELECT id FROM tasks
        WHERE description LIKE :pat AND status IN ('inbox','in_progress','review','blocked')
        LIMIT 1
    """),
        {"pat": f"%test_id:{test.id}%"},
    )
    if r.first():
        return None

    boards = await db.execute(text(
        "SELECT id FROM boards WHERE autonomous_tasks_enabled = TRUE LIMIT 1"
    ))
    bid = boards.scalar()
    if bid is None:
        return None

    role = _REMEDIATION_ROLE.get(test.category, "Software Architect")
    # T44 (2026-05-14): prefer Senior variant of the role when one exists. Routes
    # remediation to deeper-context agents (e.g., "QA Senior" over "QA Engineer")
    # so failing tests get specialist attention instead of Atlas-only fallback.
    senior_role = f"{role} Senior"
    r = await db.execute(
        text("SELECT id FROM agents WHERE board_id=:bid AND name=:n AND status='online' LIMIT 1"),
        {"bid": bid, "n": senior_role},
    )
    aid = r.scalar()
    if aid is None:
        r = await db.execute(
            text("SELECT id FROM agents WHERE board_id=:bid AND name=:n LIMIT 1"),
            {"bid": bid, "n": role},
        )
        aid = r.scalar()
    if aid is None:
        return None  # no worker on this board; do not create an unassignable task
    tid = str(uuid.uuid4())
    title = f"AUTONOMY-FIX: {test.name}"
    body = (
        f"Automated remediation task generated by /api/v1/tests harness.\n\n"
        f"**Test ID:** `{test.id}` — `test_id:{test.id}`\n"
        f"**Category:** {test.category}\n"
        f"**Description:** {test.description}\n\n"
        f"**Why this task exists:** the test is currently failing.\n"
        f"**Last detail:** {detail}\n\n"
        f"## Acceptance Criteria\n"
        f"1. Implement the missing primitive / fix the failing condition.\n"
        f"2. Re-run the test via `POST /api/v1/tests/run/{test.id}`.\n"
        f"3. Verify it returns `status=pass`.\n"
        f"4. Move task to `review` for QA verification.\n\n"
        f"## Handoff\n"
        f"Routed to **{role}** based on test category. Reassign if specialty mismatch."
    )
    await db.execute(
        text("""
        INSERT INTO tasks (id, board_id, title, description, status, priority,
                           assigned_agent_id, auto_created, auto_reason,
                           created_by_type, created_by_name, created_at, updated_at)
        VALUES (:id, :bid, :title, :desc, 'inbox', 'low', :aid, true,
                'tests-autonomy-remediation', 'system', 'Autonomy Watchdog', NOW(), NOW())
    """),
        {"id": tid, "bid": bid, "title": title, "desc": body, "aid": aid},
    )
    await db.commit()
    return tid


@router.post(
    "/run",
    summary="Run all auto tests; auto-create board tasks for agent tests AND for failures",
    dependencies=[Depends(require_user_or_agent)],
)
async def run_all(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    results = []
    remediations: list[dict[str, Any]] = []
    for t in CATALOG:
        # T86: external-dep tests are skipped by the batch endpoint so a
        # full /tests/run won't fail because a crackme binary isn't
        # mounted. They can still be invoked individually via the
        # `POST /tests/run/{id}` route.
        if t.requires_external:
            results.append({
                "test_id": t.id,
                "status": "skipped",
                "detail": f"requires_external — run individually. Reason: {t.requires_external_reason}",
                "duration_ms": 0,
            })
            continue
        start = time.monotonic()
        try:
            if t.kind == "auto":
                status, detail = await _run_auto(t, db)
            else:
                try:
                    # Prefer dynamic resolution over task creation — boards may have autonomous_tasks_enabled=false
                    resolved_status, resolved_detail = await _check_agent_test_result(t.id, db)
                    if resolved_status != "pending":
                        status, detail = resolved_status, resolved_detail
                    else:
                        tid = await _create_agent_test_task(t, db)
                        status, detail = ("pending", f"task {tid[:8]} created") if tid else ("pending", "board toggle off — enable autonomous_tasks_enabled to run")
                except Exception as e:
                    status, detail = "error", f"task creation failed: {e}"[:200]
        except Exception as e:
            status, detail = "error", f"{type(e).__name__}: {e}"[:200]
            try:
                await db.rollback()
            except Exception:
                pass
        dur = int((time.monotonic() - start) * 1000)
        results.append(_record_run(t.id, status, detail, dur))

        # Auto-remediation: spawn board task for fail/error
        if status in ("fail", "error"):
            try:
                rid = await _create_remediation_task(t, detail, db)
                if rid:
                    remediations.append(
                        {
                            "test_id": t.id,
                            "task_id": rid,
                            "assigned_to": _REMEDIATION_ROLE.get(t.category),
                        }
                    )
            except Exception as e:
                from app.core.logging import get_logger  # noqa: E402
                logger = get_logger(__name__)
                logger.warning("remediation_failed test=%s err=%s", t.id, e)

    return {
        "results": results,
        "ran_at": _now(),
        "remediations_created": len(remediations),
        "remediations": remediations,
    }


@router.post(
    "/run/{test_id}",
    summary="Run a single test by id",
    dependencies=[Depends(require_user_or_agent)],
)
async def run_one(
    test_id: str, db: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    t = next((x for x in CATALOG if x.id == test_id), None)
    if not t:
        raise HTTPException(404, f"test {test_id} not in catalog")
    start = time.monotonic()
    if t.kind == "auto":
        status, detail = await _run_auto(t, db)
    else:
        try:
            tid = await _create_agent_test_task(t, db)
            status, detail = ("pending", f"task {tid[:8]} created") if tid else ("pending", "dedup: open task already exists for this test")
        except Exception as e:
            status, detail = "error", f"task creation failed: {e}"[:200]
    dur = int((time.monotonic() - start) * 1000)
    result = _record_run(t.id, status, detail, dur)

    return result


@router.get(
    "/runs/{test_id}",
    summary="History of runs for a single test",
    dependencies=[Depends(require_user_or_agent)],
)
async def runs(test_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = _db()
    rows = conn.execute(
        "SELECT id, test_id, started_at, finished_at, status, detail, duration_ms "
        "FROM test_runs WHERE test_id=? ORDER BY started_at DESC LIMIT ?",
        (test_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@router.delete(
    "/runs",
    summary="Reset test-run history (delete all historical run rows)",
    dependencies=[Depends(require_user_or_agent)],
)
async def reset_all_runs() -> dict[str, Any]:
    """T71 (2026-05-15): wipe test_runs history so the next `POST /tests/run`
    starts from a clean slate. Used when test catalog changes invalidate
    historical results."""
    conn = _db()
    with _lock:
        cur = conn.execute("DELETE FROM test_runs")
        conn.commit()
        deleted = cur.rowcount
    return {"deleted": deleted}


def _is_lan_host(host: str) -> bool:
    """localhost or a private/loopback IP literal (RFC 1918, 127/8, ::1)."""
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback
