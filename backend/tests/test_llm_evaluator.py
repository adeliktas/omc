# ruff: noqa
"""Unit tests for `app.services.llm_evaluator` (T65 2026-05-15).

Mocks `httpx.AsyncClient.post` so tests run offline. Covers:
- pick_role_for_task: exact match, case-insensitive, UNKNOWN, contains-match, throttle (empty content), HTTP error, undecided
- is_completion_comment: trivial short-circuit (no LLM call), YES, NO, malformed reply, throttle, disabled
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services import llm_evaluator


def _mk_response(content: str, status: int = 200) -> Any:
    """Build a mock response whose `.text` is an SSE stream.

    T103 (2026-05-17): the evaluator sends `stream=True` to OmniRoute and
    parses `data: {...}` chunks from `r.text` (choices[0].delta.content) —
    NOT a single non-stream JSON body. The mock must mirror that wire shape
    or the parser finds no content and returns None. Empty `content` yields
    a stream with no content delta (simulates throttle / empty reply).
    """
    if content:
        body = (
            "data: "
            + json.dumps({"choices": [{"delta": {"content": content}}]})
            + "\n\ndata: [DONE]\n\n"
        )
    else:
        body = "data: " + json.dumps({"choices": [{"delta": {}}]}) + "\n\ndata: [DONE]\n\n"

    class _Resp:
        status_code = status

        def __init__(self, b: str) -> None:
            self._b = b

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 5},
            }

        @property
        def text(self) -> str:
            return self._b

    return _Resp(body)


async def _reset_client() -> None:
    """Ensure each test starts with a fresh client singleton."""
    await llm_evaluator.close_client()


@pytest.fixture(autouse=True)
async def _enable_evaluator(monkeypatch):
    monkeypatch.setattr(llm_evaluator, "_ENABLED", True)
    await _reset_client()
    yield
    await _reset_client()


# ── pick_role_for_task ─────────────────────────────────────────────────


async def test_pick_role_exact_match(monkeypatch):
    fake_post = AsyncMock(return_value=_mk_response("QA Engineer"))
    monkeypatch.setattr(
        "httpx.AsyncClient.post", fake_post,
    )
    role = await llm_evaluator.pick_role_for_task(
        "fix login bug", "users can't reset their password",
        ["QA Engineer", "Security Architect", "Reverse Engineer"],
    )
    assert role == "QA Engineer"
    fake_post.assert_called_once()


async def test_pick_role_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("qa engineer")),
    )
    role = await llm_evaluator.pick_role_for_task(
        "anything", "anything",
        ["QA Engineer", "Security Architect"],
    )
    assert role == "QA Engineer"


async def test_pick_role_contains_loose_match(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response('"QA Engineer" — best fit')),
    )
    role = await llm_evaluator.pick_role_for_task(
        "title", "desc", ["QA Engineer"],
    )
    assert role == "QA Engineer"


async def test_pick_role_unknown(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("UNKNOWN")),
    )
    role = await llm_evaluator.pick_role_for_task(
        "vague", "nothing actionable", ["QA Engineer"],
    )
    assert role is None


async def test_pick_role_throttled_empty_response(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("")),
    )
    role = await llm_evaluator.pick_role_for_task(
        "x", "y", ["QA Engineer"],
    )
    assert role is None


async def test_pick_role_http_error(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    role = await llm_evaluator.pick_role_for_task(
        "x", "y", ["QA Engineer"],
    )
    assert role is None


async def test_pick_role_empty_input_short_circuits(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    role = await llm_evaluator.pick_role_for_task("", "", ["QA Engineer"])
    assert role is None
    fake_post.assert_not_called()


async def test_pick_role_no_candidates_short_circuits(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    role = await llm_evaluator.pick_role_for_task("title", "desc", [])
    assert role is None
    fake_post.assert_not_called()


async def test_pick_role_disabled_via_env(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr(llm_evaluator, "_ENABLED", False)
    role = await llm_evaluator.pick_role_for_task(
        "title", "desc", ["QA Engineer"],
    )
    assert role is None
    fake_post.assert_not_called()


async def test_pick_role_unrecognized_response(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("Frontend Developer")),
    )
    role = await llm_evaluator.pick_role_for_task(
        "make a page", "ui task", ["QA Engineer", "Security Architect"],
    )
    assert role is None  # not in candidate list


# ── is_completion_comment ──────────────────────────────────────────────


async def test_completion_trivial_short_circuit(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    for word in ["OK", "ok.", "Done", "ALLES KLAR", "fertig", "✓", "✅"]:
        assert await llm_evaluator.is_completion_comment(word) is True
    fake_post.assert_not_called()


async def test_completion_empty_string():
    assert await llm_evaluator.is_completion_comment("") is False
    assert await llm_evaluator.is_completion_comment("   ") is False


async def test_completion_llm_yes(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("YES")),
    )
    out = await llm_evaluator.is_completion_comment(
        "Ich habe alle Tests durchgeführt und konnte das Problem beheben.",
    )
    assert out is True


async def test_completion_llm_no(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("NO")),
    )
    out = await llm_evaluator.is_completion_comment(
        "Working on this, will update soon.",
    )
    assert out is False


async def test_completion_llm_malformed(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("maybe?")),
    )
    out = await llm_evaluator.is_completion_comment("ambiguous comment text")
    assert out is None


async def test_completion_llm_unavailable(monkeypatch):
    monkeypatch.setattr(
        "httpx.AsyncClient.post",
        AsyncMock(return_value=_mk_response("")),
    )
    out = await llm_evaluator.is_completion_comment(
        "Some non-trivial comment that needs LLM evaluation.",
    )
    assert out is None


async def test_completion_disabled(monkeypatch):
    monkeypatch.setattr(llm_evaluator, "_ENABLED", False)
    out = await llm_evaluator.is_completion_comment(
        "Some non-trivial comment.",
    )
    assert out is None
