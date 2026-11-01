"""LLM-evaluator service — single-shot vLLM probes for routing + completion.

T65 (2026-05-15): replaces hardcoded keyword-only logic in
`watchdog_loop.py` with cheap one-shot LLM calls. Falls back to the
keyword path on any error so the system never blocks on an LLM outage.

Public API:
    pick_role_for_task(title, description, candidate_roles)  -> str | None
    is_completion_comment(comment)                           -> bool | None

`None` always means "LLM unavailable / unable to decide" → caller falls
back to the hardcoded path.

The proxy already gates on `_gateway_alive`, model-prefix-strips, traffic-
captures, and rate-limits — we hit `/api/v1/proxy/vllm/v1/chat/completions`
inside the same compose network as the backend.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from app.core.logging import get_logger
from app.services.vllm_model import resolve_vllm_model

logger = get_logger(__name__)

# T103 (2026-05-17): evaluator routes through OmniRoute instead of the
# direct vLLM proxy. Keeps legacy env vars (LLM_EVALUATOR_VLLM_*) as
# fallbacks so deployments without OmniRoute can still run.
_OMNIROUTE_RUNTIME_URL = os.environ.get(
    "OMNIROUTE_RUNTIME_URL", "http://omniroute:20129/v1",
)
_OMNIROUTE_RUNTIME_KEY = os.environ.get("OMNIROUTE_RUNTIME_KEY", "")
_VLLM_PROXY_URL = os.environ.get(
    "LLM_EVALUATOR_VLLM_URL",
    _OMNIROUTE_RUNTIME_URL.rstrip("/") + "/chat/completions",
)
# Empty default → resolve the live vllm model dynamically (never hardcode; the
# owner swaps the served model). Set LLM_EVALUATOR_VLLM_MODEL only to pin.
_VLLM_MODEL = os.environ.get("LLM_EVALUATOR_VLLM_MODEL", "").strip()
# Disable evaluator entirely by setting LLM_EVALUATOR_ENABLED=0 (testing /
# offline mode). Default on.
_ENABLED = os.environ.get("LLM_EVALUATOR_ENABLED", "1").lower() not in {
    "0", "false", "no", "off",
}
_TIMEOUT = float(os.environ.get("LLM_EVALUATOR_TIMEOUT_SECONDS", "8.0"))
_MAX_RETRIES = int(os.environ.get("LLM_EVALUATOR_MAX_RETRIES", "0"))

# T107 round-5 (2026-05-27): OmniRoute semaphore-leak detector. Tracks
# timestamps of "Semaphore timeout after 30000ms" responses. Watchdog
# inspects this list each tick to decide whether to bounce OR.
_OR_SEM_STRIKES: list[float] = []


def get_or_semaphore_strikes() -> list[float]:
    """Watchdog reads this to count recent OR semaphore-leak hits."""
    return list(_OR_SEM_STRIKES)


def reset_or_semaphore_strikes() -> None:
    """Called after OR restart attempt clears the leak."""
    _OR_SEM_STRIKES.clear()

# Reuse a single httpx client; opened lazily so import-time has no side
# effects (tests that monkeypatch the URL can substitute before first
# call).
_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOCK = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        async with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.AsyncClient(timeout=_TIMEOUT)
    return _CLIENT


async def _call_vllm(
    prompt: str,
    *,
    max_tokens: int = 32,
    system_prompt: str | None = None,
) -> str | None:
    """Single one-shot call. Returns the assistant content string or None
    on any error.

    Adds `AGENT_NAME=llm-evaluator AGENT_ID=llm-evaluator-001` system tag so
    proxy_vllm's `_extract_agent` can identify the caller and rate-limit /
    throttle independently of real agents.
    """
    if not _ENABLED:
        return None
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are an internal task-routing helper. "
                "`AGENT_NAME=llm-evaluator` `AGENT_ID=llm-evaluator-001`. "
                "Respond strictly in the format requested. No prose, no "
                "markdown, no explanation."
                + (f"\n{system_prompt}" if system_prompt else "")
            ),
        },
        {"role": "user", "content": prompt},
    ]
    # T75 (2026-05-16): evaluator was hitting `finish_reason: length` on
    # reasoning-mode models (Qwen3.6-thinking). With thinking enabled, the
    # model emits a 500-2000-token reasoning trace BEFORE the final answer,
    # so a 24-token budget never reaches the actual reply. Two guards:
    #   1. `chat_template_kwargs.enable_thinking=False` — vLLM-specific
    #      extension; tells the chat template to skip the thinking block.
    #      Ignored by upstream that doesn't recognise the field.
    #   2. Bump the floor on `max_tokens` to 32 so even non-thinking models
    #      have headroom for a multi-word role name like "Security Architect".
    # T103 (2026-05-17): OmniRoute forces stream_options when stream=False
    # and vLLM upstream rejects → use stream=True + accumulate chunks.
    payload = {
        "model": _VLLM_MODEL or resolve_vllm_model(),
        "messages": messages,
        "max_tokens": max(max_tokens, 32),
        "temperature": 0.0,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    if _OMNIROUTE_RUNTIME_KEY:
        headers["Authorization"] = f"Bearer {_OMNIROUTE_RUNTIME_KEY}"
    attempts = max(1, _MAX_RETRIES + 1)
    client = await _get_client()
    for i in range(attempts):
        try:
            r = await client.post(
                _VLLM_PROXY_URL,
                json=payload,
                headers=headers,
            )
            if r.status_code != 200:
                logger.debug(
                    "llm_evaluator.non_200 status=%d body=%s",
                    r.status_code, r.text[:200],
                )
                continue
            # T103: parse SSE stream chunks (OmniRoute requires stream=True
            # to avoid vLLM stream_options rejection). Accumulate
            # `choices[0].delta.content` across `data: {...}` lines.
            content_parts: list[str] = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload_str = line[5:].strip()
                if payload_str in {"", "[DONE]"}:
                    continue
                try:
                    chunk = json.loads(payload_str)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
            content = "".join(content_parts).strip()
            if not content:
                return None
            return content
        except Exception as exc:
            logger.debug("llm_evaluator.call_failed attempt=%d err=%s", i, exc)
    return None


async def call_model(
    model_id: str,
    prompt: str,
    *,
    system_prompt: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    timeout_seconds: float | None = None,
) -> str | None:
    """T105 Phase 3 — tier-aware LLM caller.

    Calls OmniRoute runtime endpoint with `model_id` (e.g.
    `ollama/deepseek-v4-pro:cloud`, `vllm/Qwen3.6-35B-A3B-FP8`). Returns
    assistant content string or None on any error. Owner directive: NO
    per-task token cap on planning, so `max_tokens` default is generous
    (2048) and caller can bump higher.

    Mirrors `_call_vllm` payload shape (stream=True, thinking disabled)
    but parametric on model.
    """
    if not _ENABLED or not model_id:
        return None
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max(max_tokens, 32),
        "temperature": temperature,
        "stream": True,
        # T107 (2026-05-26): tell reasoning-model upstreams to emit `content`
        # directly instead of burning tokens on a `reasoning` trace. Without
        # this, deepseek-v4-pro returns `content=""` + a long `reasoning`
        # field — OmniRoute interprets empty content as failure and returns
        # 502. `reasoning_effort=low` is honored by ollama-cloud deepseek
        # models. vllm uses `chat_template_kwargs.enable_thinking=False`
        # for the same effect.
        "reasoning_effort": "low",
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    if _OMNIROUTE_RUNTIME_KEY:
        headers["Authorization"] = f"Bearer {_OMNIROUTE_RUNTIME_KEY}"

    timeout = timeout_seconds if timeout_seconds is not None else max(_TIMEOUT, 60.0)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        # T-502-fix (2026-07-02): the single-GPU vllm behind OmniRoute emits
        # intermittent 502/503/504/524 on scheduler stalls; these are transient,
        # so retry a few times with backoff before giving up (was 0 retries →
        # ~60% no-auth error rate on `omniroute-auto`). 429/529 (rate-limit) are
        # NOT retried here — they fall through to the cooldown-cache below.
        _RETRY_STATUSES = (502, 503, 504, 524)
        r = await client.post(_VLLM_PROXY_URL, json=payload, headers=headers)
        for _attempt in range(2):
            if r.status_code not in _RETRY_STATUSES:
                break
            import asyncio as _aio
            await _aio.sleep(2.0)
            logger.debug(
                "call_model.retry model=%s status=%d attempt=%d",
                model_id, r.status_code, _attempt + 1,
            )
            r = await client.post(_VLLM_PROXY_URL, json=payload, headers=headers)
        if r.status_code != 200:
            logger.warning(
                "call_model.non_200 model=%s status=%d body=%s",
                model_id, r.status_code, r.text[:200],
            )
            # T107: short backoff on transient-block. The body text often
            # references the rate-limit *window size* (e.g. "604800s window")
            # which is NOT the cooldown — actual recovery may be seconds.
            # Default 60s cooldown so model_router retries soon. Only escalate
            # to longer cooldowns on explicit "Retry-After" header.
            if r.status_code in (429, 503, 529):
                try:
                    from app.services.model_router import mark_model_rate_limited
                    cooldown: float = 60.0
                    retry_after = r.headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        cooldown = max(float(retry_after), 5.0)
                    mark_model_rate_limited(model_id, cooldown_seconds=cooldown)
                    # T107 round-5 (2026-05-27): detect OmniRoute semaphore
                    # leak. OR's per-connection semaphore counter sometimes
                    # gets stuck after streaming timeouts (counter increments
                    # on call-start but never decrements on stream-abort).
                    # Symptom: 429 body contains "Semaphore timeout after
                    # 30000ms". Track strikes — repeated occurrences mean
                    # the slots are permanently leaked and only an OR
                    # service restart clears them.
                    if "Semaphore timeout after 30000ms" in r.text:
                        import time as _t
                        _OR_SEM_STRIKES.append(_t.time())
                        # Drop strikes older than 10 min.
                        cutoff = _t.time() - 600
                        while _OR_SEM_STRIKES and _OR_SEM_STRIKES[0] < cutoff:
                            _OR_SEM_STRIKES.pop(0)
                        logger.info(
                            "llm_evaluator.or_semaphore_strike count=%d/10min",
                            len(_OR_SEM_STRIKES),
                        )
                except Exception:
                    pass
            return None
        # T107 (2026-05-26): reasoning models (deepseek-v4-pro on ollama,
        # Qwen3.6 on vllm) emit output in `delta.reasoning_content` or
        # `delta.reasoning` while `delta.content` stays empty until the
        # reasoning trace finishes. With finite max_tokens the model may
        # never reach the final-content phase — so collect BOTH streams
        # and prefer content when present, else fall back to the reasoning
        # trace as the answer.
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if payload_str in {"", "[DONE]"}:
                continue
            try:
                chunk = json.loads(payload_str)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
            reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_piece:
                reasoning_parts.append(reasoning_piece)
            # Non-stream fallback: some upstreams return the whole message
            # in a single chunk without `delta`.
            message = choices[0].get("message") or {}
            if message:
                mc = message.get("content")
                if mc:
                    content_parts.append(mc)
                mr = message.get("reasoning_content") or message.get("reasoning")
                if mr:
                    reasoning_parts.append(mr)
        content = "".join(content_parts).strip()
        if not content and reasoning_parts:
            content = "".join(reasoning_parts).strip()
            logger.info(
                "call_model.using_reasoning_as_content model=%s reasoning_len=%d",
                model_id, len(content),
            )
        if not content:
            logger.warning("call_model.empty_content model=%s", model_id)
            return None
        # T107 round-2: success → clear any backoff state.
        try:
            from app.services.model_router import clear_model_rate_limit
            clear_model_rate_limit(model_id)
        except Exception:
            pass
        return content
    except Exception as exc:
        logger.warning("call_model.failed model=%s err=%s", model_id, exc)
        return None
    finally:
        await client.aclose()


# ── Role routing ────────────────────────────────────────────────────────


async def pick_role_for_task(
    title: str,
    description: str | None,
    candidate_roles: list[str],
) -> str | None:
    """Ask the model which role best fits a task. Returns one of
    `candidate_roles` or `None` if the LLM is unavailable / undecided.
    """
    if not candidate_roles:
        return None
    title = (title or "").strip()
    desc = (description or "").strip()
    if not title and not desc:
        return None

    role_list = "\n".join(f"- {r}" for r in candidate_roles)
    prompt = (
        f"Pick the single best role for this task from the list. "
        f"Reply with EXACTLY one role name (no quotes, no punctuation, "
        f"copy verbatim from the list). If none clearly fits, reply UNKNOWN.\n\n"
        f"ROLES:\n{role_list}\n\n"
        f"TASK TITLE: {title}\n"
        f"TASK DESCRIPTION: {desc[:600]}"
    )
    raw = await _call_vllm(prompt, max_tokens=24)
    if raw is None:
        return None
    raw = raw.split("\n", 1)[0].strip().strip(".`'\"")
    if raw.upper() == "UNKNOWN" or not raw:
        return None
    # Strict match against candidates (case-insensitive).
    lower = raw.lower()
    for r in candidate_roles:
        if r.lower() == lower:
            return r
    # Loose contains-match: model sometimes adds quotes or trailing words.
    for r in candidate_roles:
        if r.lower() in lower or lower in r.lower():
            return r
    logger.debug("llm_evaluator.role_unrecognized raw=%r", raw)
    return None


# ── Owner-directive evaluation ─────────────────────────────────────────


async def evaluate_owner_directive(
    current_description: str,
    owner_comment: str,
) -> str | None:
    """Given a task's DESCRIPTION and an OWNER's freeform COMMENT, decide if
    the comment is a directive that changes the task's goal/scope/constraints.

    Returns the FULL revised description (existing structure preserved, only the
    requested change applied) when it is a directive; ``""`` when the model says
    it is not a change directive (question / note / praise); ``None`` when no
    verdict was obtained (LLM unavailable, auth failure, unusable output) — the
    caller must not report that as "reviewed". One LLM call; owner comments are
    intentional + rare.
    """
    desc = (current_description or "").strip()
    comment = (owner_comment or "").strip()
    if not desc or not comment:
        return None
    system_prompt = (
        "You edit task specifications from owner feedback. A COMMENT is a CHANGE "
        "DIRECTIVE if it adds, removes, narrows, or alters ANY requirement, "
        "constraint, format, language, allowed source, field, quantity, or "
        "acceptance criterion — even a small one. It is NOT a directive only if "
        "it is purely a question, acknowledgement, praise, or unrelated remark."
    )
    prompt = (
        "Rewrite the DESCRIPTION to incorporate the COMMENT's directive. Keep all "
        "existing structure, absolute file paths, and unrelated criteria verbatim; "
        "apply ONLY what the comment asks. Output ONLY the revised description "
        "text (no preamble, no code fences). If the COMMENT is NOT a change "
        "directive, output exactly: NO_CHANGE\n\n"
        "--- EXAMPLE 1 ---\n"
        "DESCRIPTION: Find a poem and append it to poems.md.\n"
        "OWNER COMMENT: only english or german please, no arabic\n"
        "OUTPUT: Find an ENGLISH or GERMAN poem (reject any other language) and "
        "append it to poems.md.\n"
        "--- EXAMPLE 2 ---\n"
        "DESCRIPTION: Find a poem and append it to poems.md.\n"
        "OWNER COMMENT: looks good, thanks!\n"
        "OUTPUT: NO_CHANGE\n"
        "--- EXAMPLE 3 (adding a required field IS a directive) ---\n"
        "DESCRIPTION: Find a poem and append it to poems.md with the Source URL.\n"
        "OWNER COMMENT: also include the author's name each time\n"
        "OUTPUT: Find a poem and append it to poems.md with the Source URL and "
        "the author's name.\n"
        "--- NOW DO THIS ONE ---\n"
        f"DESCRIPTION:\n{desc[:2000]}\n\n"
        f"OWNER COMMENT:\n{comment[:1000]}\n"
        "OUTPUT:"
    )
    # 512 is ample for a rewritten description; larger (e.g. 1200) overruns the
    # evaluator's short timeout on the single GPU and returns None.
    raw = await _call_vllm(prompt, max_tokens=512, system_prompt=system_prompt)
    if raw is None:
        return None
    raw = raw.strip().strip("`")
    if raw.upper().startswith("NO_CHANGE") or raw.upper() == "NO CHANGE":
        return ""
    if len(raw) < 20:  # empty or implausibly short → model refused / echoed
        return None
    return raw


# ── Completion detection ───────────────────────────────────────────────


async def is_completion_comment(comment: str) -> bool | None:
    """Ask the model whether a comment signals task completion.
    Returns:
      True  → completion (caller can promote in_progress → review/done)
      False → not a completion (e.g. progress update, question)
      None  → LLM unavailable / undecided
    """
    text = (comment or "").strip()
    if not text:
        return False
    # Hard short-circuit on the truly trivial cases so we don't burn an
    # LLM call on a comment that's already obviously a completion.
    lowered = text.lower()
    trivial = {
        "ok", "ok.", "ok!", "okay", "done", "done.", "fertig",
        "erledigt", "alles klar", "alles klar.", "complete",
        "completed", "finished", "solved", "✓", "✅",
    }
    if lowered in trivial:
        return True

    prompt = (
        "Below is a comment posted by an agent on a task. Decide: does the "
        "comment indicate the agent has FINISHED the task? Reply with EXACTLY "
        "one word: YES or NO.\n\n"
        f"COMMENT:\n{text[:800]}"
    )
    raw = await _call_vllm(prompt, max_tokens=4)
    if raw is None:
        return None
    word = raw.split()[0].rstrip(".!,:;").upper() if raw.split() else ""
    if word == "YES":
        return True
    if word == "NO":
        return False
    return None


async def close_client() -> None:
    """Test-friendly shutdown. Lets pytest fixtures wipe the singleton."""
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None
