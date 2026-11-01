"""Literal policy examples are content, not proof of missing/redacted evidence."""
# ruff: noqa: F811 - imported pytest fixture names are injected as parameters
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import team_cycle as svc
from tests.test_team_qa_recovery import published
from tests.test_team_verification import env  # noqa: F401 - existing database fixture


@pytest.mark.parametrize("marker", ["[redacted]", "***redacted***"])
def test_complete_native_policy_examples_reach_qa_unchanged(marker):
    cycle = SimpleNamespace(id=uuid4(), board_id=uuid4(), task_id=uuid4(), round=1)
    report = f"Policy example: `{marker}`. Independently check the actual source."
    item = SimpleNamespace(cycle_id=cycle.id, board_id=cycle.board_id,
        task_id=cycle.task_id, round=1, role_kind="developer", stage_key="dev-main",
        attempt_no=1, status="succeeded", id=uuid4(), member_id=uuid4(),
        base_sha="b", head_sha="h", artifact=report,
        assignment=json.dumps({"mode": "verify", "artifact_complete": True}))
    bundle, problem = svc.worker_evidence(cycle, [item])
    assert problem is None and json.loads(bundle)[0]["artifact"] == report
    for artifact, complete in [(report, False), ("", True), ("sk-" + "x" * 24, True)]:
        item.artifact = artifact
        item.assignment = json.dumps({"mode": "verify", "artifact_complete": complete})
        assert svc.worker_evidence(cycle, [item])[1] is not None


@pytest.mark.parametrize("marker", ["[redacted]", "***redacted***"])
async def test_whole_owner_policy_examples_queue_only_qa_without_rewriting(env, marker):
    s, board, task, cycle = await published(env)
    history = await svc._attempts(s, cycle.id)
    before = [(a.id, a.artifact, a.status, a.verdict) for a in history]
    report = f"Recovery checklist: reject genuinely missing proof, e.g. `{marker}`."
    await svc.retry_qa(s, cycle, cycle.version, evidence_files={"policy.md": report})
    after = await svc._attempts(s, cycle.id)
    assert [(a.id, a.artifact, a.status, a.verdict) for a in after[:-1]] == before
    assert report in json.loads(after[-1].assignment)["owner_evidence"]
    assert after[-1].role_kind == "qa" and after[-1].status == "queued"
    assert after[-1].verdict is None and cycle.state == "qa"  # not acceptance
