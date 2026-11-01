"""Unified health check endpoint — single source of truth for system liveness."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.session import get_session
from app.models.agents import Agent

router = APIRouter(prefix="/health", tags=["health"])

TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
TESTS_DB = Path(os.environ.get("TESTS_DB_FILE", "/traffic-data/tests.db"))
VLLM_URL = os.environ.get("VLLM_HEALTH_URL", "http://host.docker.internal:9090/v1/models")
OLLAMA_URL = os.environ.get("OLLAMA_HEALTH_URL", "http://host.docker.internal:8000/api/v1/proxy/ollama/v1/models")
GATEWAY_HOST = os.environ.get("OPENCLAW_GATEWAY_HOST", "172.17.0.1")
GATEWAY_PORT = os.environ.get("OPENCLAW_GATEWAY_PORT", "18789")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")


async def _probe_gateway() -> dict[str, Any]:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/__openclaw/health",
            headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"} if GATEWAY_TOKEN else {},
        )
        # Blocking urlopen in an async handler froze every request for the
        # whole timeout; run it on a worker thread instead.
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
        return {"reachable": True, "http_code": resp.getcode()}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:120]}


# Traffic-min (2026-08-24): cache provider-probe results briefly. /health/full
# probes vllm AND ollama.com (a PAID cloud endpoint) on every call; a burst of
# external/ops hits would otherwise re-probe each time and steal Jetson decode /
# burn ollama quota. 15s TTL keeps the readout fresh without hammering upstreams.
_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PROBE_TTL_SECONDS = float(os.environ.get("HEALTH_PROBE_TTL_SECONDS", "15"))


async def _probe_provider(url: str, label: str) -> dict[str, Any]:
    now = time.monotonic()
    cached = _PROBE_CACHE.get(url)
    if cached and (now - cached[0]) < _PROBE_TTL_SECONDS:
        return {**cached[1], "cached": True}
    try:
        import urllib.request, urllib.error
        t0 = time.perf_counter()
        req = urllib.request.Request(url)
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        lat_ms = (time.perf_counter() - t0) * 1000
        result = {"reachable": True, "http_code": resp.getcode(), "latency_ms": round(lat_ms, 1)}
    except urllib.error.HTTPError as e:
        result = {"reachable": False, "http_code": e.code, "error": str(e)[:120]}
    except Exception as e:
        result = {"reachable": False, "error": str(e)[:120]}
    _PROBE_CACHE[url] = (now, result)
    return result


def _read_tests_db() -> dict[str, Any]:
    if not TESTS_DB.exists():
        return {"last_run_seconds_ago": None, "pass": 0, "fail": 0, "error": 0, "pending": 0, "fully_autonomous": False}
    try:
        conn = sqlite3.connect(f"file:{TESTS_DB}?mode=ro", uri=True, check_same_thread=False)
        row = conn.execute("SELECT finished_at FROM test_runs ORDER BY finished_at DESC LIMIT 1").fetchone()
        last_run_s = None
        if row and row[0]:
            try:
                last_dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                last_run_s = round((datetime.now(timezone.utc) - last_dt).total_seconds())
            except Exception:
                pass
        runs = conn.execute(
            "SELECT test_id, status FROM test_runs WHERE finished_at = (SELECT MAX(finished_at) FROM test_runs WHERE status != 'pending')"
        ).fetchall() if last_run_s is not None else []
        pass_count = sum(1 for _, s in runs if s == "pass")
        fail_count = sum(1 for _, s in runs if s == "fail")
        err_count = sum(1 for _, s in runs if s == "error")
        pend_count = sum(1 for _, s in runs if s == "pending")
        conn.close()
    except Exception:
        last_run_s = None
        pass_count = fail_count = err_count = pend_count = 0
    return {
        "last_run_seconds_ago": last_run_s,
        "pass": pass_count,
        "fail": fail_count,
        "error": err_count,
        "pending": pend_count,
        "fully_autonomous": fail_count == 0 and err_count == 0,
    }


def _read_traffic_snapshot() -> dict[str, Any]:
    running = False
    last_flow_age = None
    if TRAFFIC_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{TRAFFIC_DB}?mode=ro", uri=True, check_same_thread=False)
            row = conn.execute("SELECT ts FROM flows ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                try:
                    last_dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    last_flow_age = round((datetime.now(timezone.utc) - last_dt).total_seconds())
                    running = last_flow_age < 120
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
    return {"capture_running": running, "last_flow_age_seconds": last_flow_age}


@router.get("/full")
async def full_health(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    t0 = time.perf_counter()

    gateway = await _probe_gateway()
    vllm = await _probe_provider(VLLM_URL, "vllm")
    ollama = await _probe_provider(OLLAMA_URL, "ollama")

    db_ok = False
    db_lat = 0.0
    try:
        t_db = time.perf_counter()
        await session.execute(text("SELECT 1"))
        db_lat = (time.perf_counter() - t_db) * 1000
        db_ok = True
    except Exception:
        pass

    redis_ok = False
    try:
        import redis
        rc = redis.from_url("redis://redis:6379/0", socket_connect_timeout=2)
        redis_ok = await asyncio.to_thread(rc.ping)
        rc.close()
    except Exception:
        pass

    agents_online = 0
    agents_offline = 0
    agents_provisioning = 0
    try:
        for a in (await session.execute(select(Agent))).scalars().all():
            if a.status == "online":
                agents_online += 1
            elif a.status == "offline":
                agents_offline += 1
            elif a.status == "provisioning":
                agents_provisioning += 1
    except Exception:
        pass

    tasks_inbox = 0
    tasks_in_progress = 0
    tasks_review = 0
    tasks_stale = 0
    try:
        tasks_inbox = (await session.execute(text("SELECT COUNT(*) FROM tasks WHERE status='inbox'"))).scalar() or 0
        tasks_in_progress = (await session.execute(text("SELECT COUNT(*) FROM tasks WHERE status='in_progress'"))).scalar() or 0
        tasks_review = (await session.execute(text("SELECT COUNT(*) FROM tasks WHERE status='review'"))).scalar() or 0
        tasks_stale = (await session.execute(
            text("SELECT COUNT(*) FROM tasks WHERE status IN ('in_progress','review') AND updated_at < NOW() - INTERVAL '4 hours'")
        )).scalar() or 0
    except Exception:
        pass

    watchdog_active = False
    last_tick_s = None
    try:
        row = (await session.execute(
            text("SELECT EXTRACT(EPOCH FROM (NOW() - created_at)) FROM activity_events WHERE event_type='watchdog.tick' ORDER BY created_at DESC LIMIT 1")
        )).first()
        if row and row[0] is not None:
            last_tick_s = round(row[0])
            watchdog_active = last_tick_s < 600
    except Exception:
        pass

    tests = _read_tests_db()
    traffic = _read_traffic_snapshot()

    all_ok = (
        gateway.get("reachable")
        and db_ok
        and agents_offline == 0
        and agents_provisioning == 0
        and tasks_stale == 0
        and watchdog_active
    )
    if not vllm.get("reachable") and not ollama.get("reachable"):
        all_ok = False

    verdict = "healthy" if all_ok else "degraded"

    return {
        "verdict": verdict,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "gateway": gateway,
        "providers": {"vllm": vllm, "ollama": ollama},
        "database": {"connected": db_ok, "latency_ms": round(db_lat, 1)},
        "redis": {"connected": redis_ok},
        "traffic": traffic,
        "watchdog": {"last_tick_seconds_ago": last_tick_s, "active": watchdog_active},
        "tests": tests,
        "agents": {"online": agents_online, "offline": agents_offline, "provisioning": agents_provisioning},
        "tasks": {"inbox": tasks_inbox, "in_progress": tasks_in_progress, "review": tasks_review, "stale": tasks_stale},
    }
