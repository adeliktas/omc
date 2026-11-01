"""Provider usage stats endpoint — reads ollama session/weekly usage and vLLM concurrency."""

from __future__ import annotations

import json
from app.core.logging import get_logger
from app.services.runtime_settings import get_setting
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select as sa_select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.session import get_session
from app.models.provider_capacity import ProviderCapacity

router = APIRouter(prefix="/traffic", tags=["traffic"])

OLLAMA_SCRIPT = "/home/host_openclaw/ollama_settings.sh"
_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
OPENCLAW_CONFIG = _OPENCLAW_HOME / "openclaw.json"
# Sidecar snapshot file — gateway schema rejects unknown keys inside openclaw.json,
# so storing snapshot under meta._cloud_disabled_snapshot causes config rollback.
CLOUD_SNAPSHOT_FILE = _OPENCLAW_HOME / ".cloud_disabled_snapshot.json"
_VLLM_PRIMARY_ENV = os.environ.get("CLOUD_TOGGLE_VLLM_PRIMARY", "").strip()


def vllm_primary() -> str:
    """The vllm primary model for display/reporting — env override, else the
    live model resolved from vllm (never hardcoded)."""
    from app.services.vllm_model import resolve_vllm_model

    return _VLLM_PRIMARY_ENV or resolve_vllm_model()
_AGENTS_DIR = _OPENCLAW_HOME / "agents"
_SESSION_OVERRIDE_KEYS = (
    "modelProvider", "model", "providerOverride", "modelOverride",
    "modelOverrideSource",
)
_SIDECAR_KEEP = 3  # last-3 rotation: .json, .json.1, .json.2, .json.3


def _atomic_write_text(target: Path, content: str, mode: int = 0o666) -> None:
    """Write `content` to `target` atomically via tmp + os.replace.

    Falls back to direct write on cross-FS OSError with a single retry.
    Matches Python 3.8+ os.replace semantics (atomic on same FS, best-effort cross-FS).
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("atomic_write.replace_failed target=%s err=%s; falling back to direct write", target, exc)
        try:
            target.write_text(content)
            os.chmod(target, mode)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def _rotate_sidecar(path: Path, keep: int = _SIDECAR_KEEP) -> None:
    """Rotate sidecar snapshots before overwriting. Keeps last `keep` versions.

    Only rotates when the current `<path>` exists; otherwise no-op so that the
    ON→OFF cycle (which deletes `<path>` on ON) doesn't shift `.json.1` →
    `.json.2` and lose history. Rotation steps:
      drop `<path>.<keep>` → shift `<path>.{i}` → `<path>.{i+1}` for i in
      [keep-1..1] → finally move current `<path>` → `<path>.1`.
    """
    if not path.exists():
        return
    try:
        # Drop oldest first.
        oldest = path.with_suffix(path.suffix + f".{keep}")
        if oldest.exists():
            oldest.unlink()
        for i in range(keep - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_suffix(path.suffix + ".1"))
    except Exception as exc:
        logger.warning("sidecar_rotate_failed path=%s err=%s", path, exc)


def _clear_session_overrides() -> int:
    """Remove cached per-session model overrides from all agent session files.

    Offloads blocking filesystem I/O to a thread to avoid starving the
    asyncio event loop on the main FastAPI worker.

    Returns number of files modified.
    """
    if not _AGENTS_DIR.exists():
        return 0
    import concurrent.futures
    sessions_files = list(_AGENTS_DIR.glob("*/sessions/sessions.json"))
    if not sessions_files:
        return 0

    def _process_one(sf: Path) -> int:
        try:
            data = json.loads(sf.read_text())
        except Exception:
            return 0
        changed = False
        if isinstance(data, dict):
            for sk, sv in list(data.items()):
                if not isinstance(sv, dict):
                    continue
                for k in _SESSION_OVERRIDE_KEYS:
                    if k in sv:
                        del sv[k]
                        changed = True
        if changed:
            try:
                tmp = sf.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                tmp.replace(sf)
                return 1
            except Exception as exc:
                logger.warning(
                    "cloud_toggle.session_override_clear_failed file=%s err=%s",
                    sf, exc,
                )
        return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return sum(pool.map(_process_one, sessions_files))
_TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
_RECENT_WINDOW_SECONDS = int(get_setting("PROVIDER_STATS_WINDOW_SECONDS"))  # 15 min

logger = get_logger(__name__)


def _provider_for(host: str | None, model: str | None) -> str | None:
    """Map flow host/model to provider. Mirrors frontend logic."""
    if model and "/" in model:
        prefix = model.split("/", 1)[0].lower()
        if prefix in ("vllm", "ollama"):
            return prefix
    if host:
        h = host.lower()
        if "ollama" in h:
            return "ollama"
        vllm_host = urlparse(os.environ.get("VLLM_HEALTH_URL", "")).hostname
        if vllm_host and vllm_host in h:
            return "vllm"
    return None


def _live_provider_usage(window_seconds: int | None = None) -> dict[str, dict[str, Any]]:
    """Read sqlite traffic DB and compute per-provider live stats over the
    recent window. Returns:
        {"vllm": {"distinct_agents": N, "flow_count": N, "agents": [...]}, ...}

    T40c (2026-05-14): `window_seconds` overrides the default
    `PROVIDER_STATS_WINDOW_SECONDS` so callers (UI knob, scorecard, etc.)
    can probe shorter / longer windows.
    """
    out: dict[str, dict[str, Any]] = {
        "vllm": {"distinct_agents": 0, "flow_count": 0, "agents": []},
        "ollama": {"distinct_agents": 0, "flow_count": 0, "agents": []},
    }
    if not _TRAFFIC_DB.exists():
        return out
    win = window_seconds if window_seconds and window_seconds > 0 else _RECENT_WINDOW_SECONDS
    since = (datetime.now(timezone.utc) - timedelta(seconds=win)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
        rows = conn.execute(
            "SELECT host, model, agent_name, COUNT(*) AS c FROM flows "
            "WHERE ts >= ? GROUP BY host, model, agent_name",
            (since,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("provider_stats.live_usage_failed err=%s", exc)
        return out

    per_provider_agents: dict[str, set[str]] = {"vllm": set(), "ollama": set()}
    per_provider_flows: dict[str, int] = {"vllm": 0, "ollama": 0}
    for host, model, agent_name, count in rows:
        prov = _provider_for(host, model)
        if not prov:
            continue
        per_provider_flows[prov] += int(count or 0)
        if agent_name:
            per_provider_agents[prov].add(agent_name)
    for prov in ("vllm", "ollama"):
        out[prov]["distinct_agents"] = len(per_provider_agents[prov])
        out[prov]["flow_count"] = per_provider_flows[prov]
        out[prov]["agents"] = sorted(per_provider_agents[prov])
    return out


async def _fetch_ollama_quota_via_api(*, api_key: str | None) -> dict[str, Any] | None:
    """T30 (2026-05-14): fetch ollama.com session + weekly quota directly via
    HTTPS. Returns parsed dict or None on any failure (caller falls back to
    legacy shellout). Endpoint best-effort guessed from ollama.com surface;
    the function is conservative: any non-200 / parse failure → return None.
    """
    import httpx
    if not api_key:
        return None
    base = os.environ.get("OLLAMA_QUOTA_URL", "https://ollama.com/api/usage")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(base, headers=headers)
        if r.status_code != 200:
            return None
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as exc:
        logger.debug("provider_stats.ollama_api_unavailable err=%s", exc)
        return None
    # Normalize best-effort. Shape unknown; accept several common keys.
    out: dict[str, Any] = {}
    sess = body.get("session") or {}
    week = body.get("weekly") or body.get("week") or {}
    if sess:
        out["session_used_pct"] = float(sess.get("used_pct", sess.get("percent", 0.0)) or 0.0)
        out["session_reset_in"] = sess.get("reset_in") or sess.get("resets_in") or "unknown"
    if week:
        out["weekly_used_pct"] = float(week.get("used_pct", week.get("percent", 0.0)) or 0.0)
        out["weekly_reset_in"] = week.get("reset_in") or week.get("resets_in") or "unknown"
    return out or None


def _fetch_ollama_quota_via_shellout() -> dict[str, Any] | None:
    """Legacy: run ollama_settings.sh and parse stdout for `%` / `Resets in`."""
    if not os.path.exists(OLLAMA_SCRIPT):
        return None
    try:
        proc = subprocess.run(
            ["bash", OLLAMA_SCRIPT],
            capture_output=True, text=True, timeout=45,
            env={**os.environ, "HOME": "/root"},
        )
    except Exception as exc:
        logger.warning("provider_stats.ollama_script_failed err=%s", exc)
        return None
    output = proc.stdout or ""
    lines = [ln.strip() for ln in output.split("\n") if ln.strip()]
    out: dict[str, Any] = {}
    if len(lines) >= 1:
        m = re.search(r"(\d+\.?\d*)%", lines[0])
        if m:
            out["session_used_pct"] = float(m.group(1))
        m = re.search(r"Resets in ([^)]+)", lines[0])
        if m:
            out["session_reset_in"] = m.group(1).strip(")")
    if len(lines) >= 2:
        m = re.search(r"(\d+\.?\d*)%", lines[1])
        if m:
            out["weekly_used_pct"] = float(m.group(1))
        m = re.search(r"Resets in ([^)]+)", lines[1])
        if m:
            out["weekly_reset_in"] = m.group(1).strip(")")
    return out or None


def _live_provider_usage_by_board() -> dict[str, dict[str, dict[str, Any]]]:
    """T23c (2026-05-14): per-board × per-provider rolling-window stats.

    Returns: {board_id: {provider: {distinct_agents, flow_count, agents[]}}}.
    Board "" key collects flows with no board_id.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not _TRAFFIC_DB.exists():
        return out
    since = (datetime.now(timezone.utc) - timedelta(seconds=_RECENT_WINDOW_SECONDS)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
        rows = conn.execute(
            "SELECT board_id, host, model, agent_name, COUNT(*) AS c "
            "FROM flows WHERE ts >= ? "
            "GROUP BY board_id, host, model, agent_name",
            (since,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("provider_stats.by_board_failed err=%s", exc)
        return out

    for board_id, host, model, agent_name, count in rows:
        prov = _provider_for(host, model)
        if not prov:
            continue
        bid = board_id or ""
        b_entry = out.setdefault(bid, {})
        p_entry = b_entry.setdefault(prov, {"distinct_agents": 0, "flow_count": 0, "_agents": set()})
        p_entry["flow_count"] += int(count or 0)
        if agent_name:
            p_entry["_agents"].add(agent_name)
    # Finalize: distinct_agents from set + sorted list
    for bid, providers in out.items():
        for prov, entry in providers.items():
            entry["distinct_agents"] = len(entry["_agents"])
            entry["agents"] = sorted(entry["_agents"])
            del entry["_agents"]
    return out


@router.get("/provider-stats-by-board", summary="Per-board provider usage breakdown (T23c)")
async def provider_stats_by_board() -> dict[str, Any]:
    """Per-board × per-provider rolling-window stats from sqlite traffic DB."""
    return {
        "by_board": _live_provider_usage_by_board(),
        "window_seconds": _RECENT_WINDOW_SECONDS,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# T40b (2026-05-14): in-process cache for /provider-stats. 10s TTL keyed by
# `window_seconds` query param. Stats are derived (LLM proxy traffic + DB
# capacity) and tolerate staleness; the cache spares both sqlite + Postgres
# from synchronized hammer-poll under heavy UI refresh.
_PROVIDER_STATS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_PROVIDER_STATS_TTL = float(os.environ.get("PROVIDER_STATS_CACHE_TTL", "10"))


@router.get("/provider-stats", summary="Provider usage stats (ollama quotas + vLLM concurrency)")
async def provider_stats(
    window_seconds: int | None = None,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    import time as _time
    cache_key = int(window_seconds or _RECENT_WINDOW_SECONDS)
    now = _time.monotonic()
    cached = _PROVIDER_STATS_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _PROVIDER_STATS_TTL:
        return cached[1]

    result = await db.execute(sa_select(ProviderCapacity))
    capacities = {p.provider_name: p for p in result.scalars().all()}

    live = _live_provider_usage(window_seconds=cache_key)

    ollama_cap = capacities.get("ollama")
    ollama = {
        "session_used_pct": 0.0,
        "weekly_used_pct": 0.0,
        "session_reset_in": "unknown",
        "weekly_reset_in": "unknown",
        "concurrent": live["ollama"]["distinct_agents"],
        "flow_count": live["ollama"]["flow_count"],
        "agents": live["ollama"]["agents"],
        "max_concurrent": ollama_cap.max_concurrent if ollama_cap else 3,
        "unlimited": bool(ollama_cap.is_unlimited) if ollama_cap else False,
        "status": (ollama_cap.status if ollama_cap else "online") or "online",
        "window_seconds": cache_key,
    }

    vllm_cap = capacities.get("vllm")
    vllm = {
        "concurrent": live["vllm"]["distinct_agents"],
        "flow_count": live["vllm"]["flow_count"],
        "agents": live["vllm"]["agents"],
        "max_concurrent": vllm_cap.max_concurrent if vllm_cap else 15,
        "unlimited": bool(vllm_cap.is_unlimited) if vllm_cap else False,
        "status": (vllm_cap.status if vllm_cap else "online") or "online",
        "window_seconds": cache_key,
    }

    # T30 (2026-05-14): ollama quota source. Prefer the direct ollama.com API
    # when the provider is online; fall back to the legacy shellout for
    # environments that still have the script installed. Skip entirely when
    # ollama is offline (per audit invariant) or when `OLLAMA_QUOTA_DISABLED=true`.
    if (
        ollama_cap is None or (ollama_cap.status or "online") != "online"
        or os.environ.get("OLLAMA_QUOTA_DISABLED", "false").lower() == "true"
    ):
        pass  # ollama disabled — leave quota fields at defaults
    else:
        quota = await _fetch_ollama_quota_via_api(api_key=ollama_cap.api_key)
        if quota is None:
            quota = _fetch_ollama_quota_via_shellout()
        if quota:
            ollama["session_used_pct"] = quota.get("session_used_pct", 0.0)
            ollama["session_reset_in"] = quota.get("session_reset_in", "unknown")
            ollama["weekly_used_pct"] = quota.get("weekly_used_pct", 0.0)
            ollama["weekly_reset_in"] = quota.get("weekly_reset_in", "unknown")

    response = {
        "ollama": ollama,
        "vllm": vllm,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _PROVIDER_STATS_CACHE[cache_key] = (now, response)
    return response


@router.post("/cloud-toggle-atomic",
    summary="T42 (2026-05-14): atomic combine of board flag PATCH + cloud-toggle POST.")
async def toggle_cloud_providers_atomic(
    payload: dict[str, Any] = {},
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Combined endpoint: updates `boards.cloud_providers_enabled` AND runs the
    openclaw.json rewrite in one call, so the frontend doesn't have to
    coordinate two requests. Either both succeed or the entire change rolls back.

    body: `{"board_id": "<uuid>", "enabled": true/false}`

    Board flag is still tracked even though the toggle is global (per
    T16 audit decision). Useful for UI display + audit trail.
    """
    import uuid as _uuid
    enabled = bool(payload.get("enabled", True))
    board_id_raw = payload.get("board_id")
    if not board_id_raw:
        raise HTTPException(status_code=422, detail="board_id is required")
    try:
        board_id = _uuid.UUID(str(board_id_raw))
    except ValueError:
        raise HTTPException(status_code=422, detail="board_id must be a UUID")

    # 1) Update board flag in a transaction.
    from sqlalchemy import update as _sa_update
    from app.models.boards import Board
    r = await db.execute(
        _sa_update(Board).where(Board.id == board_id).values(
            cloud_providers_enabled=enabled,
        ),
    )
    if r.rowcount == 0:
        raise HTTPException(status_code=404, detail="board not found")

    # 2) Run the existing global toggle (rewrites openclaw.json + sidecar).
    try:
        toggle_result = await toggle_cloud_providers(
            payload={"enabled": enabled},
            db=db,
        )
    except Exception as exc:
        # Roll back the board flag change since the toggle failed.
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"cloud-toggle failed after board flag update: {exc}",
        )
    await db.commit()

    return {
        "board_id": str(board_id),
        "enabled": enabled,
        "toggle": toggle_result,
    }


@router.post("/cloud-toggle", summary="Toggle cloud-tier providers on/off in openclaw.json")
async def toggle_cloud_providers(
    payload: dict[str, Any] = {},
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """
    Enable or disable cloud-tier providers by rewriting agent model configs.
    Historically this only flipped ollama-cloud — today it covers every
    registered cloud-tier provider (T85). The toggle is brand-neutral:
    cloud-tier OFF means all agents fall back to their LAN-local primary.
    body: {"enabled": true/false}
    When disabling: saves current state to meta._cloud_disabled_snapshot,
    sets all agent primaries to vllm, clears fallbacks.
    When enabling: restores from snapshot.

    Records `cloud_toggle.disabled` / `cloud_toggle.enabled` row in
    `activity_events` so the Activity tab surfaces every toggle event.
    """
    enabled = payload.get("enabled", True)
    if not OPENCLAW_CONFIG.exists():
        raise HTTPException(status_code=404, detail="openclaw.json not found")

    cfg = json.loads(OPENCLAW_CONFIG.read_text())
    agents_list = cfg.get("agents", {}).get("list", [])
    defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})

    if not enabled:
        # Save current state to sidecar (gateway rejects unknown keys in openclaw.json).
        snapshot = {
            "defaults_model": defaults.get("model"),
            "agent_states": [
                {"id": a.get("id"), "model": a.get("model")} for a in agents_list
            ],
        }
        try:
            _rotate_sidecar(CLOUD_SNAPSHOT_FILE)
            _atomic_write_text(
                CLOUD_SNAPSHOT_FILE,
                json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            )
        except Exception as exc:
            logger.warning("cloud_toggle.snapshot_write_failed err=%s", exc)

        # Strip any legacy snapshot from openclaw.json (would trigger rollback).
        if isinstance(cfg.get("meta"), dict):
            cfg["meta"].pop("_cloud_disabled_snapshot", None)

        # Set defaults to vllm only
        defaults["model"] = {
            "primary": vllm_primary(),
            "fallbacks": [],
        }

        # Set every agent to vllm only
        for agent in agents_list:
            agent["model"] = {
                "primary": vllm_primary(),
                "fallbacks": [],
            }

        _atomic_write_text(
            OPENCLAW_CONFIG,
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        )
        # Drop any cached per-session ollama overrides so the new vllm-only
        # config actually takes effect. Without this gateway keeps using the
        # cached `modelOverride` and ignores openclaw.json.
        cleared = _clear_session_overrides()
        logger.info(
            "cloud_toggle.disabled agents=%d sessions_cleared=%d",
            len(agents_list), cleared,
        )
        try:
            from app.services.activity_log import record_activity
            record_activity(
                db, event_type="cloud_toggle.disabled",
                message=f"agents={len(agents_list)} sessions_cleared={cleared} primary={vllm_primary()}",
            )
            await db.commit()
        except Exception as exc:
            logger.warning("cloud_toggle.activity_log_failed err=%s", exc)
        return {
            "enabled": False,
            "agents_modified": len(agents_list),
            "sessions_cleared": cleared,
        }

    else:
        # Prefer sidecar; fall back to legacy in-config snapshot for backward compat.
        snapshot = None
        if CLOUD_SNAPSHOT_FILE.exists():
            try:
                snapshot = json.loads(CLOUD_SNAPSHOT_FILE.read_text())
            except Exception as exc:
                logger.warning("cloud_toggle.snapshot_read_failed err=%s", exc)
        if snapshot is None and isinstance(cfg.get("meta"), dict):
            snapshot = cfg["meta"].pop("_cloud_disabled_snapshot", None)
        if snapshot:
            # Restore defaults
            if snapshot.get("defaults_model"):
                defaults["model"] = snapshot["defaults_model"]

            # Restore each agent
            saved_map = {s["id"]: s for s in snapshot.get("agent_states", [])}
            for agent in agents_list:
                saved = saved_map.get(agent.get("id"))
                if saved and saved.get("model"):
                    agent["model"] = saved["model"]
                else:
                    # T49 (2026-05-14): owner constraint — NO paid fallbacks
                    # by default. vllm is free + local; deepseek charges per
                    # call. Restoration without snapshot leaves the agent on
                    # vllm-only. To opt back into paid fallback, owner must
                    # explicitly update agent.model.fallbacks in openclaw.json.
                    # Audit traffic showed 678 unauthorized deepseek-chat /
                    # deepseek-reasoner calls before this fix; never again
                    # by accident.
                    agent["model"] = {
                        "primary": vllm_primary(),
                        "fallbacks": [],
                    }

        # Strip legacy in-config snapshot (gateway would reject on next reload).
        if isinstance(cfg.get("meta"), dict):
            cfg["meta"].pop("_cloud_disabled_snapshot", None)

        _atomic_write_text(
            OPENCLAW_CONFIG,
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        )
        # Sidecar consumed — rotate to .json.1 (preserved as history) so the
        # last-3 rotation actually accumulates across OFF→ON→OFF cycles. Future
        # enables don't re-read .json (we just consumed it); only .json.{1..3}
        # remain as forensics history.
        try:
            _rotate_sidecar(CLOUD_SNAPSHOT_FILE)
        except Exception as exc:
            logger.warning("cloud_toggle.snapshot_rotate_failed err=%s", exc)
        # Symmetric to disable path — clear cached overrides so cloud agents
        # actually run on cloud models again.
        cleared = _clear_session_overrides()
        logger.info(
            "cloud_toggle.enabled agents=%d sessions_cleared=%d",
            len(agents_list), cleared,
        )
        try:
            from app.services.activity_log import record_activity
            record_activity(
                db, event_type="cloud_toggle.enabled",
                message=f"agents={len(agents_list)} sessions_cleared={cleared}",
            )
            await db.commit()
        except Exception as exc:
            logger.warning("cloud_toggle.activity_log_failed err=%s", exc)
        return {
            "enabled": True,
            "agents_modified": len(agents_list),
            "sessions_cleared": cleared,
        }
