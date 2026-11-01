"""Lane inference: route by task shape, never override an explicit choice.

The lanes are not interchangeable - only the harness lane edits files, sees
images, or reaches a vendor CLI - and it delivered a result for ~189k weighted
tokens against ~22M for the gateway lane. A task that obviously needs those
capabilities should not be left to a title-prefix convention.
"""
import pytest

from app.api.tasks import infer_execution_lane


def test_explicit_choice_always_wins():
    assert infer_execution_lane({"execution_lane": "agent", "workspace_path": "/srv/site"}) == "agent"
    assert infer_execution_lane({"execution_lane": "harness"}) == "harness"


def test_a_workspace_implies_the_harness_lane():
    assert infer_execution_lane({"workspace_path": "/root/site"}) == "harness"


@pytest.mark.parametrize(
    "data",
    [
        {"harness_provider": "deepseek-sub"},
        {"harness_model": "deepseek-flash"},
        {"harness_provider": "claude-code", "harness_model": "opus-4-8"},
    ],
)
def test_a_pinned_engine_implies_the_harness_lane(data):
    assert infer_execution_lane(data) == "harness"


def test_a_plain_task_reaches_gateway_execution():
    assert infer_execution_lane({"title": "Write the release notes"}) == "agent"
    assert infer_execution_lane({}) == "agent"


def test_empty_values_do_not_count_as_hints():
    assert infer_execution_lane({"workspace_path": "", "harness_model": None}) == "agent"
