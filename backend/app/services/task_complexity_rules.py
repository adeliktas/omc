"""Pure keyword difficulty rules shared by routing and task assessment."""

from __future__ import annotations

import re
from typing import Any

HARD_KEYWORDS = (
    "multi-stage",
    "distributed",
    "consensus",
    "cross-board",
    "migration",
    "schema",
    "breaking",
    "architecture",
    "rewrite",
    "overhaul",
    "design",
    "obfuscation",
    "custom-protocol",
    "encrypted",
    "packed",
    "malware",
    "exploit",
    "ctf",
    "reverse-engineer",
    "binary cracking",
    "binary analysis",
    "binary-analysis",
    "ida pro",
    "ida-pro",
    "disassembl",
    "decompil",
    "reverse engineer",
    "reverse engineering",
    "crackme",
    "vulnerability research",
    "rop chain",
    "shellcode",
    "kernel module",
    "device driver",
    "firmware",
)

# Do not down-weight a hard task just because its description mentions renaming.
SIMPLE_KEYWORDS = (
    "typo",
    "rename",
    "formatting",
    "docs",
    "readme",
    "comment",
    "log message",
)


def _complexity_score(title: str, description: str) -> dict[str, Any]:
    title_l = (title or "").lower()
    desc = description or ""
    desc_l = desc.lower()
    score = min(len(desc), 2000) // 200
    hard_hits = [k for k in HARD_KEYWORDS if k in title_l + " " + desc_l]
    score += 2 * len(hard_hits)
    simple_hits = [k for k in SIMPLE_KEYWORDS if k in title_l + " " + desc_l]
    if not hard_hits:
        score -= len(simple_hits)
    if "depends on:" in desc_l or "depends_on:" in desc_l or "blocked by" in desc_l:
        score += 3
    if "crackme" in desc_l and re.search(r"(level|stage)\s*[3-9]", desc_l):
        score += 2
    return {
        "score": score,
        "hard_hits": hard_hits,
        "simple_hits": simple_hits,
        "is_complex": score >= 6,
    }
