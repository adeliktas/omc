"""Runtime-editable settings store.

T103 v35 (2026-05-20): make every operational knob owner-editable from
the UI without restarting the backend. Each registered knob has:
  - key (matches the env var name)
  - default (fallback if owner hasn't overridden)
  - type (int | float | str)
  - description (shown in UI)
  - section (groups knobs per UI tab)

Lookup order at read-time:
  1. sidecar override file `~/.openclaw/runtime_settings.json`
  2. `os.environ[key]`
  3. registered default

Loops/services that want dynamic values must call `get_setting(key)` at
each tick instead of caching the module-load value. The watchdog +
benchmark loops were converted as part of T103 v35.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_SIDECAR_PATH = Path(
    os.environ.get(
        "RUNTIME_SETTINGS_SIDECAR",
        os.path.join(
            os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"),
            "runtime_settings.json",
        ),
    ),
)


def _coerce(value: Any, typ: str) -> Any:
    if value is None:
        return None
    try:
        if typ == "int":
            number = float(value)
            return int(number) if number.is_integer() else None
        if typ == "float":
            return float(value)
        if typ == "bool":
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
            return None
        return str(value)
    except Exception:
        return None


# Owner-facing knob registry. Add new entries here so the UI surfaces
# them automatically.
SETTINGS: list[dict[str, Any]] = [
    # --- Benchmark / IQ ---
    {
        "key": "BENCHMARK_ENABLED",
        "type": "bool",
        "default": False,
        "section": "benchmark",
        "description": "Master switch for the benchmark loop. Read live each cycle. Off by default since 2026-08-24: each cycle posts a real 256-token generation per (online provider × curated model) and steals GPU decode from live agents.",
    },
    {
        "key": "WATCHDOG_ENABLED",
        "type": "bool",
        "default": True,
        "section": "watchdog",
        "description": "Master switch for the watchdog loop — nudging, escalation, routing, auto-recovery. Turning this off stops the system driving itself.",
    },
    {
        "key": "BENCHMARK_INTERVAL_SECONDS",
        "type": "int",
        "default": 14400,
        "section": "benchmark",
        "description": "How often the benchmark loop probes every (provider, model). 4h default after T103 v34.",
    },
    {
        "key": "BENCHMARK_FAILURE_THRESHOLD",
        "type": "int",
        "default": 3,
        "section": "benchmark",
        "description": "Consecutive bench failures before a model gets auto-disabled in the sidecar.",
    },
    {
        "key": "BENCHMARK_FAILURE_WINDOW",
        "type": "float",
        "default": 300.0,
        "section": "benchmark",
        "description": "Rolling-window seconds for failure counting.",
    },
    # --- Watchdog cadence ---
    {
        "key": "WATCHDOG_INTERVAL",
        "type": "int",
        "default": 60,
        "section": "watchdog",
        "description": "Active watchdog tick interval (seconds). Idle interval used when no work.",
    },
    {
        "key": "WATCHDOG_IDLE_INTERVAL",
        "type": "int",
        "default": 180,
        "section": "watchdog",
        "description": "Idle watchdog tick interval (seconds).",
    },
    {
        "key": "WATCHDOG_AUTONOMOUS_IDLE_MINUTES",
        "type": "int",
        "default": 15,
        "section": "watchdog",
        "description": "Minutes of board-idle before auto-spawning self-improvement tasks. 0 disables.",
    },
    {
        "key": "WATCHDOG_AGENT_SESSION_IDLE_SECONDS",
        "type": "int",
        "default": 1800,
        "section": "watchdog",
        "description": "Idle-session abort threshold. Lower = more aggressive runaway cleanup.",
    },
    {
        "key": "WATCHDOG_INBOX_VOLUNTEER_GRACE_SECONDS",
        "type": "int",
        "default": 300,
        "section": "watchdog",
        "description": "Wait this long before auto-routing an unclaimed inbox task to the keyword pool.",
    },
    {
        "key": "WATCHDOG_STALE_INPROGRESS_SECONDS",
        "type": "int",
        "default": 1200,
        "section": "watchdog",
        "description": "Time before in_progress task is considered stale and nudged.",
    },
    {
        "key": "WATCHDOG_STALE_REVIEW_SECONDS",
        "type": "int",
        "default": 1200,
        "section": "watchdog",
        "description": "Time before review task is considered stale.",
    },
    {
        "key": "WATCHDOG_MAX_NUDGES",
        "type": "int",
        "default": 1,
        "section": "watchdog",
        "description": "Nudges before escalating to Owner Review.",
    },
    {
        "key": "WATCHDOG_AUTO_CLOSE_CONFIDENCE",
        "type": "int",
        "default": 80,
        "section": "watchdog",
        "description": "Confidence threshold (0-100) for auto-closing review tasks.",
    },
    {
        "key": "WATCHDOG_PROVIDER_HEALTH_INTERVAL_SECONDS",
        "type": "int",
        "default": 60,
        "section": "watchdog",
        "description": "Provider health-probe cadence.",
    },
    {
        "key": "WATCHDOG_OMNIROUTE_REVERSE_SYNC_FAILSAFE_SECONDS",
        "type": "int",
        "default": 600,
        "section": "watchdog",
        "description": "OR→MC reverse-sync poll failsafe. Event-driven is primary; this is the safety net.",
    },
    {
        "key": "WATCHDOG_HEARTBEAT_RATE_WINDOW_SECONDS",
        "type": "int",
        "default": 300,
        "section": "watchdog",
        "description": "Rolling window for detecting heartbeat-spam agents.",
    },
    {
        "key": "WATCHDOG_HEARTBEAT_RATE_MAX_CALLS",
        "type": "int",
        "default": 12,
        "section": "watchdog",
        "description": "Heartbeat-call ceiling within the rate window before the session is aborted.",
    },
    # --- Provider stats ---
    {
        "key": "PROVIDER_STATS_WINDOW_SECONDS",
        "type": "int",
        "default": 900,
        "section": "providers",
        "description": "Live-usage rolling window for the /providers stats bar.",
    },
    # --- Traffic ETL ---
    {
        "key": "TRAFFIC_ETL_INTERVAL_SECONDS",
        "type": "float",
        # 30.0, not the 10.0 this said until 2026-09-22 — the consumer's own
        # default was 30 and the registry default is now what actually applies.
        "default": 30.0,
        "section": "traffic",
        "description": "How often MC polls OmniRoute call-logs into the traffic DB.",
    },
    # --- Task creation (independent switches; no coding executor is enabled here) ---
    {
        "key": "TASK_COMPLEXITY_LLM_ENABLED",
        "type": "bool",
        "default": False,
        "section": "tasks",
        "description": "Use a model to assess new/existing task difficulty. Off: use the existing keyword + historical fallback without a model request. Enabling this may incur provider charges; it is not a spend cap.",
    },
    {
        "key": "TASK_AUTO_SPLIT_ENABLED",
        "type": "bool",
        "default": False,
        "section": "tasks",
        "description": "Split L4/L5 gateway tasks into three children on creation. Off: keep one task; does not disable explicit manual splitting or agent execution.",
    },
    # --- Complexity assessor ---
    {
        "key": "COMPLEXITY_ASSESSOR_MAX_TOKENS",
        "type": "int",
        "default": 120,
        "section": "llm",
        "description": "Max output tokens for the L1-L5 complexity classifier.",
    },
    # --- T105 Phase 2 (2026-05-25): tier-based model routing ---
    # Comma-separated fallback chains. First entry = primary; subsequent
    # entries used when primary is offline / over quota.
    {
        "key": "TIER_PLANNER_MODELS",
        "type": "str",
        "default": "ollama/deepseek-v4-pro:cloud,ollama/kimi-k2.6:cloud,ollama/glm-5.1:cloud,omniroute-auto",
        "section": "llm",
        "description": "Smart-tier fallback chain for `planner` agents. First-match-online wins. Cost OK — owner approved no per-task cap for planning.",
    },
    {
        "key": "TIER_VERIFIER_MODELS",
        "type": "str",
        "default": "ollama/deepseek-v4-pro:cloud,ollama/kimi-k2.6:cloud,ollama/glm-5.1:cloud,omniroute-auto",
        "section": "llm",
        "description": "Smart-tier fallback chain for `verifier` agents. Usually same as planner; verification needs equal-or-stronger model.",
    },
    {
        "key": "TIER_EXECUTOR_MODELS",
        "type": "str",
        "default": "omniroute-auto",
        "section": "llm",
        "description": "Cheap-tier fallback chain for `executor` agents. LAN-local primary; planner provides the plan, executor implements.",
    },
    {
        "key": "TIER_DEGRADED_FALLBACK",
        "type": "str",
        # 2026-07-23: MUST be bare `omniroute-auto`, NOT `omniroute/omniroute-auto`.
        # This value is passed straight to `call_model` → OmniRoute runtime, which
        # parses the `omniroute/` prefix as a (non-existent) provider named
        # "omniroute" → 404 "No active credentials for provider: omniroute". Bare
        # `omniroute-auto` hits OmniRoute's auto-combo → vllm. Verified working.
        "default": "omniroute-auto",
        "section": "llm",
        "description": "Last-resort model when every tier-specific entry is offline. Bare omniroute-auto (auto-combo → vllm) so the system never blocks.",
    },
    {
        # Global default TOOLS for ALL agents → written to openclaw.json
        # `agents.defaults.tools` by provisioning (`_agent_defaults_patch`).
        # JSON string: {"profile":str, "allow":[str], "deny":[str]}. Per-agent
        # `tool_config` overrides layer on top.
        "key": "AGENT_DEFAULT_TOOLS",
        "type": "str",
        "default": "",
        "section": "agents",
        "description": "Default tool policy for all agents (JSON {profile,allow,deny}). Written to openclaw.json agents.defaults.tools. Empty = leave gateway defaults.",
    },
    {
        # Global default SKILLS for ALL agents → openclaw.json
        # `agents.defaults.skills`. JSON string: ["skill-id", ...].
        "key": "AGENT_DEFAULT_SKILLS",
        "type": "str",
        "default": "",
        "section": "agents",
        "description": "Default skills enabled for all agents (JSON [ids]). Written to openclaw.json agents.defaults.skills. Empty = leave gateway defaults.",
    },
    # --- /pipeline hazards board (2026-09-22) ---
    # These were hardcoded constants in app/api/pipeline.py. Defaults are the
    # values that were compiled in, so registering them changes nothing.
    # Deliberately NOT the same knobs as WATCHDOG_STALE_*: the watchdog nudges
    # an agent cheaply at 20 min, while this board is a human attention queue
    # on a slower clock. Two clocks, two purposes — both now tunable.
    {
        "key": "PIPELINE_STALE_INPROGRESS_SECONDS",
        "type": "int",
        "default": 7200,
        "section": "pipeline",
        "description": "Age at which /pipeline flags an in_progress task as stale for the operator. The watchdog nudges far earlier (WATCHDOG_STALE_INPROGRESS_SECONDS); this is the slower human-escalation clock.",
    },
    {
        "key": "PIPELINE_STALE_REVIEW_SECONDS",
        "type": "int",
        "default": 14400,
        "section": "pipeline",
        "description": "Age at which /pipeline flags a review task as stale for the operator.",
    },
    {
        "key": "PIPELINE_ATLAS_OFFLINE_SECONDS",
        "type": "int",
        "default": 3600,
        "section": "pipeline",
        "description": "Time without an LLM call before /pipeline treats the acting lead as offline and names a successor.",
    },
    {
        "key": "PIPELINE_WIP_LIMIT_LEAD",
        "type": "int",
        "default": 5,
        "section": "pipeline",
        "description": "Concurrent in_progress tasks allowed for the lead before /pipeline reports a WIP violation.",
    },
    {
        "key": "PIPELINE_WIP_LIMIT_WORKER",
        "type": "int",
        "default": 2,
        "section": "pipeline",
        "description": "WIP ceiling for a worker agent.",
    },
    {
        "key": "PIPELINE_WIP_LIMIT_QA",
        "type": "int",
        "default": 3,
        "section": "pipeline",
        "description": "WIP ceiling for a QA agent.",
    },
    # --- P3 org pipeline (2026-09-01, GRAND-PLAN) — all default OFF, additive ---
    {
        "key": "WATCHDOG_ROUTING_LLM_ENABLED",
        "type": "bool",
        "default": False,
        "section": "pipeline",
        "description": "P3(b): consult the LLM role-classifier to refine keyword routing. Keyword match + fallback always run first; LLM only overrides when it returns a candidate. Live-flippable.",
    },
    {
        "key": "WATCHDOG_UNDERSTAND_ENABLED",
        "type": "bool",
        "default": False,
        "section": "pipeline",
        "description": "P3(a): run an explicit UNDERSTAND stage (one tool-less LLM summary posted as an [understand] marker) on inbox tasks. Autonomous boards only. Additive.",
    },
    {
        "key": "WATCHDOG_RESEARCH_ENABLED",
        "type": "bool",
        "default": False,
        "section": "pipeline",
        "description": "P3(a): run an explicit RESEARCH stage (tool-less LLM summary posted as a [research] marker, after [understand]) on inbox tasks. Autonomous boards only. Additive.",
    },
    {
        "key": "WATCHDOG_TOOL_ROUTE_ENABLED",
        "type": "bool",
        "default": False,
        "section": "pipeline",
        "description": "P3(c): route tool-requiring tasks (ghidra/binary/compile/deploy/...) to a tool-capable gateway agent instead of dead-ending them out of the text-only PEV loop. Autonomous boards only.",
    },
]

_BY_KEY: dict[str, dict[str, Any]] = {s["key"]: s for s in SETTINGS}

# Knobs whose consumer calls `get_setting()` at each use, so an owner edit
# takes effect on the next tick. Everything else is resolved once, at module
# import, and needs a backend restart — the UI says so per-knob rather than
# claiming (as it did until 2026-09-22) that every edit is live.
_LIVE_KEYS: frozenset[str] = frozenset(
    {
        "BENCHMARK_ENABLED",
        "BENCHMARK_INTERVAL_SECONDS",
        "BENCHMARK_FAILURE_THRESHOLD",
        "BENCHMARK_FAILURE_WINDOW",
        "TIER_PLANNER_MODELS",
        "TIER_VERIFIER_MODELS",
        "TIER_EXECUTOR_MODELS",
        "TIER_DEGRADED_FALLBACK",
        "AGENT_DEFAULT_TOOLS",
        "AGENT_DEFAULT_SKILLS",
        "WATCHDOG_ROUTING_LLM_ENABLED",
        "WATCHDOG_UNDERSTAND_ENABLED",
        "WATCHDOG_RESEARCH_ENABLED",
        "WATCHDOG_TOOL_ROUTE_ENABLED",
        "TASK_COMPLEXITY_LLM_ENABLED",
        "TASK_AUTO_SPLIT_ENABLED",
        "WATCHDOG_INTERVAL",
        "WATCHDOG_IDLE_INTERVAL",
        # Read per request by the /pipeline handler.
        "PIPELINE_STALE_INPROGRESS_SECONDS",
        "PIPELINE_STALE_REVIEW_SECONDS",
        "PIPELINE_ATLAS_OFFLINE_SECONDS",
        "PIPELINE_WIP_LIMIT_LEAD",
        "PIPELINE_WIP_LIMIT_WORKER",
        "PIPELINE_WIP_LIMIT_QA",
    },
)


def _load_sidecar(strict: bool = False) -> dict[str, Any]:
    """Read the overrides. Readers get {} on a read error; strict callers (audit
    snapshots, read-modify-write) raise instead of treating it as "no overrides",
    so an unreadable file is never overwritten with only the new key. Only a
    missing file is "no overrides" (exists() is False for e.g. a symlink loop)."""
    try:
        data = json.loads(_SIDECAR_PATH.read_text())
        if not isinstance(data, dict):
            raise ValueError("runtime settings sidecar must be an object")
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        if strict:
            raise
        logger.warning("runtime_settings.sidecar_read_err err=%s", exc)
        return {}


def _save_sidecar(data: dict[str, Any]) -> None:
    """In-place write — preserves inode/owner so host-mounted container
    permissions stay sane (same rationale as combo_sync)."""
    try:
        _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2)
        if _SIDECAR_PATH.exists():
            with open(_SIDECAR_PATH, "r+") as fh:
                fh.seek(0)
                fh.write(payload)
                fh.truncate()
                fh.flush()
                os.fsync(fh.fileno())
        else:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", dir=str(_SIDECAR_PATH.parent), delete=False,
            )
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, _SIDECAR_PATH)
    except Exception as exc:
        logger.error("runtime_settings.sidecar_write_err err=%s", exc)
        raise


def get_setting(key: str, default: Any | None = None) -> Any:
    """Resolve a knob: sidecar override → env var → registered default →
    caller-supplied default. Returns the typed value.
    """
    spec = _BY_KEY.get(key)
    typ = spec["type"] if spec else "str"

    sidecar = _load_sidecar()
    if key in sidecar:
        v = _coerce(sidecar[key], typ)
        if v is not None:
            return v

    env_v = os.environ.get(key)
    if env_v is not None:
        v = _coerce(env_v, typ)
        if v is not None:
            return v

    if spec is not None:
        return spec["default"]
    return default


def list_settings(strict: bool = False) -> list[dict[str, Any]]:
    """Return each knob with its current resolved value + source."""
    sidecar = _load_sidecar(strict=True) if strict else _load_sidecar()
    out: list[dict[str, Any]] = []
    for spec in SETTINGS:
        key = spec["key"]
        override = _coerce(sidecar.get(key), spec["type"])
        env_value = _coerce(os.environ.get(key), spec["type"])
        if override is not None:
            source, value = "override", override
        elif env_value is not None:
            source, value = "env", env_value
        else:
            source, value = "default", spec["default"]
        out.append(
            {
                **spec,
                "value": value,
                "source": source,
                "applies": "live" if key in _LIVE_KEYS else "restart",
            },
        )
    return out


def coerce_setting(key: str, value: Any) -> Any:
    """The value `update_setting` would store, or None when it is invalid."""
    spec = _BY_KEY.get(key)
    return _coerce(value, spec["type"]) if spec else None


def update_setting(key: str, value: Any) -> dict[str, Any] | None:
    """Persist a sidecar override. Pass `None` to clear (revert to env / default)."""
    spec = _BY_KEY.get(key)
    if not spec:
        return None
    sidecar = _load_sidecar(strict=True)
    if value is None:
        sidecar.pop(key, None)
    else:
        coerced = _coerce(value, spec["type"])
        if coerced is None:
            return None
        sidecar[key] = coerced
    _save_sidecar(sidecar)
    return next((s for s in list_settings() if s["key"] == key), None)
