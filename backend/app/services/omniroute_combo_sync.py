"""OmniRoute management/context helpers and explicit prompt-routing configuration.

OmniRoute owns combo membership and strategy; the watchdog tracks only the
Jetson's live model/context. The balance CLI opts into prompt-aware auto routing
without resetting existing budgets, permissions or account pins.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_MGMT_URL = os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
from app.services.traffic_etl import _mgmt_token

# Static fallback priority — only used when MC DB has no priority column
# value for the provider (or import fails). Owner-driven order comes from
# `provider_capacity.priority` (0 = highest, T80) at runtime; this list
# is a last-resort tiebreak before alphabetical.
_PRIORITY = ["vllm", "LMStudio", "ollama"]


_COMBO_NAME = "omniroute-auto"

# Path inside the backend container — bind-mounted from host
# `~/.openclaw/openclaw.json`. Same env knob as everywhere else.
_OPENCLAW_JSON = Path(
    os.environ.get(
        "OPENCLAW_JSON_PATH",
        os.path.join(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"), "openclaw.json"),
    ),
)
# Conservative floor — applies when probes fail or upstream lies.
_DEFAULT_CONTEXT_WINDOW = 131072
_DEFAULT_MAX_OUTPUT = 4096


def prompt_routing_config(config: dict[str, Any]) -> dict[str, Any]:
    """Enable existing intent/complexity scoring; unknown/scoring failures use RR."""
    result = dict(config)
    auto = dict(result.get("auto") or {})
    auto.update(promptAwareRouting=True, complexityAwareRouting=True, explorationRate=0)
    auto.setdefault("modePack", "quality-first")
    result.update(auto=auto)
    result.pop("complexityAwareRouting", None)  # OmniRoute PUT strips this legacy top-level key
    return result


def _mgmt_headers(*, write: bool = False) -> dict[str, str]:
    headers = {"Cookie": f"auth_token={_mgmt_token()}", "Content-Type": "application/json"}
    if not write:
        origin = _MGMT_URL.replace("//omniroute:", "//localhost:")
        headers.update({"Origin": origin, "Referer": f"{origin}/dashboard"})
    return headers


async def _get(path: str) -> dict[str, Any] | None:
    if not _mgmt_token():
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(_MGMT_URL.rstrip("/") + path, headers=_mgmt_headers())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def _probe_upstream_context_window(base_url: str) -> int | None:
    """GET `{base_url}/models`, return the largest context-window field.

    Field name varies by upstream:
      - vllm:   `max_model_len`
      - Anthropic: `context_window`
      - OpenAI-Compat: `context_length`, `contextWindow`
    Returns max across models on the provider. None if nothing parseable.
    """
    if not base_url:
        return None
    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return None
        body = r.json() or {}
    except Exception as exc:
        logger.debug("omniroute_combo_sync.probe_err url=%s err=%s", url, exc)
        return None

    models = body.get("data") if isinstance(body, dict) else body
    if not isinstance(models, list):
        return None
    best: int | None = None
    for m in models:
        if not isinstance(m, dict):
            continue
        for key in ("max_model_len", "context_length", "context_window", "contextWindow"):
            v = m.get(key)
            if isinstance(v, int) and v > 0:
                best = v if best is None else max(best, v)
                break
    return best


_OLLAMA_CTX_RE = None  # lazy-compiled regex


async def _probe_ollama_library_context(model_name: str) -> int | None:
    """T103 v22 (2026-05-19) — scrape ollama.com/library/<model> for context.

    Ollama Cloud's `/v1/models` does NOT expose context window, but the public
    model page does. Pattern owner verified:
      `col-span-2 text-neutral-500">256K`     → 256 * 1024 = 262144
      `col-span-2 text-neutral-500">1M`       → 1024 * 1024 = 1048576
    Suffix conventions (industry-standard, matches Qwen/DeepSeek/Kimi specs):
      K = 1024
      M = 1024 * 1024
      B = 1024 * 1024 * 1024
    Strip variant suffix (`:cloud`, `:free`, `:latest`, `:7b`...) before lookup.
    """
    global _OLLAMA_CTX_RE
    if _OLLAMA_CTX_RE is None:
        import re

        # `<p class="col-span-2 text-neutral-500">128K</p>` = context.
        # `<p x-test-model-tag-size class="col-span-2 text-neutral-500">14GB</p>`
        # = model size — must NOT match. Anchor by the exact opening `<p class=…`
        # (no extra attrs) AND require closing `</p>` with mandatory K/M suffix.
        _OLLAMA_CTX_RE = re.compile(
            r'<p class="col-span-2 text-neutral-500">\s*(\d+)\s*([KM])\s*</p>',
        )
    if not model_name:
        return None
    slug = model_name.split(":", 1)[0]
    url = f"https://ollama.com/library/{slug}"
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return None
        m = _OLLAMA_CTX_RE.search(r.text)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2) or ""
        mul = {"K": 1024, "M": 1024 * 1024}.get(unit, 1)
        ctx = n * mul
        return ctx if ctx > 0 else None
    except Exception as exc:
        logger.debug(
            "omniroute_combo_sync.ollama_lib_err model=%s url=%s err=%s",
            model_name,
            url,
            exc,
        )
        return None


def _inplace_write_json(path: Path, payload: str) -> bool:
    """In-place truncate+write. Preserves inode/owner/mode so the host
    user keeps r/w even when backend container's `appuser` maps to root
    on bind-mounted volumes.
    """
    try:
        with open(path, "r+") as fh:
            fh.seek(0)
            fh.write(payload)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception as exc:
        logger.warning("omniroute_combo_sync.write_err path=%s err=%s", path, exc)
        return False


def _sync_openclaw_json_context_window(value: int) -> bool:
    """Patch `models.providers.omniroute.models[0].contextWindow` in
    openclaw.json + every per-agent agent/models.json. Atomic in-place
    writes. Returns True if anything changed.

    Gateway hot-reloads on file change. Per-agent files are read by the
    agent runtime at session-create time — without syncing them, new
    sessions inherit the stale value despite openclaw.json being correct.
    """
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    any_changed = False

    # 1. openclaw.json
    if _OPENCLAW_JSON.exists():
        try:
            cfg = json.loads(_OPENCLAW_JSON.read_text())
        except Exception as exc:
            logger.warning("omniroute_combo_sync.openclaw_read_err err=%s", exc)
        else:
            block = (cfg.get("models") or {}).get("providers", {}).get("omniroute") or {}
            models = block.get("models") or []
            if models and models[0].get("contextWindow") != value:
                cur = models[0].get("contextWindow")
                models[0]["contextWindow"] = value
                if _inplace_write_json(_OPENCLAW_JSON, json.dumps(cfg, indent=2)):
                    logger.info(
                        "omniroute_combo_sync.openclaw_context_updated old=%s new=%d",
                        cur,
                        value,
                    )
                    any_changed = True

    # 2. Per-agent agent/models.json snapshots
    agents_dir = Path(_OPENCLAW_JSON.parent / "agents")
    per_agent_changed = False
    if agents_dir.exists():
        for agent_models in agents_dir.glob("*/agent/models.json"):
            try:
                d = json.loads(agent_models.read_text())
            except Exception:
                continue
            prov = (d.get("providers") or {}).get("omniroute")
            if not prov:
                continue
            ms = prov.get("models") or []
            if not ms:
                continue
            cur = ms[0].get("contextWindow")
            if cur == value:
                continue
            ms[0]["contextWindow"] = value
            if _inplace_write_json(agent_models, json.dumps(d, indent=2)):
                logger.info(
                    "omniroute_combo_sync.agent_models_context_updated agent=%s old=%s new=%d",
                    agent_models.parent.parent.name,
                    cur,
                    value,
                )
                any_changed = True
                per_agent_changed = True

    # 2b. Gateway watches openclaw.json (chokidar) but NOT per-agent
    # models.json. Touch openclaw.json mtime so the hot-reload fires +
    # re-reads agent configs (which then pick up the new per-agent
    # contextWindow). Self-managing — owner never needs `systemctl
    # restart openclaw-gateway` after a context-window change.
    if per_agent_changed and _OPENCLAW_JSON.exists():
        try:
            os.utime(_OPENCLAW_JSON, None)
            logger.info(
                "omniroute_combo_sync.openclaw_touched (per-agent change → trigger gateway hot-reload)",
            )
        except Exception as exc:
            logger.warning("omniroute_combo_sync.touch_err err=%s", exc)

    # 3. Scrub stale `contextTokens` from session entries so gateway
    # re-resolves dynamically from the now-current openclaw.json /
    # models.json instead of using a value frozen at session-create.
    if agents_dir.exists():
        for sessions_file in agents_dir.glob("*/sessions/sessions.json"):
            try:
                sd = json.loads(sessions_file.read_text())
            except Exception:
                continue
            changed = False
            for key, entry in sd.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("contextTokens") is not None and entry.get("contextTokens") != value:
                    entry.pop("contextTokens", None)
                    entry.pop("modelContextWindow", None)
                    changed = True
            if changed:
                _inplace_write_json(sessions_file, json.dumps(sd, indent=2))
                logger.info(
                    "omniroute_combo_sync.sessions_scrubbed agent=%s",
                    sessions_file.parent.parent.name,
                )
                any_changed = True

    return any_changed
