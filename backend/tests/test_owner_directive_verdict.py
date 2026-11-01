"""An unreachable LLM must not be reported as "reviewed, not a goal change".

With OMNIROUTE_RUNTIME_KEY unset every call 401'd, `_call_vllm` returned None,
and the watchdog posted `[owner-directive-noop] Owner comment reviewed` for
every owner instruction -- 257 in a week, none actually evaluated.
"""

import pytest

from app.services import llm_evaluator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_output", "expected"),
    [
        (None, None),  # unreachable / 401 / timeout: no verdict
        ("ok", None),  # unusable output: no verdict
        ("NO_CHANGE", ""),  # model judged it not a directive
        ("Find an ENGLISH poem and append it to poems.md.", "Find an ENGLISH poem and append it to poems.md."),
    ],
)
async def test_verdict_distinguishes_no_change_from_no_answer(monkeypatch, llm_output, expected):
    async def fake_call(*_args, **_kwargs):
        return llm_output

    monkeypatch.setattr(llm_evaluator, "_call_vllm", fake_call)
    got = await llm_evaluator.evaluate_owner_directive("Find a poem.", "english only please")
    assert got == expected
