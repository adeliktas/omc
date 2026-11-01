"""Local proxy for ollama.com that captures LLM flows into traffic SQLite.

Why: agents using ollama-cloud bypass mitmdump (which only listens on the vllm
endpoint). Routing ollama traffic through this proxy lets the Traffic tab and
scorecard see every cloud call.

Wire-up:
  openclaw.json → models.providers.ollama.baseUrl = "http://localhost:8000/api/v1/proxy/ollama/v1"
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response, HTTPException
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SMAsyncSession

from app.core.logging import get_logger
from app.models.provider_capacity import ProviderCapacity

logger = get_logger(__name__)

# Reuse traffic ingest helpers — no separate DB connection.
from app.api.traffic import _COLS, _INSERT, _row_values, _db, _db_lock

router = APIRouter(prefix="/proxy/ollama", tags=["proxy"])

_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@db:5432/mission_control"
)
_engine = create_async_engine(_DATABASE_URL, pool_size=3, max_overflow=5)

_GATEWAY_HOST = os.environ.get("OPENCLAW_GATEWAY_HOST", "172.17.0.1:18789")


def _gateway_alive() -> bool:
    host, _, port = _GATEWAY_HOST.partition(":")
    try:
        port = int(port) if port else 18789
    except (ValueError, TypeError):
        port = 18789
    try:
        s = socket.create_connection((host, port), timeout=2.0)
        s.close()
        return True
    except OSError:
        return False


async def _ollama_offline() -> bool:
    async with SMAsyncSession(_engine) as session:
        result = await session.execute(
            sa_select(ProviderCapacity).where(
                ProviderCapacity.provider_name == "ollama"
            )
        )
        p = result.scalar_one_or_none()
        if not p:
            return False
        return p.status == "offline"


async def _ollama_api_key() -> str | None:
    """T23e: return `provider_capacity.api_key` for the ollama row, or None.
    Allows rotating the key via the admin UI instead of restarting the
    container to pick up a new env var.
    """
    async with SMAsyncSession(_engine) as session:
        result = await session.execute(
            sa_select(ProviderCapacity).where(
                ProviderCapacity.provider_name == "ollama"
            )
        )
        p = result.scalar_one_or_none()
        return p.api_key if p and p.api_key else None


OLLAMA_BASE = "https://ollama.com"
# Capture both OpenAI-compat (/v1/...) and ollama-native (/api/chat, /api/generate) endpoints.
LLM_PATHS = (
    "/v1/chat/completions", "/v1/completions", "/v1/responses",
    "/api/chat", "/api/generate",
)

_AGENT_NAME_RE = re.compile(r"`AGENT_NAME=([^`\n]+)`")
_AGENT_ID_RE = re.compile(r"`AGENT_ID=([^`\n]+)`")
_BOARD_ID_RE = re.compile(r"`BOARD_ID=([^`\n]+)`")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}


def _extract_agent(messages: list) -> dict:
    for msg in messages:
        if msg.get("role") != "system":
            continue
        c = msg.get("content", "")
        if not isinstance(c, str):
            continue
        n = _AGENT_NAME_RE.search(c)
        a = _AGENT_ID_RE.search(c)
        b = _BOARD_ID_RE.search(c)
        if n or a:
            return {
                "agent_name": n.group(1).strip() if n else None,
                "agent_id": a.group(1).strip() if a else None,
                "board_id": b.group(1).strip() if b else None,
            }
    return {"agent_name": None, "agent_id": None, "board_id": None}


def _last_user_message(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        return str(item.get("text", ""))[:400]
            return str(content)[:400]
    return ""


def _shorten(text: str, n: int = 220) -> str:
    if not text:
        return ""
    return text.replace("\n", " ").strip()[:n]


def _parse_sse_body(raw: bytes) -> dict[str, Any]:
    """Reassemble streaming response into a unified dict."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict | None = None
    finish_reason: str | None = None
    tool_calls: list = []
    model = ""
    last_obj: dict | None = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        last_obj = obj
        if obj.get("model"):
            model = obj["model"]
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            r = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if r:
                reasoning_parts.append(r)
            if delta.get("tool_calls"):
                tool_calls = delta["tool_calls"]
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr
    if last_obj is None:
        return {}
    return {
        "model": model,
        "choices": [{
            "finish_reason": finish_reason or "",
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "reasoning": "".join(reasoning_parts) or None,
                "tool_calls": tool_calls or None,
            },
        }],
        **({"usage": usage} if usage else {}),
    }


def _persist(flow: dict[str, Any]) -> None:
    try:
        conn = _db()
        with _db_lock:
            conn.execute(_INSERT, _row_values(flow))
            conn.commit()
    except Exception:
        # Never break the proxy on persistence failure.
        pass


@router.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(full_path: str, request: Request) -> Response:
    path = "/" + full_path
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # ollama.com expects its host
    headers["host"] = "ollama.com"
    # T23e (2026-05-14): pull bearer token from provider_capacity.api_key
    # instead of env, so the admin UI rotates the key. Env fallback retained.
    try:
        api_key = await _ollama_api_key()
    except Exception:
        api_key = None
    if not api_key:
        api_key = os.environ.get("OLLAMA_API_KEY") or os.environ.get("OLLAMA_TOKEN")
    if api_key and "authorization" not in {k.lower() for k in headers}:
        headers["Authorization"] = f"Bearer {api_key}"

    method = request.method
    # Forward
    target_url = f"{OLLAMA_BASE}{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    start = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    # Decide if streaming
    req_body_dict: dict[str, Any] = {}
    if body:
        try:
            req_body_dict = json.loads(body)
        except Exception:
            req_body_dict = {}
    is_stream = bool(req_body_dict.get("stream"))
    if is_stream and "stream_options" not in req_body_dict:
        req_body_dict["stream_options"] = {"include_usage": True}
        body = json.dumps(req_body_dict).encode()

    # Gateway gating — refuse if platform kill-switch active
    if not _gateway_alive():
        raise HTTPException(status_code=503, detail="Gateway unreachable")

    # Provider offline check — respect the /providers toggle
    if await _ollama_offline():
        raise HTTPException(status_code=503, detail="Ollama provider is offline")

    async with httpx.AsyncClient(timeout=300, follow_redirects=False) as client:
        try:
            upstream = await client.request(
                method, target_url, headers=headers, content=body,
            )
        except Exception as e:
            return Response(
                content=json.dumps({"error": str(e)}).encode(),
                status_code=502,
                media_type="application/json",
            )

    latency_ms = int((time.monotonic() - start) * 1000)
    res_bytes = upstream.content
    status_code = upstream.status_code

    # Build response — drop hop-by-hop headers
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}

    # Capture flow only for LLM completion endpoints
    if any(path.startswith(p) for p in LLM_PATHS):
        try:
            is_native = path.startswith("/api/chat") or path.startswith("/api/generate")
            res_body: dict[str, Any] = {}
            if is_stream and not is_native:
                res_body = _parse_sse_body(res_bytes)
            elif is_native:
                # ollama-native streaming = NDJSON (newline-delimited JSON) one obj per chunk.
                # Reassemble or take final.
                content_parts = []
                last_obj = {}
                for line in res_bytes.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    last_obj = obj
                    msg_obj = obj.get("message") or {}
                    if msg_obj.get("content"):
                        content_parts.append(msg_obj["content"])
                if last_obj:
                    res_body = {
                        "model": last_obj.get("model", ""),
                        "choices": [{
                            "finish_reason": last_obj.get("done_reason") or ("stop" if last_obj.get("done") else ""),
                            "message": {
                                "role": "assistant",
                                "content": "".join(content_parts),
                                "reasoning": (last_obj.get("message") or {}).get("thinking") or None,
                                "tool_calls": (last_obj.get("message") or {}).get("tool_calls") or None,
                            },
                        }],
                        "usage": {
                            "prompt_tokens": last_obj.get("prompt_eval_count"),
                            "completion_tokens": last_obj.get("eval_count"),
                            "total_tokens": (
                                (last_obj.get("prompt_eval_count") or 0)
                                + (last_obj.get("eval_count") or 0)
                            ) or None,
                        },
                    }
            else:
                try:
                    res_body = json.loads(res_bytes)
                except Exception:
                    res_body = {}

            messages = req_body_dict.get("messages", [])
            agent = _extract_agent(messages)
            usage = res_body.get("usage") or {}
            choices = res_body.get("choices") or []
            choice = choices[0] if choices else {}
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
            tool_calls = msg.get("tool_calls") or []

            model_id = req_body_dict.get("model", "?")
            # Tag as ollama provider
            if "/" not in model_id and model_id != "?":
                model_id = f"ollama/{model_id}"

            flow = {
                "ts": started_iso,
                "host": "ollama.com",
                "path": path,
                "method": method,
                "model": model_id,
                "status_code": status_code,
                "latency_ms": latency_ms,
                **agent,
                "messages_count": len(messages),
                "has_system": any(m.get("role") == "system" for m in messages),
                "has_tools": bool(req_body_dict.get("tools") or req_body_dict.get("functions")),
                "tools_count": len(req_body_dict.get("tools") or req_body_dict.get("functions") or []),
                "stream": is_stream,
                "temperature": req_body_dict.get("temperature"),
                "max_tokens": req_body_dict.get("max_tokens") or req_body_dict.get("max_completion_tokens"),
                "last_user_message": _last_user_message(messages),
                "finish_reason": choice.get("finish_reason", ""),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "content_preview": _shorten(content),
                "reasoning_preview": _shorten(reasoning) if reasoning else "",
                "has_tool_calls": bool(tool_calls),
                "tool_calls_count": len(tool_calls),
                "req_body": req_body_dict,
                "res_body": res_body,
            }
            _persist(flow)
        except Exception:
            # Never break upstream on persistence
            pass

    return Response(content=res_bytes, status_code=status_code, headers=out_headers,
                    media_type=upstream.headers.get("content-type"))
