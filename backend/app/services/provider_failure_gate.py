"""T111 (2026-09-16) — Traffic-error provider gate + error breakdown.

Why: probes (`<base_url>/models`) say llamacpp is fine while EVERY routed
request to it fails (7d: 2343× 429 SEMAPHORE_TIMEOUT, "server_error" model).
MC's health model was probe-only; the flows sqlite already records ground
truth. This service turns recent traffic errors into:

1. `provider_failure_rates()` — per-provider (total, errors, rate) over a
   rolling window, for the watchdog gate.
2. `gate_decision()` — should a provider be gated offline? (min flows +
   error pct thresholds, hysteresis via a process-level cooldown so the
   autorecover probe path doesn't flap it back).
3. `error_breakdown()` — status-code + top-error summary for the /traffic
   health strip.

No new infra: reads the same `flows` sqlite the ETL writes every 10s.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Gate knobs (env-overridable). Error pct is 0-100. Window must be long
# enough to accumulate min-flows on a FAILING provider — its own 30s
# semaphore timeout throttles arrival rate to a few/min, so 15m can
# collect <10 flows even at 100% errors.
_GATE_WINDOW_SECONDS = float(__import__("os").environ.get("PROVIDER_GATE_WINDOW_SECONDS", "3600"))
_GATE_MIN_FLOWS = int(__import__("os").environ.get("PROVIDER_GATE_MIN_FLOWS", "10"))
_GATE_ERROR_PCT = float(__import__("os").environ.get("PROVIDER_GATE_ERROR_PCT", "70"))
_GATE_COOLDOWN_SECONDS = float(__import__("os").environ.get("PROVIDER_GATE_COOLDOWN_SECONDS", "1800"))

# Process-level cooldown registry: provider -> gate epoch. Restarting the
# backend resets it; the gate re-fires within one watchdog tick anyway.
_gate_flipped_at: dict[str, float] = {}


def _flows_db_path() -> str:
    try:
        from app.services.traffic_etl import _FLOWS_DB
        return str(_FLOWS_DB)
    except Exception:
        return "/traffic-data/llm_flows.db"


def _mc_provider(prefix: str) -> str:
    from app.services.model_router import _PREFIX_TO_MC_PROVIDER
    return _PREFIX_TO_MC_PROVIDER.get(prefix, prefix)


def provider_failure_rates(window_seconds: float | None = None) -> dict[str, dict[str, Any]]:
    """Per-MC-provider traffic stats over the window.

    Returns {provider: {"total": int, "errors": int, "error_pct": float,
    "last_status": int}}. Empty dict when the flows DB is missing.
    """
    window = window_seconds or _GATE_WINDOW_SECONDS
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - window))
    db = _flows_db_path()
    stats: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT model, status_code FROM flows
                WHERE ts >= ? AND model IS NOT NULL AND model != ''
                """,
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("provider_failure_gate.rates_read_failed err=%s", exc)
        return {}
    for model, status in rows:
        if "/" in model:
            prefix, _, _ = model.partition("/")
        else:
            prefix = model
        prov = _mc_provider(prefix)
        s = stats.setdefault(prov, {"total": 0, "errors": 0, "error_pct": 0.0})
        s["total"] += 1
        if status is not None and status >= 400:
            s["errors"] += 1
    for s in stats.values():
        s["error_pct"] = round(s["errors"] * 100 / s["total"], 1) if s["total"] else 0.0
    return stats


def gate_decision(provider: str, rates: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """(gate?, reason) for one provider given provider_failure_rates()."""
    s = rates.get(provider)
    if not s or s["total"] < _GATE_MIN_FLOWS:
        return False, ""
    if s["error_pct"] < _GATE_ERROR_PCT:
        return False, ""
    reason = (
        f"auto_gate: {s['error_pct']}% of {s['total']} requests failed "
        f"in last {int(_GATE_WINDOW_SECONDS / 60)}min"
    )
    return True, reason


def mark_gated(provider: str) -> None:
    _gate_flipped_at[provider] = time.time()


def in_gate_cooldown(provider: str, now: float | None = None) -> bool:
    flipped = _gate_flipped_at.get(provider)
    if not flipped:
        return False
    now = now or time.time()
    if now - flipped >= _GATE_COOLDOWN_SECONDS:
        _gate_flipped_at.pop(provider, None)
        return False
    return True


def clear_gate(provider: str) -> None:
    _gate_flipped_at.pop(provider, None)


def error_breakdown(window_seconds: float = 86400) -> dict[str, Any]:
    """Global error summary for the /traffic health strip (default 24h):
    status-code counts + top error messages + per-provider error rates."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - window_seconds))
    db = _flows_db_path()
    out: dict[str, Any] = {
        "window_seconds": int(window_seconds),
        "total": 0,
        "errors": 0,
        "error_pct": 0.0,
        "by_status": {},
        "top_errors": [],
        "providers": {},
    }
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT model, status_code, finish_reason FROM flows WHERE ts >= ?",
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("provider_failure_gate.breakdown_read_failed err=%s", exc)
        return out
    statuses: Counter = Counter()
    messages: Counter = Counter()
    for model, status, reason in rows:
        out["total"] += 1
        if status is not None and status >= 400:
            out["errors"] += 1
            statuses[str(status)] += 1
            msg = (reason or "")[:110].strip()
            if msg:
                messages[msg] += 1
    out["error_pct"] = round(out["errors"] * 100 / out["total"], 1) if out["total"] else 0.0
    out["by_status"] = dict(statuses.most_common())
    out["top_errors"] = [
        {"message": m, "count": n} for m, n in messages.most_common(5)
    ]
    out["providers"] = provider_failure_rates(window_seconds)
    return out