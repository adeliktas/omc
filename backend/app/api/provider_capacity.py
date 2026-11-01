"""Provider capacity API — runtime-configurable concurrency limits and usage tracking.

T58 (2026-05-15): provider-status changes from `/providers` UI now also rewrite
`~/.openclaw/openclaw.json` to strip the toggled-offline provider from every
agent's `model.fallbacks` chain and clear gateway session overrides so the
change takes effect without a manual gateway restart.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent, get_session as _db_dep
from app.core.logging import get_logger
from app.models.provider_capacity import ProviderCapacity

logger = get_logger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_OPENCLAW_JSON = _OPENCLAW_HOME / "openclaw.json"


def _atomic_write_json(target: Path, data: Any) -> None:
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, target)


def _strip_provider_from_fallbacks(provider_name: str) -> dict[str, int]:
    """Strip every `{provider_name}/...` entry from `model.fallbacks` lists
    in `agents.defaults` + each `agents.list[*]`. Leaves `primary` alone —
    that's a deliberate per-agent assignment the owner should explicitly
    move. Returns counters: {stripped, primary_warnings}.
    """
    counters = {"stripped": 0, "primary_warnings": 0}
    if not _OPENCLAW_JSON.exists():
        return counters
    try:
        cfg = json.loads(_OPENCLAW_JSON.read_text())
    except Exception as exc:
        logger.warning("provider_routing.read_failed err=%s", exc)
        return counters

    prefix = f"{provider_name}/"

    def _scrub(model_block: dict[str, Any] | None) -> None:
        if not isinstance(model_block, dict):
            return
        fbs = model_block.get("fallbacks")
        if isinstance(fbs, list):
            before = len(fbs)
            model_block["fallbacks"] = [f for f in fbs if not (isinstance(f, str) and f.startswith(prefix))]
            counters["stripped"] += before - len(model_block["fallbacks"])
        primary = model_block.get("primary")
        if isinstance(primary, str) and primary.startswith(prefix):
            counters["primary_warnings"] += 1

    defaults = cfg.get("agents", {}).get("defaults", {})
    _scrub(defaults.get("model"))
    for agent in cfg.get("agents", {}).get("list", []) or []:
        _scrub(agent.get("model"))

    if counters["stripped"] > 0:
        try:
            _atomic_write_json(_OPENCLAW_JSON, cfg)
        except Exception as exc:
            logger.warning("provider_routing.write_failed err=%s", exc)
    return counters


def _scrub_gateway_caches() -> int:
    """Clear gateway per-session model overrides. Lifts the helper from
    `provider_stats` so the provider-toggle path doesn't need to import a
    sibling API module."""
    try:
        from app.api.provider_stats import _clear_session_overrides
        return _clear_session_overrides()
    except Exception as exc:
        logger.warning("provider_routing.scrub_failed err=%s", exc)
        return 0


def _load_cfg() -> dict[str, Any]:
    if not _OPENCLAW_JSON.exists():
        return {}
    try:
        return json.loads(_OPENCLAW_JSON.read_text())
    except Exception as exc:
        logger.warning("openclaw_json.read_failed err=%s", exc)
        return {}


# ── OmniRoute sync (T71 2026-05-15) ────────────────────────────────────
# Optional one-way mirror: when env `OMNIROUTE_URL` + `OMNIROUTE_MGMT_TOKEN`
# are set, every provider mutation made through the MC `/providers` API
# (create / update / delete / model add / model delete / rename) also
# fires a best-effort sync call to the OmniRoute dashboard backend
# at `<OMNIROUTE_URL>/api/providers`. If the env vars are unset, all sync
# calls are no-ops + a single debug-log line is emitted to make the
# "OmniRoute not wired" state visible without spamming the log.
#
# OmniRoute auth model uses an API key with the `manage` scope. Generate
# one in the OmniRoute dashboard (`http://localhost:20128`) → Settings →
# API Manager → New key → toggle `manage` scope, then place the value in
# `OMNIROUTE_MGMT_TOKEN`. No automatic discovery — owner must opt in.

_OMNIROUTE_URL = os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
_OMNIROUTE_TOKEN = os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
# T103 (2026-05-17): OmniRoute /api/* accepts ONLY dashboard session cookies
# (auth_token JWT). Bearer mgmt tokens were removed upstream. Caller stores
# the JWT in OMNIROUTE_MGMT_TOKEN; we send it as a Cookie header.
_OMNIROUTE_ENABLED = bool(_OMNIROUTE_TOKEN and _OMNIROUTE_URL)


async def _omniroute_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """One-shot call to OmniRoute. Returns (status_code, body). Returns
    (0, None) when sync is disabled or the network call raises — caller
    should treat that as best-effort soft-fail."""
    if not _OMNIROUTE_ENABLED:
        return 0, None
    try:
        import httpx
        url = _OMNIROUTE_URL.rstrip("/") + path
        # 2026-08-25: prefer a freshly SELF-MINTED mgmt JWT (same HS256/JWT_SECRET
        # path as traffic_etl) over the static OMNIROUTE_MGMT_TOKEN env, which is
        # a 30d token that silently expires → 401 AUTH_001 on provider create/sync
        # (exactly what broke adding the llamacpp provider). Fall back to the env
        # token if minting is unavailable.
        try:
            from app.services.traffic_etl import _mgmt_token as _mint
            _tok = _mint() or _OMNIROUTE_TOKEN
        except Exception:
            _tok = _OMNIROUTE_TOKEN
        # T103 (2026-05-17): OmniRoute enforces CSRF origin check on /api/*.
        # GOTCHA (2026-08-25): WRITE endpoints (POST/PUT/PATCH/DELETE) REJECT the
        # localhost-rewritten Origin with 403 INVALID_ORIGIN — same as the
        # watchdog combo-sync writer. Only the read (GET) path tolerates the
        # Origin/Referer; for writes send Cookie-only (no Origin/Referer).
        headers = {
            "Cookie": f"auth_token={_tok}",
            "Content-Type": "application/json",
        }
        if method.upper() == "GET":
            _origin = _OMNIROUTE_URL.replace("//omniroute:", "//localhost:")
            headers["Origin"] = _origin
            headers["Referer"] = f"{_origin}/dashboard"
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.request(method, url, headers=headers, json=payload)
        body: dict[str, Any] | None = None
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code, body
    except Exception as exc:
        logger.warning("omniroute_sync.network_failed method=%s path=%s err=%s",
                       method, path, exc)
        return 0, None


# T103 v2 (2026-05-17): all upstreams are now OpenAI-Compatible nodes in
# OmniRoute (created via "Add OpenAI Compatible" button). Each node has
# id `openai-compatible-chat-<uuid>` and `name` matching the MC
# provider_name. Sync looks up the node by name on each call.


async def _omniroute_find_node(provider_name: str) -> dict[str, Any] | None:
    """GET /api/provider-nodes; return node dict where node.name == provider_name."""
    code, body = await _omniroute_request("GET", "/api/provider-nodes")
    if code != 200 or not isinstance(body, dict):
        return None
    for n in body.get("nodes", []) or []:
        if n.get("name") == provider_name:
            return n
    return None


async def _omniroute_sync_provider_upsert(
    provider_name: str,
    base_url: str | None,
    api_key: str | None,
    status: str,
    *,
    max_concurrent: int | None = None,
    priority: int | None = None,
) -> None:
    """Upsert a provider connection in OmniRoute. No-op when sync disabled.

    T103 v2 (2026-05-17): all upstreams are OpenAI-Compatible nodes. We
    look up the existing node by name; if missing, create the node first
    (POST /api/provider-nodes) then the connection (POST /api/providers).
    On subsequent calls we PUT the connection to update testStatus, key,
    baseUrl.
    """
    if not _OMNIROUTE_ENABLED:
        logger.debug(
            "omniroute_sync.skipped reason=not_configured provider=%s "
            "(set OMNIROUTE_MGMT_TOKEN to enable)", provider_name,
        )
        return
    node = await _omniroute_find_node(provider_name)
    node_id = node.get("id") if node else None
    if not node_id:
        # Auto-provision OpenAI-Compatible node for new provider.
        node_body = {
            "name": provider_name,
            "prefix": provider_name.lower().replace("_", "-"),
            "baseUrl": base_url or "https://example.invalid/v1",
            "type": "openai-compatible",
            "apiType": "chat",
            "chatPath": "",
            "modelsPath": "",
        }
        code, body = await _omniroute_request("POST", "/api/provider-nodes", node_body)
        if code not in (200, 201):
            logger.warning(
                "omniroute_sync.node_create_failed provider=%s status=%d body=%s",
                provider_name, code, str(body)[:200],
            )
            return
        node_id = (body or {}).get("node", {}).get("id") if isinstance(body, dict) else None
        if not node_id:
            return
    elif base_url and node.get("baseUrl") != base_url:
        # T103 v15 (2026-05-19): provider-node carries the *routing* baseUrl;
        # the per-connection record only mirrors it for display. If MC owner
        # changed base_url in `/providers`, propagate it to the node so the
        # gateway actually hits the new upstream. Without this PUT, the
        # connection shows the new URL but OmniRoute routes to the old one
        # (silent split-brain).
        node_put = {
            "name": node.get("name") or provider_name,
            "prefix": node.get("prefix") or provider_name.lower().replace("_", "-"),
            "baseUrl": base_url,
            "type": node.get("type") or "openai-compatible",
            "apiType": node.get("apiType") or "chat",
            "chatPath": node.get("chatPath") or "",
            "modelsPath": node.get("modelsPath") or "",
        }
        code, body = await _omniroute_request(
            "PUT", f"/api/provider-nodes/{node_id}", node_put,
        )
        if code not in (200, 201):
            logger.warning(
                "omniroute_sync.node_baseurl_update_failed provider=%s status=%d body=%s",
                provider_name, code, str(body)[:200],
            )
        else:
            logger.info(
                "omniroute_sync.node_baseurl_updated provider=%s old=%s new=%s",
                provider_name, node.get("baseUrl"), base_url,
            )
    existing_id = await _omniroute_find_connection_id(node_id, provider_name)
    # T103 v5 (2026-05-17): MC owns isActive (owner on/off toggle); OmniRoute
    # owns testStatus (probe result). Don't push testStatus from MC.
    # T103 v18 (2026-05-19): push all bi-dir fields — priority + maxConcurrent.
    # Priority axis convention mismatch: MC is 0-based (0 = highest, T80),
    # OmniRoute is 1-based (>=1 required, rejected with 400 otherwise).
    # Translate MC N → OR N+1 outbound; reverse-sync inverts inbound.
    mc_prio = priority if priority is not None else 0
    eff_priority = max(1, mc_prio + 1)
    payload: dict[str, Any] = {
        "provider": node_id,
        "name": provider_name,
        "apiKey": api_key or "no-key-needed",
        "priority": eff_priority,
        "globalPriority": eff_priority,
        "providerSpecificData": {
            "baseUrl": base_url,
            "displayName": provider_name,
            "autoSync": True,
        },
        "isActive": status == "online",
    }
    if max_concurrent is not None:
        payload["maxConcurrent"] = max_concurrent
    if existing_id:
        code, body = await _omniroute_request(
            "PUT", f"/api/providers/{existing_id}", payload,
        )
    else:
        code, body = await _omniroute_request("POST", "/api/providers", payload)
    if code == 0:
        return
    if code in (200, 201):
        logger.info(
            "omniroute_sync.upserted provider=%s node=%s mode=%s",
            provider_name, node_id[:20], "PUT" if existing_id else "POST",
        )
    else:
        logger.warning(
            "omniroute_sync.failed provider=%s status=%d body=%s",
            provider_name, code, str(body)[:200],
        )


async def _omniroute_find_connection_id(node_id: str, name: str) -> str | None:
    """Look up an existing OmniRoute connection by (provider-node-id, name)."""
    code, body = await _omniroute_request("GET", "/api/providers")
    if code != 200 or not isinstance(body, dict):
        return None
    for c in body.get("connections", []) or []:
        if c.get("provider") == node_id and c.get("name") == name:
            return c.get("id")
    return None


async def _omniroute_sync_provider_delete(provider_name: str) -> None:
    """Delete a provider's connection (keep node for potential re-add).

    T103 v2: lookup node by name, find connection under that node, DELETE.
    Connection-id is in /api/providers; we filter by connection.name."""
    if not _OMNIROUTE_ENABLED:
        logger.debug(
            "omniroute_sync.skipped reason=not_configured provider=%s (delete)",
            provider_name,
        )
        return
    code, body = await _omniroute_request("GET", "/api/providers")
    if code != 200 or not isinstance(body, dict):
        return
    cid = None
    for c in body.get("connections", []) or []:
        if c.get("name") == provider_name:
            cid = c.get("id")
            break
    if not cid:
        logger.debug("omniroute_sync.delete_noop provider=%s (not found)", provider_name)
        return
    code, body = await _omniroute_request("DELETE", f"/api/providers/{cid}")
    if code == 0:
        return
    if 200 <= code < 300:
        logger.info("omniroute_sync.deleted provider=%s", provider_name)
    else:
        logger.warning(
            "omniroute_sync.delete_failed provider=%s status=%d body=%s",
            provider_name, code, str(body)[:200],
        )


def _save_cfg(cfg: dict[str, Any]) -> None:
    _atomic_write_json(_OPENCLAW_JSON, cfg)


def _provider_models_block(cfg: dict[str, Any], provider_name: str, create: bool = False) -> dict[str, Any] | None:
    """Return the `models.providers.<name>` dict from openclaw.json.
    When `create=True`, missing levels are populated so callers can mutate
    a guaranteed-existing block.
    """
    if create:
        cfg.setdefault("models", {}).setdefault("providers", {}).setdefault(
            provider_name, {"models": []},
        )
        block = cfg["models"]["providers"][provider_name]
        if not isinstance(block.get("models"), list):
            block["models"] = []
        return block
    return cfg.get("models", {}).get("providers", {}).get(provider_name)


def _rename_provider_in_cfg(cfg: dict[str, Any], old_name: str, new_name: str) -> int:
    """Rename a provider key everywhere it appears in openclaw.json.

    Touches: `models.providers.<name>`, agent `model.primary` / `model.fallbacks`
    prefixes, defaults model prefix. Returns count of references rewritten.
    """
    if old_name == new_name:
        return 0
    count = 0
    providers = cfg.get("models", {}).get("providers", {})
    if old_name in providers and new_name not in providers:
        providers[new_name] = providers.pop(old_name)
        count += 1

    old_prefix = f"{old_name}/"
    new_prefix = f"{new_name}/"

    def _rewrite(model_block: dict[str, Any] | None) -> None:
        nonlocal count
        if not isinstance(model_block, dict):
            return
        primary = model_block.get("primary")
        if isinstance(primary, str) and primary.startswith(old_prefix):
            model_block["primary"] = new_prefix + primary[len(old_prefix):]
            count += 1
        fbs = model_block.get("fallbacks")
        if isinstance(fbs, list):
            for i, fb in enumerate(fbs):
                if isinstance(fb, str) and fb.startswith(old_prefix):
                    fbs[i] = new_prefix + fb[len(old_prefix):]
                    count += 1

    _rewrite(cfg.get("agents", {}).get("defaults", {}).get("model"))
    for agent in cfg.get("agents", {}).get("list", []) or []:
        _rewrite(agent.get("model"))
    return count


class ModelEntry(BaseModel):
    """A model exposed by a provider (mirrors openclaw.json schema)."""
    id: str
    name: str | None = None
    reasoning: bool = False
    input: list[str] = ["text"]
    cost: dict[str, float] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    contextWindow: int | None = None
    maxTokens: int | None = None
    # T80 (2026-05-16): per-model quality rating (0-100). Used by the
    # complexity router to pick "smarter" models for L4/L5 tasks. Seeded
    # by a one-shot eval suite (see scripts/eval_models.py); owner can
    # override via the providers UI.
    iq_score: int | None = None


class ModelEntryUpdate(BaseModel):
    """Partial update for a model entry."""
    name: str | None = None
    reasoning: bool | None = None
    input: list[str] | None = None
    cost: dict[str, float] | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None
    iq_score: int | None = None
    # T76 (2026-05-16): auto-disable bookkeeping, MC-sidecar-owned. Exposed
    # here so the owner can re-enable a model the benchmark loop auto-disabled
    # (`{disabled: false, disabled_reason: null, disabled_at: null}`).
    disabled: bool | None = None
    disabled_reason: str | None = None
    disabled_at: int | None = None


class ProviderRead(BaseModel):
    id: UUID
    provider_name: str
    provider_type: str
    max_concurrent: int
    current_usage: int
    is_unlimited: bool
    base_url: str | None
    api_key_masked: str | None = None
    status: str
    last_checked_at: datetime | None
    quota_reset_at: datetime | None
    usage_pct: float = 0.0
    # T76 (2026-05-16): surfaces the most-recent auto-failure reason
    # (e.g. "no_enabled_models", "401 unauthorized on benchmark"). Set by
    # the benchmark loop, cleared on first success.
    last_error: str | None = None
    # T80 (2026-05-16): provider preference rank — 0=highest, larger
    # number = lower priority. Watchdog/router uses this to break ties
    # when multiple providers can serve a model.
    priority: int = 0
    # P1 (2026-09-01): provider tier label (NULL = untiered/dormant).
    tier: str | None = None


class ProviderCreate(BaseModel):
    provider_name: str
    provider_type: str = "openai-compatible"
    max_concurrent: int = 10
    is_unlimited: bool = False
    base_url: str | None = None
    api_key: str | None = None
    status: str = "online"
    # Routing rank (0 = highest). `create_provider` forwards this to the
    # OmniRoute mirror; ProviderCreate previously lacked it → AttributeError
    # on every create call (fixed 2026-08-25). Default 999 sorts new providers
    # below explicit owner-ranked ones until set.
    priority: int = 999
    # P1 (2026-09-01): provider tier label. Added 2026-09-22 — create_provider
    # has read payload.tier since the tier commit, so every POST 500'd without it.
    tier: str | None = None


class ProviderUpdate(BaseModel):
    max_concurrent: int | None = None
    current_usage: int | None = None
    is_unlimited: bool | None = None
    base_url: str | None = None
    api_key: str | None = None
    status: str | None = None
    provider_type: str | None = None
    quota_reset_at: datetime | None = None
    # T60 (2026-05-15): rename — rewrites the openclaw.json key + every
    # `<old>/...` prefix in agent primary/fallback chains atomically.
    new_provider_name: str | None = None
    # T80 (2026-05-16): provider routing priority — 0 = highest preference.
    priority: int | None = None
    # P1 (2026-09-01): provider tier label. Empty string clears it to NULL.
    tier: str | None = None


def _mask_key(key: str | None) -> str | None:
    if not key:
        return None
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:3] + "***" + key[-3:]


def _provider_to_read(p: ProviderCapacity) -> ProviderRead:
    usage_pct = 0.0
    if p.max_concurrent > 0 and not p.is_unlimited:
        usage_pct = (p.current_usage / p.max_concurrent) * 100
    return ProviderRead(
        id=p.id,
        provider_name=p.provider_name,
        provider_type=p.provider_type,
        max_concurrent=p.max_concurrent,
        current_usage=p.current_usage,
        is_unlimited=p.is_unlimited,
        base_url=p.base_url,
        api_key_masked=_mask_key(p.api_key),
        status=p.status,
        last_checked_at=p.last_checked_at,
        quota_reset_at=p.quota_reset_at,
        usage_pct=round(usage_pct, 1),
        last_error=p.last_error,
        priority=p.priority or 0,
        tier=p.tier,
    )


@router.get("", summary="List all provider capacities")
async def list_providers(
    db: AsyncSession = Depends(_db_dep),
) -> list[ProviderRead]:
    result = await db.execute(sa_select(ProviderCapacity).order_by(ProviderCapacity.provider_name))
    return [_provider_to_read(p) for p in result.scalars().all()]


@router.get(
    "/pick-for-complexity",
    summary="T80 — Pick best model for a given task complexity (L1-L5)",
)
async def pick_model_for_complexity(
    complexity: int = 3,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Returns the best-iq + highest-priority model that's online + not
    disabled. UI uses this to preview which model a task at level N would
    land on. Routing code can call the underlying service directly.
    """
    from app.services.model_router import pick_model_for_complexity as _pick
    res = await _pick(db, complexity=complexity)
    if res is None:
        return {"complexity": complexity, "picked": None}
    provider, model_id, iq = res
    return {
        "complexity": complexity,
        "picked": {"provider": provider, "model": model_id, "iq_score": iq},
    }


@router.post(
    "/eval",
    summary="T80 — Run IQ eval suite against every enabled model",
)
async def run_model_eval(
    only_provider: str | None = None,
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Owner-triggered: runs a 5-prompt suite against every enabled model
    and writes the 0-100 `iq_score` into `openclaw.json`. Long-running
    (depends on provider count + latency); the response includes the full
    per-model breakdown when done.
    """
    from app.services.model_eval import evaluate_all
    return await evaluate_all(only_provider=only_provider)


@router.post(
    "/benchmark-run",
    summary="T103 v34 — Trigger benchmark cycle now (skips offline providers)",
)
async def run_benchmark_now(
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Owner-triggered benchmark pass. Same logic as the loop's tick —
    iterates online providers + curated models, records TPS/duration/tokens.
    Fire-and-forget; returns immediately so a single run can't block the
    HTTP worker even when an upstream is slow."""
    import asyncio as _aio
    from app.services.provider_benchmark import benchmark_all_once
    _aio.create_task(benchmark_all_once())
    return {"started": True}


@router.post(
    "/benchmark-self",
    summary="Benchmark the live vllm model both ways (direct + omniroute-auto)",
)
async def run_self_benchmark(
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """On-demand: resolve the model vllm currently serves and benchmark it
    DIRECT (raw Jetson decode) AND via the omniroute-auto proxy (what agents
    see), writing a `provider_benchmark` row for each so the /benchmark page
    shows both. Runs synchronously (one generation per path, ~seconds) and
    returns the measured TPS. Use this instead of /benchmark-run when only one
    provider is online — the comparison loop skips with <2 providers."""
    from app.services.provider_benchmark import benchmark_self_once
    return await benchmark_self_once()


@router.get(
    "/benchmarks",
    summary="T76 — Latest benchmark TPS per (provider, model)",
)
async def get_benchmarks_top(
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Same as the later route, registered up-front to win over the
    catch-all `GET /{provider_name}` matcher below.
    """
    from app.models.provider_benchmark import ProviderBenchmark

    rows = (
        await db.execute(
            sa_select(ProviderBenchmark).order_by(ProviderBenchmark.ts.desc()).limit(2000),
        )
    ).scalars().all()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r.provider_name}::{r.model_id}"
        if key in latest:
            continue
        latest[key] = {
            "provider": r.provider_name,
            "model": r.model_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "duration_ms": r.duration_ms,
            "total_tokens": r.total_tokens,
            "tps": round(r.tps, 2),
            "ok": r.ok,
            "error": r.error,
        }
    return {"benchmarks": list(latest.values()), "count": len(latest)}


@router.get("/{provider_name}", summary="Get single provider detail")
async def get_provider(
    provider_name: str,
    db: AsyncSession = Depends(_db_dep),
) -> ProviderRead:
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    return _provider_to_read(p)


@router.post("", summary="Create a new provider", status_code=201)
async def create_provider(
    payload: ProviderCreate,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> ProviderRead:
    existing = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == payload.provider_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Provider '{payload.provider_name}' already exists")
    p = ProviderCapacity(
        provider_name=payload.provider_name,
        provider_type=payload.provider_type,
        max_concurrent=payload.max_concurrent,
        is_unlimited=payload.is_unlimited,
        base_url=payload.base_url,
        api_key=payload.api_key,
        status=payload.status,
        priority=payload.priority,
        tier=(payload.tier.strip() or None) if payload.tier else None,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    # Seed an empty `models.providers.<name>` block in openclaw.json so the
    # gateway sees the new provider on next reload. baseUrl is mirrored
    # there too; agents still need explicit `model.primary/fallbacks`
    # entries to actually use it.
    try:
        cfg = _load_cfg()
        block = _provider_models_block(cfg, payload.provider_name, create=True)
        if payload.base_url and block is not None:
            block["baseUrl"] = payload.base_url
        _save_cfg(cfg)
        _scrub_gateway_caches()
    except Exception as exc:
        logger.warning("provider_create.openclaw_json_sync_failed err=%s", exc)
    # T71: best-effort mirror to OmniRoute if configured.
    await _omniroute_sync_provider_upsert(
        payload.provider_name, payload.base_url, payload.api_key, payload.status,
        max_concurrent=payload.max_concurrent,
        priority=payload.priority,
    )
    # Validate openclaw.json refs against the updated registry. Non-blocking.
    try:
        from app.services.provider_registry_validator import warn_on_mapping_issues
        await warn_on_mapping_issues(db)
    except Exception as exc:
        logger.warning("provider_registry.validator_failed err=%s", exc)
    return _provider_to_read(p)


@router.patch("/{provider_name}", summary="Update provider capacity settings")
async def update_provider(
    provider_name: str,
    payload: ProviderUpdate,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> ProviderRead:
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

    prev_status = p.status
    prev_base_url = p.base_url
    prev_name = p.provider_name

    if payload.max_concurrent is not None:
        p.max_concurrent = payload.max_concurrent
    if payload.current_usage is not None:
        p.current_usage = payload.current_usage
    if payload.is_unlimited is not None:
        p.is_unlimited = payload.is_unlimited
    if payload.base_url is not None:
        p.base_url = payload.base_url
    if payload.api_key is not None:
        p.api_key = payload.api_key
    if payload.status is not None:
        p.status = payload.status
        # T79 (2026-05-16): owner-driven online toggle clears the auto-disable
        # error so the rose banner disappears immediately. Benchmark loop will
        # re-flip on actual failure if the upstream is still broken; no harm in
        # clearing here since the loop's own re-enable logic only gates on
        # `last_error IS NOT NULL` so owner manual offlines stay sticky.
        if payload.status == "online":
            p.last_error = None
    if payload.priority is not None:
        p.priority = payload.priority
    if payload.tier is not None:
        # empty string clears the tier back to NULL (untiered/dormant)
        p.tier = payload.tier.strip() or None
    if payload.provider_type is not None:
        p.provider_type = payload.provider_type
    if payload.quota_reset_at is not None:
        p.quota_reset_at = payload.quota_reset_at
    if payload.new_provider_name is not None and payload.new_provider_name != prev_name:
        clash = await db.execute(
            sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == payload.new_provider_name)
        )
        if clash.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Provider '{payload.new_provider_name}' already exists",
            )
        p.provider_name = payload.new_provider_name

    p.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(p)

    # T60: sync the openclaw.json side. baseUrl change is mirrored into
    # `models.providers.<name>.baseUrl`. Rename rewrites the key + every
    # `<old>/...` prefix in primary/fallbacks. Status flip propagates to
    # fallback chains (T58 semantics preserved).
    cfg = _load_cfg()
    cfg_dirty = False

    rename_count = 0
    if payload.new_provider_name is not None and payload.new_provider_name != prev_name:
        rename_count = _rename_provider_in_cfg(cfg, prev_name, payload.new_provider_name)
        cfg_dirty = cfg_dirty or rename_count > 0
        # T89: port the MC-only sidecar key so iq_score / disabled state
        # survives the rename.
        from app.services.mc_sidecar import rename_provider as _rename_sidecar
        _rename_sidecar(prev_name, payload.new_provider_name)

    effective_name = payload.new_provider_name or prev_name
    if payload.base_url is not None and payload.base_url != prev_base_url:
        block = _provider_models_block(cfg, effective_name, create=True)
        if block is not None:
            block["baseUrl"] = payload.base_url
            cfg_dirty = True

    if cfg_dirty:
        try:
            _save_cfg(cfg)
        except Exception as exc:
            logger.warning("provider_update.openclaw_json_sync_failed err=%s", exc)

    # T58: status flip → propagate to openclaw.json + scrub session caches.
    if payload.status is not None and payload.status != prev_status:
        if payload.status == "offline":
            counters = _strip_provider_from_fallbacks(effective_name)
            scrubbed = _scrub_gateway_caches()
            logger.info(
                "provider_routing.offline provider=%s fallbacks_stripped=%d primary_warnings=%d sessions_scrubbed=%d",
                effective_name, counters["stripped"], counters["primary_warnings"], scrubbed,
            )
        else:
            scrubbed = _scrub_gateway_caches()
            logger.info(
                "provider_routing.online provider=%s sessions_scrubbed=%d",
                effective_name, scrubbed,
            )
    elif cfg_dirty:
        # Even without status flip, baseUrl/rename changed the routing —
        # scrub session caches so gateway re-reads.
        _scrub_gateway_caches()
        if rename_count > 0:
            logger.info(
                "provider_routing.renamed old=%s new=%s refs_rewritten=%d",
                prev_name, effective_name, rename_count,
            )

    # T71: best-effort mirror to OmniRoute. On rename we delete the old
    # connection then upsert the new — OmniRoute has no native rename API.
    if payload.new_provider_name is not None and payload.new_provider_name != prev_name:
        await _omniroute_sync_provider_delete(prev_name)
    await _omniroute_sync_provider_upsert(
        effective_name, p.base_url, p.api_key, p.status,
        max_concurrent=p.max_concurrent,
        priority=p.priority,
    )


    return _provider_to_read(p)


@router.delete("/{provider_name}", summary="Delete a provider")
async def delete_provider(
    provider_name: str,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    await db.delete(p)
    await db.commit()
    # Cascade openclaw.json: remove the provider entry + strip from agent chains.
    try:
        cfg = _load_cfg()
        providers = cfg.get("models", {}).get("providers", {})
        removed = providers.pop(provider_name, None) is not None
        _strip_provider_from_fallbacks(provider_name)
        # Also clear primary references (rare but if present, agents would
        # have dangling primaries pointing at a deleted provider).
        prefix = f"{provider_name}/"
        for block in [cfg.get("agents", {}).get("defaults", {}).get("model")] + [
            a.get("model") for a in cfg.get("agents", {}).get("list", []) or []
        ]:
            if isinstance(block, dict):
                primary = block.get("primary")
                if isinstance(primary, str) and primary.startswith(prefix):
                    block["primary"] = ""
        if removed:
            _save_cfg(cfg)
        _scrub_gateway_caches()
    except Exception as exc:
        logger.warning("provider_delete.openclaw_json_sync_failed err=%s", exc)
    # T71: best-effort mirror — drop from OmniRoute too.
    await _omniroute_sync_provider_delete(provider_name)
    return {"deleted": provider_name}


# ── Model CRUD per provider ─────────────────────────────────────────────
# T60 (2026-05-15): UI-driven add/edit/delete of models exposed by a
# provider. Source of truth is `~/.openclaw/openclaw.json`
# `models.providers.<name>.models` — gateway reads from there on reload.


@router.get("/{provider_name}/models", summary="List curated models for a provider")
async def list_provider_models(
    provider_name: str,
    include_available: bool = False,
    db: AsyncSession = Depends(_db_dep),
) -> list[dict[str, Any]]:
    """T103 v5 (2026-05-17): default returns curated models only
    (owner-added). Pass `?include_available=true` to also return the
    upstream's full /v1/models catalog (for picking new additions).
    """
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    from app.services import omniroute_sync
    from app.services.mc_sidecar import merge_into_model_entry
    models = await omniroute_sync.list_models_for_provider(
        provider_name, include_available=include_available,
    )
    return [merge_into_model_entry(provider_name, m) for m in models]


@router.post("/{provider_name}/models", summary="Add a model to a provider", status_code=201)
async def add_provider_model(
    provider_name: str,
    payload: ModelEntry,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """T103 v3 (2026-05-17): POST to OmniRoute /api/provider-models.

    MC-only metadata (iq_score, disabled, ...) goes to sidecar; the model
    record itself is owned by OmniRoute.
    """
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

    entry = payload.model_dump()
    if not entry.get("name"):
        entry["name"] = entry["id"]

    from app.services.mc_sidecar import set_model_meta, merge_into_model_entry
    from app.services import omniroute_sync

    _MC_KEYS = {"iq_score", "iq_detail", "iq_evaluated_at",
                "disabled", "disabled_reason", "disabled_at"}
    mc_meta: dict[str, Any] = {}
    for k in list(entry.keys()):
        if k in _MC_KEYS:
            mc_meta[k] = entry.pop(k)

    ok, resp = await omniroute_sync.add_model_for_provider(
        provider_name,
        entry["id"],
        api_format=entry.get("api") or "chat-completions",
        supported_endpoints=["chat"],
    )
    if not ok:
        if isinstance(resp, dict) and "exists" in str(resp).lower():
            raise HTTPException(
                status_code=409,
                detail=f"Model '{entry['id']}' already exists on '{provider_name}'",
            )
        raise HTTPException(
            status_code=502,
            detail=f"OmniRoute model add failed: {resp}",
        )
    if mc_meta:
        set_model_meta(provider_name, entry["id"], **mc_meta)
    logger.info(
        "provider_models.added provider=%s model=%s mc_meta=%s (via OmniRoute)",
        provider_name, entry["id"], list(mc_meta.keys()),
    )
    return merge_into_model_entry(provider_name, {"id": entry["id"], "name": entry["name"]})


@router.patch("/{provider_name}/models/{model_id:path}", summary="Update a model entry")
async def update_provider_model(
    provider_name: str,
    model_id: str,
    payload: ModelEntryUpdate,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """T103 v3 (2026-05-17): only MC-only metadata (iq_score, disabled,
    ...) is patchable. Model presence + supported endpoints are owned
    by OmniRoute and require delete + re-add (out of UI scope).
    """
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    from app.services import omniroute_sync
    from app.services.mc_sidecar import set_model_meta, merge_into_model_entry

    or_models = await omniroute_sync.list_models_for_provider(provider_name)
    existing = next((m for m in or_models if m.get("id") == model_id), None)
    if not existing:
        raise HTTPException(status_code=404,
            detail=f"Model '{model_id}' not found on '{provider_name}'")

    updates = payload.model_dump(exclude_unset=True)
    _MC_KEYS = {"iq_score", "iq_detail", "iq_evaluated_at",
                "disabled", "disabled_reason", "disabled_at"}
    mc_meta: dict[str, Any] = {}
    for k in list(updates.keys()):
        if k in _MC_KEYS:
            mc_meta[k] = updates.pop(k)
    if updates:
        logger.warning(
            "provider_models.update.gateway_fields_ignored provider=%s model=%s "
            "fields=%s (owned by OmniRoute — delete + re-add to change)",
            provider_name, model_id, list(updates.keys()),
        )
    if mc_meta:
        set_model_meta(provider_name, model_id, **mc_meta)
    logger.info(
        "provider_models.updated provider=%s model=%s mc_fields=%s",
        provider_name, model_id, list(mc_meta.keys()),
    )
    return merge_into_model_entry(provider_name, existing)


@router.delete("/{provider_name}/models/{model_id:path}", summary="Remove a model")
async def delete_provider_model(
    provider_name: str,
    model_id: str,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """T103 v3 (2026-05-17): DELETE on OmniRoute /api/provider-models +
    drop MC sidecar metadata."""
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    from app.services import omniroute_sync
    from app.services.mc_sidecar import clear_model_meta

    ok = await omniroute_sync.delete_model_for_provider(provider_name, model_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_id}' not found on '{provider_name}' (OmniRoute)",
        )
    clear_model_meta(provider_name, model_id)
    cfg = _load_cfg()

    # T103: strip `omniroute/<prefix>/<model_id>` from agent chains.
    # Prefix = lowercased provider_name (OmniRoute node prefix convention).
    full_ref = f"omniroute/{provider_name.lower()}/{model_id}"
    refs_rewritten = 0
    for block_check in [cfg.get("agents", {}).get("defaults", {}).get("model")] + [
        a.get("model") for a in cfg.get("agents", {}).get("list", []) or []
    ]:
        if not isinstance(block_check, dict):
            continue
        if block_check.get("primary") == full_ref:
            block_check["primary"] = ""
            refs_rewritten += 1
        fbs = block_check.get("fallbacks")
        if isinstance(fbs, list):
            before_fbs = len(fbs)
            block_check["fallbacks"] = [f for f in fbs if f != full_ref]
            refs_rewritten += before_fbs - len(block_check["fallbacks"])

    _save_cfg(cfg)
    _scrub_gateway_caches()
    logger.info(
        "provider_models.deleted provider=%s model=%s refs_rewritten=%d",
        provider_name, model_id, refs_rewritten,
    )
    return {"deleted": model_id, "refs_rewritten": refs_rewritten}


@router.get(
    "/{provider_name}/discover-models",
    summary="T76 — Discover available model IDs from a provider's /v1/models endpoint",
)
async def discover_provider_models(
    provider_name: str,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Call the upstream provider's `<base>/models` endpoint and return the
    raw model-id list. Frontend uses this to populate the "add model"
    dropdown so the owner doesn't have to type model ids verbatim.
    """
    import httpx

    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    if not p.base_url:
        raise HTTPException(status_code=400, detail="Provider has no base_url")

    url = f"{p.base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if p.api_key and p.api_key.lower() not in ("noapikey", "none", ""):
        headers["Authorization"] = f"Bearer {p.api_key}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream error: {type(exc).__name__}: {exc}")

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream returned {r.status_code}: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    ids: list[str] = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        for item in items:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name") or item.get("model")
                if mid:
                    ids.append(str(mid))
            elif isinstance(item, str):
                ids.append(item)
    elif isinstance(data, list):
        ids = [str(x.get("id") if isinstance(x, dict) else x) for x in data if x]

    return {"provider": provider_name, "models": ids, "count": len(ids)}


@router.get(
    "/benchmarks",
    summary="T76 — Latest benchmark TPS per (provider, model)",
)
async def get_benchmarks(
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Return the most-recent benchmark row per (provider, model).
    Used by the providers UI to render TPS chips.
    """
    from app.models.provider_benchmark import ProviderBenchmark

    rows = (
        await db.execute(
            sa_select(ProviderBenchmark).order_by(ProviderBenchmark.ts.desc()).limit(2000),
        )
    ).scalars().all()

    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r.provider_name}::{r.model_id}"
        if key in latest:
            continue
        latest[key] = {
            "provider": r.provider_name,
            "model": r.model_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "duration_ms": r.duration_ms,
            "total_tokens": r.total_tokens,
            "tps": round(r.tps, 2),
            "ok": r.ok,
            "error": r.error,
        }
    return {"benchmarks": list(latest.values()), "count": len(latest)}


@router.get("/capacity-check", summary="Check if a provider has available capacity")
async def check_capacity(
    provider_name: str = "ollama",
    db: AsyncSession = Depends(_db_dep),
) -> dict[str, Any]:
    result = await db.execute(
        sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
    )
    p = result.scalar_one_or_none()
    if not p:
        return {"available": True, "remaining": None, "max": None, "provider": provider_name, "not_found": True}

    if p.is_unlimited:
        return {"available": True, "remaining": 999, "max": p.max_concurrent, "provider": provider_name}

    remaining = max(0, p.max_concurrent - p.current_usage)
    return {
        "available": remaining > 0,
        "remaining": remaining,
        "max": p.max_concurrent,
        "provider": provider_name,
        "usage_pct": round((p.current_usage / p.max_concurrent) * 100, 1) if p.max_concurrent > 0 else 0,
    }
