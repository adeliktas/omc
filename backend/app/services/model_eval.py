"""T80 — Model IQ evaluation.

Run a small standardized prompt suite against every enabled (provider,
model) and emit a 0-100 score that the complexity router uses to pick
the "best" model for L4/L5 tasks.

This is a deliberately small + cheap eval (5 prompts, ≤256 output tokens
each). It is NOT a research-grade benchmark; it's a sanity gate to
prevent "smol-7b-2bit assigned to a major refactor" failures.

Tests:
  1. Math   — exact-integer answer
  2. Logic  — keyword check for "not necessarily / cannot / no"
  3. Code   — regex check for valid Python palindrome implementation
  4. JSON   — strict parse + value equality
  5. Trivia — case-insensitive substring of canonical answer

Each test contributes 0-20 points. Sum → 0-100 score.

Designed to be run on-demand via `POST /providers/eval`; owner-triggered.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import os

from app.core.logging import get_logger
from app.db.session import async_session_maker
from app.models.provider_capacity import ProviderCapacity
from sqlalchemy import select as sa_select

logger = get_logger(__name__)

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_REQUEST_TIMEOUT = float(os.environ.get("EVAL_REQUEST_TIMEOUT", "45"))


SUITE: list[dict[str, Any]] = [
    {
        "id": "math",
        "prompt": (
            "What is 17 * 23? Reply with ONLY the integer, no words, "
            "no explanation."
        ),
        "max_points": 20,
    },
    {
        "id": "logic",
        "prompt": (
            "If all roses are flowers, and some flowers fade quickly, "
            "can we conclude that some roses fade quickly? "
            "Answer with one short sentence."
        ),
        "max_points": 20,
    },
    {
        "id": "code",
        "prompt": (
            "Write a Python function named `is_palindrome` that takes one "
            "string argument and returns True if the string reads the same "
            "forwards and backwards (case-sensitive), else False. Reply "
            "with ONLY the function definition, no markdown fences, no "
            "explanation."
        ),
        "max_points": 20,
    },
    {
        "id": "json",
        "prompt": (
            'Output the literal JSON object {"answer": 42} and absolutely '
            "nothing else. No prose, no markdown fences, no commentary."
        ),
        "max_points": 20,
    },
    {
        "id": "trivia",
        "prompt": (
            "What is the capital of Australia? Reply with only the city "
            "name."
        ),
        "max_points": 20,
    },
]


def _score_math(text: str) -> int:
    # Limit each captured number to 9 digits before parsing — some models
    # emit extreme integers in their reasoning trace that overflow on
    # `int()` on certain platforms or just slow it down. We only care about
    # the literal 391 either way.
    nums = re.findall(r"-?\d+", text)
    for n in nums:
        if len(n) > 9:
            continue
        try:
            if int(n) == 391:
                return 20
        except ValueError:
            continue
    return 0


def _score_logic(text: str) -> int:
    t = text.lower()
    # The valid answer is some form of "no / not necessarily / cannot
    # conclude". A common wrong answer is "yes" (illicit major premise).
    if any(k in t for k in ("not necessarily", "cannot conclude",
                            "can't conclude", "doesn't follow", "does not follow",
                            "no, ", "no.", "fallacy", "invalid")):
        return 20
    if "yes" in t.split():
        return 0
    # Partial credit for ambiguous hedging.
    if any(k in t for k in ("maybe", "possibly", "it depends")):
        return 8
    return 4


def _score_code(text: str) -> int:
    has_def = bool(re.search(r"def\s+is_palindrome\s*\(", text))
    has_reverse = bool(
        re.search(r"\[\s*::\s*-\s*1\s*\]", text)
        or re.search(r"reversed\s*\(", text)
        or re.search(r"\.reverse\(\)", text)
    )
    has_return = "return" in text
    score = 0
    if has_def:
        score += 8
    if has_reverse:
        score += 8
    if has_return:
        score += 4
    return min(score, 20)


def _score_json(text: str) -> int:
    text = text.strip()
    # Strip ```json fences if model insists on markdown despite the prompt.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        d = json.loads(text)
    except Exception:
        return 0
    if isinstance(d, dict) and d.get("answer") == 42 and len(d) == 1:
        return 20
    if isinstance(d, dict) and d.get("answer") == 42:
        return 14  # extra keys, but value correct
    return 4  # parsed JSON but wrong shape


def _score_trivia(text: str) -> int:
    t = text.lower()
    if "canberra" in t:
        # Penalise verbose answers (instruction was "only the city name").
        words = re.findall(r"[a-zA-Z]+", t)
        return 20 if len(words) <= 3 else 14
    if "sydney" in t or "melbourne" in t:
        return 0  # common wrong answer
    return 4


_SCORERS = {
    "math": _score_math,
    "logic": _score_logic,
    "code": _score_code,
    "json": _score_json,
    "trivia": _score_trivia,
}


# ── openclaw.json helpers ──────────────────────────────────────────────

def _load_cfg() -> dict[str, Any]:
    p = _OPENCLAW_HOME / "openclaw.json"
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        logger.warning("eval.openclaw_read_failed err=%s", exc)
        return {}


def _save_cfg(cfg: dict[str, Any]) -> None:
    p = _OPENCLAW_HOME / "openclaw.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(p)


def _list_models_for(provider: str) -> list[dict[str, Any]]:
    """T89: merge MC-only sidecar metadata so the assessor sees the same
    `disabled` flag the benchmark loop wrote (sidecar, not openclaw.json).
    """
    from app.services.mc_sidecar import merge_into_model_entry
    cfg = _load_cfg()
    block = (cfg.get("models") or {}).get("providers") or {}
    prov = block.get(provider) or {}
    return [
        merge_into_model_entry(provider, m)
        for m in (prov.get("models") or [])
        if isinstance(m, dict) and m.get("id")
    ]


def _persist_iq_score(provider: str, model_id: str, score: int, detail: dict[str, int]) -> None:
    """T89 (2026-05-16): IQ metadata now lives in the MC-only sidecar
    `~/.openclaw/openclaw.mc.json` so we don't corrupt the gateway's
    openclaw.json schema (`Unrecognized keys: iq_score, …`).
    """
    from app.services.mc_sidecar import set_model_meta
    set_model_meta(
        provider, model_id,
        iq_score=score,
        iq_detail=detail,
        iq_evaluated_at=int(time.time()),
    )
    logger.info(
        "eval.iq_persisted provider=%s model=%s score=%d detail=%s (→ sidecar)",
        provider, model_id, score, detail,
    )


# ── Per-prompt call ────────────────────────────────────────────────────

async def _ask(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str | None,
    model_id: str,
    prompt: str,
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("noapikey", "none", ""):
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.0,
        "stream": False,
    }
    r = await client.post(url, json=body, headers=headers, timeout=_REQUEST_TIMEOUT)
    if r.status_code != 200:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""
    try:
        return (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
    except Exception:
        return ""


# ── Top-level runner ───────────────────────────────────────────────────

async def evaluate_all(only_provider: str | None = None) -> dict[str, Any]:
    """Run the suite against every enabled model.

    Returns `{ evaluations: [...], summary: {...} }`. Best-effort —
    individual upstream failures are logged + recorded with score=0.
    """
    results: list[dict[str, Any]] = []
    async with async_session_maker() as db:
        q = sa_select(ProviderCapacity)
        providers = (await db.execute(q)).scalars().all()
        async with httpx.AsyncClient() as client:
            for prov in providers:
                if only_provider and prov.provider_name != only_provider:
                    continue
                if not prov.base_url:
                    continue
                # T103 v34 (2026-05-20): skip offline providers — IQ
                # evaluation should never hit a disabled upstream + waste
                # tokens/credits.
                if prov.status != "online":
                    logger.debug(
                        "eval.skip provider=%s status=%s",
                        prov.provider_name, prov.status,
                    )
                    continue
                models = _list_models_for(prov.provider_name)
                models = [m for m in models if not m.get("disabled")]
                for m in models:
                    model_id = m["id"]
                    detail: dict[str, int] = {}
                    total = 0
                    for test in SUITE:
                        try:
                            ans = await _ask(
                                client, prov.base_url, prov.api_key, model_id, test["prompt"],
                            )
                        except Exception as exc:
                            logger.warning(
                                "eval.upstream_failed provider=%s model=%s test=%s err=%s",
                                prov.provider_name, model_id, test["id"], exc,
                            )
                            ans = ""
                        try:
                            s = _SCORERS[test["id"]](ans)
                        except Exception as exc:
                            logger.warning(
                                "eval.score_failed provider=%s model=%s test=%s err=%s",
                                prov.provider_name, model_id, test["id"], exc,
                            )
                            s = 0
                        detail[test["id"]] = s
                        total += s
                    _persist_iq_score(prov.provider_name, model_id, total, detail)
                    results.append({
                        "provider": prov.provider_name,
                        "model": model_id,
                        "iq_score": total,
                        "detail": detail,
                    })
                    logger.info(
                        "eval.done provider=%s model=%s iq=%d %s",
                        prov.provider_name, model_id, total, detail,
                    )
    return {
        "evaluations": results,
        "count": len(results),
    }
