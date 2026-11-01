"""T110 (2026-09-15) — Model Ranking Board: level/rank/budget/pricing metadata
on top of the mc_sidecar, plus usage-based eligibility computation.

Ranking model (owner spec):
- Level 1..5, 1 = frontier/mastermind, 5 = cheapest worker.
  Same level = equivalents (side-by-side boxes on the board).
- rank: order within a level.
- cost_class: local (own hw, preferred) < free < credits.
- budget.type: local | percent | credits.
    percent  -> windows [{seconds, limit_tokens}] (relative session limits)
    credits  -> windows [{seconds, limit_credits}] + pricing {in,out}_per_mtok
- Eligibility = provider online AND not sidecar-disabled AND every window
  under its limit. Usage is aggregated from the traffic ETL `flows` sqlite
  (already captured per model every 10s).

No new DB table (plan-model-ranking-2026-09-15): everything lives in the
openclaw.mc.json sidecar, which model_router._merge already folds into every
routing decision.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.services import mc_sidecar

logger = get_logger(__name__)

# Level 1 = frontier. Complexity L5 needs level <= 1, L1 trivial -> level <= 5.
_MAX_LEVEL_FOR_COMPLEXITY: dict[int, int] = {5: 1, 4: 2, 3: 3, 2: 4, 1: 5}

_COST_CLASS_ORDER: dict[str, int] = {"local": 0, "free": 1, "credits": 2}

_MAX_LEVEL = 5

_AGENT_HARNESS_PROVIDERS = frozenset(
    {
        "claude-code",
        "ollama-cloud-sub",
        "deepseek-sub",
        "pi-neuralwatt",
        "omniroute-codex",  # Pi through OmniRoute's codex account pool (2026-10-07)
        "metis",  # Pi -> an on-prem vLLM Qwen3.8 (owner 2026-10-24)
        "strata",  # Pi -> llama.cpp on the owner's strata GPU (owner 2026-10-26: drafts only for now)
    }
)


def _is_agent_harness_provider(provider: str) -> bool:
    return provider in _AGENT_HARNESS_PROVIDERS or provider.startswith("openai-codex-")


_AGENT_HARNESS_SANCTION = (
    "Sanctioned agent harness: executes board tasks with local coding-agent tools. "
    "Not routed as a model endpoint behind OmniRoute "
    "(model_router._NON_ROUTABLE_PROVIDERS)."
)

# (level, rank within level, provider priority). Claude is normally first;
# temporary quota windows make GPT the effective first choice automatically.
# 2026-10-05: owner-authorized acceptance trio added (gpt-6.1-sol, opus-5-5,
# full glm-5.3). New entries only fill absent keys, so existing manual
# disabled flags, availability holds and observed usage are never overwritten,
# and a same-account hold recorded before the seed ran stays in force.
# 2026-10-06: owner directive "always use the latest versions": models
# superseded on the same account (opus-5, opus-4-8, sonnet, codex-2 gpt-5.6-*,
# glm-5.2, glm-5.3-flash) are no longer seeded, and the cloud policy no longer
# re-enables them. Their existing rows keep usage and holds; disable them there.
_AGENT_HARNESS_MODELS: dict[tuple[str, str], tuple[int, int, int]] = {
    ("openai-codex-1", "gpt-5.6-sol"): (1, 1, 1),
    # 2026-10-07: same model as openai-codex-2, served by OmniRoute's three codex accounts.
    ("omniroute-codex", "gpt-6.1-sol"): (1, 2, 1),
    ("ollama-cloud-sub", "glm-5.3"): (1, 4, 2),
    ("openai-codex-2", "gpt-6.1-sol"): (1, 5, 1),
    ("claude-code", "opus-5-5"): (1, 6, 0),
    ("pi-neuralwatt", "glm-5.3"): (1, 7, 4),
    ("openai-codex-1", "gpt-5.6-terra"): (2, 1, 1),
    ("ollama-cloud-sub", "kimi-k3"): (2, 3, 2),
    ("ollama-cloud-sub", "deepseek-v4-pro:cloud"): (3, 2, 2),
    ("deepseek-sub", "deepseek-v4-pro"): (3, 3, 3),
}

_CLOUD_POLICY_REVISION = 1

# ── Usage aggregation (flows sqlite) ────────────────────────────────────────

_FLOWS_DB = Path(os.environ.get("FLOWS_DB_PATH", "/traffic-data/llm_flows.db"))


def _flow_model_names(provider: str, model_name: str) -> list[str]:
    """Names a flow row can carry for this model: bare + <orprefix>/<name>."""
    # The ETL records the OmniRoute-requested model; prefixes vary per
    # upstream slug. We cannot know the slug here, so match by suffix.
    return [model_name]


def _usage_since(model_names: list[str], since_ts: float) -> tuple[int, int, int]:
    """(prompt_tokens, completion_tokens, total_tokens) for flows whose model
    ends with any of `model_names` (matches `vllm/<name>` and `<name>`)
    since `since_ts`. Errors degrade to zeros — never blocks eligibility.
    """
    if not model_names or not _FLOWS_DB.exists():
        return (0, 0, 0)
    try:
        conn = sqlite3.connect(_FLOWS_DB, timeout=5.0)
        try:
            cur = conn.cursor()
            pt = ct = tt = 0
            for name in model_names:
                rows = cur.execute(
                    """
                    SELECT COALESCE(SUM(prompt_tokens),0),
                           COALESCE(SUM(completion_tokens),0),
                           COALESCE(SUM(total_tokens),0)
                    FROM flows
                    WHERE ts >= ? AND (model = ? OR model LIKE '%/' || ?)
                    """,
                    (since_ts, name, name),
                ).fetchall()
                if rows:
                    pt += rows[0][0]
                    ct += rows[0][1]
                    tt += rows[0][2]
            return (pt, ct, tt)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("model_ranking.usage_read_failed err=%s", exc)
        return (0, 0, 0)


def _pricing_cost(entry_meta: dict[str, Any], pt: int, ct: int) -> float:
    pricing = entry_meta.get("pricing") or {}
    try:
        return pt / 1_000_000 * float(
            pricing.get("in_per_mtok") or 0
        ) + ct / 1_000_000 * float(pricing.get("out_per_mtok") or 0)
    except Exception:
        return 0.0


def compute_usage(
    provider: str, model_name: str, entry_meta: dict[str, Any]
) -> list[dict[str, Any]]:
    """Per-window usage for one (provider, model) from its budget config.

    Returns [] for local/unbudgeted models. Each row:
      {seconds, used, limit, unit: tokens|credits, used_pct}
    """
    budget = entry_meta.get("budget")
    if not isinstance(budget, dict) or budget.get("type") not in ("percent", "credits"):
        return []
    now = time.time()
    windows = budget.get("windows") or []
    out: list[dict[str, Any]] = []
    for w in windows:
        try:
            seconds = int(w.get("seconds") or 0)
        except Exception:
            continue
        if seconds <= 0:
            continue
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - seconds))
        pt, ct, tt = _usage_since(_flow_model_names(provider, model_name), since_iso)
        if budget.get("type") == "credits":
            limit = float(w.get("limit_credits") or 0)
            used = _pricing_cost(entry_meta, pt, ct)
            unit = "credits"
        else:
            limit = float(w.get("limit_tokens") or 0)
            used = float(tt)
            unit = "tokens"
        used_pct = round(used / limit * 100, 1) if limit > 0 else 0.0
        out.append(
            {
                "seconds": seconds,
                "used": round(used, 4),
                "limit": limit,
                "unit": unit,
                "used_pct": used_pct,
                "remaining": round(max(limit - used, 0.0), 4) if limit > 0 else None,
            }
        )
    return out


def eligibility(usage: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Owner policy: spending/token windows are telemetry, never dispatch gates."""
    return True, None


def _availability(meta: dict[str, Any], provider: str | None = None) -> tuple[bool, str | None]:
    """Live model and same-account quota/auth holds, including newly seeded models."""
    if provider:
        for sibling in mc_sidecar.get_provider_meta(provider).values():
            available, reason = _availability(sibling)
            if not available:
                return False, reason
    selected = provider != "openai-codex-canonical" or meta.get("canonical_pi_selected") is True
    selection = (True, None) if selected else (False, "canonical_pi_unselected")
    until = meta.get("unavailable_until")
    if not until:
        return selection
    try:
        reset = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        if reset > datetime.now(timezone.utc):
            return (
                False,
                meta.get("availability_reason") or f"unavailable_until_{until}",
            )
    except (TypeError, ValueError):
        return False, "invalid_unavailable_until"
    return selection


def record_usage(provider: str, model: str, event: dict[str, Any]) -> None:
    """Accumulate usage reported by a CLI harness in the sidecar.

    The whole read/modify/write runs inside one serialized sidecar update
    (thread + process lock) so concurrent record_usage calls never lose
    increments or interleave the sibling quota-window fan-out.
    """
    with mc_sidecar.update_lock():
        meta = mc_sidecar.get_model_meta(provider, model)
        observed = dict(meta.get("observed_usage") or {})
        # Sum each token class separately. Events/reporters use two spellings
        # for cache accounting (Anthropic-style `*_input_tokens` and the older
        # short form); both are accepted and accumulated under the key the
        # event actually used, so cache-creation is never folded into cache
        # reads or plain input and never silently dropped.
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
            "total_tokens",
        ):
            observed[key] = int(observed.get(key) or 0) + int(event.get(key) or 0)
        observed["cost_usd"] = round(
            float(observed.get("cost_usd") or 0) + float(event.get("cost_usd") or 0), 6
        )
        observed["calls"] = int(observed.get("calls") or 0) + 1
        observed["last_at"] = datetime.now(timezone.utc).isoformat()
        observed["source"] = event.get("source") or "cli_report"
        updates: dict[str, Any] = {"observed_usage": observed}
        if isinstance(event.get("harness"), dict):  # skills/MCP the run actually loaded
            updates["observed_harness"] = {**{k: list(event["harness"].get(k) or [])[:20] for k in ("skills", "mcp")},
                                           "at": observed["last_at"]}
        unavailable_until = event.get("unavailable_until")
        if unavailable_until:
            reason = event.get("availability_reason") or "quota_exhausted"
            # Subscription/session limits apply to every model behind that account.
            # A late short router cooldown cannot replace live quota/auth authority.
            if reason != "router_cooldown" or _availability(meta, provider)[0]:
                for sibling in mc_sidecar.get_provider_meta(provider):
                    mc_sidecar.set_model_meta(
                        provider,
                        sibling,
                        unavailable_until=unavailable_until,
                        availability_reason=reason,
                    )
        elif event.get("success") and _availability(meta, provider)[0]:
            # A sibling success can predate an account-wide quota failure; never
            # clear its still-live reset window merely because that response arrives later.
            updates.update(unavailable_until=None, availability_reason=None)
        mc_sidecar.set_model_meta(provider, model, **updates)


# ── Board assembly ─────────────────────────────────────────────────────────


async def get_board(provider_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the full ranking board.

    `provider_rows`: [{provider_name, status}] from provider_capacity.
    Combines sidecar ranking metadata with live usage + eligibility +
    which complexity levels each entry would be picked for.
    """
    status_by_provider = {
        r["provider_name"]: r.get("status", "offline") for r in provider_rows
    }

    entries: list[dict[str, Any]] = []
    for provider, models in sorted(
        (mc_sidecar.load_sidecar().get("models") or {}).items()
    ):
        # Agent harnesses deliberately have no provider_capacity row: they run
        # local CLIs and never become OmniRoute upstreams.
        is_harness = _is_agent_harness_provider(provider)
        prov_status = (
            "harness"
            if is_harness
            else status_by_provider.get(provider, "unregistered")
        )
        prov_online = is_harness or prov_status == "online"
        for model_name, meta in (models or {}).items():
            if not isinstance(meta, dict) or "level" not in meta:
                continue  # unranked -> not on the board
            level = int(meta.get("level") or 5)
            rank = int(meta.get("rank") or 0)
            disabled = bool(meta.get("disabled"))
            usage = compute_usage(provider, model_name, meta)
            available, availability_reason = _availability(meta, provider)
            eligible = prov_online and not disabled and available
            entries.append(
                {
                    "provider": provider,
                    "model": model_name,
                    "level": level,
                    "rank": rank,
                    "provider_priority": int(meta.get("provider_priority", 999)),
                    "max_complexity": int(meta.get("max_complexity", 5)),
                    "thinking_level": meta.get("thinking_level"),
                    "draft_only": meta.get("draft_only") is True,
                    "observed_harness": meta.get("observed_harness"),
                    "write_enabled": meta.get("write_enabled", provider != "vllm") is True,
                    "fully_trusted": meta.get("fully_trusted") is True,
                    "cost_class": meta.get("cost_class") or "free",
                    "iq_score": meta.get("iq_score"),
                    "disabled": disabled,
                    "disabled_reason": meta.get("disabled_reason"),
                    "budget_enforced": False,
                    "max_concurrency": max(
                        1,
                        min(
                            32,
                            int(
                                meta.get("max_concurrency") or (4 if is_harness else 1)
                            ),
                        ),
                    ),
                    "budget_type": (meta.get("budget") or {}).get("type", "local"),
                    "budget": (meta.get("budget") or {}),
                    "pricing": meta.get("pricing"),
                    "provider_status": prov_status,
                    "execution": "agent_harness" if is_harness else "model_endpoint",
                    "usage": usage,
                    "observed_usage": meta.get("observed_usage") or {},
                    "unavailable_until": meta.get("unavailable_until"),
                    "availability_reason": meta.get("availability_reason"),
                    "eligible": eligible,
                    "block_reason": (
                        None
                        if eligible
                        else (
                            f"provider_{prov_status}"
                            if not prov_online
                            else "disabled" if disabled else availability_reason
                        )
                    ),
                }
            )

    # Keep endpoint routing and coding-agent execution distinct. Harnesses are
    # ranked on this board, but can only be selected by the unattended runner.
    picks = _picks_by_complexity(
        [e for e in entries if e["execution"] == "model_endpoint"]
    )
    harness_entries = [e for e in entries if e["execution"] == "agent_harness"]
    harness_picks = _harness_picks_by_complexity(harness_entries)
    picked_for: dict[tuple[str, str], list[int]] = {}
    harness_picked_for: dict[tuple[str, str], list[int]] = {}
    for complexity, pick in picks.items():
        if pick:
            picked_for.setdefault((pick["provider"], pick["model"]), []).append(
                complexity
            )
    for complexity, pick in harness_picks.items():
        if pick:
            harness_picked_for.setdefault((pick["provider"], pick["model"]), []).append(
                complexity
            )

    levels = []
    for level in range(1, _MAX_LEVEL + 1):
        row = sorted(
            [e for e in entries if e["level"] == level],
            key=lambda e: (e["rank"], e["model"]),
        )
        for e in row:
            key = (e["provider"], e["model"])
            e["picked_for"] = sorted(picked_for.get(key, []))
            e["harness_picked_for"] = sorted(harness_picked_for.get(key, []))
        levels.append({"level": level, "entries": row})

    return {
        "levels": levels,
        "complexity_picks": {
            str(k): _pick_summary(v) for k, v in sorted(picks.items())
        },
        "agent_harness_picks": {
            str(k): _pick_summary(v) for k, v in sorted(harness_picks.items())
        },
        "max_level_for_complexity": _MAX_LEVEL_FOR_COMPLEXITY,
    }


def _pick_summary(e: dict[str, Any] | None) -> dict[str, Any] | None:
    if not e:
        return None
    return {
        "provider": e["provider"],
        "model": e["model"],
        "level": e["level"],
        "iq_score": e["iq_score"],
        "block_reason": e.get("block_reason"),
    }


def _sort_key(e: dict[str, Any]) -> tuple:
    # cheapest sufficient first: highest level number allowed, then local
    # hardware, then owner rank, then measured IQ.
    return (
        -e["level"],
        _COST_CLASS_ORDER.get(e.get("cost_class") or "free", 3),
        e.get("rank") or 0,
        -(e.get("iq_score") or 0),
    )


def _harness_picks_by_complexity(
    entries: list[dict[str, Any]],
) -> dict[int, dict[str, Any] | None]:
    """Pick by owner provider order, then the cheapest sufficient model."""
    picks: dict[int, dict[str, Any] | None] = {}
    for complexity in range(1, 6):
        max_level = _MAX_LEVEL_FOR_COMPLEXITY.get(complexity, 5)
        eligible = [
            e
            for e in entries
            if e["eligible"]
            and e["level"] <= max_level
            and complexity <= e.get("max_complexity", 5)
        ]
        picks[complexity] = min(
            eligible,
            key=lambda e: (
                e.get("provider_priority", 999),
                -e["level"],
                e.get("rank", 0),
                -(e.get("iq_score") or 0),
            ),
            default=None,
        )
    return picks


def _picks_by_complexity(
    entries: list[dict[str, Any]],
) -> dict[int, dict[str, Any] | None]:
    picks: dict[int, dict[str, Any] | None] = {}
    for complexity in range(1, 6):
        max_level = _MAX_LEVEL_FOR_COMPLEXITY.get(complexity, 5)
        eligible = [e for e in entries if e["eligible"] and e["level"] <= max_level]
        if eligible:
            picks[complexity] = sorted(eligible, key=_sort_key)[0]
        else:
            picks[complexity] = None
    return picks


def apply_moves(moves: list[dict[str, Any]]) -> int:
    """Persist drag-drop moves: [{provider, model, level, rank?}].
    Returns number of entries written. level is required; rank defaults
    to appending at the end of the target level.
    """
    written = 0
    # Group by level to auto-assign ranks for entries that moved without one.
    by_level: dict[int, list[dict[str, Any]]] = {}
    for m in moves:
        by_level.setdefault(int(m["level"]), []).append(m)
    for level, group in by_level.items():
        existing = sorted(
            [
                (meta.get("rank") or 0, name, prov)
                for prov, models in (
                    mc_sidecar.load_sidecar().get("models") or {}
                ).items()
                for name, meta in (models or {}).items()
                if isinstance(meta, dict)
                and meta.get("level") == level
                and not any(g["provider"] == prov and g["model"] == name for g in group)
            ],
        )
        next_rank = (existing[-1][0] + 1) if existing else 0
        for m in sorted(
            group,
            key=lambda g: g.get("rank", 10**9) if g.get("rank") is not None else 10**9,
        ):
            rank = m.get("rank")
            if rank is None:
                rank = next_rank
                next_rank += 1
            mc_sidecar.set_model_meta(
                m["provider"],
                m["model"],
                level=level,
                rank=int(rank),
            )
            written += 1
    return written


def set_entry_meta(provider: str, model: str, meta: dict[str, Any]) -> None:
    """Set arbitrary ranking metadata fields on one entry (budget, pricing,
    cost_class, disabled, disabled_reason, level, rank)."""
    allowed = {
        "level",
        "rank",
        "provider_priority",
        "max_complexity",
        "write_enabled",
        "canonical_pi_selected",
        "fully_trusted",
        "cost_class",
        "budget",
        "pricing",
        "disabled",
        "disabled_reason",
        "max_concurrency",
        "unavailable_until",
        "availability_reason",
        "thinking_level",
        "draft_only",
    }
    mc_sidecar.set_model_meta(
        provider, model, **{k: v for k, v in meta.items() if k in allowed}
    )


# Neuralwatt bills per use on a shared key. It is routed through OmniRoute so
# every call is recorded in the flows DB, which is what makes this window real
# rather than decorative. Pricing is an estimate until billed rates are confirmed.
_METERED_BUDGET: dict[str, Any] = {"type": "unlimited"}
_METERED_PRICING: dict[str, Any] = {"in_per_mtok": 0.5, "out_per_mtok": 1.5}


def seed_default_ranking() -> dict[str, int]:
    """Owner's initial board (see plan-model-ranking-2026-09-15.md). Idempotent:
    only fills level when absent, never overwrites owner drag-drops. Budgets
    are placeholders the owner edits via UI/sidecar with real provider data.
    """
    seed: dict[str, dict[str, Any]] = {}
    for (provider, model), (level, cost_class, budget, pricing) in {
        # Level 1 — frontier / mastermind
        ("zai", "glm-5.3"): (
            1,
            "credits",
            {
                "type": "credits",
                "windows": [{"seconds": 604800, "limit_credits": 10.0}],
            },
            {"in_per_mtok": 0.5, "out_per_mtok": 1.5},
        ),
        ("gemini", "gemini-3-pro"): (
            1,
            "credits",
            {"type": "credits", "windows": [{"seconds": 604800, "limit_credits": 5.0}]},
            {"in_per_mtok": 0.25, "out_per_mtok": 2.0},
        ),
        ("deepseek", "deepseek-v4-pro"): (
            1,
            "credits",
            {"type": "credits", "windows": [{"seconds": 604800, "limit_credits": 5.0}]},
            {"in_per_mtok": 0.27, "out_per_mtok": 1.1},
        ),
        # Level 2 — strong planner
        ("zai", "glm-5.3-flash"): (
            2,
            "free",
            {
                "type": "percent",
                "windows": [
                    {"seconds": 3600, "limit_tokens": 200000},
                    {"seconds": 604800, "limit_tokens": 1000000},
                ],
            },
            None,
        ),
        ("ollama", "deepseek-v4-pro"): (
            2,
            "free",
            {
                "type": "percent",
                "windows": [{"seconds": 604800, "limit_tokens": 500000}],
            },
            None,
        ),
        ("dashscope", "qwen3.6-max-preview"): (
            2,
            "free",
            {
                "type": "percent",
                "windows": [{"seconds": 604800, "limit_tokens": 500000}],
            },
            None,
        ),
        # Level 3 — mid
        ("groq", "openai/gpt-oss-120b"): (
            3,
            "free",
            {
                "type": "percent",
                "windows": [
                    {"seconds": 3600, "limit_tokens": 100000},
                    {"seconds": 86400, "limit_tokens": 500000},
                ],
            },
            None,
        ),
        ("opencode-zen", "big-pickle"): (
            3,
            "free",
            {
                "type": "percent",
                "windows": [{"seconds": 604800, "limit_tokens": 300000}],
            },
            None,
        ),
        # Level 4 — worker, own hardware (preferred)
        ("vllm", "Qwen3.8-27B-FP8"): (4, "local", None, None),
        ("llamacpp", "qwen3.8-27b"): (4, "local", None, None),
        # Level 5 — cheapest
        ("LMStudio", "qwen3.6-35b-a3b-uncensored"): (5, "local", None, None),
        ("ollama", "qwen3.8-27b"): (
            5,
            "free",
            {
                "type": "percent",
                "windows": [{"seconds": 604800, "limit_tokens": 200000}],
            },
            None,
        ),
    }.items():
        existing = mc_sidecar.get_model_meta(provider, model)
        updates: dict[str, Any] = {}
        if "level" not in existing:
            updates["level"] = level
        if "cost_class" not in existing:
            updates["cost_class"] = cost_class
        if "budget" not in existing and budget:
            updates["budget"] = budget
        if "pricing" not in existing and pricing:
            updates["pricing"] = pricing
        if updates:
            mc_sidecar.set_model_meta(provider, model, **updates)
            seed[f"{provider}/{model}"] = level

    # Harness subscriptions have no synthetic token budget: each CLI reports
    # quota exhaustion directly and the runner advances to the next provider.
    for (provider, model), (level, rank, priority) in _AGENT_HARNESS_MODELS.items():
        existing = mc_sidecar.get_model_meta(provider, model)
        defaults: dict[str, Any] = {
            "level": level,
            "rank": rank,
            "provider_priority": priority,
            "max_complexity": 3 if provider == "deepseek-sub" else 5,
            "cost_class": "credits",
            "budget": (
                dict(_METERED_BUDGET)
                if provider == "pi-neuralwatt"
                else {"type": "subscription"}
            ),
            "pricing": dict(_METERED_PRICING) if provider == "pi-neuralwatt" else None,
            "disabled": False,
            "disabled_reason": (
                None if provider == "pi-neuralwatt" else _AGENT_HARNESS_SANCTION
            ),
        }
        updates = {key: value for key, value in defaults.items() if key not in existing}
        if updates:
            mc_sidecar.set_model_meta(provider, model, **updates)
            seed[f"{provider}/{model} (harness)"] = level
    return seed


def apply_cloud_policy() -> dict[str, int]:
    """Apply the owner's cloud-first execution policy without touching usage windows."""
    changed: dict[str, int] = {}
    for (provider, model), (level, rank, priority) in _AGENT_HARNESS_MODELS.items():
        mc_sidecar.set_model_meta(
            provider,
            model,
            level=level,
            rank=rank,
            provider_priority=priority,
            max_complexity=3 if provider == "deepseek-sub" else 5,
            cost_class="credits",
            budget=(
                dict(_METERED_BUDGET)
                if provider == "pi-neuralwatt"
                else {"type": "subscription"}
            ),
            pricing=dict(_METERED_PRICING) if provider == "pi-neuralwatt" else None,
            disabled=False,
            disabled_reason=(
                None if provider == "pi-neuralwatt" else _AGENT_HARNESS_SANCTION
            ),
            policy_revision=_CLOUD_POLICY_REVISION,
        )
        changed[f"{provider}/{model}"] = priority

    # Local inference is intentionally parked until cloud orchestration is reliable.
    for provider in ("vllm", "llamacpp", "LMStudio"):
        for model in mc_sidecar.get_provider_meta(provider):
            mc_sidecar.set_model_meta(
                provider,
                model,
                disabled=True,
                disabled_reason="Parked by cloud-first policy; re-enable after validation.",
            )

    # One auth.json cannot represent two OAuth accounts. Migrate historical
    # single-account counters to profile 1, then remove the ambiguous entries.
    for model in ("gpt-5.6-sol", "gpt-5.6-terra"):
        legacy = mc_sidecar.get_model_meta("openai-codex", model)
        if legacy.get("observed_usage"):
            current = mc_sidecar.get_model_meta("openai-codex-1", model)
            if not current.get("observed_usage"):
                mc_sidecar.set_model_meta(
                    "openai-codex-1",
                    model,
                    observed_usage=legacy["observed_usage"],
                    unavailable_until=legacy.get("unavailable_until"),
                    availability_reason=legacy.get("availability_reason"),
                )
        mc_sidecar.clear_model_meta("openai-codex", model)
    return changed
