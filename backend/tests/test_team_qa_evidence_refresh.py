"""A production-moved exact-head review keeps the whole owner evidence packet."""
import json

import pytest

from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_recovery_refresh import NEW, recovery
from tests.test_team_verification import BASE, claim
from tests.test_team_verification import env  # noqa: F401


@pytest.mark.parametrize("reviewed", [False, True])
async def test_refresh_keeps_owner_packet_prior_findings_and_finite_window(env, reviewed):
    files = {"receipt.md": "Whole external proof, untrusted; not new authority."}
    s, board, task, cycle, failed, queued = await recovery(env, files)
    original = json.loads(queued.assignment)
    if reviewed:
        qa, token = await claim(s, cycle)
        await svc.complete_attempt(s, board, cycle, qa, token, ok=True,
                                   artifact="Retained evidence checked\nVERDICT: PASS",
                                   head_sha=BASE, verdict="pass")
        assert cycle.state == "qa_passed"
    history = await svc._attempts(s, cycle.id)
    before = [(a.id, a.artifact, a.assignment, a.status) for a in history]
    authority = (cycle.round, cycle.max_rounds, cycle.deadline_at,
                 task.tokens_used, task.budget_exhausted_at)
    await svc.refresh_integration(s, cycle, NEW, NEW, cycle.version)
    after = await svc._attempts(s, cycle.id)
    pending = after[-1]
    assert len(after) == len(history) + int(reviewed)
    if reviewed:
        assert [(a.id, a.artifact, a.assignment, a.status) for a in after[:-1]] == before
    else:
        assert pending.id == queued.id
    policy = json.loads(pending.assignment)
    assert policy["owner_evidence"] == original["owner_evidence"]
    assert policy["prior_qa_reports"] == original["prior_qa_reports"]
    assert (cycle.round, cycle.max_rounds, cycle.deadline_at,
            task.tokens_used, task.budget_exhausted_at) == authority
    qa, token = await claim(s, cycle)
    assert qa.id == pending.id and qa.base_sha == NEW
    member = await s.get(TeamMember, qa.member_id)
    brief = svc.brief(task, cycle, qa, member, None, history=after)
    assert files["receipt.md"] in brief and failed.artifact in brief
    assert svc.attempt_capability("qa", policy) == "read_only"
