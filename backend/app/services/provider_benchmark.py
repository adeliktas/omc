"""T76 — Provider benchmark loop.

Every 30 min, fire `"why is the sky blue"` at every (provider, model) we
know about. Record TPS (total_tokens / duration_s). Auto-flip a model to
disabled after 3 timeouts in 5 min; flip the provider offline when all
its models are disabled.

Design choices:
- Talks DIRECTLY to the upstream (NOT through proxy_vllm) so model latency
  isn't biased by the proxy. The proxy's traffic capture is best-effort,
  not a benchmarking primitive.
- API key + base_url read from `provider_capacity`. Authoritative.
- Model list read from `~/.openclaw/openclaw.json models.providers.<name>.models[]`.
- Watchdog already runs separate health-probe loop (T68b). This is a
  SECOND background task — see `app/main.py` startup.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_maker
from app.models.provider_benchmark import ProviderBenchmark
from app.models.provider_capacity import ProviderCapacity

logger = get_logger(__name__)

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
# T103 v35 (2026-05-20): owner-editable at runtime via /settings/runtime.
# Module-load value retained as a fallback; the loop re-reads each tick.
_INTERVAL = float(os.environ.get("BENCHMARK_INTERVAL_SECONDS", "14400"))  # 4h
_REQUEST_TIMEOUT = float(os.environ.get("BENCHMARK_REQUEST_TIMEOUT", "30"))
# Traffic-min (2026-08-24): the benchmark posts a REAL 256-token generation per
# (online-provider × curated-model) each cycle — the only recurring LLM traffic
# MC itself generates, and on a single-GPU Jetson it steals decode from live
# agents. With one owner-pinned provider/model its comparison purpose is moot.
# Default OFF; owner sets BENCHMARK_ENABLED=1 (env or runtime setting) to restore
# when multiple providers/models are configured and worth ranking.
_BENCHMARK_ENABLED_DEFAULT = os.environ.get("BENCHMARK_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# 2026-09-22: these two were registered as owner-editable knobs but the loop
# read these module constants, so editing them in the UI did nothing. Read
# through the registry at each use instead — both are called once per
# (provider, model) per cycle, not a hot path.
_FAILURE_WINDOW_DEFAULT = 300.0  # 5 min rolling failure window
_FAILURE_THRESHOLD_DEFAULT = 3   # 3 timeouts/errors → auto-disable model


def _failure_window() -> float:
    from app.services.runtime_settings import get_setting

    return float(get_setting("BENCHMARK_FAILURE_WINDOW", _FAILURE_WINDOW_DEFAULT))


def _failure_threshold() -> int:
    from app.services.runtime_settings import get_setting

    return int(get_setting("BENCHMARK_FAILURE_THRESHOLD", _FAILURE_THRESHOLD_DEFAULT))

PROMPT = "why is the sky blue"


# ── In-process failure tracking ────────────────────────────────────────

@dataclass
class _ModelKey:
    provider: str
    model: str

    def __hash__(self) -> int:
        return hash((self.provider, self.model))


@dataclass
class _FailureLog:
    timestamps: deque[float] = field(default_factory=deque)


_failures: dict[_ModelKey, _FailureLog] = {}


def _record_failure(provider: str, model: str) -> int:
    """Append failure timestamp. Returns count within window."""
    key = _ModelKey(provider, model)
    log = _failures.setdefault(key, _FailureLog())
    now = time.monotonic()
    log.timestamps.append(now)
    cutoff = now - _failure_window()
    while log.timestamps and log.timestamps[0] < cutoff:
        log.timestamps.popleft()
    return len(log.timestamps)


def _clear_failures(provider: str, model: str) -> None:
    _failures.pop(_ModelKey(provider, model), None)


# ── openclaw.json helpers ──────────────────────────────────────────────

def _load_openclaw_cfg() -> dict[str, Any]:
    p = _OPENCLAW_HOME / "openclaw.json"
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        logger.warning("benchmark.openclaw_read_failed err=%s", exc)
        return {}


def _list_models_for(provider: str) -> list[dict[str, Any]]:
    """T103 v4 (2026-05-17): models live in OmniRoute. Pull synchronously
    via httpx so the bench loop can stay sync. Merges MC sidecar metadata
    (disabled, iq_score, ...) for each entry.
    """
    import os
    import httpx
    from app.services.mc_sidecar import merge_into_model_entry

    mgmt_url = os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
    mgmt_token = os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
    if not mgmt_token:
        return []
    origin = mgmt_url.replace("//omniroute:", "//localhost:")
    headers = {
        "Cookie": f"auth_token={mgmt_token}",
        "Origin": origin,
        "Referer": f"{origin}/dashboard",
    }
    # T103 v5: only curated/manual models (provider-models), NOT synced-
    # available (the upstream's full /v1/models catalog). Owner picks
    # which models to bench by adding them via MC `/providers/<n>/models`.
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(mgmt_url.rstrip("/") + "/api/provider-nodes", headers=headers)
            if r.status_code != 200:
                return []
            nodes = (r.json() or {}).get("nodes", []) or []
            nid = next((n["id"] for n in nodes if n.get("name") == provider), None)
            if not nid:
                return []
            rr = c.get(mgmt_url.rstrip("/") + f"/api/provider-models?provider={nid}",
                       headers=headers)
            if rr.status_code != 200:
                return []
            out: list[dict[str, Any]] = []
            for m in (rr.json() or {}).get("models", []) or []:
                mid = m.get("id") or m.get("modelId")
                if not mid:
                    continue
                out.append(merge_into_model_entry(provider, {"id": mid, "name": m.get("name") or mid}))
            return out
    except Exception as exc:
        logger.debug("benchmark._list_models_omniroute_err provider=%s err=%s", provider, exc)
        return []


def _set_model_disabled(provider: str, model_id: str, reason: str) -> bool:
    """T89 (2026-05-16): disabled state lives in the MC-only sidecar
    `~/.openclaw/openclaw.mc.json` — openclaw's zod schema rejects
    unknown keys on model entries.
    """
    from app.services.mc_sidecar import get_model_meta, set_model_meta
    if get_model_meta(provider, model_id).get("disabled"):
        return False
    set_model_meta(
        provider, model_id,
        disabled=True,
        disabled_reason=reason[:200],
        disabled_at=int(time.time()),
    )
    logger.warning(
        "benchmark.model_disabled provider=%s model=%s reason=%s (→ sidecar)",
        provider, model_id, reason,
    )
    return True


def _clear_model_disabled(provider: str, model_id: str) -> None:
    from app.services.mc_sidecar import get_model_meta, set_model_meta
    if not get_model_meta(provider, model_id).get("disabled"):
        return
    set_model_meta(
        provider, model_id,
        disabled=None,
        disabled_reason=None,
        disabled_at=None,
    )
    logger.info("benchmark.model_reenabled provider=%s model=%s (→ sidecar)", provider, model_id)


# ── Provider state helpers ─────────────────────────────────────────────

async def _set_provider_offline(
    db: AsyncSession, provider_name: str, error: str,
) -> None:
    row = (
        await db.execute(
            sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
        )
    ).scalar_one_or_none()
    if not row:
        return
    row.status = "offline"
    row.last_error = error[:500]
    await db.commit()
    logger.warning(
        "benchmark.provider_offline provider=%s err=%s", provider_name, error,
    )


async def _set_provider_online_if_was_auto_offline(
    db: AsyncSession, provider_name: str,
) -> None:
    """If a previous benchmark cycle flipped a provider offline AND a model
    just succeeded, flip it back online. We use `last_error` presence as the
    'was auto-disabled' signal — owner-set offlines (T49 cost lockdown for
    deepseek etc.) deliberately leave `last_error` empty.
    """
    row = (
        await db.execute(
            sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == provider_name)
        )
    ).scalar_one_or_none()
    if not row or row.status == "online":
        return
    if not row.last_error:
        return  # owner-managed offline; don't touch
    row.status = "online"
    row.last_error = None
    await db.commit()
    logger.info(
        "benchmark.provider_reenabled provider=%s", provider_name,
    )


# ── Benchmark one model ────────────────────────────────────────────────

def _record_traffic_flow(
    provider_name: str,
    model_id: str,
    base_url: str,
    status_code: int,
    latency_ms: int,
    req_body: dict[str, Any],
    res_body: dict[str, Any],
    finish_reason: str = "",
    content_preview: str = "",
) -> None:
    """T76: capture benchmark calls into the traffic DB so every LLM hit
    shows up in `/traffic`, regardless of which proxy (or none) the call
    went through. Best-effort; never raises.
    """
    try:
        from datetime import datetime, timezone
        from app.api import traffic as traffic_mod

        usage = res_body.get("usage") or {}
        # T103 v36 (2026-05-22): model_id passed from `_bench_one` already
        # carries the OmniRoute node prefix (e.g. `vllm/Qwen3.6-...`)
        # because that's the form sent to OR. Re-prepending `provider_name/`
        # produced the misleading `vllm/vllm/...` in `/traffic`. Use model_id
        # raw; back-compat for callers passing a bare id.
        display_model = model_id if "/" in model_id else f"{provider_name}/{model_id}"
        flow = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "host": base_url,
            "path": "/chat/completions",
            "method": "POST",
            "model": display_model,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "agent_name": "benchmark",
            "agent_id": "benchmark-loop",
            "board_id": None,
            "messages_count": len(req_body.get("messages") or []),
            "has_system": False,
            "has_tools": False,
            "tools_count": 0,
            "stream": False,
            "temperature": req_body.get("temperature"),
            "max_tokens": req_body.get("max_tokens"),
            "last_user_message": PROMPT,
            "finish_reason": finish_reason,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "content_preview": content_preview[:500],
            "reasoning_preview": "",
            "has_tool_calls": False,
            "tool_calls_count": 0,
            "req_body": req_body,
            "res_body": res_body,
        }
        conn = traffic_mod._db()
        with traffic_mod._db_lock:
            conn.execute(traffic_mod._INSERT, traffic_mod._row_values(flow))
            conn.commit()
    except Exception as exc:
        logger.warning("benchmark.traffic_capture_failed err=%s", exc)


async def _bench_one(
    client: httpx.AsyncClient,
    provider_name: str,
    base_url: str,
    api_key: str | None,
    model_id: str,
    *,
    max_tokens: int = 256,
    timeout: float | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Run one benchmark. Returns (ok, error, stats). `max_tokens`/`timeout`
    default to the loop's values; the on-demand self-benchmark overrides them
    (fewer tokens + longer timeout) since the slow single-GPU dense model needs
    >30s to emit a full 256-token budget."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("noapikey", "none", ""):
        headers["Authorization"] = f"Bearer {api_key}"

    # T103 v38 (2026-05-22): disable Qwen3.6 / reasoning-default models'
    # chain-of-thought via `chat_template_kwargs.enable_thinking=false` —
    # without it the small bench prompt burns the entire token budget
    # on a reasoning trace + emits empty `content`. Harmless for upstreams
    # that ignore unknown chat_template_kwargs.
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    t0 = time.monotonic()
    try:
        r = await client.post(url, json=body, headers=headers, timeout=(timeout or _REQUEST_TIMEOUT))
    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _record_traffic_flow(
            provider_name, model_id, base_url, 0, latency_ms, body,
            {"error": "timeout"}, "timeout", "",
        )
        return False, "timeout", {"duration_ms": latency_ms}
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
        _record_traffic_flow(
            provider_name, model_id, base_url, 0, latency_ms, body,
            {"error": err}, "error", "",
        )
        return False, err, {"duration_ms": latency_ms}

    duration_ms = int((time.monotonic() - t0) * 1000)

    if r.status_code != 200:
        # Still capture so owner can see the failure in /traffic.
        try:
            res_json = r.json()
        except Exception:
            res_json = {"raw_text": r.text[:1000]}
        _record_traffic_flow(
            provider_name, model_id, base_url, r.status_code, duration_ms, body,
            res_json, "http_error", "",
        )
        return False, f"http_{r.status_code}: {r.text[:200]}", {"duration_ms": duration_ms}

    try:
        data = r.json()
    except Exception:
        _record_traffic_flow(
            provider_name, model_id, base_url, r.status_code, duration_ms, body,
            {"raw_text": r.text[:1000]}, "non_json", "",
        )
        return False, "non_json_response", {"duration_ms": duration_ms}

    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    tt = int(usage.get("total_tokens") or (pt + ct))

    content = ""
    finish_reason = ""
    try:
        ch = (data.get("choices") or [{}])[0]
        content = (ch.get("message") or {}).get("content") or ""
        finish_reason = ch.get("finish_reason") or ""
    except Exception:
        pass

    # Some providers don't return token counts. Fall back to len(content)/4.
    if tt == 0:
        tt = max(1, len(content) // 4)
        ct = tt

    duration_s = max(duration_ms / 1000.0, 1e-3)
    tps = tt / duration_s

    _record_traffic_flow(
        provider_name, model_id, base_url, r.status_code, duration_ms, body,
        data, finish_reason, content,
    )

    return True, None, {
        "duration_ms": duration_ms,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "tps": tps,
    }


# ── Main loop ──────────────────────────────────────────────────────────

async def benchmark_all_once() -> None:
    """One pass — bench every curated model on every ONLINE provider.

    T103 v5 (2026-05-17): skip offline providers entirely (owner toggled
    off in MC UI = don't burn upstream credits). Bench routes through
    OmniRoute (`OMNIROUTE_RUNTIME_URL`) so it respects OmniRoute's enabled
    state too + uses MC's already-paid runtime key instead of per-provider
    direct API keys.
    """
    import os as _os
    or_url = _os.environ.get("OMNIROUTE_RUNTIME_URL", "http://omniroute:20129/v1")
    or_key = _os.environ.get("OMNIROUTE_RUNTIME_KEY", "")

    async with async_session_maker() as db:
        providers = (
            await db.execute(sa_select(ProviderCapacity).order_by(ProviderCapacity.provider_name))
        ).scalars().all()

        # Minimize LLM calls: benchmarking exists to compare providers for
        # routing tie-breaks. With 0-1 ONLINE providers there is no choice to
        # inform, so the bench calls are pure waste — skip the whole pass.
        online = [p for p in providers if p.status == "online" and p.base_url]
        if len(online) < 2:
            logger.info(
                "benchmark.skip_single_provider online=%d (no multi-provider tie-break to inform)",
                len(online),
            )
            return

        async with httpx.AsyncClient() as client:
            for prov in providers:
                # T103 v5: skip offline providers — owner-intent (or auto-
                # propagated from OmniRoute reverse-sync). No upstream calls.
                if prov.status != "online":
                    logger.debug("benchmark.skip provider=%s status=%s",
                                 prov.provider_name, prov.status)
                    continue
                if not prov.base_url:
                    continue

                models = _list_models_for(prov.provider_name)
                # Skip disabled-in-openclaw.json models entirely.
                models = [m for m in models if not m.get("disabled")]

                if not models:
                    # No enabled models on this provider → set offline + error.
                    if prov.status != "offline" or prov.last_error != "no_enabled_models":
                        await _set_provider_offline(
                            db, prov.provider_name, "no_enabled_models",
                        )
                    continue

                any_ok = False
                for m in models:
                    model_id = m["id"]
                    # T103 v37 (2026-05-22): always go direct to the
                    # upstream baseUrl for benchmarking. OmniRoute's
                    # runtime API key has a per-model allow-list policy
                    # (`API key policy rejected: <model>`); benchmarks hit
                    # arbitrary curated models per provider so the
                    # OR-routed path returned 429 in <20ms. Direct calls
                    # also give a cleaner TPS reading (no OmniRoute
                    # middleware overhead) which is the actual purpose.
                    ok, err, stats = await _bench_one(
                        client, prov.provider_name, prov.base_url, prov.api_key, model_id,
                    )

                    bench = ProviderBenchmark(
                        provider_name=prov.provider_name,
                        model_id=model_id,
                        duration_ms=stats.get("duration_ms", 0),
                        prompt_tokens=stats.get("prompt_tokens", 0),
                        completion_tokens=stats.get("completion_tokens", 0),
                        total_tokens=stats.get("total_tokens", 0),
                        tps=stats.get("tps", 0.0),
                        ok=ok,
                        error=err,
                    )
                    db.add(bench)
                    await db.commit()

                    if ok:
                        any_ok = True
                        _clear_failures(prov.provider_name, model_id)
                        _clear_model_disabled(prov.provider_name, model_id)
                        logger.info(
                            "benchmark.ok provider=%s model=%s tps=%.2f tokens=%d ms=%d",
                            prov.provider_name, model_id, stats.get("tps", 0.0),
                            stats.get("total_tokens", 0), stats.get("duration_ms", 0),
                        )
                    else:
                        count = _record_failure(prov.provider_name, model_id)
                        logger.warning(
                            "benchmark.fail provider=%s model=%s err=%s count=%d",
                            prov.provider_name, model_id, err, count,
                        )
                        if count >= _failure_threshold():
                            _set_model_disabled(
                                prov.provider_name, model_id,
                                f"{count} failures in {int(_failure_window())}s: {err}",
                            )

                if any_ok:
                    await _set_provider_online_if_was_auto_offline(db, prov.provider_name)


async def benchmark_self_once() -> dict[str, Any]:
    """On-demand single-model self-benchmark — the RIGHT tool when there is one
    owner-pinned provider (`benchmark_all_once` skips with <2 online providers
    and depends on curated model lists, so it leaves the current model unmeasured).

    Resolves the model vllm CURRENTLY serves (live `/v1/models`) and benches it
    BOTH ways, writing a `provider_benchmark` row for each so the /benchmark page
    shows them:
      - `provider_name='vllm'`         → DIRECT to the Jetson (raw decode tok/s)
      - `provider_name='omniroute-auto'` → through the OmniRoute runtime proxy
        (what agents actually experience: proxy + system-prompt + compression)

    Not a loop — invoked from the owner endpoint. One generation per path.
    """
    import os as _os

    from app.services.vllm_model import current_vllm_model

    live_model = current_vllm_model(force=True)
    if not live_model:
        logger.warning("benchmark_self.no_live_model — vllm /v1/models unreachable")
        return {"ok": False, "error": "vllm model unresolved"}

    # Direct vllm base_url: prefer the ProviderCapacity row, fall back to env.
    direct_base = _os.environ.get("VLLM_BASE_URL", "").rstrip("/")
    async with async_session_maker() as db:
        prov = (
            await db.execute(
                sa_select(ProviderCapacity).where(ProviderCapacity.provider_name == "vllm"),
            )
        ).scalars().first()
        if prov and prov.base_url:
            direct_base = prov.base_url.rstrip("/")
        if not direct_base:
            # Derive from the health URL (…/v1/models → …/v1).
            hm = _os.environ.get("VLLM_HEALTH_URL", "")
            direct_base = hm.rsplit("/models", 1)[0]

    or_url = _os.environ.get("OMNIROUTE_RUNTIME_URL", "http://omniroute:20129/v1").rstrip("/")
    or_key = _os.environ.get("OMNIROUTE_RUNTIME_KEY", "")

    # (label, base_url, api_key, model_id) — direct uses the bare served name;
    # the proxy path uses the model-agnostic `omniroute-auto` combo.
    targets = [
        ("vllm", direct_base, None, live_model),
        ("omniroute-auto", or_url, or_key, "omniroute-auto"),
    ]

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=95.0) as client, async_session_maker() as db:
        for label, base, key, model_id in targets:
            if not base:
                results.append({"provider": label, "ok": False, "error": "no base_url"})
                continue
            # 128 tokens is enough to read steady-state TPS; 90s timeout covers
            # the slow dense model + a prefill even under light agent contention.
            ok, err, stats = await _bench_one(
                client, label, base, key, model_id, max_tokens=128, timeout=90.0,
            )
            db.add(ProviderBenchmark(
                provider_name=label,
                model_id=model_id if label == "vllm" else f"omniroute-auto ({live_model})",
                duration_ms=stats.get("duration_ms", 0),
                prompt_tokens=stats.get("prompt_tokens", 0),
                completion_tokens=stats.get("completion_tokens", 0),
                total_tokens=stats.get("total_tokens", 0),
                tps=stats.get("tps", 0.0),
                ok=ok,
                error=err,
            ))
            await db.commit()
            logger.info(
                "benchmark_self.%s model=%s ok=%s tps=%.2f ms=%d err=%s",
                label, model_id, ok, stats.get("tps", 0.0), stats.get("duration_ms", 0), err,
            )
            results.append({
                "provider": label,
                "model": model_id,
                "ok": ok,
                "tps": round(stats.get("tps", 0.0), 2),
                "duration_ms": stats.get("duration_ms", 0),
                "total_tokens": stats.get("total_tokens", 0),
                "error": err,
            })
    return {"ok": True, "model": live_model, "results": results}


def benchmarking_enabled() -> bool:
    """The shared runtime gate for the benchmark loop and its health test."""
    from app.services.runtime_settings import get_setting

    val = get_setting("BENCHMARK_ENABLED", None)
    if val is None:
        return _BENCHMARK_ENABLED_DEFAULT
    return str(val).strip().lower() in ("1", "true", "yes", "on")


async def benchmark_loop() -> None:
    """Run forever. T103 v35: re-reads BENCHMARK_INTERVAL_SECONDS from
    runtime settings each loop so owner can change cadence live without
    restart."""
    from app.services.runtime_settings import get_setting

    logger.info(
        "benchmark.loop_started interval=%ds enabled=%s", int(_INTERVAL), benchmarking_enabled(),
    )
    await asyncio.sleep(30)
    while True:
        try:
            if benchmarking_enabled():
                await benchmark_all_once()
            else:
                logger.debug("benchmark.skipped disabled (BENCHMARK_ENABLED=0)")
        except Exception as exc:
            logger.exception("benchmark.cycle_failed err=%s", exc)
        delay = float(get_setting("BENCHMARK_INTERVAL_SECONDS", _INTERVAL) or _INTERVAL)
        if delay <= 0:
            delay = _INTERVAL
        # When disabled, re-check the gate on the same cadence so an owner can
        # flip BENCHMARK_ENABLED=1 at runtime and see it pick up within one cycle.
        await asyncio.sleep(delay)
