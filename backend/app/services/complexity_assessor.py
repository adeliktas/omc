"""Task difficulty assessment shared by human and lead creation.

The optional LLM stage is live-switchable (off by default). Keyword scoring
provides a fallback without model requests;
explicit task complexity is never overridden. Persist the verdict and its
provenance in tasks.complexity_rationale for the UI.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services import runtime_settings
from app.services.model_router import pick_top_n_models_for_complexity
from app.services.task_complexity_rules import _complexity_score

if TYPE_CHECKING:
    from app.models.tasks import Task

logger = get_logger(__name__)


_ASSESSOR_TIMEOUT = float(os.environ.get("COMPLEXITY_ASSESSOR_TIMEOUT", "15"))
_ASSESSOR_MAX_TOKENS = int(runtime_settings.get_setting("COMPLEXITY_ASSESSOR_MAX_TOKENS"))


@dataclass
class ComplexityVerdict:
    level: int  # 1..5
    keyword_floor: int  # 1..5 — lower bound from static scorer
    llm_level: int | None  # 1..5 if LLM gave a verdict, else None
    llm_reasoning: str = ""
    llm_model: str = ""
    llm_provider: str = ""
    keyword_score: int = 0
    keyword_hard_hits: list[str] = field(default_factory=list)
    keyword_simple_hits: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "level": self.level,
            "keyword_floor": self.keyword_floor,
            "llm_level": self.llm_level,
            "llm_reasoning": self.llm_reasoning,
            "llm_model": self.llm_model,
            "llm_provider": self.llm_provider,
            "keyword_score": self.keyword_score,
            "keyword_hard_hits": self.keyword_hard_hits,
            "keyword_simple_hits": self.keyword_simple_hits,
            "notes": self.notes,
            "schema": "complexity_rationale.v1",
        })


# ── Keyword floor ──────────────────────────────────────────────────────

def _keyword_floor(title: str, description: str) -> tuple[int, dict[str, Any]]:
    """Run the existing keyword scorer and translate its score into a
    *minimum* level. Tasks can never come out below this floor.

    The mapping is intentionally conservative — we use the floor as a
    sanity check, not a primary signal. An LLM that judges higher than
    the floor wins; an LLM that judges lower is overridden.
    """
    data = _complexity_score(title, description)
    score = int(data["score"])
    hard_hits = list(data.get("hard_hits", []))
    simple_hits = list(data.get("simple_hits", []))

    # Conservative floor: every 4 raw points lift the floor by one level.
    # A few hard signals are enough to guarantee at least L2.
    if score >= 11:
        floor = 4  # 11+ score still allows L5 verdict from LLM
    elif score >= 7:
        floor = 3
    elif score >= 4:
        floor = 2
    elif score >= 1 or hard_hits:
        floor = 2  # any hard-keyword presence forces ≥L2
    else:
        floor = 1

    return floor, {
        "score": score,
        "hard_hits": hard_hits,
        "simple_hits": simple_hits,
    }


# ── LLM call ──────────────────────────────────────────────────────────

_LEVEL_RE = re.compile(r"\bL\s*([1-5])\b", re.IGNORECASE)
_BARE_DIGIT_RE = re.compile(r"^\s*([1-5])\b")
_REASONING_RE = re.compile(r"(?:reason|why|because)\s*[:\-]\s*(.+)", re.IGNORECASE)


def _parse_llm_response(text: str) -> tuple[int | None, str]:
    """Extract `L1..L5` plus a free-text reasoning blob."""
    text = (text or "").strip()
    if not text:
        return None, ""

    level: int | None = None
    m = _LEVEL_RE.search(text)
    if m:
        level = int(m.group(1))
    else:
        m2 = _BARE_DIGIT_RE.search(text)
        if m2:
            level = int(m2.group(1))

    # Reasoning = everything except the verdict line, trimmed.
    reasoning = text
    if level is not None:
        # Strip the verdict token from the reasoning for readability.
        reasoning = _LEVEL_RE.sub("", reasoning, count=1).strip(" :,-\n\t.")
    reasoning = reasoning.replace("\n\n", "\n").strip()
    if len(reasoning) > 600:
        reasoning = reasoning[:597] + "…"
    return level, reasoning


SYSTEM_PROMPT = (
    "You are an expert engineering manager. You rate task complexity on a 1-5 "
    "scale where:\n"
    "  L1 = trivial (typo, rename, one-line config, log message)\n"
    "  L2 = simple (small feature, single-file change, easy bugfix)\n"
    "  L3 = moderate (multi-file change, requires understanding of one subsystem)\n"
    "  L4 = complex (cross-subsystem, requires research / design, may need split)\n"
    "  L5 = major (architecture, security-critical, multi-day, high blast radius)\n\n"
    "Output format (STRICT):\n"
    "  Line 1: `L<N>` where N is 1..5\n"
    "  Line 2: one-sentence reasoning explaining the verdict.\n"
    "Do not write prose. Do not output anything else."
)


async def _ask_llm(
    db: AsyncSession,
    title: str,
    description: str,
) -> tuple[int | None, str, str, str]:
    """Pick the best available model and ask it to rate the task. Iterates
    over the top-5 candidates so a single rate-limited or down provider
    doesn't strand the call. Returns the first parseable verdict.
    """
    # Try the top-5 L5-floor models first, then descend if all fail.
    candidates: list[tuple[str, str, int]] = []
    for target in (5, 4, 3, 2, 1):
        more = await pick_top_n_models_for_complexity(db, complexity=target, limit=5)
        for c in more:
            if c not in candidates:
                candidates.append(c)
        if candidates:
            break

    if not candidates:
        logger.warning("complexity_assessor.no_models_available")
        return None, "", "", ""

    # T108 (2026-05-30): route through the UNIFIED `call_model()` path
    # (OmniRoute :20129) instead of the old direct-to-`base_url` POST.
    # The previous path was triple-broken post-T103:
    #   1. candidates carry provider="omniroute", but provider_capacity has
    #      no `omniroute` row → base_url="" → every candidate skipped →
    #      `all_candidates_exhausted` on EVERY task → keyword floor only →
    #      deterministic instant L4 on any hard-keyword (crackme/RE/binary).
    #   2. even with a base_url, model_ids are OmniRoute-prefixed
    #      (`ollama/deepseek-v4-pro`) — upstream rejects the prefixed name.
    #   3. reasoning models return empty `content` without
    #      `reasoning_effort=low` (handled inside call_model, not here).
    # `call_model` fixes all three: posts to OmniRoute with the canonical
    # id, sets reasoning_effort=low + reasoning_content fallback, shares
    # the per-model rate-limit backoff cache, and — crucially — is VISIBLE
    # in /traffic exactly like the role-pick `pick_role_for_task` call.
    from app.services.llm_evaluator import call_model

    user_prompt = (
        f"TITLE: {title}\n\nDESCRIPTION:\n{description or '(none)'}\n\n"
        "Rate this task's complexity."
    )
    for provider_name, model_id, iq in candidates:
        try:
            content = await call_model(
                model_id,
                user_prompt,
                system_prompt=SYSTEM_PROMPT,
                max_tokens=_ASSESSOR_MAX_TOKENS,
                temperature=0.0,
                timeout_seconds=_ASSESSOR_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "complexity_assessor.call_model_failed model=%s err=%r",
                model_id, exc,
            )
            continue
        if not content:
            continue
        level, reasoning = _parse_llm_response(content)
        if level is not None:
            logger.info(
                "complexity_assessor.verdict model=%s iq=%d → L%d",
                model_id, iq, level,
            )
            return level, reasoning, model_id, provider_name

    logger.warning(
        "complexity_assessor.all_candidates_exhausted tried=%d",
        len(candidates),
    )
    return None, "", "", ""


# ── Public entry point ────────────────────────────────────────────────

async def assess_complexity(
    db: AsyncSession,
    title: str,
    description: str,
) -> ComplexityVerdict:
    """Primary entry. Returns a `ComplexityVerdict` whose `.level`
    is the final assessment (always 1..5, never None).

    Policy: `final = max(keyword_floor, llm_level or 0)`. If the LLM
    yields no verdict (no models online, parse failure, etc.), the
    keyword floor stands alone (L1 when no keywords match).
    """
    floor, kw = _keyword_floor(title, description)

    enabled = runtime_settings.get_setting("TASK_COMPLEXITY_LLM_ENABLED")
    llm_level, reasoning, model_id, provider_name = (
        await _ask_llm(db, title, description) if enabled else (None, "", "", "")
    )

    notes: list[str] = []
    if llm_level is None:
        final = floor
        notes.append(
            "LLM assessor unavailable or returned no verdict — using keyword floor only."
            if enabled else "LLM assessor disabled — using keyword fallback."
        )
    else:
        final = max(floor, llm_level)
        if llm_level < floor:
            notes.append(
                f"LLM verdict L{llm_level} below keyword floor L{floor} — using floor."
            )
        elif llm_level > floor:
            notes.append(
                f"LLM verdict L{llm_level} exceeds keyword floor L{floor} — using LLM."
            )
    if final < 1:
        final = 1
    if final > 5:
        final = 5

    return ComplexityVerdict(
        level=final,
        keyword_floor=floor,
        llm_level=llm_level,
        llm_reasoning=reasoning,
        llm_model=model_id or "",
        llm_provider=provider_name or "",
        keyword_score=kw["score"],
        keyword_hard_hits=kw["hard_hits"],
        keyword_simple_hits=kw["simple_hits"],
        notes=notes,
    )


async def populate_task_complexity(db: AsyncSession, task: Task) -> None:
    """One assessment path for human and lead-created tasks; never override explicit levels."""
    if task.complexity is not None:
        return
    try:
        verdict = await assess_complexity(db, task.title, task.description or "")
    except Exception:
        # The optional enrichment must not make creating a task impossible.
        logger.exception("complexity_assessor.failed; using keyword fallback")
        floor, kw = _keyword_floor(task.title, task.description or "")
        verdict = ComplexityVerdict(
            level=floor, keyword_floor=floor, llm_level=None,
            keyword_score=kw["score"], keyword_hard_hits=kw["hard_hits"],
            keyword_simple_hits=kw["simple_hits"],
            notes=["Assessment failed — keyword-only fallback."],
        )
    task.complexity = verdict.level
    task.complexity_rationale = verdict.to_json()
