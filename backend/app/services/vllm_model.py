"""Dynamic vllm model resolution.

The Jetson vllm serves exactly ONE model at a time, and the owner swaps it
(e.g. Qwen3.6-35B-A3B-FP8 → Qwen3.8-27B-FP8). NEVER hardcode the model id —
resolve it live from vllm's `/v1/models` (same as `curl .../v1/models`), cache
it briefly, and fall back safely. Everything that needs a concrete vllm model
id calls `resolve_vllm_model()`; callers that route via OmniRoute auto-combo can
keep using bare `omniroute-auto` (OmniRoute autoSync already tracks vllm).
"""

from __future__ import annotations

import os
import time

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

# `VLLM_HEALTH_URL` already points at vllm's `/v1/models` in the backend env.
_MODELS_URL = os.environ.get("VLLM_HEALTH_URL", "")  # unset: no local vLLM
# Optional last-resort model name (bare, no prefix) if vllm is unreachable AND
# nothing was cached yet. Empty → fall back to `omniroute-auto` which never 404s.
_FALLBACK = os.environ.get("VLLM_MODEL_FALLBACK", "").strip()
_TTL_SECONDS = float(os.environ.get("VLLM_MODEL_TTL_SECONDS", "300"))

_cache: dict[str, object] = {"model": None, "ctx": None, "ts": 0.0}


def _parse_first_entry(payload: object) -> tuple[str | None, int | None]:
    """Return (model_id, max_model_len) of vllm's first served model."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, None
    for entry in data:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid.strip():
                ctx = entry.get("max_model_len") or entry.get("context_length")
                try:
                    ctx = int(ctx) if ctx else None
                except (TypeError, ValueError):
                    ctx = None
                return mid.strip(), ctx
    return None, None


def _fetch_live() -> tuple[str | None, int | None]:
    try:
        r = httpx.get(_MODELS_URL, timeout=6)
        if r.status_code != 200:
            return None, None
        return _parse_first_entry(r.json())
    except Exception as exc:  # noqa: BLE001 — network best-effort
        logger.debug("vllm_model.fetch_failed url=%s err=%s", _MODELS_URL, exc)
        return None, None


def _refresh(force: bool) -> None:
    """Refresh the (model, ctx) cache from vllm when stale/forced."""
    now = time.monotonic()
    cached = _cache.get("model")
    ts = float(_cache.get("ts") or 0.0)
    if force or not cached or (now - ts) > _TTL_SECONDS:
        model, ctx = _fetch_live()
        if model:
            _cache["model"] = model
            if ctx:
                _cache["ctx"] = ctx
            _cache["ts"] = now


def current_vllm_model(*, force: bool = False) -> str | None:
    """Return the bare model id vllm currently serves (e.g. `Qwen3.8-27B-FP8`),
    or the last cached value, or the configured fallback, or None. Cached for
    `_TTL_SECONDS`; a successful fetch always refreshes the cache."""
    _refresh(force)
    cached = _cache.get("model")
    if cached:
        return str(cached)
    return _FALLBACK or None


def current_vllm_context_length(*, force: bool = False) -> int | None:
    """Return vllm's live `max_model_len` (context window), cached alongside the
    model. None if never successfully fetched."""
    _refresh(force)
    ctx = _cache.get("ctx")
    return int(ctx) if ctx else None


def resolve_vllm_model(*, prefixed: bool = True, force: bool = False) -> str:
    """Best model id to send. `prefixed` → `vllm/<model>` (OmniRoute-canonical);
    else the bare model. Falls back to `omniroute-auto` (model-agnostic, never
    404s) when vllm is unreachable and nothing is cached/configured."""
    model = current_vllm_model(force=force)
    if not model:
        return "omniroute-auto"
    return f"vllm/{model}" if prefixed else model


def rewrite_vllm_model_id(model_id: str) -> str:
    """Self-heal a possibly-stale `vllm/<old>` id to the live vllm model. Any
    `vllm/...` id → `vllm/<current>`; non-vllm ids returned unchanged. Keeps
    stale config (tier chains, catalogs) working across model swaps."""
    if not isinstance(model_id, str) or not model_id.startswith("vllm/"):
        return model_id
    live = current_vllm_model()
    return f"vllm/{live}" if live else model_id
