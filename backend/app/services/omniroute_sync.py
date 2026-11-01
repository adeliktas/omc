"""OmniRoute model-registry pull (T103, 2026-05-17).

OmniRoute is the LLM gateway for all agent inference. Models are
registered upstream (vllm/ollama-cloud/LMStudio) and exposed via
`GET /v1/models` on the OmniRoute runtime port.

This module fetches that catalog on demand so MC can:
- Populate agent-provisioning model dropdowns with what OmniRoute can
  actually serve
- Route by complexity tier with a known model name
- Detect drift between agent.preferred_model and OmniRoute's catalog

Auth uses the gateway-runtime API key (Bearer). Cache TTL 60s to avoid
hammering OmniRoute when many agents poll concurrently.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_OMNIROUTE_RUNTIME_URL = os.environ.get(
    "OMNIROUTE_RUNTIME_URL", "http://omniroute:20129/v1",
)
_OMNIROUTE_RUNTIME_KEY = os.environ.get("OMNIROUTE_RUNTIME_KEY", "")

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, Any] = {"fetched_at": 0.0, "models": []}
_curated_cache: dict[str, Any] = {"fetched_at": 0.0, "models": []}
_cache_lock = asyncio.Lock()


async def list_models(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return OmniRoute FULL model catalog (synced-available + /v1/models).
    Cached for 60s. Use for capability checks (`model_exists`), NOT for
    model SELECTION — selection must respect owner curation, see
    `list_curated_models`."""
    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _cache["models"]
            and now - _cache["fetched_at"] < _CACHE_TTL_SECONDS
        ):
            return list(_cache["models"])
        models = await _fetch_models()
        _cache["fetched_at"] = now
        _cache["models"] = models
        return models


async def list_curated_models(force_refresh: bool = False) -> list[dict[str, Any]]:
    """T108 (2026-05-30): return ONLY owner-curated models — the set the
    owner selected per provider in the /providers tab (OmniRoute
    `/api/provider-models`). This is the authoritative selection set.

    Why this exists: the model-selection pickers (complexity assessor,
    tier fallback) previously iterated `list_models()` = the FULL upstream
    catalog (e.g. 39 ollama models). With curated models lacking IQ scores,
    the IQ floor dropped them and the picker fell through to UNcurated
    models (cogito-2.1, deepseek-v3.1) — owner saw random models running.
    Selection now draws from THIS curated set only, giving full control +
    consistency. Same shape as `list_models`: [{id, name}, ...]."""
    async with _cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _curated_cache["models"]
            and now - _curated_cache["fetched_at"] < _CACHE_TTL_SECONDS
        ):
            return list(_curated_cache["models"])
        models = await _fetch_curated_models()
        _curated_cache["fetched_at"] = now
        _curated_cache["models"] = models
        return models


async def _fetch_curated_models() -> list[dict[str, Any]]:
    """Aggregate `/api/provider-models` (curated, manually-added) across all
    OmniRoute provider-nodes. NO synced-available, NO /v1/models merge —
    those would re-introduce uncurated models."""
    import os as _os
    mgmt_token = _self_mint_token()
    mgmt_url = _os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
    if not mgmt_token:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as c:
            origin = mgmt_url.replace("//omniroute:", "//localhost:")
            headers = {
                "Cookie": f"auth_token={mgmt_token}",
                "Origin": origin,
                "Referer": f"{origin}/dashboard",
            }
            nodes_r = await c.get(mgmt_url.rstrip("/") + "/api/provider-nodes", headers=headers)
            if nodes_r.status_code != 200:
                logger.warning("omniroute_sync.curated_nodes_failed status=%d", nodes_r.status_code)
                return []
            nodes = (nodes_r.json() or {}).get("nodes", []) or []
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for n in nodes:
                nid = n.get("id")
                prefix = n.get("prefix") or n.get("name", "").lower()
                if not nid:
                    continue
                try:
                    mr = await c.get(
                        mgmt_url.rstrip("/") + f"/api/provider-models?provider={nid}",
                        headers=headers,
                    )
                    if mr.status_code != 200:
                        continue
                    body = mr.json() or {}
                    items = body.get("models", body) if isinstance(body, dict) else body
                    for m in (items or []):
                        raw_id = m.get("modelId") or m.get("id") or ""
                        if not raw_id:
                            continue
                        full = raw_id if raw_id.startswith(prefix + "/") else f"{prefix}/{raw_id}"
                        if full in seen:
                            continue
                        seen.add(full)
                        out.append({"id": full, "name": raw_id})
                except Exception as e:
                    logger.debug("omniroute_sync.curated_per_node_err node=%s err=%s", prefix, e)
            return out
    except Exception as exc:
        logger.warning("omniroute_sync.curated_aggregate_error err=%s", exc)
        return []


async def _fetch_models() -> list[dict[str, Any]]:
    """T103 v2: aggregate synced-available-models across all provider-nodes.

    OmniRoute's `/v1/models` endpoint hides OpenAI-Compatible nodes from
    the aggregated list (only shows typed providers). The dashboard
    /api/synced-available-models?provider=<node-id> per node gives the
    full cached catalog. Concat + dedup.
    """
    import os as _os
    mgmt_token = _self_mint_token()
    mgmt_url = _os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
    if not mgmt_token:
        # Fallback to /v1/models (typed providers only).
        return await _fetch_v1_models()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as c:
            origin = mgmt_url.replace("//omniroute:", "//localhost:")
            headers = {
                "Cookie": f"auth_token={mgmt_token}",
                "Origin": origin,
                "Referer": f"{origin}/dashboard",
            }
            nodes_r = await c.get(mgmt_url.rstrip("/") + "/api/provider-nodes", headers=headers)
            if nodes_r.status_code != 200:
                logger.warning("omniroute_sync.nodes_failed status=%d", nodes_r.status_code)
                return await _fetch_v1_models()
            nodes = (nodes_r.json() or {}).get("nodes", []) or []
            all_models: list[dict[str, Any]] = []
            seen: set[str] = set()
            for n in nodes:
                nid = n.get("id")
                prefix = n.get("prefix") or n.get("name", "").lower()
                if not nid:
                    continue
                try:
                    mr = await c.get(
                        mgmt_url.rstrip("/")
                        + f"/api/synced-available-models?provider={nid}",
                        headers=headers,
                    )
                    if mr.status_code != 200:
                        continue
                    body = mr.json() or {}
                    for m in body.get("models", []) or []:
                        raw_id = m.get("id") or m.get("modelId") or ""
                        if not raw_id:
                            continue
                        full = f"{prefix}/{raw_id}" if not raw_id.startswith(prefix + "/") else raw_id
                        if full in seen:
                            continue
                        seen.add(full)
                        all_models.append({"id": full, "name": raw_id})
                except Exception as e:
                    logger.debug("omniroute_sync.per_node_err node=%s err=%s", prefix, e)
            # Merge with /v1/models for completeness
            v1 = await _fetch_v1_models()
            for m in v1:
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    all_models.append(m)
            return all_models
    except Exception as exc:
        logger.warning("omniroute_sync.aggregate_error err=%s", exc)
        return await _fetch_v1_models()


# ── T103 v3 (2026-05-17): OmniRoute model CRUD wrappers ─────────────
# MC's /providers/<name>/models endpoints proxy directly to OmniRoute's
# /api/provider-models. MC remains the UI, OmniRoute owns the storage.

import os as _os

_MGMT_URL = _os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")


def _self_mint_token() -> str:
    """T110: self-mint a valid OmniRoute mgmt JWT (traffic_etl._mgmt_token
    pattern) — the static OMNIROUTE_MGMT_TOKEN env expires every 30d and
    is only the enable-gate/fallback. Local import avoids import cycles.
    """
    try:
        from app.services.traffic_etl import _mgmt_token
        return _mgmt_token()
    except Exception:
        return _os.environ.get("OMNIROUTE_MGMT_TOKEN", "")


async def _mgmt_headers(*, write: bool = True) -> dict[str, str]:
    # OmniRoute rule: WRITE endpoints reject the localhost-rewritten Origin
    # (403 INVALID_ORIGIN) — writes are Cookie-ONLY. GETs may carry Origin.
    origin = _MGMT_URL.replace("//omniroute:", "//localhost:")
    h = {
        "Cookie": f"auth_token={_self_mint_token()}",
        "Content-Type": "application/json",
    }
    if not write:
        h["Origin"] = origin
        h["Referer"] = f"{origin}/dashboard"
    return h


async def find_node_id_by_name(provider_name: str) -> str | None:
    if not _self_mint_token():
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                _MGMT_URL.rstrip("/") + "/api/provider-nodes",
                headers=await _mgmt_headers(write=False),
            )
        if r.status_code != 200:
            return None
        for n in (r.json() or {}).get("nodes", []) or []:
            if n.get("name") == provider_name:
                return n.get("id")
    except Exception as exc:
        logger.warning("omniroute_sync.find_node_err name=%s err=%s", provider_name, exc)
    return None


async def list_models_for_provider(
    provider_name: str,
    *,
    include_available: bool = False,
) -> list[dict[str, Any]]:
    """Return curated models for the OmniRoute node matching provider_name.

    T103 v5 (2026-05-17): default = curated only (`/api/provider-models`
    = manually-added). Set `include_available=True` to also include
    `/api/synced-available-models` (upstream's full catalog, deduped).
    Owner UI shows curated by default; uses available for picking new
    additions.
    """
    nid = await find_node_id_by_name(provider_name)
    if not nid:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    paths = [f"/api/provider-models?provider={nid}"]
    if include_available:
        paths.append(f"/api/synced-available-models?provider={nid}")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            h = await _mgmt_headers(write=False)
            for path in paths:
                r = await c.get(_MGMT_URL.rstrip("/") + path, headers=h)
                if r.status_code != 200:
                    continue
                body = r.json() or {}
                for m in body.get("models", []) or []:
                    mid = m.get("id") or m.get("modelId")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    out.append({
                        "id": mid,
                        "name": m.get("name") or mid,
                        "source": m.get("source") or "synced",
                        "apiFormat": m.get("apiFormat"),
                        "supportedEndpoints": m.get("supportedEndpoints"),
                    })
    except Exception as exc:
        logger.warning("omniroute_sync.list_models_err provider=%s err=%s", provider_name, exc)
    return out


async def add_model_for_provider(
    provider_name: str,
    model_id: str,
    *,
    api_format: str = "chat-completions",
    supported_endpoints: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """POST /api/provider-models on the matching OmniRoute node."""
    nid = await find_node_id_by_name(provider_name)
    if not nid:
        return False, {"error": f"node '{provider_name}' missing in OmniRoute"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                _MGMT_URL.rstrip("/") + "/api/provider-models",
                headers=await _mgmt_headers(),
                json={
                    "provider": nid,
                    "modelId": model_id,
                    "apiFormat": api_format,
                    "supportedEndpoints": supported_endpoints or ["chat"],
                },
            )
        if r.status_code in (200, 201):
            return True, (r.json() or {}).get("model", {})
        return False, {"status": r.status_code, "body": r.text[:200]}
    except Exception as exc:
        return False, {"error": str(exc)}


async def list_connections() -> list[dict[str, Any]]:
    """GET /api/providers — all OmniRoute connections."""
    if not _self_mint_token():
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                _MGMT_URL.rstrip("/") + "/api/providers",
                headers=await _mgmt_headers(),
            )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("connections", []) or []
    except Exception as exc:
        logger.warning("omniroute_sync.list_connections_err err=%s", exc)
        return []


def _omniroute_status_to_mc(c: dict[str, Any]) -> str:
    """Map OmniRoute connection → MC status. Mirror **isActive** (the owner
    on/off toggle in dashboard tile), NOT testStatus (independent runtime probe
    result: cooldown, credits_exhausted, etc.). testStatus surfaces as
    last_error for diagnostics but doesn't change MC's online/offline.
    """
    return "online" if c.get("isActive", True) else "offline"


def _omniroute_test_status_to_error(c: dict[str, Any]) -> str | None:
    """testStatus → MC `last_error` (diagnostic only, doesn't gate routing)."""
    ts = (c.get("testStatus") or "").lower()
    if ts in ("", "active", "online"):
        return None
    return ts  # 'inactive' / 'credits_exhausted' / 'rate_limited' / 'error'


async def _list_nodes_by_name() -> dict[str, dict[str, Any]]:
    """T103 v15 (2026-05-19): map provider_name → node dict (id, baseUrl, ...).

    Used by reverse-sync to mirror OR-side baseUrl edits back into MC
    `provider_capacity.base_url`. The connection record only carries a
    cosmetic copy of baseUrl in `providerSpecificData`; the *routing*
    baseUrl lives on the node. Reverse-sync therefore must read the node,
    not the connection.
    """
    if not _self_mint_token():
        return {}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                _MGMT_URL.rstrip("/") + "/api/provider-nodes",
                headers=await _mgmt_headers(write=False),
            )
        if r.status_code != 200:
            return {}
        return {
            (n.get("name") or ""): n
            for n in (r.json() or {}).get("nodes", []) or []
            if n.get("name")
        }
    except Exception as exc:
        logger.warning("omniroute_sync.list_nodes_err err=%s", exc)
        return {}


async def reverse_sync_to_mc_db() -> int:
    """Pull OmniRoute connection state, write into MC `provider_capacity.status`.

    Used by watchdog to keep MC `/providers` UI mirrored with OmniRoute
    (so external dashboard edits, cooldowns, credit-exhausted flags etc.
    surface in MC). Returns number of rows updated.
    """
    if not _self_mint_token():
        return 0
    try:
        from sqlalchemy import update as sa_update

        from app.db.session import async_session_maker as _asm
        from app.models.provider_capacity import ProviderCapacity
    except Exception as exc:
        logger.warning("omniroute_sync.reverse_sync_import_err err=%s", exc)
        return 0

    conns = await list_connections()
    if not conns:
        return 0
    nodes_by_name = await _list_nodes_by_name()
    # T103 v18 (2026-05-19): SYMMETRIC bi-directional sync for all common
    # provider fields. Last-write-wins per field. No asymmetric guards
    # (owner intent is captured by the most recent edit, regardless of
    # side). Fields mirrored:
    #   isActive      → status            (online / offline)
    #   maxConcurrent → max_concurrent
    #   priority      → priority          (prefers `priority` over `globalPriority`)
    #   rateLimitedUntil → quota_reset_at
    #   lastTested    → last_checked_at
    #   testStatus    → last_error        (diagnostic; doesn't gate routing)
    #   node.baseUrl  → base_url          (routing target — node-level, not connection-level)
    # apiKey is NOT reverse-synced: OR masks it in GET responses, so we
    # cannot read it back. apiKey remains forward-only (MC → OR).
    from datetime import datetime as _dt

    from sqlalchemy import select as _sa_sel
    updated = 0

    def _parse_iso(s: Any) -> _dt | None:
        if not s or not isinstance(s, str):
            return None
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    # T104 round-4 (2026-05-24): true bidirectional last-write-wins.
    # Previous behavior: reverse-sync clobbered MC-side edits regardless of
    # timestamp (max_concurrent bump via `/providers` UI reverted to OR's
    # value within 10 min). Now: per-row, skip reverse-sync if MC's
    # `updated_at` is newer than OR's `updatedAt`. Forward-sync from MC UI
    # advances OR's `updatedAt` so the next reverse-tick sees OR as fresher
    # and starts mirroring again.
    from datetime import timezone as _tz

    def _aware(d: _dt | None) -> _dt | None:
        if d is None:
            return None
        return d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d

    async with _asm() as db:
        rows = (await db.execute(_sa_sel(ProviderCapacity))).scalars().all()
        existing = {r.provider_name: r for r in rows}
        for c in conns:
            name = c.get("name")
            if not name or name not in existing:
                continue
            row = existing[name]
            values: dict[str, Any] = {}

            # T104 round-4: compare MC-row updated_at vs OR connection updatedAt.
            mc_ts = _aware(row.updated_at)
            or_ts = _aware(_parse_iso(c.get("updatedAt")))
            mc_is_fresher = (
                mc_ts is not None
                and or_ts is not None
                and mc_ts > or_ts
            )
            if mc_is_fresher:
                logger.debug(
                    "omniroute_sync.reverse_skip_mc_fresher provider=%s mc=%s or=%s",
                    name, mc_ts.isoformat(), or_ts.isoformat(),
                )
                continue

            # An explicit MC disable is stronger than OmniRoute's binary online/offline.
            or_status = _omniroute_status_to_mc(c)
            if row.status != "disabled" and or_status != row.status:
                values["status"] = or_status
                logger.info(
                    "omniroute_sync.reverse_status_mirrored provider=%s old=%s new=%s",
                    name, row.status, or_status,
                )

            # last_error always mirrored (diagnostic only)
            new_err = _omniroute_test_status_to_error(c)
            if new_err != row.last_error:
                values["last_error"] = new_err

            # baseUrl (node-level; routing target)
            node = nodes_by_name.get(name)
            or_base = node.get("baseUrl") if node else None
            if or_base and or_base != row.base_url:
                values["base_url"] = or_base
                logger.info(
                    "omniroute_sync.reverse_baseurl_mirrored provider=%s old=%s new=%s",
                    name, row.base_url, or_base,
                )

            # max_concurrent — T107 round-4 (2026-05-26): OWNER-AUTHORITATIVE.
            # Owner sets this in /providers UI to match their account plan
            # (ollama-cloud free=3, paid=10, etc). Reverse-sync from OR
            # used to clobber it whenever OR's per-connection counter
            # drifted. No more — MC is the source of truth for capacity.
            # Forward-sync still pushes MC's value to OR (one-way).

            # priority — prefer `globalPriority` (cross-provider routing rank
            # set by owner) over `priority` (auto-renumbered per-node by
            # OR's `_reorderConnections` so it's always 1 for single-conn
            # nodes, never owner-controlled).
            # Translate OR (1-based, >=1) → MC (0-based) by subtracting 1.
            # Forward sync `_omniroute_sync_provider_upsert` does the inverse.
            or_prio_raw = c.get("globalPriority")
            if or_prio_raw is None:
                or_prio_raw = c.get("priority")
            if isinstance(or_prio_raw, int):
                or_prio = max(0, or_prio_raw - 1)
                if or_prio != row.priority:
                    values["priority"] = or_prio
                    logger.info(
                        "omniroute_sync.reverse_priority_mirrored provider=%s old=%s new=%s (or=%d)",
                        name, row.priority, or_prio, or_prio_raw,
                    )

            # quota_reset_at ← rateLimitedUntil
            or_quota = _parse_iso(c.get("rateLimitedUntil"))
            if or_quota != row.quota_reset_at:
                values["quota_reset_at"] = or_quota

            # last_checked_at ← lastTested (OmniRoute's probe timestamp)
            or_last = _parse_iso(c.get("lastTested")) or _parse_iso(c.get("updatedAt"))
            if or_last and or_last != row.last_checked_at:
                values["last_checked_at"] = or_last

            if values:
                await db.execute(
                    sa_update(ProviderCapacity)
                    .where(ProviderCapacity.provider_name == name)
                    .values(**values)
                )
                updated += 1
        await db.commit()
    logger.debug("omniroute_sync.reverse_sync_done rows=%d (symmetric)", updated)
    return updated


async def delete_model_for_provider(provider_name: str, model_id: str) -> bool:
    nid = await find_node_id_by_name(provider_name)
    if not nid:
        return False
    try:
        from urllib.parse import quote

        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            # OmniRoute DELETE query param is `model`, not `modelId`.
            r = await c.delete(
                _MGMT_URL.rstrip("/")
                + f"/api/provider-models?provider={nid}&model={quote(model_id, safe='')}",
                headers=await _mgmt_headers(),
            )
        return 200 <= r.status_code < 300
    except Exception as exc:
        logger.warning("omniroute_sync.delete_err provider=%s model=%s err=%s",
                       provider_name, model_id, exc)
        return False


async def _fetch_v1_models() -> list[dict[str, Any]]:
    if not _OMNIROUTE_RUNTIME_KEY:
        return []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                _OMNIROUTE_RUNTIME_URL.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {_OMNIROUTE_RUNTIME_KEY}"},
            )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data", []) or []
    except Exception:
        return []


async def list_model_ids() -> list[str]:
    """Convenience: just the canonical model id strings."""
    models = await list_models()
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


async def model_exists(model_id: str) -> bool:
    """Check whether OmniRoute can serve `model_id` right now."""
    ids = await list_model_ids()
    return model_id in ids
