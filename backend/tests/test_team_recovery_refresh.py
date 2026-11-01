"""Already published external corrections need exact-head QA, not replay of known bad bytes."""
import json

import pytest

from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_verification import BASE, EVIDENCE, VERIFY_PLAN, _planned, claim
from tests.test_team_verification import env  # noqa: F401

NEW = "2" * 40


async def recovery(env, evidence_files=None):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    worker, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, worker, token, ok=True,
                               artifact=EVIDENCE, head_sha=BASE, changed_files=[])
    await svc.integrate(session, cycle, BASE)
    failed, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, failed, token, ok=True,
                               artifact="CORRECTION_KIND: EVIDENCE\nMissing boundary\nVERDICT: FAIL",
                               head_sha=BASE, verdict="fail")
    await svc.retry_qa(session, cycle, cycle.version, evidence_files=evidence_files)
    queued = (await svc._attempts(session, cycle.id))[-1]
    return session, board, task, cycle, failed, queued


async def test_refresh_unstarted_recovery_preserves_window_attempt_and_whole_findings(env):
    s, board, task, cycle, failed, queued = await recovery(env)
    before = (cycle.round, cycle.max_rounds, cycle.deadline_at, task.tokens_used, task.budget_exhausted_at)
    policy = json.loads(queued.assignment)
    count = len(await svc._attempts(s, cycle.id))
    await svc.refresh_integration(s, cycle, NEW, NEW, cycle.version)
    assert cycle.state == "qa" and cycle.base_sha == cycle.integration_sha == NEW
    assert (cycle.round, cycle.max_rounds, cycle.deadline_at, task.tokens_used, task.budget_exhausted_at) == before
    assert len(await svc._attempts(s, cycle.id)) == count and queued.status == "queued"
    assert failed.head_sha == BASE and failed.verdict == "fail"
    after = json.loads(queued.assignment)
    assert all(after[k] == v for k, v in policy.items() if k != "reason")
    assert after["recovery_previous_integration"] == BASE
    qa, token = await claim(s, cycle)
    assert qa.id == queued.id and qa.base_sha == NEW
    assert failed.artifact in svc.brief(task, cycle, qa, await s.get(TeamMember, qa.member_id), None, history=await svc._attempts(s, cycle.id))
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True,
                               artifact="Whole corrected head checked\nVERDICT: PASS", head_sha=NEW, verdict="pass")
    assert cycle.state == "qa_passed" and cycle.reviewed_sha == NEW and cycle.published_sha is None


@pytest.mark.parametrize("refusal", ["ordinary_qa", "running", "published", "stale"])
async def test_refresh_refuses_without_mutating_claim_or_history(env, refusal):
    s, board, task, cycle, failed, queued = await recovery(env)
    version = cycle.version
    if refusal == "ordinary_qa":
        policy = json.loads(queued.assignment); policy.pop("qa_only_recovery")
        queued.assignment = json.dumps(policy)
    elif refusal == "running":
        await claim(s, cycle)
    elif refusal == "published":
        cycle.published_sha = BASE
    else:
        version -= 1
    before = (cycle.model_dump(), queued.model_dump(), failed.model_dump())
    with pytest.raises(svc.TeamError):
        await svc.refresh_integration(s, cycle, NEW, NEW, version)
    assert (cycle.model_dump(), queued.model_dump(), failed.model_dump()) == before
