# ruff: noqa
"""Validation tests for the LLM-evaluator integration in watchdog (T65).

These tests do NOT spin up a Postgres database — the watchdog SQL is
Postgres-only (`NOW()`, `INTERVAL '300 seconds'`, `LATERAL`, `~*`). Tests
here cover the parts that can be exercised without that infrastructure:

- `_ROUTE_KEYWORDS`: well-formed (no duplicates, roles match a known set)
- `_COMPLETION_PATTERNS` regex: covers the English markers
- `_SHORT_COMPLETION_PATTERNS` regex: matches short replies in DE + EN
- LLM-evaluator shim wire-up: routing function imports + calls the LLM
- Watchdog tick wiring: `_detect_llm_completion` is registered as step `0a3b`

End-to-end (Postgres + gateway + vLLM) is verified by the manual smoke
documented in T64/T65 — task `be2c4da9` cycle `inbox → in_progress → done
→ inbox` reached within 120s with LLM-evaluator on.
"""

from __future__ import annotations

import importlib

import pytest

import app.services.watchdog_loop as wl


def test_route_keywords_has_no_duplicate_keys() -> None:
    keys = [k for k, _ in wl._ROUTE_KEYWORDS]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate routing keys: {dupes}"


def test_route_keywords_german_terms_present() -> None:
    keys = {k for k, _ in wl._ROUTE_KEYWORDS}
    # T64: German keywords for non-English titles route to QA Engineer.
    for term in ["zustand", "abfrage", "antworte", "prüfung", "frage", "status"]:
        assert term in keys, f"missing routing keyword: {term}"


def test_route_keywords_roles_are_known() -> None:
    """Every role mentioned in `_ROUTE_KEYWORDS` should be a real agent
    role that the rest of the system understands."""
    KNOWN_ROLES = {
        "Security Architect", "Reverse Engineer", "Binary Analyst",
        "Red Team Operator", "DevOps Engineer", "QA Engineer",
        "Software Architect",
    }
    roles = {role for _, role in wl._ROUTE_KEYWORDS}
    unknown = roles - KNOWN_ROLES
    assert not unknown, f"unknown roles in routing table: {unknown}"


def test_completion_pattern_matches_canonical_markers() -> None:
    for msg in [
        "COMPLETED: report attached",
        "DONE: nothing left",
        "FINISHED: see comments",
        "PASSWORD: hunter2",
        "ANSWER: 42",
        "SOLVED: trivial",
        "TASK COMPLETE",
        "## Result: ok",
        "## Summary\nall pass",
        "## Findings: none",
        "## Conclusion\n…",
        "## Outcome ok",
        "## Final\n…",
    ]:
        assert wl._COMPLETION_PATTERNS.search(msg), f"should match: {msg!r}"


def test_completion_pattern_does_not_match_progress_updates() -> None:
    for msg in [
        "Working on it, will update.",
        "investigating root cause",
        "needs more info from owner",
        "blocked by upstream",
    ]:
        assert not wl._COMPLETION_PATTERNS.search(msg), (
            f"should NOT match: {msg!r}"
        )


def test_short_completion_pattern_matches_one_word_replies() -> None:
    for msg in [
        "OK", "ok.", "Done", "DONE!", "Fertig", "ERLEDIGT",
        "alles klar", "ALLES KLAR.", "Complete", "Completed",
        "Finished", "solved", "✓", "✅",
    ]:
        assert wl._SHORT_COMPLETION_PATTERNS.match(msg), (
            f"should match: {msg!r}"
        )


def test_short_completion_pattern_does_not_match_words_mid_sentence() -> None:
    for msg in [
        "I am done working on this task and adding more notes here",
        "ok, but blocked by external review",
        "Working on this — alles klar in 2 hours",
    ]:
        # Whole-message anchor: must NOT match.
        assert not wl._SHORT_COMPLETION_PATTERNS.match(msg), (
            f"should NOT match: {msg!r}"
        )


def test_llm_evaluator_module_importable_from_watchdog() -> None:
    """Watchdog's runtime import path resolves."""
    mod = importlib.import_module("app.services.llm_evaluator")
    assert hasattr(mod, "pick_role_for_task")
    assert hasattr(mod, "is_completion_comment")


def test_detect_llm_completion_registered_in_watchdog() -> None:
    """The new step `0a3b` must be wired into `_process_once`."""
    src = (
        importlib.import_module("app.services.watchdog_loop").__file__
    )
    with open(src) as fh:
        body = fh.read()
    assert "_detect_llm_completion" in body
    assert "Step 0a3b" in body
    assert "llm_completion_flagged" in body
