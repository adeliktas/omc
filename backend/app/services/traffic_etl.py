"""T103 v6 (2026-05-18) — Traffic ETL: poll OmniRoute /api/usage/call-logs
and mirror into MC's `llm_flows.db` so `/traffic` UI shows all gateway
inference traffic without MC sitting in the request path.

OmniRoute is the single egress for all LLM traffic post-T103. Its
call-logs endpoint exposes every chat-completion call with: timestamp,
model, requested-model, provider, account, duration, tokens, status,
combo info, API-key name.

Run as a background asyncio task started from `app.main` lifespan or
called from the watchdog loop. 10s poll interval default; uses the
`omniroute_id` UNIQUE column on `flows` to dedup.

Auth: dashboard JWT cookie via `OMNIROUTE_MGMT_TOKEN`.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Any

from app.core.logging import get_logger
from app.services.runtime_settings import get_setting

logger = get_logger(__name__)


# T104 round-4 (2026-05-24): per-agent identity sniffer DROPPED. Live
# heartbeat traffic strips system prompts before reaching the gateway,
# so the sniffer matched on zero recent flows. `agent_name` falls back
# to OmniRoute's `apiKeyName` (collapses to "gateway-runtime" across
# all agents). Per-agent traffic breakdown is not currently available.
# Real fix would require openclaw runtime to propagate `X-Agent-Id`
# through to OmniRoute. Tracked in improvements-todo.

_MGMT_URL = os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
_MGMT_TOKEN = os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
_MGMT_JWT_SECRET = os.environ.get("OMNIROUTE_JWT_SECRET", "")

# Self-minted mgmt token cache: (token, expiry_epoch). Auto-renews so the
# OmniRoute mgmt token can never expire and stall the /traffic ETL.
_minted_token: str = ""
_minted_exp: float = 0.0


def _mgmt_token() -> str:
    """Return a VALID OmniRoute mgmt token. If OMNIROUTE_JWT_SECRET is set,
    self-mint a fresh short-lived JWT (`{authenticated:true, exp}`, HS256) and
    cache it — this is what OmniRoute's management surface accepts via the
    `auth_token` cookie. Falls back to the static OMNIROUTE_MGMT_TOKEN env
    (which expires every 30 days) when no secret is available.
    """
    global _minted_token, _minted_exp
    if not _MGMT_JWT_SECRET:
        return _MGMT_TOKEN
    now = time.time()
    if _minted_token and now < _minted_exp - 300:  # reuse until 5 min before exp
        return _minted_token
    import base64
    import hashlib
    import hmac
    import json as _j

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    exp = int(now) + 3600  # 1-hour token, refreshed well before expiry
    header = _b64(_j.dumps({"alg": "HS256"}, separators=(",", ":")).encode())
    payload = _b64(_j.dumps({"authenticated": True, "exp": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_MGMT_JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    _minted_token = f"{header}.{payload}.{sig}"
    _minted_exp = exp
    return _minted_token
# Traffic-min (2026-08-24): 10s → 30s. This is a mgmt-API poll of OmniRoute
# call-logs for the traffic dashboard (which itself refreshes at 60s), not an
# inference path — 30s freshness is ample and cuts the poll rate 3x.
_POLL_INTERVAL = float(get_setting("TRAFFIC_ETL_INTERVAL_SECONDS"))
_POLL_LIMIT = int(os.environ.get("TRAFFIC_ETL_POLL_LIMIT", "200"))
_FLOWS_DB = os.environ.get(
    "FLOWS_DB_PATH", "/traffic-data/llm_flows.db",
)

# T107 round-7 (2026-05-27): agent_name → board_id cache. Refreshed every
# 5 min via Postgres query. Avoids per-flow DB lookup latency.
_AGENT_BOARD_CACHE: dict[str, str | None] = {}
_AGENT_BOARD_CACHE_AT: float = 0.0


def _agent_board_lookup(agent_name: str) -> str | None:
    """Return the board_id of an MC agent by name. Cached 5 min."""
    import time as _t
    global _AGENT_BOARD_CACHE_AT
    if _t.time() - _AGENT_BOARD_CACHE_AT > 300:
        try:
            import psycopg
            dsn = os.environ.get("DATABASE_URL", "").replace("+psycopg", "").replace("+asyncpg", "")
            if dsn:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name, board_id FROM agents WHERE board_id IS NOT NULL")
                        _AGENT_BOARD_CACHE.clear()
                        for n, b in cur.fetchall():
                            _AGENT_BOARD_CACHE[n] = str(b) if b else None
                _AGENT_BOARD_CACHE_AT = _t.time()
        except Exception as exc:
            logger.debug("traffic_etl.agent_board_cache_refresh_failed err=%s", exc)
    return _AGENT_BOARD_CACHE.get(agent_name)


def _mgmt_headers() -> dict[str, str]:
    origin = _MGMT_URL.replace("//omniroute:", "//localhost:")
    return {
        "Cookie": f"auth_token={_mgmt_token()}",
        "Origin": origin,
        "Referer": f"{origin}/dashboard",
    }


async def _fetch_call_logs(limit: int) -> list[dict[str, Any]]:
    if not _mgmt_token():
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                _MGMT_URL.rstrip("/") + f"/api/usage/call-logs?limit={limit}",
                headers=_mgmt_headers(),
            )
        if r.status_code != 200:
            logger.debug("traffic_etl.fetch_failed status=%d", r.status_code)
            return []
        return r.json() or []
    except Exception as exc:
        logger.debug("traffic_etl.fetch_err err=%s", exc)
        return []


async def _fetch_call_log_detail(log_id: str) -> dict[str, Any] | None:
    """T103 v7 (2026-05-18): pull `requestBody` + `responseBody` for a single
    call-log row so MC `/traffic` UI can show the conversation contents.
    """
    if not _mgmt_token():
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                _MGMT_URL.rstrip("/") + f"/api/usage/call-logs/{log_id}",
                headers=_mgmt_headers(),
            )
        if r.status_code != 200:
            return None
        return r.json() or None
    except Exception:
        return None


def _insert_flow_with_bodies(r: dict[str, Any], detail: dict[str, Any] | None) -> bool:
    """Insert one call-log row. Returns True if NEW."""
    or_id = r.get("id")
    if not or_id:
        return False
    # Skip combo-internal backend legs: OmniRoute logs one interim leg row per
    # combo→backend attempt (no client apiKeyName, raw model name, provider "-")
    # IN ADDITION to the client-facing result row. These legs carry REAL
    # statuses (503/524/504/429 as well as the old status:0), so filtering only
    # status:0 left thousands of them ingested → /traffic read ~1.6× inflated.
    # The client-facing row always carries an apiKeyName (e.g. gateway-runtime);
    # keep only those, drop every keyless leg regardless of status. (2026-08-17)
    _status = r.get("status")
    _keyname = (r.get("apiKeyName") or "").strip()
    if not _keyname:
        return False
    tokens = r.get("tokens") or {}
    err = r.get("error")
    err_str = err if isinstance(err, str) else (err.get("type") if isinstance(err, dict) else None)

    req_body = None
    res_body = None
    messages_count = 0
    has_system = 0
    has_tools = 0
    tools_count = 0
    last_user_message = ""
    finish_reason = ""
    stream_flag = 0
    temperature = None
    max_tokens = None
    content_preview = ""
    has_tool_calls = 0
    tool_calls_count = 0
    if detail:
        rb = detail.get("requestBody")
        rs = detail.get("responseBody")
        if rb is not None:
            try:
                import json as _j
                req_body = _j.dumps(rb)[:65536]
                msgs = (rb.get("messages") or []) if isinstance(rb, dict) else []
                messages_count = len(msgs)
                has_system = int(any((m.get("role") == "system") for m in msgs if isinstance(m, dict)))
                tools = (rb.get("tools") or []) if isinstance(rb, dict) else []
                tools_count = len(tools)
                has_tools = int(tools_count > 0)
                stream_flag = int(bool(rb.get("stream"))) if isinstance(rb, dict) else 0
                temperature = rb.get("temperature") if isinstance(rb, dict) else None
                max_tokens = (rb.get("max_tokens") or rb.get("max_completion_tokens")) if isinstance(rb, dict) else None
                # last user message preview
                for m in reversed(msgs):
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            last_user_message = c[:500]
                            break
                        if isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    last_user_message = (part.get("text") or "")[:500]
                                    break
                            if last_user_message:
                                break
            except Exception:
                pass
        if rs is not None:
            try:
                import json as _j
                res_body = _j.dumps(rs)[:65536]
                choices = (rs.get("choices") or []) if isinstance(rs, dict) else []
                if choices and isinstance(choices[0], dict):
                    finish_reason = choices[0].get("finish_reason") or ""
                    msg = choices[0].get("message") or {}
                    if isinstance(msg, dict):
                        c = msg.get("content")
                        if isinstance(c, str):
                            content_preview = c[:500]
                        tc = msg.get("tool_calls") or []
                        tool_calls_count = len(tc)
                        has_tool_calls = int(tool_calls_count > 0)
            except Exception:
                pass

    # T107 round-7 (2026-05-27): board_id lookup. When apiKeyName matches an
    # MC agent name, pull that agent's board_id so finance.cost_attribution
    # can compute per-board spend. Cached in memory (this module) to avoid
    # per-row Postgres queries.
    agent_name_value = r.get("apiKeyName") or ""
    board_id_value: str | None = None
    if agent_name_value and agent_name_value not in ("", "gateway-runtime"):
        board_id_value = _agent_board_lookup(agent_name_value)

    conn = sqlite3.connect(_FLOWS_DB, timeout=5.0)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO flows (
                    omniroute_id, ts, host, path, method,
                    model, status_code, latency_ms,
                    agent_name, board_id, prompt_tokens, completion_tokens, total_tokens,
                    finish_reason, req_body, res_body,
                    messages_count, has_system, has_tools, tools_count,
                    stream, temperature, max_tokens, last_user_message,
                    content_preview, has_tool_calls, tool_calls_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    or_id,
                    r.get("timestamp"),
                    f"omniroute:{r.get('account', '')}",
                    r.get("path"),
                    r.get("method"),
                    r.get("requestedModel") or r.get("model"),
                    r.get("status"),
                    r.get("duration"),
                    agent_name_value,
                    board_id_value,
                    tokens.get("in"),
                    tokens.get("out"),
                    (tokens.get("in") or 0) + (tokens.get("out") or 0) or None,
                    err_str or finish_reason,
                    req_body,
                    res_body,
                    messages_count, has_system, has_tools, tools_count,
                    stream_flag, temperature, max_tokens, last_user_message,
                    content_preview, has_tool_calls, tool_calls_count,
                ),
            )
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()


async def poll_once() -> int:
    rows = await _fetch_call_logs(_POLL_LIMIT)
    if not rows:
        return 0
    # First pass: figure out which rows are new (need detail fetch)
    conn = sqlite3.connect(_FLOWS_DB, timeout=5.0)
    try:
        cur = conn.cursor()
        ids = [r.get("id") for r in rows if r.get("id")]
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        cur.execute(
            f"SELECT omniroute_id FROM flows WHERE omniroute_id IN ({placeholders})",
            ids,
        )
        existing = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    new_rows = [r for r in rows if r.get("id") and r["id"] not in existing]

    inserted = 0
    # Fetch detail concurrently (semaphore-limited) for new rows only.
    import asyncio as _aio
    sem = _aio.Semaphore(8)

    async def _one(r: dict[str, Any]):
        nonlocal inserted
        async with sem:
            detail = await _fetch_call_log_detail(r["id"])
        if _insert_flow_with_bodies(r, detail):
            inserted += 1

    await _aio.gather(*[_one(r) for r in new_rows], return_exceptions=True)
    if inserted:
        # The TRAFFIC_MAX_ROWS cap was only enforced on the dead /traffic/ingest
        # path, so this, the only live writer, grew llm_flows.db to 5.8 GB.
        from app.api.traffic import _trim_if_needed

        conn = sqlite3.connect(_FLOWS_DB, timeout=5.0)
        try:
            _trim_if_needed(conn)
        finally:
            conn.close()
    return inserted


async def traffic_etl_loop() -> None:
    """Run forever. Polls OmniRoute → llm_flows.db every `_POLL_INTERVAL`s."""
    if not _mgmt_token():
        logger.info("traffic_etl.disabled reason=no_OMNIROUTE_MGMT_TOKEN")
        return
    logger.info("traffic_etl.loop_started interval=%.0fs limit=%d", _POLL_INTERVAL, _POLL_LIMIT)
    while True:
        try:
            n = await poll_once()
            if n:
                logger.info("traffic_etl.poll inserted=%d", n)
        except Exception as exc:
            logger.warning("traffic_etl.poll_err err=%s", exc)
        await asyncio.sleep(_POLL_INTERVAL)
