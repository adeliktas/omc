"""T80 — Model picker that respects complexity + IQ score + provider priority.

T103 (2026-05-17): rewritten to source the model catalog from OmniRoute
(`/v1/models`) instead of openclaw.json. Each OmniRoute model id is
`<upstream-prefix>/<model-name>` (e.g., `vllm/Qwen3.6-35B-A3B-FP8`,
`ollama-cloud/glm-4.7`, `lm-studio/qwen3.6-35b-a3b-uncensored`). The
upstream prefix maps to `provider_capacity.provider_name`; we filter to
upstream providers whose `status == 'online'` in MC, so toggling a
provider offline in the UI excludes it from routing within one cache TTL.

Returns the OmniRoute-canonical model id; agents send this as the `model`
field in chat-completion bodies routed via the `omniroute` provider in
openclaw.json.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.provider_capacity import ProviderCapacity
from app.services import omniroute_sync
from app.services.mc_sidecar import merge_into_model_entry as _merge
from app.services.vllm_model import rewrite_vllm_model_id

logger = get_logger(__name__)


# T107 (2026-05-26): per-model rate-limit cache. Now with EXPONENTIAL BACKOFF
# (T107 round-2 2026-05-26): a flat 60s cooldown was burning through the
# chain → 429 cycle every 2 min forever. We now track consecutive-429 count
# per model and double the cooldown each time, capped at 30 min. Successful
# call clears the counter.
_RATE_LIMITED_UNTIL: dict[str, float] = {}
_RATE_LIMIT_STRIKES: dict[str, int] = {}
_BASE_COOLDOWN_SECONDS = 60.0
_MAX_COOLDOWN_SECONDS = 1800.0


def mark_model_rate_limited(model_id: str, cooldown_seconds: float | None = None) -> None:
    """Mark a model_id as temporarily unavailable. Called by call_model on 429.

    Exponential: each consecutive strike doubles the cooldown (60 → 120 →
    240 → 480 → 960 → 1800 capped). Explicit `cooldown_seconds` override
    bypasses the curve (used for Retry-After header values).
    """
    if cooldown_seconds is None:
        strikes = _RATE_LIMIT_STRIKES.get(model_id, 0) + 1
        _RATE_LIMIT_STRIKES[model_id] = strikes
        cooldown_seconds = min(_BASE_COOLDOWN_SECONDS * (2 ** (strikes - 1)), _MAX_COOLDOWN_SECONDS)
    _RATE_LIMITED_UNTIL[model_id] = time.time() + cooldown_seconds
    logger.info(
        "model_router.rate_limited model=%s strikes=%d cooldown_s=%d",
        model_id, _RATE_LIMIT_STRIKES.get(model_id, 0), int(cooldown_seconds),
    )


def clear_model_rate_limit(model_id: str) -> None:
    """Called by call_model on successful response. Resets strike counter."""
    _RATE_LIMITED_UNTIL.pop(model_id, None)
    _RATE_LIMIT_STRIKES.pop(model_id, None)


def _is_model_rate_limited(model_id: str) -> bool:
    until = _RATE_LIMITED_UNTIL.get(model_id, 0.0)
    if until <= 0.0:
        return False
    if time.time() >= until:
        _RATE_LIMITED_UNTIL.pop(model_id, None)
        return False
    return True


# Per-complexity-level minimum IQ. Tasks below the threshold prefer a
# better model when one is available; they don't strictly require it.
_MIN_IQ_BY_COMPLEXITY: dict[int, int] = {
    1: 0,
    2: 40,
    3: 60,
    4: 80,
    5: 90,
}

# T110 (2026-09-15) — Model Ranking Board. When sidecar metadata assigns a
# `level` (1=frontier .. 5=cheapest) to catalog models, selection becomes
# level-aware: a task of complexity C may use models with level <=
# _MAX_LEVEL_FOR_COMPLEXITY[C], picking the CHEAPEST SUFFICIENT eligible
# candidate (highest level number, then local hardware, then owner rank,
# then IQ). Unranked models are excluded while any ranked candidate exists.
# Fallback: no ranked+eligible candidate -> legacy IQ-band pick.
_MAX_LEVEL_FOR_COMPLEXITY: dict[int, int] = {5: 1, 4: 2, 3: 3, 2: 4, 1: 5}

_COST_CLASS_ORDER: dict[str, int] = {"local": 0, "free": 1, "credits": 2}


# Map OmniRoute model-id prefix → MC provider_capacity.provider_name.
# OmniRoute exposes models under prefixes that match the upstream
# connection slug. MC uses slightly different names for some providers,
# so we normalize here. Unknown prefixes are treated as their own
# provider name (fail-open).
_PREFIX_TO_MC_PROVIDER: dict[str, str] = {
    "vllm": "vllm",
    "lm-studio": "LMStudio",
    "lmstudio": "LMStudio",
    "ollama-cloud": "ollama",
    "ollamacloud": "ollama",
    "cloudflare": "cloudflare",
    "openrouter": "openrouter",
    "groq": "groq",
    "gemini": "gemini",
    "deepseek": "deepseek",
    "dashscope": "dashscope",
    "kilo-code": "kilocode",
    "zai": "zai",
    "opencode-zen": "opencode-zen",
}


def _split_model_id(model_id: str) -> tuple[str, str]:
    """`vllm/Qwen3.6-35B-A3B-FP8` → (`vllm`, `Qwen3.6-35B-A3B-FP8`)."""
    if "/" in model_id:
        prefix, _, name = model_id.partition("/")
        return prefix, name
    return "", model_id


# Providers that may appear on the ranking board but must never be resolved as
# a routing target, whatever their sidecar `disabled` flag says. These are local
# coding-agent harnesses, not API upstreams; returning None excludes them from
# both candidate paths at once.
_NON_ROUTABLE_PROVIDERS: frozenset[str] = frozenset({
    "claude-code", "claude-code-p", "openai-codex", "ollama-cloud-sub", "deepseek-sub",
    "pi-neuralwatt",  # native harness identity, not an OmniRoute endpoint
    "omniroute-codex",  # Pi harness that reaches codex through OmniRoute; not a router target
    "metis",  # Pi harness for the on-prem metis vLLM; not a router target
    "strata",  # Pi harness for the strata llama.cpp; OmniRoute has its own strata node
})


def _is_non_routable_provider(provider: str) -> bool:
    return provider in _NON_ROUTABLE_PROVIDERS or provider.startswith("openai-codex-")


def _model_id_to_mc_provider(model_id: str) -> str | None:
    # A virtual combo is not the Jetson: OmniRoute owns its member availability.
    # The prefixed omniroute/omniroute-auto is not an upstream model id.
    if model_id == "omniroute-auto":
        return "omniroute"
    prefix, _ = _split_model_id(model_id)
    if not prefix:
        return None
    mc_provider = _PREFIX_TO_MC_PROVIDER.get(prefix, prefix)
    if _is_non_routable_provider(mc_provider):
        return None
    return mc_provider


async def _online_provider_names(db: AsyncSession) -> set[str]:
    # T107 (2026-05-26): `last_error` is diagnostic (OR's `testStatus` —
    # cooldown, credits_exhausted, etc) and per design comment in
    # omniroute_sync.py NOT a routing gate. Only `status='online'` matters.
    # The previous `if not r.last_error` clause was excluding ollama (which
    # had stale `last_error='unavailable'` from a transient OR backoff).
    rows = (
        await db.execute(
            sa_select(ProviderCapacity).where(ProviderCapacity.status == "online")
        )
    ).scalars().all()
    return {r.provider_name for r in rows}


def _sort_key_level_mode(e: dict[str, Any]) -> tuple:
    # cheapest sufficient first: highest level number allowed, then local
    # hardware, then owner rank, then measured IQ.
    return (
        -int(e.get("level") or 5),
        _COST_CLASS_ORDER.get(e.get("cost_class") or "free", 3),
        int(e.get("rank") or 0),
        -int(e.get("iq_score") or 0),
    )


async def _build_candidates(
    db: AsyncSession,
    complexity: int = 3,
    *,
    prefer_provider: str | None = None,
) -> list[dict[str, Any]]:
    """Ordered candidate list: (model_id, mc_provider, iq, merged_meta).

    T110: level-mode when any curated model carries sidecar `level`
    metadata; unranked models excluded in level-mode. Falls back to the
    legacy IQ-band pick when no ranked candidate is eligible.
    """
    catalog = await omniroute_sync.list_curated_models()  # T108: curated-only
    online = await _online_provider_names(db)

    merged_all: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in catalog:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        mc_prov = _model_id_to_mc_provider(mid)
        if not mc_prov or mc_prov not in online:
            continue
        if prefer_provider and mc_prov != prefer_provider:
            continue
        _, model_name = _split_model_id(mid)
        merged = _merge(mc_prov, {"id": model_name})
        if merged.get("disabled"):
            continue
        merged["_mc_provider"] = mc_prov
        merged["_catalog_id"] = mid
        merged_all.append(merged)

    ranked = [e for e in merged_all if "level" in e]
    if ranked:
        max_level = _MAX_LEVEL_FOR_COMPLEXITY.get(complexity, 5)
        eligible = [
            e for e in ranked
            if int(e["level"]) <= max_level
        ]
        if eligible:
            eligible.sort(key=_sort_key_level_mode)
            return eligible

    # Legacy fallback: IQ-band over all curated (incl. unranked) models.
    min_iq = _MIN_IQ_BY_COMPLEXITY.get(complexity, 60)
    legacy: list[tuple[int, dict[str, Any]]] = []
    for e in merged_all:
        iq = int(e.get("iq_score") or 0)
        if iq < min_iq:
            continue
        legacy.append((-iq, e))
    legacy.sort(key=lambda t: t[0])
    return [e for _neg, e in legacy]


async def pick_top_n_models_for_complexity(
    db: AsyncSession,
    complexity: int = 3,
    *,
    limit: int = 5,
) -> list[tuple[str, str, int]]:
    """T83 — top-N candidates. Returns list of (provider, model_id, iq).

    `provider` is always `"omniroute"` (single egress); `model_id` is the
    OmniRoute-canonical id (e.g., `vllm/Qwen3.8-27B-FP8`).
    """
    candidates = await _build_candidates(db, complexity)
    return [
        ("omniroute", rewrite_vllm_model_id(e["_catalog_id"]), int(e.get("iq_score") or 0))
        for e in candidates[:limit]
    ]


async def pick_model_for_complexity(
    db: AsyncSession,
    complexity: int = 3,
    *,
    prefer_provider: str | None = None,
) -> tuple[str, str, int] | None:
    """Best single model for complexity. Returns (provider, model_id, iq) or None.

    `prefer_provider` filters to a specific MC upstream provider name
    (e.g., `vllm`, `ollama`). When unset, the smartest available across
    all online upstreams wins.
    """
    candidates = await _build_candidates(db, complexity, prefer_provider=prefer_provider)
    if not candidates:
        return None
    e = candidates[0]
    mid = rewrite_vllm_model_id(e["_catalog_id"])
    iq = int(e.get("iq_score") or 0)
    logger.info(
        "model_router.picked complexity=%d → omniroute/%s iq=%d level=%s cost=%s "
        "(out of %d candidates)",
        complexity, mid, iq, e.get("level"), e.get("cost_class"), len(candidates),
    )
    return "omniroute", mid, iq


# ─────────────────────────────────────────────────────────────────────────
# T105 Phase 2 (2026-05-25): tier-based model routing
# ─────────────────────────────────────────────────────────────────────────


_TIER_TO_SETTING_KEY: dict[str, str] = {
    "planner": "TIER_PLANNER_MODELS",
    "verifier": "TIER_VERIFIER_MODELS",
    "executor": "TIER_EXECUTOR_MODELS",
}


def _parse_model_chain(value: str) -> list[str]:
    if not value:
        return []
    return [m.strip() for m in value.split(",") if m.strip()]


async def pick_model_for_tier(
    db: AsyncSession,
    tier: str,
) -> tuple[str, str, list[str]] | None:
    """Resolve a tier (`planner`/`executor`/`verifier`) to the first
    online model in its configured fallback chain.

    Returns (`omniroute`, model_id, attempted_list) — the resolved
    primary model + the list of every chain entry that was tried (for
    observability). Returns None when nothing in the chain is online.

    `tier='mixed'` and unknown tiers return None — caller should fall
    back to the existing `pick_model_for_complexity` / preferred_provider
    path.

    Use only owner-curated, online, enabled candidates (or the local
    omniroute-auto combo). An ineligible degraded fallback fails closed.
    """
    from app.services.runtime_settings import get_setting

    key = _TIER_TO_SETTING_KEY.get(tier)
    if not key:
        return None
    chain = _parse_model_chain(get_setting(key, ""))
    if not chain:
        return None

    online = await _online_provider_names(db)
    curated = {str(m.get("id") or "") for m in await omniroute_sync.list_curated_models()}
    attempted: list[str] = []

    def eligible(model_id: str) -> bool:
        if model_id == "omniroute-auto":
            return not _is_model_rate_limited(model_id)
        if model_id not in curated:
            return False
        provider = _model_id_to_mc_provider(model_id)
        if provider is None or provider not in online:
            return False
        _, model_name = _split_model_id(model_id)
        return not _merge(provider, {"id": model_name}).get("disabled") and not _is_model_rate_limited(model_id)

    for model_id in chain:
        attempted.append(model_id)
        if eligible(model_id):
            model_id = rewrite_vllm_model_id(model_id)
            logger.info("model_router.tier_picked tier=%s model=%s", tier, model_id)
            return "omniroute", model_id, attempted

    degraded = get_setting("TIER_DEGRADED_FALLBACK", "omniroute-auto")
    if degraded:
        attempted.append(degraded)
        if eligible(degraded):
            logger.warning("model_router.tier_degraded tier=%s model=%s", tier, degraded)
            return "omniroute", degraded, attempted
    return None
